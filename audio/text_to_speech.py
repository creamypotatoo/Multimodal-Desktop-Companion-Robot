"""Text-to-speech for Eli.

``speak(text)`` is the public interface and blocks until speech finishes.

Backends:
  * ``pyttsx3`` - Windows/desktop development (original behaviour).
  * ``espeak``  - Raspberry Pi: espeak-ng -> WAV, ffmpeg applies digital
                  volume, aplay plays it on a *named* ALSA device
                  (MAX98357A I2S amplifier by default).
  * ``auto``    - ``espeak`` on Linux when espeak-ng, ffmpeg and aplay are
                  all installed, otherwise ``pyttsx3``.

Configuration (explicit arguments override environment variables):
  ELI_TTS_BACKEND  auto | espeak | pyttsx3       (default: auto)
  ELI_TTS_DEVICE   ALSA device name              (default: plughw:CARD=MAX98357A,DEV=0)
  ELI_TTS_VOLUME   digital volume, 0 < v <= 1.0  (default: 0.10)

Numeric ALSA card indices (e.g. ``hw:1,0`` or ``CARD=1``) are rejected because
they change between boots; use the card name shown by ``aplay -L``.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

RATE = 175  # words per minute, shared by both backends

DEFAULT_BACKEND = "auto"
DEFAULT_DEVICE = "plughw:CARD=MAX98357A,DEV=0"
DEFAULT_VOLUME = 0.10
BACKENDS = ("auto", "espeak", "pyttsx3")
PI_TOOLS = ("espeak-ng", "ffmpeg", "aplay")

SYNTH_TIMEOUT = 30   # seconds for espeak-ng / ffmpeg
PLAY_TIMEOUT = 120   # seconds for aplay

# One utterance at a time, across all threads and backends.
_speech_lock = threading.Lock()


class TTSError(RuntimeError):
    """Speech could not be produced or played."""


def validate_device(device):
    """Return a cleaned ALSA device name; reject numeric card indices."""
    device = (device or "").strip()
    if not device:
        raise ValueError("ALSA device must not be empty")
    if ":" in device:
        args = device.split(":", 1)[1].split(",")
        card = None
        for index, arg in enumerate(args):
            if arg.upper().startswith("CARD="):
                card = arg[5:]
            elif index == 0 and "=" not in arg:
                card = arg
        if card is not None and card.strip().isdigit():
            raise ValueError(
                f"ALSA device {device!r} uses numeric card index {card!r}; "
                "use the card name instead, e.g. plughw:CARD=MAX98357A,DEV=0")
    return device


def validate_volume(volume):
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        raise ValueError(f"TTS volume must be a number, got {volume!r}") from None
    if not 0 < volume <= 1.0:
        raise ValueError(f"TTS volume must be > 0 and <= 1.0, got {volume}")
    return volume


@dataclass(frozen=True)
class TTSConfig:
    backend: str = DEFAULT_BACKEND
    device: str = DEFAULT_DEVICE
    volume: float = DEFAULT_VOLUME

    def __post_init__(self):
        backend = (self.backend or "").strip().lower()
        if backend not in BACKENDS:
            raise ValueError(f"TTS backend must be one of {BACKENDS}, got {self.backend!r}")
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "device", validate_device(self.device))
        object.__setattr__(self, "volume", validate_volume(self.volume))

    @classmethod
    def from_env(cls, environ=None, **overrides):
        env = os.environ if environ is None else environ
        values = {
            "backend": env.get("ELI_TTS_BACKEND", DEFAULT_BACKEND),
            "device": env.get("ELI_TTS_DEVICE", DEFAULT_DEVICE),
            "volume": env.get("ELI_TTS_VOLUME", DEFAULT_VOLUME),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


def _find_pi_tools():
    """Return {tool: absolute path} or None if any tool is missing."""
    paths = {tool: shutil.which(tool) for tool in PI_TOOLS}
    return paths if all(paths.values()) else None


def resolve_backend(config):
    if config.backend != "auto":
        return config.backend
    if sys.platform.startswith("linux") and _find_pi_tools():
        return "espeak"
    return "pyttsx3"


def _run(cmd, *, timeout, stage, **kwargs):
    """Run a command (argument list, never a shell) and map failures to TTSError."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise TTSError(f"{stage} timed out after {timeout}s") from None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        detail = (stderr or "").strip() or f"exit status {exc.returncode}"
        raise TTSError(f"{stage} failed: {detail}") from None
    except OSError as exc:
        raise TTSError(f"{stage} could not start: {exc}") from None


