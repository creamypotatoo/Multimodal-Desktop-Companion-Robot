import cv2
import time
import threading

from perception.face import face_cascade
from intelligence.recognize import recognize_face
from intelligence.emotion import detect_emotion
from intelligence.multimodal import fuse
from intelligence.memory import remember_encounter, get_person_info
from intelligence.conversation import conversation_turn
from behavior.state_machine import RobotStateMachine, State
from hardware.mock_hardware import MockMotors

EMOTION_CHECK_INTERVAL = 1.0
CONVERSATION_COOLDOWN = 8.0

def main():
    fsm = RobotStateMachine()
    motors = MockMotors()
    cap = cv2.VideoCapture(0)

    last_emotion_check = 0
    current_emotion = None
    last_conversation_time = 0
    last_seen_person = None
    conversation_active = False  # our simple flag

    print("Eli is online. Press 'q' to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))

        person_name = None
        confidence = None
        now = time.time()
        run_emotion_check = (now - last_emotion_check) >= EMOTION_CHECK_INTERVAL

        for (x, y, w, h) in faces:
            face_crop = gray[y:y+h, x:x+w]
            person_name, confidence = recognize_face(face_crop)

            if run_emotion_check:
                current_emotion = detect_emotion(face_crop)

            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            label = f"{person_name} ({int(confidence)})"
            if current_emotion:
                label += f" - {current_emotion}"
            cv2.putText(frame, label, (x, y-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            break

        if run_emotion_check:
            last_emotion_check = now

        snapshot = fuse(person_name=person_name, emotion=current_emotion)

        if person_name and person_name != "Unknown" and person_name != last_seen_person:
            remember_encounter(person_name)
            info = get_person_info(person_name)
            print(f"[Memory] {person_name} — visit #{info['visit_count']}")
            last_seen_person = person_name

        # Start a conversation turn in the BACKGROUND, only if:
        # - we recognize someone
        # - Eli is idle
        # - enough time has passed since the last attempt
        # - no conversation is already running
        if (person_name and person_name != "Unknown"
                and fsm.current_state() == State.IDLE
                and not conversation_active
                and (now - last_conversation_time) >= CONVERSATION_COOLDOWN):

            last_conversation_time = now
            conversation_active = True

            def run_and_release():
                nonlocal conversation_active
                conversation_turn(fsm)
                motors.stop()
                conversation_active = False

            thread = threading.Thread(target=run_and_release, daemon=True)
            thread.start()

        cv2.imshow("Eli", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()