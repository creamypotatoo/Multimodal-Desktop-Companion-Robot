import cv2
import os
import numpy as np

DATA_DIR = os.path.join("data", "faces")
MODEL_PATH = os.path.join("data", "trained_model.yml")
LABELS_PATH = os.path.join("data", "labels.txt")

def train():
    recognizer = cv2.face.LBPHFaceRecognizer_create()

    images = []
    labels = []
    label_names = []  # index in this list = numeric label

    for person_name in sorted(os.listdir(DATA_DIR)):
        person_dir = os.path.join(DATA_DIR, person_name)
        if not os.path.isdir(person_dir):
            continue

        label_id = len(label_names)
        label_names.append(person_name)

        for filename in os.listdir(person_dir):
            img_path = os.path.join(person_dir, filename)
            img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            images.append(img)
            labels.append(label_id)

    if len(images) == 0:
        print("No training images found. Run registration first.")
        return

    print(f"Training on {len(images)} images across {len(label_names)} people...")
    recognizer.train(images, np.array(labels))

    recognizer.save(MODEL_PATH)
    with open(LABELS_PATH, "w") as f:
        f.write("\n".join(label_names))

    print(f"Model saved to {MODEL_PATH}")
    print(f"Labels saved to {LABELS_PATH}")

if __name__ == "__main__":
    train()