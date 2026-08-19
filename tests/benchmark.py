import time
import cv2
import psutil
import os

from perception.face import face_cascade
from intelligence.recognize import recognize_face
from intelligence.emotion import detect_emotion
from audio.speech_to_text import record_audio, transcribe
from audio.text_to_speech import speak

process = psutil.Process(os.getpid())

def measure_ram_mb():
    return process.memory_info().rss / (1024 * 1024)  # bytes -> MB

def measure_cpu_percent():
    # interval=1 makes it measure over 1 second for a meaningful reading
    return process.cpu_percent(interval=1)

def benchmark_face_detection(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    start = time.perf_counter()
    faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, faces, gray

def benchmark_face_recognition(gray, faces):
    if len(faces) == 0:
        return None
    (x, y, w, h) = faces[0]
    face_crop = gray[y:y+h, x:x+w]

    start = time.perf_counter()
    name, confidence = recognize_face(face_crop)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms

def benchmark_emotion(gray, faces):
    if len(faces) == 0:
        return None
    (x, y, w, h) = faces[0]
    face_crop = gray[y:y+h, x:x+w]

    start = time.perf_counter()
    emotion = detect_emotion(face_crop)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms

def benchmark_stt():
    start = time.perf_counter()
    audio_file = record_audio()  # this includes the fixed 5s recording time
    text = transcribe(audio_file)
    elapsed_sec = time.perf_counter() - start
    return elapsed_sec, text

def benchmark_tts(text="Hello, I am Eli."):
    start = time.perf_counter()
    speak(text)
    elapsed_sec = time.perf_counter() - start
    return elapsed_sec

def run_benchmark():
    print("=== Eli Performance Benchmark ===\n")

    # Warm-up call so cpu_percent() reports real data later, not 0.0
    process.cpu_percent(interval=None)

    print("RAM before loading anything: {:.1f} MB".format(measure_ram_mb()))

    cap = cv2.VideoCapture(0)
    ret, frame = cap.read()
    if not ret:
        print("Could not read from camera.")
        return

    # --- Vision ---
    det_ms, faces, gray = benchmark_face_detection(frame)
    print(f"Face detection:       {det_ms:.1f} ms  (faces found: {len(faces)})")

    rec_ms = benchmark_face_recognition(gray, faces)
    if rec_ms is not None:
        print(f"Face recognition:     {rec_ms:.1f} ms")
    else:
        print("Face recognition:     skipped (no face detected)")

    emo_ms = benchmark_emotion(gray, faces)
    if emo_ms is not None:
        print(f"Emotion inference:    {emo_ms:.1f} ms")
    else:
        print("Emotion inference:    skipped (no face detected)")

    cap.release()

    # --- Audio ---
    print("\nGet ready to speak for the STT benchmark...")
    time.sleep(1)
    stt_sec, heard_text = benchmark_stt()
    print(f"STT:                   {stt_sec:.2f} sec  (heard: '{heard_text}')")
    response_text = heard_text if heard_text.strip() else "I didn't catch that."
    
    tts_sec = benchmark_tts()
    print(f"TTS:                   {tts_sec:.2f} sec")

    # --- Full interaction estimate ---
    full_interaction_sec = (det_ms + (rec_ms or 0) + (emo_ms or 0)) / 1000 + stt_sec + tts_sec
    print(f"\nFull interaction:     {full_interaction_sec:.2f} sec (approx, sequential)")

    # --- Resource usage ---
    ram_mb = measure_ram_mb()
    cpu_pct = measure_cpu_percent()
    print(f"RAM usage:             {ram_mb:.1f} MB")
    print(f"CPU usage:             {cpu_pct:.1f} %")

if __name__ == "__main__":
    run_benchmark()