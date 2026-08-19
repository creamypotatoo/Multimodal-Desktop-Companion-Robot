import cv2
import os
from perception.face import face_cascade  # reuse the detector we already built

def register_person(name, num_samples=20):
    """
    Captures face images from the webcam and saves them for one person.
    Press SPACE to capture a sample, ESC to stop early.
    """
    save_dir = os.path.join("data", "faces", name)
    os.makedirs(save_dir, exist_ok=True)

    cap = cv2.VideoCapture(0)
    count = 0

    print(f"Registering '{name}'. Press SPACE to capture, ESC to quit.")

    while count < num_samples:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))

        display = frame.copy()
        for (x, y, w, h) in faces:
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)

        cv2.putText(display, f"Samples: {count}/{num_samples}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.imshow("Register Face - SPACE to capture, ESC to quit", display)

        key = cv2.waitKey(1) & 0xFF

        if key == 27:  # ESC
            break
        elif key == 32 and len(faces) > 0:  # SPACE, and a face is visible
            (x, y, w, h) = faces[0]
            face_crop = gray[y:y + h, x:x + w]
            file_path = os.path.join(save_dir, f"{count}.jpg")
            cv2.imwrite(file_path, face_crop)
            count += 1
            print(f"Saved sample {count}")

    cap.release()
    cv2.destroyAllWindows()
    print(f"Done. {count} samples saved to {save_dir}")