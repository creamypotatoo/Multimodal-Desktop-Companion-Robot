"""Hardware-free tests for the persistent conversation session and session locking."""
import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

# These tests expect the rule-based default responder; never contact Ollama even
# if ELI_OLLAMA_URL is exported (tests/__init__.py is skipped by `discover -s tests`).
os.environ.pop("ELI_OLLAMA_URL", None)

import main
from behavior.state_machine import State

# Stub the audio stack so these tests need no microphone, whisper.cpp or speaker.
_stt = types.ModuleType("audio.speech_to_text")
_stt.record_audio = Mock(name="record_audio")
_stt.transcribe = Mock(name="transcribe")
# Import a fresh intelligence.conversation with only audio.speech_to_text stubbed.
# Swap just that one sys.modules entry and restore it afterwards: patch.dict
# would also drop every module first imported during this import (state
# machine, Ollama client, ...), so later imports would create duplicate classes
# (State, OllamaUnavailable) and break identity checks in other test modules.
# importlib.import_module ignores stale `intelligence.conversation` attributes.
_saved_stt = sys.modules.get("audio.speech_to_text")
sys.modules["audio.speech_to_text"] = _stt
try:
    sys.modules.pop("intelligence.conversation", None)
    conv = importlib.import_module("intelligence.conversation")
finally:
    if _saved_stt is None:
        sys.modules.pop("audio.speech_to_text", None)
    else:
        sys.modules["audio.speech_to_text"] = _saved_stt
sys.modules.pop("intelligence.conversation", None)


def write_wav(path, amplitude):
    samples = (np.sin(np.linspace(0, 200, 16000)) * amplitude).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    return str(path)


class Script:
    """Records the order of audio events to prove record/speak never overlap."""

    def __init__(self, transcripts, wav="loud"):
        self.events = []
        self.transcripts = list(transcripts)
        self.wav = wav
        self.active = None

    def record(self):
        assert self.active is None, f"record during {self.active}"
        self.events.append("record")
        if not self.transcripts:
            raise KeyboardInterrupt
        return self.wav

    def transcribe(self, audio_file, timeout=None):
        self.events.append(f"transcribe(timeout={timeout})")
        item = self.transcripts.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def speak(self, text):
        self.active = "speak"
        self.events.append(f"speak:{text}")
        self.active = None


def make_session(script, **kwargs):
    fsm = Mock()
    session = conv.ConversationSession(
        "Shanta", fsm, record=script.record, transcriber=script.transcribe,
        speaker=script.speak, sleep=Mock(), silence_rms=0, **kwargs)
    return session, fsm


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class TranscriptTests(unittest.TestCase):
    def test_clean_transcript(self):
        self.assertEqual(conv.clean_transcript("[BLANK_AUDIO]"), "")
        self.assertEqual(conv.clean_transcript("  [ Silence ] (music) "), "")
        self.assertEqual(conv.clean_transcript(" Hello  there [noise] "), "Hello there")
        self.assertEqual(conv.clean_transcript(None), "")

    def test_rms_detects_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertLess(conv.recording_rms(write_wav(Path(tmp) / "q.wav", 5)), 50)
            self.assertGreater(conv.recording_rms(write_wav(Path(tmp) / "l.wav", 3000)), 50)
            self.assertIsNone(conv.recording_rms(Path(tmp) / "missing.wav"))

    def test_response_uses_locked_identity(self):
        self.assertEqual(conv.generate_response("who am I", "Shanta"), "You are Shanta.")
        self.assertEqual(conv.generate_response("what is your name"), "My name is Eli.")


