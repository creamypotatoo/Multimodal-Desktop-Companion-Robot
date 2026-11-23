import array
import inspect
import os
import re
import subprocess
import sys
import threading
import time
import wave

from audio.speech_to_text import record_audio, transcribe
from audio.text_to_speech import speak
from behavior.state_machine import RobotStateMachine, State
from intelligence.ollama_client import (OllamaBadResponse, OllamaClient, OllamaConfig,
                                        OllamaUnavailable, build_messages)
from intelligence.personality import build_system_prompt, load_base_prompt

STT_TIMEOUT = 90.0           # seconds allowed for one whisper.cpp run
POST_SPEECH_DELAY = 0.3      # let the speaker fall silent before recording
MAX_CONSECUTIVE_ERRORS = 5   # stop the session if the audio stack keeps failing
ERROR_BACKOFF = 1.0          # seconds to wait after a failed turn
MAX_HISTORY = 20             # (user, eli) pairs kept in memory
# Recordings quieter than this int16 RMS are not sent to whisper (0 disables).
def _env_float(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        print(f"[Conversation] Ignoring invalid {name}; using {default}", flush=True)
        return float(default)


SILENCE_RMS = _env_float("ELI_SILENCE_RMS", 50)

# whisper.cpp emits markers such as [BLANK_AUDIO], [ Silence ], (music).
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def clean_transcript(text):
    """Strip whisper non-speech markers; return '' when nothing was said."""
    text = _NON_SPEECH.sub(" ", text or "")
    return " ".join(text.split())


def recording_rms(filename):
    """RMS of a 16-bit WAV, or None if it cannot be measured."""
    try:
        with wave.open(str(filename), "rb") as wav:
            if wav.getsampwidth() != 2:
                return None
            samples = array.array("h", wav.readframes(wav.getnframes()))
    except (OSError, EOFError, wave.Error):
        return None
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def _stop_recording():
    """Abort any in-progress sounddevice recording (no-op if never imported)."""
    sd = sys.modules.get("sounddevice")
    if sd is not None:
        try:
            sd.stop()
        except Exception:
            pass


def generate_response(user_text, person_name=None):
    text = user_text.lower()

    if person_name and ("who am i" in text or "my name" in text):
        return f"You are {person_name}."
    if "hello" in text or "hi" in text:
        return f"Hello {person_name}! Nice to hear you." if person_name else "Hello! I am Eli, nice to hear you."
    elif "how are you" in text:
        return "I am just a bunch of code right now, but I am doing great."
    elif "name" in text:
        return "My name is Eli."
    elif "bye" in text:
        return "Goodbye! Talk to you soon."
    else:
        return "I heard you, but I am not sure how to respond to that yet."


class OllamaResponder:
    """Responder backed by Ollama, falling back to ``generate_response``.

    Call signature: ``(user_text, person_name, history=(), context=None)``.
    It never raises for network problems: when Ollama is unreachable or times
    out, it answers with the rule-based fallback and skips Ollama for
    ``retry_cooldown`` seconds so an offline laptop does not add a long wait to
    every turn. The first call after the cooldown tries Ollama again.
    Malformed or empty replies fall back for that turn only (no cooldown).
    """

    def __init__(self, client, *, fallback=generate_response, clock=time.monotonic,
                 base_prompt=None, retry_cooldown=None):
        self.client = client
        self.fallback = fallback
        self._clock = clock
        self.base_prompt = base_prompt
        self.retry_cooldown = (client.config.retry_cooldown if retry_cooldown is None
                               else retry_cooldown)
        self.history_turns = client.config.history_turns
        self._retry_at = None   # monotonic time when Ollama may be tried again
        self.last_source = None  # "ollama" or "fallback" (for logs and tests)

    @staticmethod
    def _log(message):
        print(f"[Ollama] {message}", flush=True)

    @property
    def cooling_down(self):
        return self._retry_at is not None and self._clock() < self._retry_at

    def _fallback(self, user_text, person_name):
        self.last_source = "fallback"
        return self.fallback(user_text, person_name)

    def __call__(self, user_text, person_name=None, history=(), context=None):
        if self.cooling_down:
            return self._fallback(user_text, person_name)
        system = build_system_prompt(person_name, context, self.base_prompt)
        messages = build_messages(system, history, user_text, self.history_turns)
        try:
            reply = self.client.chat(messages)
        except OllamaUnavailable as exc:
            self._retry_at = self._clock() + self.retry_cooldown
            self._log(f"Unavailable: {exc}. Using rule-based replies; "
                      f"retrying in {self.retry_cooldown:g}s.")
            return self._fallback(user_text, person_name)
        except OllamaBadResponse as exc:
            self._log(f"Unusable reply ({exc}); using a rule-based reply this turn.")
            return self._fallback(user_text, person_name)
        except Exception as exc:  # never let an unexpected client bug end the session
            self._log(f"Unexpected client error ({type(exc).__name__}); "
                      "using a rule-based reply this turn.")
            return self._fallback(user_text, person_name)
        if self._retry_at is not None:
            self._log("Reconnected.")
            self._retry_at = None
        self.last_source = "ollama"
        return reply

    def warm_up(self, person_name=None, context=None):
        """Preload the model and Eli's prompt in a background thread.

        Network only: no audio, so it can safely overlap the spoken greeting.
        """
        system = build_system_prompt(person_name, context, self.base_prompt)

        def load():
            try:
                self.client.warm_up(system)
                self._log("Model ready.")
            except Exception as exc:
                self._log(f"Warm-up skipped ({exc}).")

        thread = threading.Thread(target=load, name="ollama-warm-up", daemon=True)
        thread.start()
        return thread


def default_responder(environ=None):
    """OllamaResponder when ELI_OLLAMA_URL is set, otherwise the rule-based responder."""
    config = OllamaConfig.from_env(environ)
    if config is None:
        return generate_response
    print(f"[Ollama] Using model {config.model} at {config.host} "
          f"(timeout {config.timeout:g}s); rule-based fallback enabled.", flush=True)
    return OllamaResponder(OllamaClient(config), base_prompt=load_base_prompt(environ))


def _optional_kwargs(responder, names=("history", "context")):
    """Which of ``names`` the responder explicitly accepts as keyword arguments.

    Legacy responders taking only ``(user_text, person_name)`` - or anything
    whose signature cannot be inspected - receive no extra arguments.
    """
    try:
        params = inspect.signature(responder).parameters
    except (TypeError, ValueError):
        return frozenset()
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return frozenset(n for n in names if n in params and params[n].kind in kinds)


def greeting_for(person_name, visit_count=None):
    if visit_count and visit_count > 1:
        return f"Welcome back, {person_name}! I'm listening."
    return f"Hello, {person_name}! I'm listening."


class ConversationSession:
    """A persistent LISTEN -> TRANSCRIBE -> RESPOND -> SPEAK loop for one person.

    Everything runs sequentially on the calling thread: recording never starts
    until speech playback has returned, so Eli cannot transcribe himself.
    Runs until Ctrl+C (KeyboardInterrupt propagates after cleanup), until
    ``max_turns`` turns have been attempted, or after too many consecutive errors.
    """

    def __init__(self, person_name=None, fsm=None, *, emotion=None, visit_count=None,
                 record=None, transcriber=None, speaker=None, responder=None,
                 sleep=time.sleep, stt_timeout=STT_TIMEOUT, silence_rms=None,
                 post_speech_delay=POST_SPEECH_DELAY, max_turns=None):
        self.person_name = person_name
        self.fsm = fsm or RobotStateMachine()
        self.emotion = emotion
        self.visit_count = visit_count
        self.history = []
        self.turns = 0
        self.consecutive_errors = 0
        self._record = record or record_audio
        self._transcribe = transcriber or transcribe
        self._speak = speaker or speak
        self._respond = responder or default_responder()
        self._respond_kwargs = _optional_kwargs(self._respond)
        # Session facts the language model may use (never images or audio).
        self.context = {"emotion": emotion, "visit_count": visit_count}
        self._sleep = sleep
        self.stt_timeout = stt_timeout
        self.silence_rms = SILENCE_RMS if silence_rms is None else silence_rms
        self.post_speech_delay = post_speech_delay
        self.max_turns = max_turns

    def _log(self, message):
        print(f"[Conversation] {message}", flush=True)

    def say(self, text):
        """Speak and block; returns False (logged) if TTS failed."""
        self.fsm.transition_to(State.SPEAKING)
        print("Eli says:", text, flush=True)
        try:
            self._speak(text)
            return True
        except Exception as exc:
            self._log(f"TTS failed: {exc}")
            return False
        finally:
            # Settle time so the speaker tail is not captured by the next recording.
            if self.post_speech_delay:
                self._sleep(self.post_speech_delay)

    def turn(self):
        """One listen/respond cycle. Returns 'spoke', 'empty' or 'error'."""
        self.turns += 1
        self.fsm.transition_to(State.LISTENING)
        try:
            audio_file = self._record()
            if audio_file is None:
                # The voice-activity recorder heard no speech before its start
                # timeout: a quiet room is not an error, keep listening.
                return self._ok("empty")
            rms = recording_rms(audio_file) if self.silence_rms > 0 else None
            if rms is not None and rms < self.silence_rms:
                self._log(f"Silence (rms={rms:.0f}); listening again.")
                return self._ok("empty")
            self.fsm.transition_to(State.THINKING)
            raw = self._transcribe(audio_file, timeout=self.stt_timeout)
        except subprocess.TimeoutExpired:
            return self._error(f"Transcription timed out after {self.stt_timeout:g}s")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            return self._error(f"Transcription failed: {detail or exc}")
        except Exception as exc:
            return self._error(f"Listening failed: {exc}")

        user_text = clean_transcript(raw)
        if not user_text:
            self._log("No speech detected; listening again.")
            return self._ok("empty")

        print("You said:", user_text, flush=True)
        extra = {}
        if "history" in self._respond_kwargs:
            extra["history"] = tuple(self.history)  # completed turns only
        if "context" in self._respond_kwargs:
            extra["context"] = dict(self.context)
        try:
            response = self._respond(user_text, self.person_name, **extra)
        except Exception as exc:
            return self._error(f"Response generation failed: {exc}")
        if not isinstance(response, str) or not response.strip():
            return self._error("Response generation returned nothing")
        if not self.say(response):
            return self._error("Could not speak response")
        # Only exchanges the person actually heard become conversation history.
        self.history.append((user_text, response))
        del self.history[:-MAX_HISTORY]
        return self._ok("spoke")

    def _ok(self, outcome):
        self.consecutive_errors = 0
        return outcome

    def _error(self, message):
        self.consecutive_errors += 1
        self._log(f"{message} ({self.consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})")
        if self.consecutive_errors < MAX_CONSECUTIVE_ERRORS:
            self._sleep(ERROR_BACKOFF)
        return "error"

    def run(self, greeting=None):
        """Run until Ctrl+C. Returns 0 normally, 1 if stopped by repeated errors."""
        who = self.person_name or "unknown person"
        self._log(f"Session started with {who}. Press Ctrl+C to stop.")
        try:
            warm_up = getattr(self._respond, "warm_up", None)
            if isinstance(self._respond, OllamaResponder):
                try:
                    warm_up(self.person_name, dict(self.context))
                except Exception:
                    pass
            if greeting:
                self.say(greeting)
            while self.max_turns is None or self.turns < self.max_turns:
                self.turn()
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._log("Too many consecutive errors; ending session.")
                    return 1
            return 0
        finally:
            _stop_recording()
            self.fsm.transition_to(State.IDLE)


def run_session(person_name, fsm=None, *, emotion=None, visit_count=None, **kwargs):
    """Entry point used by main.py once an identity is locked."""
    session = ConversationSession(person_name, fsm, emotion=emotion,
                                  visit_count=visit_count, **kwargs)
    return session.run(greeting=greeting_for(person_name, visit_count))


def conversation_turn(fsm):
    """
    One full listen -> think -> speak cycle (kept for compatibility).
    """
    session = ConversationSession(fsm=fsm, max_turns=1)
    session.run()


def run_conversation_loop():
    """Standalone conversation without vision; stop with Ctrl+C."""
    print("Eli is listening. Press Ctrl+C to stop.")
    try:
        return ConversationSession().run()
    except KeyboardInterrupt:
        print("\nConversation ended.")
        return 0


if __name__ == "__main__":
    raise SystemExit(run_conversation_loop())
