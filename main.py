"""Eli application; use --headless --vision-only for Raspberry Pi SSH."""
import argparse
import time
import threading

import cv2

from perception.camera import Camera, CameraConfig
from intelligence.multimodal import fuse
from intelligence.memory import remember_encounter, get_person_info
from behavior.state_machine import RobotStateMachine, State
from hardware.mock_hardware import MockMotors

EMOTION_CHECK_INTERVAL = 1.0
CONVERSATION_COOLDOWN = 8.0


def load_vision():
    # Existing loader reads data/trained_model.yml and keeps threshold 50.
    from perception.face import face_cascade
    from intelligence.recognize import recognize_face
    if face_cascade.empty():
        raise RuntimeError('Face cascade did not load')
    return face_cascade, recognize_face


def load_emotion():
    from intelligence.emotion import detect_emotion
    return detect_emotion


def load_conversation():
    from intelligence.conversation import conversation_turn
    return conversation_turn


def recognize_frame(frame, cascade, recognize):
    """Return the largest eligible face; never resize/equalise the crop."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    boxes = cascade.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
    eligible = [box for box in boxes if box[2] >= 80 and box[3] >= 80]
    if not eligible:
        return None, None, None, None
    box = max(eligible, key=lambda b: int(b[2]) * int(b[3]))
    x, y, w, h = box
    crop = gray[y:y+h, x:x+w]
    name, distance = recognize(crop)
    return name, distance, box, crop


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true', help='Disable OpenCV GUI; stop with Ctrl+C')
    parser.add_argument('--vision-only', action='store_true', help='Do not load emotion or audio/conversation')
    args = parser.parse_args(argv)
    fsm = RobotStateMachine()
    motors = MockMotors()
    camera = Camera(CameraConfig())
    greeted = set()
    last_seen_person = None
    last_reported_person = None
    last_emotion_check = 0
    last_conversation_time = 0
    current_emotion = None
    conversation_active = threading.Event()
    emotion = conversation = None
    status = 0

    try:
        try:
            cascade, recognize = load_vision()
        except Exception as exc:
            print(f'[Vision unavailable] {exc}', flush=True)
            return 1
        if not args.vision_only:
            for loader, label in ((load_emotion, 'Emotion'), (load_conversation, 'Conversation')):
                try:
                    callback = loader()
                    if label == 'Emotion':
                        emotion = callback
                    else:
                        conversation = callback
                except Exception as exc:
                    print(f'[{label} unavailable] {exc}', flush=True)

        camera.open()
        print('Camera settings:', camera.settings, flush=True)
        print("Eli is online. " + ('Press Ctrl+C to quit.' if args.headless else "Press 'q' to quit."), flush=True)
        while True:
            ret, frame = camera.read()
            if not ret or frame is None or not frame.size:
                print('[Camera] Could not read frame; stopping vision.', flush=True)
                status = 1
                break
            now = time.monotonic()
            person_name = confidence = box = crop = None
            try:
                # Always execute, including after an identity has been greeted.
                person_name, confidence, box, crop = recognize_frame(frame, cascade, recognize)
            except Exception as exc:
                print(f'[Vision] Frame recognition failed: {exc}', flush=True)

            if person_name != last_reported_person:
                print(f'[Vision] Identity: {person_name or "No recognised face"}', flush=True)
                last_reported_person = person_name
            if crop is None:
                current_emotion = None
            elif emotion and now - last_emotion_check >= EMOTION_CHECK_INTERVAL:
                last_emotion_check = now
                try:
                    current_emotion = emotion(crop)
                except Exception as exc:
                    print(f'[Emotion disabled] {exc}', flush=True)
                    emotion = None
                    current_emotion = None
            snapshot = fuse(person_name=person_name, emotion=current_emotion)

            known = bool(person_name and person_name != 'Unknown')
            if known and person_name not in greeted:
                # Record before side effects: failures must not repeat greetings.
                greeted.add(person_name)
                print(f'Hello, {person_name}!', flush=True)
            if known and person_name != last_seen_person:
                last_seen_person = person_name
                try:
                    remember_encounter(person_name)
                    info = get_person_info(person_name)
                    print(f"[Memory] {person_name} - visit #{info['visit_count']}")
                except Exception as exc:
                    print(f'[Memory] {exc}', flush=True)

            if (known and conversation and fsm.current_state() == State.IDLE
                    and not conversation_active.is_set()
                    and now - last_conversation_time >= CONVERSATION_COOLDOWN):
                last_conversation_time = now
                conversation_active.set()

                def run_and_release():
                    try:
                        conversation(fsm)
                    except Exception as exc:
                        print(f'[Conversation] {exc}', flush=True)
                    finally:
                        try:
                            motors.stop()
                        finally:
                            fsm.transition_to(State.IDLE)
                            conversation_active.clear()

                threading.Thread(target=run_and_release, daemon=True).start()

            if not args.headless:
                if box is not None:
                    x, y, w, h = box
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    label = f'{person_name} ({confidence:.1f})'
                    if current_emotion:
                        label += f' - {current_emotion}'
                    cv2.putText(frame, label, (x, max(20, y-10)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow('Eli', frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    except KeyboardInterrupt:
        print('Eli stopped.', flush=True)
    except (RuntimeError, OSError, cv2.error) as exc:
        print(f'[Camera/preview unavailable] {exc}', flush=True)
        status = 1
    finally:
        camera.release()
        motors.stop()
        if not args.headless:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass
    return status


if __name__ == '__main__':
    raise SystemExit(main())
