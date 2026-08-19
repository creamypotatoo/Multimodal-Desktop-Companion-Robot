import cv2
import time
from perception.face import face_cascade
from intelligence.emotion import detect_emotion

EMOTION_CHECK_INTERVAL = 1.0  # seconds between emotion checks

cap = cv2.VideoCapture(0)
last_check_time = 0
current_emotion = ""

print("Press 'q' to quit.")
while True:
    ret, frame = cap.read()
    if not ret:
        break

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))

    now = time.time()
    run_emotion_check = (now - last_check_time) >= EMOTION_CHECK_INTERVAL

    for (x, y, w, h) in faces:
        if run_emotion_check:
            face_crop = gray[y:y+h, x:x+w]
            current_emotion = detect_emotion(face_crop)

        cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 0), 2)
        if current_emotion:
            cv2.putText(frame, current_emotion, (x, y-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

    if run_emotion_check:
        last_check_time = now

    cv2.imshow("Emotion Test", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()