class SessionTests(unittest.TestCase):
    def test_repeated_turns_until_ctrl_c(self):
        script = Script(["hello", "how are you", "what is your name"])
        session, fsm = make_session(script)
        with quiet(), patch.object(conv, "_stop_recording") as stop, self.assertRaises(KeyboardInterrupt):
            session.run(greeting="Hello, Shanta!")
        self.assertEqual(script.events.count("record"), 4)  # 3 turns + the interrupted one
        self.assertEqual(script.events[0], "speak:Hello, Shanta!")
        self.assertIn("transcribe(timeout=90.0)", script.events)
        self.assertEqual(len(session.history), 3)
        self.assertEqual(session.history[0], ("hello", "Hello Shanta! Nice to hear you."))
        # Strict alternation: every recording follows the previous playback.
        for i, event in enumerate(script.events):
            if event.startswith("speak"):
                self.assertEqual(script.events[i + 1], "record")
        stop.assert_called_once()
        fsm.transition_to.assert_called_with(State.IDLE)

    def test_empty_speech_keeps_listening_without_speaking(self):
        script = Script(["", "[BLANK_AUDIO]", "  ", "hello"])
        session, _ = make_session(script)
        with quiet() as log, self.assertRaises(KeyboardInterrupt):
            session.run()
        spoken = [e for e in script.events if e.startswith("speak")]
        self.assertEqual(spoken, ["speak:Hello Shanta! Nice to hear you."])
        self.assertEqual(session.consecutive_errors, 0)

    def test_silent_recording_skips_whisper(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(["unused"], wav=write_wav(Path(tmp) / "s.wav", 3))
            session, _ = make_session(script, max_turns=1)
            session.silence_rms = 50
            with quiet():
                self.assertEqual(session.run(), 0)
        self.assertEqual(script.events, ["record"])

    def test_stt_timeout_and_errors_recover(self):
        script = Script([subprocess.TimeoutExpired("whisper-cli", 90),
                         subprocess.CalledProcessError(1, "whisper-cli", stderr="bad model"),
                         "hello"])
        session, _ = make_session(script)
        with quiet() as log, self.assertRaises(KeyboardInterrupt):
            session.run()
        self.assertIn("timed out", log.getvalue())
        self.assertIn("bad model", log.getvalue())
        self.assertEqual(session.consecutive_errors, 0)
        self.assertIn("speak:Hello Shanta! Nice to hear you.", script.events)

    def test_tts_failure_does_not_end_session(self):
        script = Script(["hello", "hi"])
        script.speak = Mock(side_effect=[RuntimeError("aplay busy"), None])
        session, _ = make_session(script)
        with quiet(), self.assertRaises(KeyboardInterrupt):
            session.run()
        self.assertEqual(script.speak.call_count, 2)

    def test_repeated_failures_end_session(self):
        record = Mock(side_effect=RuntimeError("microphone not found"))
        session = conv.ConversationSession("Shanta", Mock(), record=record, sleep=Mock(), speaker=Mock())
        with quiet():
            self.assertEqual(session.run(), 1)
        self.assertEqual(record.call_count, conv.MAX_CONSECUTIVE_ERRORS)

    def test_ctrl_c_during_transcription_cleans_up(self):
        script = Script([KeyboardInterrupt()])
        session, fsm = make_session(script)
        with quiet(), patch.object(conv, "_stop_recording") as stop, self.assertRaises(KeyboardInterrupt):
            session.run()
        stop.assert_called_once()
        fsm.transition_to.assert_called_with(State.IDLE)

    def test_stop_recording_calls_sounddevice_stop(self):
        sd = Mock()
        with patch.dict(sys.modules, {"sounddevice": sd}):
            conv._stop_recording()
        sd.stop.assert_called_once()


class MainSessionLockTests(unittest.TestCase):
    def run_main(self, predictions, session_result=0, argv=("--headless",)):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        camera = Mock()
        camera.read.side_effect = [(True, frame.copy()) for _ in predictions] + [(False, None)]
        detector = Mock()
        detector.detectMultiScale.return_value = np.array([[20, 30, 200, 160]])
        recognize = Mock(side_effect=predictions)
        session = Mock()
        if isinstance(session_result, BaseException):
            session.side_effect = session_result
        else:
            session.return_value = session_result
        log = io.StringIO()
        with patch("main.Camera", return_value=camera), \
                patch("main.load_vision", return_value=(detector, recognize)), \
                patch("main.load_emotion", return_value=Mock(return_value="happy")), \
                patch("main.load_conversation", return_value=session), \
                patch("main.remember_encounter") as remember, \
                patch("main.get_person_info", return_value={"visit_count": 4}), \
                contextlib.redirect_stdout(log):
            status = main.main(list(argv))
        return log.getvalue(), recognize, remember, session, camera, status

    def test_identity_locks_after_consecutive_frames_and_stops_vision(self):
        predictions = [("Shanta", 40), ("Unknown", 70), ("Shanta", 41), ("Shanta", 42),
                       ("Shanta", 43)] + [("Unknown", 90)] * 5
        log, recognize, remember, session, camera, status = self.run_main(predictions)
        self.assertEqual(status, 0)
        self.assertEqual(recognize.call_count, 5)  # no recognition once locked
        self.assertEqual(log.count("Hello, Shanta!"), 1)
        remember.assert_called_once_with("Shanta")
        session.assert_called_once()
        args, kwargs = session.call_args
        self.assertEqual(args[0], "Shanta")
        self.assertEqual(kwargs["visit_count"], 4)
        self.assertEqual(kwargs["emotion"], "happy")
        self.assertGreaterEqual(camera.release.call_count, 1)
        self.assertIn("Face recognition paused", log)
        self.assertNotIn("Identity: Unknown\n[Session]", log)

    def test_unknown_faces_never_start_session(self):
        log, recognize, remember, session, _, status = self.run_main([("Unknown", 80)] * 6)
        session.assert_not_called()
        remember.assert_not_called()
        self.assertEqual(recognize.call_count, 6)
        self.assertEqual(status, 1)  # camera ran out of frames

    def test_ctrl_c_in_session_exits_cleanly(self):
        predictions = [("Shanta", 40)] * 3
        log, _, _, session, camera, status = self.run_main(predictions, KeyboardInterrupt())
        self.assertEqual(status, 0)
        self.assertIn("Eli stopped.", log)
        session.assert_called_once()
        camera.release.assert_called()

    def test_session_error_reported_not_as_camera_error(self):
        log, _, _, _, _, status = self.run_main([("Shanta", 40)] * 3, RuntimeError("boom"))
        self.assertEqual(status, 1)
        self.assertIn("[Conversation] boom", log)
        self.assertNotIn("Camera/preview unavailable", log)


if __name__ == "__main__":
    unittest.main()
