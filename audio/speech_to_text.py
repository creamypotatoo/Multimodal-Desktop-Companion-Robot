"""Speech capture and whisper.cpp transcription for Eli.

``record_audio()`` listens on the USB webcam microphone and records one
utterance of variable length using lightweight RMS voice-activity detection:

  1. Wait (up to ELI_VAD_START_TIMEOUT s) for speech to begin.
  2. Record while the person talks; pauses shorter than ELI_VAD_SILENCE s are
     kept, and each new sound resets the silence timer.
  3. Stop after ELI_VAD_SILENCE s of continuous silence, or at
     ELI_VAD_MAX_DURATION s at the latest.

About 0.3 s of audio before speech onset is kept so the first word is not
clipped, and about 0.5 s of trailing silence is kept for whisper. Returns the
WAV filename (16 kHz, mono, 16-bit, as before), or ``None`` if nobody spoke
before the start timeout.

Configuration (environment variables):
  ELI_VAD_SILENCE        seconds of silence that end an utterance  (default 1.5)
  ELI_VAD_THRESHOLD      minimum int16 RMS counted as speech       (default 150)
  ELI_VAD_NOISE_RATIO    speech must also be this many times the
                         measured background-noise RMS             (default 3.0)
  ELI_VAD_START_TIMEOUT  seconds to wait for speech to begin       (default 15)
  ELI_VAD_MAX_DURATION   hard cap on one recording, seconds        (default 60)

Tune the threshold with:  python -m audio.speech_to_text --levels
"""
import argparse
import math
import os
import subprocess
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.03                       # 30 ms analysis chunks
BLOCK_FRAMES = int(SAMPLE_RATE * BLOCK_SECONDS)
MIC_NAME = "USB 2.0 Camera"

WHISPER_DIR = Path.home() / "whisper.cpp"
WHISPER_CLI = WHISPER_DIR / "build/bin/whisper-cli"
MODEL_PATH = WHISPER_DIR / "models/ggml-tiny.bin"

# Speech continues while RMS stays above this fraction of the onset threshold,
# so quieter word endings do not count as silence (hysteresis).
CONTINUE_RATIO = 0.7
# Background-noise estimate: falls immediately to quieter levels, rises slowly
# (per 30 ms chunk) so steady fan/hum noise raises the threshold over ~1.5 s.
NOISE_RISE = 0.02


def _log(message):
    print(f"[Mic] {message}", flush=True)


def _env_seconds(environ, name, default, minimum):
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value < minimum:
        _log(f"Ignoring invalid {name}={raw!r}; using {default:g}")
        return default
    return value


@dataclass(frozen=True)
class VadSettings:
    silence: float = 1.5          # seconds of silence that end the utterance
    threshold: float = 150.0      # absolute minimum speech RMS (int16 units)
    noise_ratio: float = 3.0      # speech must exceed noise floor * ratio
    start_timeout: float = 15.0   # seconds to wait for speech to begin
    max_duration: float = 60.0    # hard cap for one recording
    pre_roll: float = 0.3         # audio kept from before speech onset
    trailing: float = 0.5         # silence kept after the last speech
    min_speech: float = 0.12      # sustained sound needed to count as onset
    calibration: float = 0.2      # initial noise measurement window

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        d = cls()
        return cls(
            silence=_env_seconds(env, "ELI_VAD_SILENCE", d.silence, 0.2),
            threshold=_env_seconds(env, "ELI_VAD_THRESHOLD", d.threshold, 1),
            noise_ratio=_env_seconds(env, "ELI_VAD_NOISE_RATIO", d.noise_ratio, 1),
            start_timeout=_env_seconds(env, "ELI_VAD_START_TIMEOUT", d.start_timeout, 1),
            max_duration=_env_seconds(env, "ELI_VAD_MAX_DURATION", d.max_duration, 1),
        )


def chunk_rms(chunk):
    samples = np.asarray(chunk, dtype=np.float64).ravel()
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def _blocks(seconds, block_seconds):
    return max(1, int(math.ceil(seconds / block_seconds - 1e-9)))


