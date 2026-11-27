"""Mock and opt-in local adapters for the existing conversation and eye engines."""
from dataclasses import replace
from pathlib import Path
from tempfile import mkstemp
from time import sleep
import os

from controller.errors import LIVE, SIMULATED
from runtime import Components


def mock_components():
    def eyes():
        from display.eyes import EyeEngine
        return EyeEngine(niceness=0)

    def record():
        sleep(0.55)
        return 'simulated-input'

    def transcribe(recording, *, timeout):
        return 'Hello Eli'

    def respond(text, person_name, **kwargs):
        sleep(0.55)
        return 'Hello! I am here with you.'

    def speak(text, volume, current, on_start):
        if current():
            if on_start:
                on_start()
            sleep(0.45)

    return Components(eyes, record, transcribe, respond, speak)


def local_components(*, camera=False):
    from audio.speech_to_text import record_audio, transcribe
    from audio.text_to_speech import TTSConfig, speak
    from display.robot_eyes import create_robot_eyes, selected_backend
    from intelligence.conversation import default_responder

    config = TTSConfig.from_env()
    recording = None

    def eyes():
        backend = selected_backend()
        if backend is None:
            return None
        if backend not in ('tft', 'mock'):
            raise ValueError('Local I/O requires tft, mock, or off for the display')
        engine = create_robot_eyes(backend)
        engine.availability = LIVE if backend == 'tft' else SIMULATED
        return engine

    def record():
        nonlocal recording
        fd, filename = mkstemp(prefix='eli-runtime-', suffix='.wav')
        os.close(fd)
        recording = Path(filename)
        return record_audio(filename)

    def cleanup():
        nonlocal recording
        if recording is not None:
            recording.unlink(missing_ok=True)
            recording = None

    def play(text, volume, current, on_start):
        speak(text, replace(config, volume=volume / 100), on_start=on_start, should_play=current)

    components = Components(eyes, record, transcribe, default_responder(), play, simulated=False,
                            cleanup_recording=cleanup, initial_volume=max(1, round(config.volume * 100)))
    return replace(components, profile=local_profile) if camera else components


def local_profile(current):
    """Capture a bounded sample on the conversation worker and release before audio."""
    from main import IDENTITY_CONFIRM_FRAMES, load_emotion, load_vision, recognize_frame
    from perception.camera import Camera
    from intelligence.memory import get_person_info, remember_encounter

    if not current():
        return None
    cascade, recognize = load_vision()
    identity = None
    count = 0
    with Camera() as camera:
        for _ in range(12):
            if not current():
                return None
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError('Camera read failed')
            name, _, _, crop = recognize_frame(frame, cascade, recognize)
            if not name or name == 'Unknown':
                identity, count = None, 0
                continue
            count = count + 1 if name == identity else 1
            identity = name
            if count >= IDENTITY_CONFIRM_FRAMES:
                break
    if count < IDENTITY_CONFIRM_FRAMES or not current():
        return {}
    profile = dict(person_name=identity)
    try:
        profile['emotion'] = load_emotion()(crop)
    except Exception:
        pass  # Emotion recognition is optional.
    if current():
        try:
            remember_encounter(identity)
            profile['visit_count'] = (get_person_info(identity) or {}).get('visit_count')
        except Exception:
            pass  # Memory must not prevent a recognized conversation.
    return profile
