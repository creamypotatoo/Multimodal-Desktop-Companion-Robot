"""Synthetic-audio tests for Eli's voice-activity recorder (no microphone needed)."""
import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

try:
    import sounddevice  # noqa: F401
except (ImportError, OSError):  # PortAudio missing: the tests replace stt.sd anyway
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")

# Keep the conversation tests below rule-based and offline.
os.environ.pop("ELI_OLLAMA_URL", None)

from audio import speech_to_text as stt  # noqa: E402

SR = stt.SAMPLE_RATE
BLOCK = stt.BLOCK_FRAMES
RNG = np.random.default_rng(1234)


def noise(seconds, rms=20.0):
    return np.clip(RNG.normal(0, rms, int(seconds * SR)), -32768, 32767).astype(np.int16)


def speech(seconds, amplitude=3000):
    """Voiced-speech stand-in: a 220 Hz tone on top of room noise."""
    t = np.arange(int(seconds * SR)) / SR
    wave_ = amplitude * np.sin(2 * np.pi * 220 * t) + RNG.normal(0, 20, t.size)
    return np.clip(wave_, -32768, 32767).astype(np.int16)


class Reader:
    """Serves a synthetic signal as 30 ms chunks, like InputStream.read."""

    def __init__(self, *segments, after=None):
        self.signal = np.concatenate(segments)
        self.pos = 0
        self.chunks = 0
        self.after = after  # exception raised once the signal is used up

    def __call__(self):
        if self.pos >= self.signal.size:
            if self.after is not None:
                raise self.after
            raise AssertionError("recorder read past the end of the test signal")
        chunk = self.signal[self.pos:self.pos + BLOCK]
        if chunk.size < BLOCK:
            chunk = np.pad(chunk, (0, BLOCK - chunk.size))
        self.pos += BLOCK
        self.chunks += 1
        return chunk.reshape(-1, 1)  # sounddevice shape: (frames, channels)

    @property
    def seconds_read(self):
        return self.chunks * stt.BLOCK_SECONDS


def capture(*segments, settings=None, **kwargs):
    reader = Reader(*segments, **kwargs)
    log = Mock()
    audio = stt.capture_utterance(reader, settings or stt.VadSettings(), log=log)
    return audio, reader, log


def seconds(audio):
    return audio.size / SR


def contains(haystack, needle):
    """True if ``needle`` appears intact as a contiguous slice of ``haystack``."""
    hits = np.flatnonzero(haystack == needle[0])
    return any(np.array_equal(haystack[i:i + needle.size], needle) for i in hits)


BLOCK_S = stt.BLOCK_SECONDS


