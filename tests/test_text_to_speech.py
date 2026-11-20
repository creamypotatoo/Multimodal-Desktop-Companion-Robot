"""Hardware-free tests for audio.text_to_speech (no speaker, no Pi tools needed)."""
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from audio import text_to_speech as tts

TOOLS = {"espeak-ng": "/usr/bin/espeak-ng", "ffmpeg": "/usr/bin/ffmpeg", "aplay": "/usr/bin/aplay"}


def fake_which(name):
    return TOOLS.get(name)


class FakeRun:
    """Simulates espeak-ng/ffmpeg/aplay by creating the output files they would."""

    def __init__(self, fail_stage=None):
        self.calls = []
        self.fail_stage = fail_stage

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        tool = os.path.basename(cmd[0])
        if tool == self.fail_stage:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"device busy")
        if tool == "espeak-ng":
            Path(cmd[cmd.index("-w") + 1]).write_bytes(b"RIFF")
        elif tool == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = tts.TTSConfig.from_env({})
        self.assertEqual(config.backend, "auto")
        self.assertEqual(config.device, "plughw:CARD=MAX98357A,DEV=0")
        self.assertAlmostEqual(config.volume, 0.10)

    def test_env_and_overrides(self):
        env = {"ELI_TTS_BACKEND": "ESPEAK", "ELI_TTS_DEVICE": "default", "ELI_TTS_VOLUME": "0.25"}
        config = tts.TTSConfig.from_env(env)
        self.assertEqual((config.backend, config.device, config.volume), ("espeak", "default", 0.25))
        config = tts.TTSConfig.from_env(env, volume=0.5, backend=None)
        self.assertEqual((config.backend, config.volume), ("espeak", 0.5))

    def test_numeric_card_indices_rejected(self):
        for device in ("hw:0", "hw:1,0", "plughw:1,0", "plughw:CARD=1,DEV=0", "sysdefault:CARD=2"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                tts.TTSConfig(device=device)

    def test_named_devices_accepted(self):
        for device in ("plughw:CARD=MAX98357A,DEV=0", "hw:MAX98357A", "default", "dmix:CARD=MAX98357A,DEV=0"):
            with self.subTest(device=device):
                self.assertEqual(tts.TTSConfig(device=device).device, device)

    def test_invalid_volume_and_backend(self):
        for volume in (0, -0.1, 1.5, "loud"):
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                tts.TTSConfig(volume=volume)
        with self.assertRaises(ValueError):
            tts.TTSConfig(backend="festival")

    def test_auto_backend_selection(self):
        auto = tts.TTSConfig()
        with patch.object(tts.sys, "platform", "linux"), patch.object(tts.shutil, "which", fake_which):
            self.assertEqual(tts.resolve_backend(auto), "espeak")
        with patch.object(tts.sys, "platform", "linux"), patch.object(tts.shutil, "which", return_value=None):
            self.assertEqual(tts.resolve_backend(auto), "pyttsx3")
        with patch.object(tts.sys, "platform", "win32"), patch.object(tts.shutil, "which", fake_which):
            self.assertEqual(tts.resolve_backend(auto), "pyttsx3")


class EspeakBackendTests(unittest.TestCase):
    config = tts.TTSConfig(backend="espeak")

    def speak(self, text, run, config=None):
        with patch.object(tts.shutil, "which", fake_which), patch.object(tts.subprocess, "run", run):
            tts.speak(text, config or self.config)

    def test_pipeline_commands(self):
        run = FakeRun()
        self.speak("-rf hello; $(reboot)", run)
        (espeak, ek), (ffmpeg, fk), (aplay, ak) = run.calls
        self.assertEqual(espeak[:4], ["/usr/bin/espeak-ng", "--stdin", "-s", "175"])
        self.assertEqual(ek["input"], "-rf hello; $(reboot)".encode())
        self.assertNotIn("-rf hello; $(reboot)", espeak)
        self.assertIn("volume=0.1", ffmpeg)
        self.assertEqual(ffmpeg[ffmpeg.index("-i") + 1], espeak[-1])
        self.assertEqual(aplay[:4], ["/usr/bin/aplay", "-q", "-D", "plughw:CARD=MAX98357A,DEV=0"])
        self.assertEqual(aplay[-1], ffmpeg[-1])
        for cmd, kwargs in run.calls:
            self.assertIsInstance(cmd, list)
            self.assertNotIn("shell", kwargs)
            self.assertTrue(kwargs["check"])
            self.assertIn("timeout", kwargs)

    def test_configured_device_and_volume(self):
        run = FakeRun()
        self.speak("hi", run, tts.TTSConfig(backend="espeak", device="hw:MAX98357A", volume=0.3))
        self.assertIn("volume=0.3", run.calls[1][0])
        self.assertEqual(run.calls[2][0][3], "hw:MAX98357A")

    def test_temp_files_removed_after_success_and_failure(self):
        for fail in (None, "aplay", "ffmpeg"):
            with self.subTest(fail=fail):
                run = FakeRun(fail_stage=fail)
                if fail:
                    with self.assertRaises(tts.TTSError) as ctx:
                        self.speak("hi", run)
                    self.assertIn(fail, str(ctx.exception))
                    self.assertIn("device busy", str(ctx.exception))
                else:
                    self.speak("hi", run)
                tmpdir = Path(run.calls[0][0][-1]).parent
                self.assertFalse(tmpdir.exists())

    def test_timeout_and_missing_binary(self):
        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        with self.assertRaises(tts.TTSError):
            self.speak("hi", timeout)
        with patch.object(tts.shutil, "which", lambda n: None if n == "ffmpeg" else TOOLS[n]), \
                patch.object(tts.subprocess, "run") as run:
            with self.assertRaisesRegex(tts.TTSError, "ffmpeg"):
                tts.speak("hi", self.config)
            run.assert_not_called()

    def test_empty_text_is_silent(self):
        with patch.object(tts.subprocess, "run") as run:
            for text in ("", "   ", None):
                tts.speak(text, self.config)
            run.assert_not_called()

    def test_concurrent_calls_do_not_overlap(self):
        active = 0
        peak = 0
        guard = threading.Lock()
        inner = FakeRun()

        def slow_run(cmd, **kwargs):
            nonlocal active, peak
            if os.path.basename(cmd[0]) == "aplay":
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
            return inner(cmd, **kwargs)

        with patch.object(tts.shutil, "which", fake_which), patch.object(tts.subprocess, "run", slow_run):
            threads = [threading.Thread(target=tts.speak, args=(f"line {i}", self.config)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(peak, 1)
        self.assertEqual(sum(os.path.basename(c[0][0]) == "aplay" for c in inner.calls), 4)


class Pyttsx3BackendTests(unittest.TestCase):
    def test_pyttsx3_backend_preserved(self):
        engine = Mock()
        module = types.SimpleNamespace(init=Mock(return_value=engine))
        with patch.dict(sys.modules, {"pyttsx3": module}), patch.object(tts.subprocess, "run") as run:
            tts.speak("Hello", tts.TTSConfig(backend="pyttsx3"))
        engine.setProperty.assert_called_once_with("rate", 175)
        engine.say.assert_called_once_with("Hello")
        engine.runAndWait.assert_called_once()
        run.assert_not_called()

    def test_speak_reads_environment(self):
        engine = Mock()
        module = types.SimpleNamespace(init=Mock(return_value=engine))
        with patch.dict(os.environ, {"ELI_TTS_BACKEND": "pyttsx3"}), \
                patch.dict(sys.modules, {"pyttsx3": module}):
            tts.speak("Hi")
        engine.say.assert_called_once_with("Hi")


if __name__ == "__main__":
    unittest.main()
