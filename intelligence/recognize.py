"""LBPH model loading shared by the legacy and dashboard pipelines."""
from math import isfinite
from pathlib import Path

import cv2

MODEL_PATH = Path('data/trained_model.yml')
LABELS_PATH = Path('data/labels.txt')
CONFIDENCE_THRESHOLD = 50


def load_recognizer(model=MODEL_PATH, labels=LABELS_PATH, threshold=CONFIDENCE_THRESHOLD):
    if not isfinite(threshold) or threshold <= 0:
        raise ValueError('LBPH threshold must be positive and finite')
    if not Path(model).is_file() or not Path(labels).is_file():
        raise FileNotFoundError('LBPH model or labels unavailable')
    names = Path(labels).read_text(encoding='utf-8').splitlines()
    if not names:
        raise ValueError('LBPH labels are empty')
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(str(model))

    def predict(crop):
        index, distance = recognizer.predict(crop)
        name = names[index].strip() if 0 <= index < len(names) and distance < threshold else ''
        return name or 'Unknown', float(distance)

    return predict


_legacy_recognizer = None


def recognize_face(gray_face_crop):
    global _legacy_recognizer
    if _legacy_recognizer is None:
        _legacy_recognizer = load_recognizer()
    return _legacy_recognizer(gray_face_crop)
