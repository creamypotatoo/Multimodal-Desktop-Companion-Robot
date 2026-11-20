import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import main


class MainTests(unittest.TestCase):
    def run_frames(self, predictions, *, open_error=None):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        camera = Mock()
        camera.read.side_effect = [(True, frame.copy()) for _ in predictions] + [(False, None)]
        if open_error:
            camera.open.side_effect = open_error
        detector = Mock()
        detector.detectMultiScale.return_value = np.array([[0, 0, 51, 51], [20, 30, 200, 160]])
        recognize = Mock(side_effect=predictions)
        log = io.StringIO()
        with patch('main.Camera', return_value=camera), \
                patch('main.load_vision', return_value=(detector, recognize)), \
                patch('main.load_emotion') as emotion, \
                patch('main.load_conversation') as conversation, \
                patch('main.remember_encounter') as remember, \
                patch('main.get_person_info', return_value={'visit_count': 1}), \
                patch('main.cv2.imshow') as show, \
                patch('main.cv2.waitKey') as key, \
                patch('main.cv2.destroyAllWindows') as destroy, \
                contextlib.redirect_stdout(log):
            result = main.main(['--headless', '--vision-only'])
        emotion.assert_not_called()
        conversation.assert_not_called()
        show.assert_not_called()
        key.assert_not_called()
        destroy.assert_not_called()
        camera.release.assert_called_once()
        return log.getvalue(), recognize, remember, result

    def test_continuous_recognition_and_one_greeting_despite_errors(self):
        predictions = [('Shanta', 41.67), ('Shanta', 42), RuntimeError('prediction failed'),
                       ('Unknown', 68), ('Shanta', 43)]
        log, recognize, remember, _ = self.run_frames(predictions)
        self.assertEqual(recognize.call_count, 5)
        self.assertEqual(log.count('Hello, Shanta!'), 1)
        self.assertIn('Identity: Unknown', log)
        self.assertIn('Frame recognition failed', log)
        remember.assert_called_once_with('Shanta')
        self.assertEqual(recognize.call_args.args[0].shape, (160, 200))

    def test_unknown_never_greeted_or_remembered(self):
        log, recognize, remember, _ = self.run_frames([('Unknown', 63.3)] * 3)
        self.assertNotIn('Hello,', log)
        self.assertEqual(recognize.call_count, 3)
        remember.assert_not_called()

    def test_camera_open_failure_is_controlled_and_released(self):
        log, recognize, remember, status = self.run_frames([], open_error=RuntimeError('no camera'))
        self.assertEqual(status, 1)
        self.assertIn('no camera', log)
        recognize.assert_not_called()

    def test_no_face_does_not_call_recognizer(self):
        detector = Mock()
        detector.detectMultiScale.return_value = np.array([[0, 0, 51, 51]])
        recognize = Mock()
        result = main.recognize_frame(np.zeros((480, 640, 3), dtype=np.uint8), detector, recognize)
        self.assertEqual(result, (None, None, None, None))
        recognize.assert_not_called()

    def test_crop_pixels_unchanged(self):
        frame = np.random.default_rng(2).integers(0, 256, (480, 640, 3), dtype=np.uint8)
        detector = Mock()
        detector.detectMultiScale.return_value = [(0, 0, 90, 90), (20, 30, 200, 160)]
        recognize = Mock(return_value=('Shanta', 41))
        main.recognize_frame(frame, detector, recognize)
        np.testing.assert_array_equal(recognize.call_args.args[0],
                                      cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[30:190, 20:220])

    def test_import_and_cli_with_optional_dependencies_blocked(self):
        code = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('audio', 'onnxruntime', 'faster_whisper', 'sounddevice', 'pyttsx3') or fullname in ('intelligence.emotion', 'intelligence.conversation'):
            raise AssertionError('Forbidden optional import: ' + fullname)
sys.meta_path.insert(0, Block())
import main
from unittest.mock import Mock, patch
camera = Mock()
camera.read.side_effect = KeyboardInterrupt
with patch('main.Camera', return_value=camera), patch('main.load_vision', return_value=(Mock(), Mock())):
    assert main.main(['--headless', '--vision-only']) == 0
camera.release.assert_called_once()
'''
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
