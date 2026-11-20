import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import cv2
import numpy as np
from perception.camera import Camera, CameraConfig
from perception.capture import main, save_jpeg


def device(readable=True):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8)) if readable else (False, None)
    cap.get.return_value = 0
    cap.getBackendName.return_value = 'FAKE'
    return cap


class CameraTests(unittest.TestCase):
    def test_platform_defaults(self):
        for system, expected in [('Linux', ('/dev/video0', cv2.CAP_V4L2)), ('Windows', (0, cv2.CAP_ANY))]:
            with self.subTest(system=system), patch('perception.camera.platform.system', return_value=system):
                self.assertEqual(CameraConfig().resolved(), expected)

    def test_overrides_and_validation(self):
        self.assertEqual(CameraConfig(2, cv2.CAP_DSHOW).resolved(), (2, cv2.CAP_DSHOW))
        for kwargs in [dict(width=0), dict(fps=-1), dict(pixel_format='BAD'), dict(read_attempts=0)]:
            with self.assertRaises(ValueError):
                CameraConfig(**kwargs)

    def test_requested_settings_and_probe_frame(self):
        cap = device()
        with patch('perception.camera.cv2.VideoCapture', return_value=cap):
            with Camera(CameraConfig(source=3, backend=cv2.CAP_ANY)) as camera:
                self.assertTrue(camera.read()[0])
                self.assertEqual(camera.settings['frame_width'], 640)
                self.assertEqual(camera.settings['mode'], 'requested')
                cap.read.assert_called_once()
                cap.set.assert_any_call(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set.assert_any_call(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set.assert_any_call(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set.assert_any_call(cv2.CAP_PROP_FPS, 30)
            camera.release()
        cap.release.assert_called_once()

    def test_all_fallbacks_reopen_and_release(self):
        caps = [device(False), device(False), device()]
        with patch('perception.camera.cv2.VideoCapture', side_effect=caps) as factory:
            with Camera(CameraConfig(read_attempts=2)) as camera:
                self.assertEqual(camera.settings['mode'], 'driver defaults')
            self.assertEqual(factory.call_count, 3)
        for cap in caps:
            cap.release.assert_called_once()
        self.assertNotIn(cv2.CAP_PROP_FOURCC, [c.args[0] for c in caps[1].set.call_args_list])
        caps[2].set.assert_not_called()

    def test_total_failure_releases_every_device(self):
        caps = [device(False) for _ in range(3)]
        caps[0].isOpened.return_value = False
        with patch('perception.camera.cv2.VideoCapture', side_effect=caps):
            with self.assertRaises(RuntimeError):
                Camera(CameraConfig(read_attempts=1)).open()
        for cap in caps:
            cap.release.assert_called_once()

    def test_cleanup_on_consumer_exception(self):
        cap = device()
        with patch('perception.camera.cv2.VideoCapture', return_value=cap):
            with self.assertRaises(ValueError):
                with Camera():
                    raise ValueError('consumer failure')
        cap.release.assert_called_once()

    def test_driver_exception_falls_back(self):
        first, second = device(), device()
        first.set.side_effect = cv2.error('unsupported setting')
        with patch('perception.camera.cv2.VideoCapture', side_effect=[first, second]):
            with Camera() as camera:
                self.assertEqual(camera.settings['mode'], 'size/fps')
        first.release.assert_called_once()
        second.release.assert_called_once()

    def test_headless_capture_and_no_overwrite(self):
        cap = device()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.jpg'
            with patch('perception.camera.cv2.VideoCapture', return_value=cap), patch('cv2.imshow') as show:
                self.assertEqual(main(['--output', str(path)]), 0)
                show.assert_not_called()
            original = path.read_bytes()
            self.assertEqual(cv2.imread(str(path)).shape, (480, 640, 3))
            with self.assertRaises(FileExistsError):
                save_jpeg(path, cap.read.return_value[1])
            with patch('perception.capture.Camera') as factory:
                with self.assertRaises(SystemExit) as exc:
                    main(['--output', str(path)])
                self.assertEqual(exc.exception.code, 1)
                factory.assert_not_called()
            self.assertEqual(original, path.read_bytes())
        cap.release.assert_called_once()

    def test_import_isolation(self):
        code = "import perception.capture, sys; assert not any(n.startswith(('audio', 'intelligence', 'perception.face')) for n in sys.modules)"
        subprocess.run([sys.executable, '-c', code], check=True)


if __name__ == '__main__':
    unittest.main()
