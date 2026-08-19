import sounddevice as sd
from scipy.io.wavfile import write
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000  # Whisper expects 16kHz audio
DURATION = 5          # seconds to record

def record_audio(filename="temp_audio.wav"):
    print(f"Recording for {DURATION} seconds... speak now.")
    audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='int16')
    sd.wait()  # blocks until recording finishes
    write(filename, SAMPLE_RATE, audio)
    print("Recording finished.")
    return filename

def transcribe(filename="temp_audio.wav"):
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(filename)

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text)

    return " ".join(text_parts).strip()

if __name__ == "__main__":
    audio_file = record_audio()
    result = transcribe(audio_file)
    print("You said:", result)