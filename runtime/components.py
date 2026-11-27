"""Mock and opt-in local adapters for the existing conversation and eye engines."""
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
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
        if not current():
            return False
        if on_start:
            on_start()
        deadline = monotonic() + 0.45
        while current() and monotonic() < deadline:
            sleep(0.05)
        return current()

    def capture(current, *, wake=False, timeout=None):
        if not current():
            return None
        if wake:
            sleep(0.2)
            return None
        return record()

    return Components(eyes, record, transcribe, respond, speak, capture=capture)


def local_components(*, camera=False):
    return configured_components('pi', 'ollama', configured_eyes=True, camera=camera)


def configured_components(audio='mock', ai='mock', *, configured_eyes=False, camera=False):
    from audio.runtime_audio import Microphone, OfflineWhisper, isolated_player
    from audio.speech_to_text import find_microphone, transcribe
    from audio.text_to_speech import TTSConfig, resolve_backend
    from display.robot_eyes import create_robot_eyes, selected_backend
    from intelligence.conversation import default_responder

    components = mock_components()
    if ai == 'ollama':
        components = replace(components, respond=default_responder(), ai_simulated=False)
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

    if configured_eyes:
        components = replace(components, eyes=eyes)
    if camera:
        components = replace(components, profile=local_profile)
    if audio == 'mock':
        return components
    config = TTSConfig.from_env(backend='pyttsx3' if audio == 'windows' else None)
    config = replace(config, backend=resolve_backend(config))
    selection = os.environ.get('ELI_AUDIO_INPUT', '').strip()
    rate = int(os.environ.get('ELI_AUDIO_SAMPLE_RATE', '16000'))
    device = int(selection) if selection.isdecimal() else selection or None
    if device == 'default':
        device = None
    elif device is None and audio == 'pi':
        try:
            device = find_microphone()
        except Exception:
            pass
    microphone = Microphone(device=device, rate=rate, pcm=audio == 'windows',
                            max_gain=float(os.environ.get('ELI_AUDIO_GAIN_MAX', '1')))
    try:
        inputs = microphone.devices()
    except Exception:
        inputs = []
    try:
        default_input = microphone.driver.query_devices(kind='input')['name']
    except Exception:
        default_input = None
    stt = (OfflineWhisper(os.environ.get('ELI_WHISPER_MODEL', ''),
                          prompt=os.environ.get('ELI_WHISPER_PROMPT', 'Eli is a personal robot assistant.'),
                          beam_size=int(os.environ.get('ELI_WHISPER_BEAM_SIZE', '5')))
           if audio == 'windows' else transcribe)
    output = ('Windows default playback device' if audio == 'windows'
              else f'Local output configuration: {config.device}')

    def capture(current, *, wake=False, timeout=None):
        nonlocal recording
        recording = microphone.capture(current, wake=wake, timeout=timeout)
        return recording

    def cleanup():
        nonlocal recording
        if recording is not None:
            if isinstance(recording, (str, os.PathLike)):
                Path(recording).unlink(missing_ok=True)
            recording = None

    def play(text, volume, current, on_start):
        return isolated_player(config, text, volume, current, on_start)

    return replace(components, capture=capture, transcribe=stt, speak=play, simulated=False,
                   ai_simulated=ai == 'mock', microphone=microphone, audio_backend=audio,
                   cleanup_recording=cleanup, initial_volume=max(1, round(config.volume * 100)),
                   audio_info=dict(inputs=inputs, default_input=default_input, output=output, sample_rate=rate,
                                   stt='faster-whisper' if audio == 'windows' else 'whisper.cpp',
                                   tts=config.backend))


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
