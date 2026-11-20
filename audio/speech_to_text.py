import subprocess
import wave
from pathlib import Path

import sounddevice as sd

SAMPLE_RATE = 16000
DURATION = 5

WHISPER_DIR = Path.home() / "whisper.cpp"
WHISPER_CLI = WHISPER_DIR / "build/bin/whisper-cli"
MODEL_PATH = WHISPER_DIR / "models/ggml-tiny.bin"


def record_audio(filename="temp_audio.wav"):
    devices = sd.query_devices()

    mic_index = next(
        (
            i for i, device in enumerate(devices)
            if device["max_input_channels"] > 0
            and "USB 2.0 Camera" in device["name"]
        ),
        None,
    )

    if mic_index is None:
        raise RuntimeError("Eli's USB webcam microphone was not found.")

    print(f"Recording for {DURATION} seconds... speak now.")

    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=mic_index,
    )
    sd.wait()

    with wave.open(str(filename), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio.tobytes())

    print("Recording finished.")
    return filename


def transcribe(filename="temp_audio.wav"):
    result = subprocess.run(
        [
            str(WHISPER_CLI),
            "-m", str(MODEL_PATH),
            "-f", str(filename),
            "-l", "en",
            "-t", "4",
            "-nt",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    return result.stdout.strip()


if __name__ == "__main__":
    audio_file = record_audio()
    result = transcribe(audio_file)
    print("Eli heard:", result)
