"""Offline regression coverage for Windows PCM and voice diagnostics."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from audio.runtime_audio import Microphone, OfflineWhisper, prepare_pcm
from audio.speech_to_text import VadSettings, capture_utterance
from runtime.voice import matches_wake
import test_voice_runtime


class WindowsAudioTests(TestCase):
    def test_offline_whisper_gets_an_array_and_explicit_options(self):
        with TemporaryDirectory() as tmp:
            for name in ('model.bin', 'tokenizer.json', 'config.json'):
                (Path(tmp) / name).touch()
            model = Mock()
            model.transcribe.return_value = (iter([SimpleNamespace(text=' Set a timer for 10 seconds.')]), None)
            factory = Mock(return_value=model)
            receiver = OfflineWhisper(tmp, prompt='Eli', beam_size=5)
            pcm = prepare_pcm(np.array([200, -300, 200], dtype=np.int16))
            with patch.dict('sys.modules', {'faster_whisper': SimpleNamespace(WhisperModel=factory)}):
                self.assertEqual(receiver(pcm), 'Set a timer for 10 seconds.')
            self.assertIs(model.transcribe.call_args.args[0], pcm)
            self.assertTrue(factory.call_args.kwargs['local_files_only'])
            self.assertEqual(model.transcribe.call_args.kwargs['initial_prompt'], 'Eli')
            self.assertEqual(model.transcribe.call_args.kwargs['beam_size'], 5)

    def test_windows_capture_returns_pcm_without_a_wav(self):
        driver = MagicMock()
        driver.InputStream.return_value.__enter__.return_value = Mock()
        microphone = Microphone(input_module=driver, pcm=True)
        samples = np.array([-32768, 16384], dtype=np.int16)
        with patch('audio.speech_to_text.capture_utterance', return_value=samples), \
                patch('audio.runtime_audio.mkstemp') as tempfile:
            result = microphone.capture(lambda: True)
            isolated_test = microphone.capture(lambda: True, test=True)
        tempfile.assert_not_called()
        self.assertIs(isolated_test, True)
        self.assertEqual(result.dtype, np.float32)
        np.testing.assert_array_equal(result, [-1.0, 0.5])

    def test_quiet_speech_gets_through_with_a_calibrated_floor(self):
        settings = replace(VadSettings(), threshold=50, noise_ratio=2, silence=0.3, start_timeout=1)
        values = iter([18, 0, 21, 19, 20, 21, 22] + [95] * 10 + [20] * 11)
        events = []
        samples = capture_utterance(lambda: np.full(480, next(values), dtype=np.int16),
                                    settings, on_status=lambda *event: events.append(event))
        self.assertIsNotNone(samples)
        self.assertTrue(any(event[0] == 'speech' for event in events))
        self.assertGreater(events[0][1], 0)  # An isolated zero does not set the noise floor.
        values = iter([20] * 40)
        self.assertIsNone(capture_utterance(lambda: np.full(480, next(values), dtype=np.int16),
                                            settings, on_status=lambda *event: None))

    def test_gain_bounds_keep_silence_and_loud_audio_intact(self):
        silence = np.array([0, 1, -1], dtype=np.int16)
        weak = np.array([100, -100], dtype=np.int16)
        loud = np.array([27017, -27017], dtype=np.int16)
        np.testing.assert_array_equal(prepare_pcm(silence, 8), prepare_pcm(silence))
        np.testing.assert_array_equal(prepare_pcm(loud, 8), prepare_pcm(loud))
        np.testing.assert_allclose(prepare_pcm(weak, 4), 4 * prepare_pcm(weak))
        self.assertLessEqual(max(abs(prepare_pcm(np.array([10000], dtype=np.int16), 8))), .700001)

    def test_wake_aliases_are_exact_token_matches(self):
        for text in (' HEY, Eli!', 'Hey    Ellie.', 'hey Ely'):
            self.assertTrue(matches_wake(text, 'Hey Eli'))
        for text in ('hey Lily', 'hey Eli please', 'not hey Ellie', 'Eli'):
            self.assertFalse(matches_wake(text, 'Hey Eli'))


class WindowsVoiceIntegration(TestCase):
    setUp = test_voice_runtime.VoiceRuntimeTests.setUp
    wait_for = test_voice_runtime.VoiceRuntimeTests.wait_for
    command = test_voice_runtime.VoiceRuntimeTests.command
    # Exercise the existing fake runtime fixture with PCM and real-audio flags.
    def test_pcm_dispatches_timer_and_diagnostics_expire(self):
        capture = self.runtime.components.capture

        def pcm(current, **kwargs):
            return np.ones(512, dtype=np.float32) if capture(current, **kwargs) else None

        def transcribe(recording, *, timeout):
            self.assertEqual(recording.dtype, np.float32)
            return 'Set a timer for 10 seconds'

        self.runtime.components = replace(self.runtime.components, simulated=False,
                                          capture=pcm, transcribe=transcribe)
        self.runtime.startup()
        from controller import Action
        self.command(Action.MODE, mode='companion')
        self.command(Action.CONVERSATION, action='start')
        self.phrases.put('speech')
        self.wait_for(lambda: self.robot.utilities.snapshot()['timer']['duration'] == 10)
        self.assertEqual(self.runtime.audio_status()['recognition']['command']['text'],
                         'Set a timer for 10 seconds')
        self.clock.t += 121
        self.assertEqual(self.runtime.audio_status()['recognition'], {})

    def test_decoder_failure_is_distinguishable_from_silence(self):
        def broken(recording, *, timeout):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

        self.runtime.components = replace(self.runtime.components, transcribe=broken)
        with self.assertRaisesRegex(RuntimeError, 'Incompatible PyAV'):
            self.runtime._transcribe(object(), timeout=8, wake=True)
        status = self.runtime.audio_status()
        self.assertEqual(status['health']['stt']['availability'], 'unavailable')
        self.assertEqual(status['recognition']['wake']['text'], '')
        self.assertIn('metadata_errors', status['recognition']['wake']['error'])
