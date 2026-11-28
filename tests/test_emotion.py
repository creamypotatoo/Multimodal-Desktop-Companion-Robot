from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from intelligence.emotion import load_classifier


class EmotionTests(unittest.TestCase):
    def test_missing_model_requires_neither_camera_nor_onnx_runtime(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                load_classifier(Path(directory) / 'missing.onnx')
