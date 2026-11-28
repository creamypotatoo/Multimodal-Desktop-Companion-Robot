"""Optional FER+ facial-expression classification using a local model."""
from pathlib import Path

import cv2
import numpy as np

MODEL_PATH = Path('data/models/emotion-ferplus-8.onnx')
EMOTIONS = ['neutral', 'happy', 'surprise', 'sad', 'angry', 'disgust', 'fear', 'contempt']


def load_classifier(model=MODEL_PATH):
    if not Path(model).is_file():
        raise FileNotFoundError('FER+ model unavailable')
    import onnxruntime as ort

    session = ort.InferenceSession(str(model), providers=['CPUExecutionProvider'])
    input_name = session.get_inputs()[0].name

    def classify(crop):
        tensor = cv2.resize(crop, (64, 64)).astype(np.float32).reshape(1, 1, 64, 64)
        return EMOTIONS[np.argmax(session.run(None, {input_name: tensor})[0][0])]

    return classify


_legacy_classifier = None


def detect_emotion(face_crop_gray):
    global _legacy_classifier
    if _legacy_classifier is None:
        _legacy_classifier = load_classifier()
    return _legacy_classifier(face_crop_gray)
