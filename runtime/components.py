"""Mock and opt-in local adapters for the existing conversation and eye engines."""
from dataclasses import replace
from pathlib import Path
from shutil import which
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
    from audio.speech_to_text import find_microphone, transcribe_cancellable, whisper_paths, whisper_problem
    from audio.text_to_speech import PI_TOOLS, TTSConfig, resolve_backend
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
        components = replace(components, eyes=eyes, optional_eyes=True)
    if camera:
        try:
            from perception.service import CameraService
        except (ImportError, OSError):
            components = replace(components, camera_problem='OpenCV unavailable. Install the Pi camera dependencies')
        else:
            service = CameraService(preview_fps=int(os.environ.get('ELI_CAMERA_PREVIEW_FPS', '8')),
                                    analysis_fps=int(os.environ.get('ELI_CAMERA_ANALYSIS_FPS', '8')))
            if any(os.environ.get(f'ELI_CAMERA_{field}') for field in ('WIDTH', 'HEIGHT', 'FPS')):
                width = int(os.environ.get('ELI_CAMERA_WIDTH', '640'))
                height = int(os.environ.get('ELI_CAMERA_HEIGHT', '480'))
                fps = int(os.environ.get('ELI_CAMERA_FPS', '15'))
                if not (160 <= width <= 1920 and 120 <= height <= 1080 and 1 <= fps <= 30):
                    raise ValueError('Camera width, height or FPS outside the Perception controls range')
                service.config = replace(service.config, width=width, height=height, fps=fps)
            components = replace(components, camera=service)
    if audio == 'mock':
        return components
    config = TTSConfig.from_env(backend='pyttsx3' if audio == 'windows' else None)
    if audio == 'pi' and config.backend == 'auto':
        config = replace(config, backend='espeak')
    config = replace(config, backend=resolve_backend(config))
    missing_tools = [tool for tool in PI_TOOLS if not which(tool)] if config.backend == 'espeak' else []
    output_problem = 'Install speech tools: ' + ', '.join(missing_tools) if missing_tools else None
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
    input_problem = None
    try:
        microphone = Microphone(device=device, rate=rate, pcm=audio == 'windows',
                                max_gain=float(os.environ.get('ELI_AUDIO_GAIN_MAX', '1')))
    except (ImportError, OSError):
        if audio != 'pi':
            raise
        microphone = None
        input_problem = 'PortAudio microphone unavailable. Install libportaudio2 and sounddevice'
    try:
        inputs = microphone.devices() if microphone else []
    except Exception:
        inputs = []
    try:
        default_input = microphone.driver.query_devices(kind='input')['name'] if microphone else None
    except Exception:
        default_input = None
    stt = (OfflineWhisper(os.environ.get('ELI_WHISPER_MODEL', ''),
                          prompt=os.environ.get('ELI_WHISPER_PROMPT', 'Eli is a personal robot assistant.'),
                          beam_size=int(os.environ.get('ELI_WHISPER_BEAM_SIZE', '5')))
           if audio == 'windows' else transcribe_cancellable)
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
                   input_problem=input_problem, output_problem=output_problem,
                   stt_problem=whisper_problem(*whisper_paths()) if audio == 'pi' else None,
                   cancellable_stt=audio == 'pi',
                   audio_info=dict(inputs=inputs, default_input=default_input, output=output, sample_rate=rate,
                                   stt='faster-whisper' if audio == 'windows' else 'whisper.cpp',
                                   tts=config.backend))