class CaptureTests(unittest.TestCase):
    def test_waits_for_speech_then_stops_after_silence(self):
        said = speech(1.0)
        audio, reader, log = capture(noise(2.0), said, noise(5.0))
        self.assertIsNotNone(audio)
        self.assertEqual(audio.dtype, np.int16)
        self.assertTrue(contains(audio, said))
        # 0.3 s pre-roll + 1.0 s speech + 0.5 s trailing silence (one-chunk tolerance).
        self.assertAlmostEqual(seconds(audio), 1.8, delta=2 * BLOCK_S)
        # Stopped ~1.5 s after the speech ended; the rest of the signal is unread.
        self.assertAlmostEqual(reader.seconds_read, 2.0 + 1.0 + 1.5, delta=2 * BLOCK_S)
        log.assert_not_called()

    def test_pre_roll_keeps_audio_before_onset(self):
        said = speech(0.8)
        audio, _, _ = capture(noise(1.0), said, noise(3.0))
        onset = int(np.flatnonzero(np.abs(audio) > 1500)[0]) / SR
        self.assertAlmostEqual(onset, 0.3, delta=BLOCK_S)

    def test_soft_word_start_is_not_clipped(self):
        """A quiet lead-in below the threshold is kept by the pre-roll."""
        soft = speech(0.2, amplitude=150)   # RMS ~106 < threshold 150
        loud = speech(0.8)
        audio, _, _ = capture(noise(1.0), soft, loud, noise(3.0))
        self.assertTrue(contains(audio, soft))
        self.assertTrue(contains(audio, loud))

    def test_natural_pauses_are_kept_in_one_recording(self):
        a, b, c = speech(0.8), speech(0.6), speech(0.5)
        audio, _, _ = capture(noise(1.0), a, noise(1.2), b, noise(1.3), c, noise(4.0))
        for part in (a, b, c):
            self.assertTrue(contains(audio, part))
        self.assertAlmostEqual(seconds(audio), 0.3 + 0.8 + 1.2 + 0.6 + 1.3 + 0.5 + 0.5,
                               delta=2 * BLOCK_S)

    def test_silence_timer_resets_and_long_pause_ends_utterance(self):
        first, late = speech(0.6), speech(0.6)
        audio, reader, _ = capture(noise(1.0), first, noise(1.4), speech(0.3), noise(1.7),
                                   late, noise(3.0))
        self.assertTrue(contains(audio, first))
        self.assertFalse(contains(audio, late))  # spoken after a 1.7 s pause
        self.assertLess(reader.seconds_read, 1.0 + 0.6 + 1.4 + 0.3 + 1.7)

    def test_custom_silence_duration(self):
        settings = stt.VadSettings(silence=0.8)
        audio, _, _ = capture(noise(1.0), speech(0.5), noise(1.0), speech(0.5), noise(2.0),
                              settings=settings)
        self.assertAlmostEqual(seconds(audio), 0.3 + 0.5 + 0.5, delta=2 * BLOCK_S)

    def test_start_timeout_returns_none(self):
        audio, reader, _ = capture(noise(20.0))
        self.assertIsNone(audio)
        self.assertAlmostEqual(reader.seconds_read, 15.0, delta=BLOCK_S)
        audio, reader, _ = capture(noise(5.0), settings=stt.VadSettings(start_timeout=3))
        self.assertIsNone(audio)
        self.assertAlmostEqual(reader.seconds_read, 3.0, delta=BLOCK_S)

    def test_speech_after_timeout_is_ignored(self):
        audio, _, _ = capture(noise(3.1), speech(1.0), noise(2.0),
                              settings=stt.VadSettings(start_timeout=3))
        self.assertIsNone(audio)

    def test_maximum_duration(self):
        audio, reader, log = capture(noise(0.5), speech(70.0))
        self.assertAlmostEqual(seconds(audio), 60.0, delta=BLOCK_S)
        log.assert_called_once()
        self.assertIn("60s recording limit", log.call_args[0][0])
        audio, _, _ = capture(noise(0.5), speech(10.0), settings=stt.VadSettings(max_duration=4))
        self.assertAlmostEqual(seconds(audio), 4.0, delta=BLOCK_S)

    def test_steady_background_noise_is_not_speech(self):
        hum_rms = 400.0  # well above the absolute threshold of 150
        audio, _, _ = capture(noise(16.0, rms=hum_rms))
        self.assertIsNone(audio)
        # ...but real speech over that hum is still detected and ends normally.
        said = speech(1.0, amplitude=6000) + noise(1.0, rms=hum_rms)
        audio, _, _ = capture(noise(1.0, rms=hum_rms), said, noise(4.0, rms=hum_rms))
        self.assertTrue(contains(audio, said))
        self.assertAlmostEqual(seconds(audio), 1.8, delta=2 * BLOCK_S)

    def test_short_click_is_not_speech(self):
        click = speech(0.03, amplitude=20000)
        audio, _, _ = capture(noise(1.0), click, noise(15.0),
                              settings=stt.VadSettings(start_timeout=10))
        self.assertIsNone(audio)

    def test_quiet_sound_below_threshold_is_ignored(self):
        audio, _, _ = capture(noise(1.0), speech(2.0, amplitude=150), noise(3.0),
                              settings=stt.VadSettings(start_timeout=5))
        self.assertIsNone(audio)

    def test_speech_at_very_start_is_captured(self):
        said = speech(1.0)
        audio, _, _ = capture(noise(0.3), said, noise(3.0))
        self.assertTrue(contains(audio, said))

    def test_interruption_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            capture(noise(1.0), speech(0.5), after=KeyboardInterrupt())


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = stt.VadSettings.from_env({})
        self.assertEqual((s.silence, s.threshold, s.start_timeout, s.max_duration),
                         (1.5, 150.0, 15.0, 60.0))
        self.assertEqual((s.pre_roll, s.trailing), (0.3, 0.5))

    def test_environment_overrides_and_invalid_values(self):
        env = {"ELI_VAD_SILENCE": "2", "ELI_VAD_THRESHOLD": "300",
               "ELI_VAD_START_TIMEOUT": "20", "ELI_VAD_MAX_DURATION": "30",
               "ELI_VAD_NOISE_RATIO": "4"}
        s = stt.VadSettings.from_env(env)
        self.assertEqual((s.silence, s.threshold, s.start_timeout, s.max_duration, s.noise_ratio),
                         (2.0, 300.0, 20.0, 30.0, 4.0))
        bad = {"ELI_VAD_SILENCE": "soon", "ELI_VAD_THRESHOLD": "-1",
               "ELI_VAD_MAX_DURATION": "nan", "ELI_VAD_START_TIMEOUT": "0"}
        with contextlib.redirect_stdout(io.StringIO()) as log:
            s = stt.VadSettings.from_env(bad)
        self.assertEqual(s, stt.VadSettings())
        self.assertEqual(log.getvalue().count("Ignoring invalid"), 4)


