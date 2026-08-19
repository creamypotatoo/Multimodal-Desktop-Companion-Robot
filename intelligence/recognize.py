import cv2
import os

MODEL_PATH = os.path.join("data", "trained_model.yml")
LABELS_PATH = os.path.join("data", "labels.txt")

recognizer = cv2.face.LBPHFaceRecognizer_create()
recognizer.read(MODEL_PATH)

with open(LABELS_PATH, "r") as f:
    label_names = f.read().splitlines()

CONFIDENCE_THRESHOLD = 50  # lower = stricter. Tune this based on testing.

def recognize_face(gray_face_crop):
    """
    Takes a cropped grayscale face image.
    Returns (name, confidence).
    """
    label_id, confidence = recognizer.predict(gray_face_crop)

    if confidence < CONFIDENCE_THRESHOLD:
        name = label_names[label_id]
    else:
        name = "Unknown"

    return name, confidence