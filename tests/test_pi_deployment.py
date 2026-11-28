import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio import speech_to_text
from deploy import pi
from controller.esp32 import discover_ports
from test_perception import await_true, mock_service
from runtime import UnifiedRuntime
from runtime.components import configured_components, mock_components


class PiLauncherTests(unittest.TestCase):
    def test_literal_env_retains_hash_and_spaces(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pi.env'
            path.write_text('ELI_WEB_PASSWORD_HASH=scrypt:1:2:3$test$placeholder\n'
                            'ELI_AUDIO_INPUT=USB Camera\n', encoding='utf8')
            values = pi.read_env(path)
            self.assertEqual(values['ELI_WEB_PASSWORD_HASH'], 'scrypt:1:2:3$test$placeholder')
            self.assertEqual(values['ELI_AUDIO_INPUT'], 'USB Camera')
            path.write_text('BROKEN_KEY=value', encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'line 1'):
                pi.read_env(path)

    def test_profiles_stay_on_loopback_and_only_serial_profile_enables_esp32(self):
        for profile in pi.PROFILES:
            env = {'ELI_WEB_HOST': '0.0.0.0'}
            args = pi.launch_args(profile, env)
            self.assertEqual(args[args.index('--host') + 1], '127.0.0.1')
            self.assertEqual(args[args.index('--motors') + 1], 'esp32' if profile == 'serial' else 'mock')
            self.assertEqual('--local-camera' in args, profile in ('camera', 'serial', 'run'))
            self.assertNotIn('--connect', args)
        args = pi.launch_args('mock', {'ELI_PI_AUDIO': 'pi'})
        self.assertEqual(args[args.index('--audio') + 1], 'mock')

    def test_https_requires_configured_credentials_and_sets_secure_cookies(self):
        env = {'ELI_PI_HTTPS': '1'}
        with self.assertRaisesRegex(ValueError, 'requires'):
            pi.launch_args('run', env)
        env.update(ELI_WEB_SECRET_KEY='test-only', ELI_WEB_PASSWORD_HASH='test-only')
        pi.launch_args('run', env)
        self.assertEqual(env['ELI_WEB_COOKIE_SECURE'], '1')

    def test_preflight_warns_without_starting_a_backend(self):
        env = {'ELI_DISPLAY_BACKEND': 'off'}
        args = pi.launch_args('camera', env)
        with (patch.object(pi, 'import_module', side_effect=lambda name:
                           SimpleNamespace() if name != 'sounddevice' else self._missing_module()),
              patch.object(pi, 'which', return_value=None),
              patch.object(speech_to_text, 'whisper_problem', return_value='whisper.cpp model unavailable')):
            checks = pi.preflight(args, env)
        messages = ' '.join(message for _, message in checks)
        self.assertIn('libportaudio2', messages)
        self.assertIn('whisper.cpp model', messages)
        self.assertIn('LBPH', messages)
        self.assertIn('Ollama URL unset', messages)
        self.assertNotIn('ERROR', [level for level, _ in checks])

    @staticmethod
    def _missing_module():
        raise OSError('missing native PortAudio')


class PiRuntimeTests(unittest.TestCase):
    @patch.dict(os.environ, {'ELI_DISPLAY_BACKEND': 'tft', 'ELI_TTS_BACKEND': 'espeak'}, clear=True)
    def test_missing_pi_hardware_keeps_dashboard_controller_in_standby(self):
        with (patch('display.robot_eyes.create_robot_eyes', side_effect=OSError('SPI unavailable')),
              patch('audio.runtime_audio.Microphone', side_effect=OSError('PortAudio missing')),
              patch('runtime.components.which', return_value=None),
              patch('audio.speech_to_text.whisper_problem', return_value='whisper.cpp model unavailable')):
            components = configured_components('pi', 'mock', configured_eyes=True)
            runtime = UnifiedRuntime(components)
            self.addCleanup(runtime.shutdown)
            runtime.startup()
        self.assertEqual(runtime.controller.get_mode()['mode'], 'standby')
        status = runtime.controller.status()['runtime']
        self.assertIn('SPI unavailable', status['eyes']['detail'])
        self.assertIn('PortAudio', status['microphone']['detail'])
        self.assertIn('whisper.cpp', status['stt']['detail'])
        self.assertIn('espeak-ng', status['audio']['detail'])
        self.assertFalse(runtime.controller.movement()['moving'])
        runtime.shutdown()
        self.assertTrue(runtime.wait_for_shutdown(1))

    def test_cancellable_whisper_process_is_killed_and_drained(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = None
        is_current = iter((True, False))
        with (patch.object(speech_to_text, '_whisper_command', return_value=['whisper-cli']),
              patch.object(speech_to_text.subprocess, 'Popen', return_value=process)):
            self.assertEqual(speech_to_text.transcribe_cancellable('test.wav', timeout=8,
                                                                   current=lambda: next(is_current)), '')
        process.kill.assert_called_once()
        process.communicate.assert_called_once()

    def test_camera_metrics_and_separate_processing_cap(self):
        service, _, _ = mock_service()
        service.preview_fps = 5
        service.analysis_fps = 2
        self.addCleanup(service.shutdown)
        service.start()
        await_true(lambda: service.snapshot()['camera']['performance'].get('fps') is not None)
        camera = service.snapshot()['camera']
        self.assertEqual(camera['preview_fps'], 5)
        self.assertEqual(camera['analysis_fps'], 2)
        self.assertGreater(camera['performance']['fps'], 0)
        self.assertGreaterEqual(camera['performance']['frame_ms'], 0)
        service.stop()
        self.assertEqual(service.snapshot()['camera']['performance'], {})

    def test_linux_serial_discovery_returns_stable_alias_and_device(self):
        from unittest.mock import MagicMock

        alias = MagicMock(spec=Path)
        alias.__str__.return_value = '/dev/serial/by-id/usb-ESP32'
        alias.is_symlink.return_value = alias.exists.return_value = True
        by_id = MagicMock()
        by_path = MagicMock()
        by_id.glob.return_value = [alias]
        by_path.glob.return_value = []
        adapter = SimpleNamespace(device='/dev/ttyUSB0', description='CP210x')
        list_ports = SimpleNamespace(comports=lambda: [adapter])
        with (patch.dict(sys.modules, {'serial.tools.list_ports': list_ports}),
              patch('controller.esp32.os.name', 'posix'),
              patch('controller.esp32.os.path.realpath', return_value=adapter.device),
              patch('controller.esp32.Path', side_effect=[by_id, by_path, Path(adapter.device)])):
            devices = discover_ports()
        self.assertEqual(devices, [{'port': str(alias), 'description': 'CP210x'},
                                   {'port': adapter.device, 'description': 'CP210x'}])


if __name__ == '__main__':
    unittest.main()