class FakeStream:
    def __init__(self, reader):
        self.reader = reader
        self.closed = False

    def read(self, frames):
        assert frames == BLOCK
        return self.reader(), False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class RecordAudioTests(unittest.TestCase):
    DEVICES = [{"name": "Built-in", "max_input_channels": 0},
               {"name": "USB 2.0 Camera: Audio (hw:2,0)", "max_input_channels": 1}]

    def record(self, reader, **kwargs):
        sd = types.SimpleNamespace(query_devices=Mock(return_value=self.DEVICES))
        streams = []

        def input_stream(**params):
            streams.append((params, FakeStream(reader)))
            return streams[-1][1]

        sd.InputStream = input_stream
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "utterance.wav"
        with patch.object(stt, "sd", sd), contextlib.redirect_stdout(io.StringIO()):
            try:
                result = stt.record_audio(path, settings=stt.VadSettings(**kwargs))
            finally:
                self.params, self.stream = streams[0] if streams else (None, None)
        return result, path

    def test_writes_whisper_compatible_wav(self):
        said = speech(1.0)
        result, path = self.record(Reader(noise(1.0), said, noise(3.0)))
        self.assertEqual(result, path)
        self.assertEqual(self.params, {"samplerate": 16000, "channels": 1, "dtype": "int16",
                                       "device": 1, "blocksize": BLOCK})
        self.assertTrue(self.stream.closed)
        with wave.open(str(path), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                             (1, 2, 16000))
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        self.assertTrue(contains(samples, said))

    def test_no_speech_returns_none_and_writes_nothing(self):
        result, path = self.record(Reader(noise(5.0)), start_timeout=2)
        self.assertIsNone(result)
        self.assertFalse(path.exists())
        self.assertTrue(self.stream.closed)

    def test_ctrl_c_releases_microphone(self):
        with self.assertRaises(KeyboardInterrupt):
            self.record(Reader(noise(1.0), speech(0.5), after=KeyboardInterrupt()))
        self.assertTrue(self.stream.closed)

    def test_stream_error_releases_microphone(self):
        with self.assertRaises(RuntimeError):
            self.record(Reader(noise(0.5), after=RuntimeError("device unplugged")))
        self.assertTrue(self.stream.closed)

    def test_missing_microphone_keeps_existing_error(self):
        with self.assertRaisesRegex(RuntimeError, "USB webcam microphone was not found"):
            stt.find_microphone([{"name": "Built-in", "max_input_channels": 2}])


# Conversation behaviour when the recorder hears nobody.
_stub = types.ModuleType("audio.speech_to_text")
_stub.record_audio = Mock(name="record_audio")
_stub.transcribe = Mock(name="transcribe")
# Import a fresh intelligence.conversation with only audio.speech_to_text stubbed.
# Swap just that one sys.modules entry and restore it afterwards: patch.dict
# would also drop every module first imported during this import (state
# machine, Ollama client, ...), so later imports would create duplicate classes
# (State, OllamaUnavailable) and break identity checks in other test modules.
# importlib.import_module ignores stale `intelligence.conversation` attributes.
_saved_stt = sys.modules.get("audio.speech_to_text")
sys.modules["audio.speech_to_text"] = _stub
try:
    sys.modules.pop("intelligence.conversation", None)
    conv = importlib.import_module("intelligence.conversation")
finally:
    if _saved_stt is None:
        sys.modules.pop("audio.speech_to_text", None)
    else:
        sys.modules["audio.speech_to_text"] = _saved_stt
sys.modules.pop("intelligence.conversation", None)


class ConversationNoSpeechTests(unittest.TestCase):
    def test_no_speech_keeps_listening_without_error(self):
        events = []
        outcomes = [None, None, "loud.wav"]

        def record():
            events.append("record")
            if not outcomes:
                raise KeyboardInterrupt
            return outcomes.pop(0)

        def transcribe(audio_file, timeout=None):
            events.append(f"transcribe:{audio_file}")
            return "hello"

        def speak(text):
            events.append("speak")

        session = conv.ConversationSession("Shanta", Mock(), record=record,
                                           transcriber=transcribe, speaker=speak,
                                           sleep=Mock(), silence_rms=0)
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(conv, "_stop_recording"), self.assertRaises(KeyboardInterrupt):
            session.run()
        self.assertEqual(events, ["record", "record", "record", "transcribe:loud.wav",
                                  "speak", "record"])
        self.assertEqual(session.consecutive_errors, 0)
        self.assertEqual(session.history, [("hello", "Hello Shanta! Nice to hear you.")])


if __name__ == "__main__":
    unittest.main()