def capture_utterance(read_chunk, settings=None, block_seconds=BLOCK_SECONDS, log=_log):
    """Run voice-activity detection over chunks returned by ``read_chunk()``.

    ``read_chunk`` must block until the next chunk of int16 samples is
    available (as ``sounddevice.InputStream.read`` does), so this loop never
    busy-waits. Time is counted in chunks, which makes the logic deterministic.
    Returns a 1-D int16 array, or None if speech never started.
    """
    s = settings or VadSettings()
    n_pre = int(round(s.pre_roll / block_seconds))
    n_onset = _blocks(s.min_speech, block_seconds)
    n_calibrate = _blocks(s.calibration, block_seconds)
    n_wait = _blocks(s.start_timeout, block_seconds)
    n_silence = _blocks(s.silence, block_seconds)
    n_trailing = int(round(s.trailing / block_seconds))
    n_max = _blocks(s.max_duration, block_seconds)

    recent = deque(maxlen=n_pre + n_onset)  # pre-roll + onset candidate chunks
    noise = None
    run = 0
    onset_threshold = s.threshold
    for index in range(n_wait):
        chunk = np.asarray(read_chunk(), dtype=np.int16).ravel()
        recent.append(chunk)
        level = chunk_rms(chunk)
        if index < n_calibrate:
            # Quietest chunk of the first moments = background-noise estimate.
            noise = level if noise is None else min(noise, level)
            continue
        onset_threshold = max(s.threshold, noise * s.noise_ratio)
        if level >= onset_threshold:
            run += 1
            if run >= n_onset:
                break
        else:
            run = 0
            noise = level if level < noise else noise + NOISE_RISE * (level - noise)
    else:
        return None

    frames = list(recent)
    continue_threshold = onset_threshold * CONTINUE_RATIO
    silent = 0
    stopped_by_silence = False
    while len(frames) < n_max:
        chunk = np.asarray(read_chunk(), dtype=np.int16).ravel()
        frames.append(chunk)
        if chunk_rms(chunk) >= continue_threshold:
            silent = 0  # speech resumed: restart the silence timer
        else:
            silent += 1
            if silent >= n_silence:
                stopped_by_silence = True
                break
    if stopped_by_silence:
        del frames[len(frames) - (silent - n_trailing):]
    else:
        log(f"Reached the {s.max_duration:g}s recording limit.")
    return np.concatenate(frames).astype(np.int16, copy=False)


def find_microphone(devices=None):
    devices = sd.query_devices() if devices is None else devices
    mic_index = next(
        (
            i for i, device in enumerate(devices)
            if device["max_input_channels"] > 0
            and MIC_NAME in device["name"]
        ),
        None,
    )
    if mic_index is None:
        raise RuntimeError("Eli's USB webcam microphone was not found.")
    return mic_index


def write_wav(filename, samples):
    with wave.open(str(filename), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def _open_stream(mic_index):
    return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                          device=mic_index, blocksize=BLOCK_FRAMES)


def record_audio(filename="temp_audio.wav", settings=None):
    """Record one utterance to ``filename``; return it, or None if no one spoke.

    The input stream is always closed (microphone released) on return, error
    or Ctrl+C.
    """
    settings = settings or VadSettings.from_env()
    mic_index = find_microphone()
    overflows = 0

    print("Listening... speak now.", flush=True)
    with _open_stream(mic_index) as stream:
        def read_chunk():
            nonlocal overflows
            data, overflowed = stream.read(BLOCK_FRAMES)
            overflows += bool(overflowed)
            return data
        audio = capture_utterance(read_chunk, settings)
    if overflows:
        _log(f"{overflows} input overflow(s); some audio may be missing.")

    if audio is None:
        print(f"No speech heard in {settings.start_timeout:g}s.", flush=True)
        return None
    write_wav(filename, audio)
    print(f"Recording finished ({len(audio) / SAMPLE_RATE:.1f}s).", flush=True)
    return filename


def transcribe(filename="temp_audio.wav", timeout=None):
    """Run whisper.cpp; ``timeout`` (seconds) raises subprocess.TimeoutExpired."""
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
        timeout=timeout,
    )

    return result.stdout.strip()


def show_levels(seconds=10.0):
    """Print microphone RMS levels, to choose ELI_VAD_THRESHOLD."""
    settings = VadSettings.from_env()
    per_line = max(1, int(round(0.25 / BLOCK_SECONDS)))
    print(f"Showing mic RMS for {seconds:g}s (threshold {settings.threshold:g}). "
          "Stay quiet, then talk normally.", flush=True)
    with _open_stream(find_microphone()) as stream:
        for _ in range(int(seconds / (per_line * BLOCK_SECONDS))):
            level = max(chunk_rms(stream.read(BLOCK_FRAMES)[0]) for _ in range(per_line))
            bar = "#" * min(60, int(level / 25))
            print(f"{level:7.0f} {'SPEECH' if level >= settings.threshold else '      '} {bar}",
                  flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record one utterance and transcribe it.")
    parser.add_argument("--levels", action="store_true",
                        help="only print live microphone levels for threshold tuning")
    args = parser.parse_args()
    if args.levels:
        show_levels()
    else:
        audio_file = record_audio()
        if audio_file is None:
            print("Eli heard nothing.")
        else:
            print("Eli heard:", transcribe(audio_file))