def _notify(on_start):
    """Run the playback-start callback; it must never break speech."""
    if on_start is not None:
        try:
            on_start()
        except Exception:
            pass


def _speak_espeak(text, config, on_start=None, *, should_play=None):
    tools = _find_pi_tools()
    if tools is None:
        missing = [tool for tool in PI_TOOLS if not shutil.which(tool)]
        raise TTSError("Raspberry Pi TTS needs: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="eli_tts_") as tmp:
        raw_wav = str(Path(tmp) / "speech_raw.wav")
        out_wav = str(Path(tmp) / "speech.wav")
        # Text goes through stdin so it can never be parsed as an option.
        _run([tools["espeak-ng"], "--stdin", "-s", str(RATE), "-w", raw_wav],
             input=text.encode("utf-8"), timeout=SYNTH_TIMEOUT, stage="espeak-ng")
        _run([tools["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-i", raw_wav, "-filter:a", f"volume={config.volume:g}", out_wav],
             timeout=SYNTH_TIMEOUT, stage="ffmpeg")
        if should_play is not None and not should_play():
            return
        _notify(on_start)
        _run([tools["aplay"], "-q", "-D", config.device, out_wav],
             timeout=PLAY_TIMEOUT, stage="aplay")


def _speak_pyttsx3(text, config, on_start=None, *, should_play=None):
    import pyttsx3  # imported lazily so the Pi does not need it

    engine = pyttsx3.init()
    engine.setProperty("rate", RATE)
    if should_play is not None:
        if not should_play():
            return
        engine.setProperty("volume", config.volume)
    engine.say(text)
    _notify(on_start)
    engine.runAndWait()  # blocks until speech finishes


def speak(text, config=None, on_start=None, *, should_play=None):
    """Speak ``text`` and block until finished. Calls never overlap.

    ``on_start`` (optional) is called once, just before audio playback begins
    (after synthesis), e.g. to show the speaking expression.
    An optional ``should_play`` predicate can suppress queued speech at that
    boundary. It does not interrupt playback that has already started.
    """
    if text is None or not str(text).strip():
        return
    text = str(text)
    config = config or TTSConfig.from_env()
    backend = resolve_backend(config)
    with _speech_lock:
        if should_play is not None and not should_play():
            return
        options = {} if should_play is None else {"should_play": should_play}
        if backend == "espeak":
            _speak_espeak(text, config, on_start, **options)
        else:
            _speak_pyttsx3(text, config, on_start, **options)


def _cli(argv=None):
    parser = argparse.ArgumentParser(description="Speak a test phrase.")
    parser.add_argument("text", nargs="?", default="Hello, I am Eli. Nice to meet you.")
    parser.add_argument("--backend", choices=BACKENDS)
    parser.add_argument("--device", help="ALSA device name (not a numeric card index)")
    parser.add_argument("--volume", type=float, help="digital volume, 0 < v <= 1.0")
    args = parser.parse_args(argv)
    try:
        config = TTSConfig.from_env(backend=args.backend, device=args.device, volume=args.volume)
        backend = resolve_backend(config)
        print(f"[TTS] backend={backend} device={config.device} volume={config.volume:g}", flush=True)
        speak(args.text, config)
    except (ValueError, TTSError) as exc:
        print(f"[TTS] {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
