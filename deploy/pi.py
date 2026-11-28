"""Raspberry Pi deployment launcher and hardware-safe preflight."""
import argparse
from importlib import import_module, util
import os
from pathlib import Path
import re
from shutil import which
import signal
import sys

PROFILES = ('mock', 'audio', 'camera', 'serial', 'run')
TRUE = ('1', 'yes', 'true', 'on')
FALSE = ('0', 'no', 'false', 'off')


def read_env(path):
    """Read literal KEY=value pairs. Never evaluate shell syntax or expand dollars."""
    values = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'ELI_[A-Z0-9_]+', name) or name in values:
            raise ValueError(f'Invalid or duplicate ELI setting on line {number} of {path.name}')
        values[name] = value.strip()
    return values


def enabled(env, name, default):
    value = env.get(name, default).strip().lower()
    if value not in TRUE + FALSE:
        raise ValueError(f'{name} must be 0 or 1')
    return value in TRUE


def launch_args(profile, env):
    if profile not in PROFILES:
        raise ValueError('Unknown Pi launch profile')
    mock = profile == 'mock'
    audio = 'mock' if mock else env.get('ELI_PI_AUDIO', 'pi')
    ai = 'mock' if mock else env.get('ELI_PI_AI', 'ollama')
    motors = env.get('ELI_PI_MOTORS', 'mock') if profile == 'run' else 'mock'
    if profile == 'serial':
        motors = 'esp32'
    if audio not in ('mock', 'pi') or ai not in ('mock', 'ollama') or motors not in ('mock', 'esp32'):
        raise ValueError('Invalid Pi audio, AI or motor backend')
    camera = profile in ('camera', 'serial') or profile == 'run' and enabled(env, 'ELI_PI_CAMERA', '1')
    https = enabled(env, 'ELI_PI_HTTPS', '0')
    if https:
        if not env.get('ELI_WEB_SECRET_KEY') or not env.get('ELI_WEB_PASSWORD_HASH'):
            raise ValueError('HTTPS access requires ELI_WEB_SECRET_KEY and ELI_WEB_PASSWORD_HASH')
        env['ELI_WEB_COOKIE_SECURE'] = '1'
    if mock:
        env['ELI_DISPLAY_BACKEND'] = 'mock'
    else:
        env.setdefault('ELI_DISPLAY_BACKEND', 'tft')
        if env['ELI_DISPLAY_BACKEND'].strip().lower() not in ('tft', 'mock', 'auto', 'off', 'none', 'disabled', ''):
            raise ValueError('ELI_DISPLAY_BACKEND must select tft, mock, auto or off')
    args = ['--unified', '--local-io', '--host', '127.0.0.1', '--audio', audio, '--ai', ai, '--motors', motors]
    if audio == 'pi' and enabled(env, 'ELI_PI_WAKE', '1'):
        args += ['--wake', '--wake-phrase', env.get('ELI_PI_WAKE_PHRASE', 'Hey Eli')]
    if audio == 'pi' and enabled(env, 'ELI_PI_ANNOUNCE', '1'):
        args.append('--announce')
    if camera:
        args.append('--local-camera')
        for setting, option in (('FACE_MODEL', '--face-model'), ('FACE_LABELS', '--face-labels'),
                                ('FACE_THRESHOLD', '--face-threshold'), ('EXPRESSION_MODEL', '--expression-model')):
            if env.get(f'ELI_{setting}'):
                args.extend([option, env[f'ELI_{setting}']])
    return args


def preflight(args, env):
    """Check dependencies, paths and permissions without opening any devices."""
    problems = []
    for name in ('flask', 'PIL', 'numpy'):
        try:
            import_module(name)
        except (ImportError, OSError):
            problems.append(('ERROR', f'Cannot import {name} in the selected venv'))
    if env['ELI_DISPLAY_BACKEND'].strip().lower() in ('tft', 'auto'):
        if any(util.find_spec(module) is None for module in ('board', 'digitalio', 'adafruit_rgb_display')):
            problems.append(('WARN', 'TFT dependencies missing. Install requirements-display.txt'))
        _check_devices(problems, Path('/dev').glob('spidev*'), 'SPI')
        _check_devices(problems, Path('/dev').glob('gpiochip*'), 'GPIO')
    if args[args.index('--audio') + 1] == 'pi':
        if not any(level == 'ERROR' for level, _ in problems):
            from audio.speech_to_text import whisper_paths, whisper_problem
            problem = whisper_problem(*whisper_paths(env))
            if problem:
                problems.append(('WARN', problem))
        try:
            import_module('sounddevice')
        except (ImportError, OSError):
            problems.append(('WARN', 'Install libportaudio2 and sounddevice for microphone capture'))
        missing = [name for name in ('espeak-ng', 'ffmpeg', 'aplay') if not which(name)]
        if missing:
            problems.append(('WARN', 'Speech tools missing: ' + ', '.join(missing)))
        _check_devices(problems, Path('/dev/snd').glob('pcm*'), 'ALSA')
    if '--local-camera' in args:
        try:
            cv2 = import_module('cv2')
        except (ImportError, OSError):
            problems.append(('WARN', 'OpenCV unavailable. Camera controls will be disabled'))
        else:
            if not hasattr(cv2, 'face'):
                problems.append(('WARN', 'OpenCV contrib LBPH unavailable. Detection remains available'))
        _check_devices(problems, Path('/dev').glob('video*'), 'Camera')
    if args[args.index('--motors') + 1] == 'esp32':
        from controller.esp32 import validate_port
        port = env.get('ELI_SERIAL_PORT')
        if port and (not port.startswith('/dev/') or not validate_port(port)):
            problems.append(('ERROR', 'ELI_SERIAL_PORT must be a Linux serial device path'))
        devices = [Path(port)] if port else [
            path for pattern in ('serial/by-id/*', 'ttyUSB*', 'ttyACM*') for path in Path('/dev').glob(pattern)]
        _check_devices(problems, devices, 'ESP32 serial')
        if util.find_spec('serial') is None:
            problems.append(('WARN', 'Install requirements-esp32.txt for the ESP32 link'))
    if args[args.index('--ai') + 1] == 'ollama' and not env.get('ELI_OLLAMA_URL'):
        problems.append(('WARN', 'Ollama URL unset. Conversation uses the built-in fallback'))
    return problems


def _check_devices(problems, paths, label):
    if not any(os.access(path, os.R_OK | os.W_OK) for path in paths):
        problems.append(('WARN', f'{label} device unavailable or access denied. Check hardware and groups'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', nargs='?', choices=PROFILES, default='run')
    parser.add_argument('--check', action='store_true', help='only run preflight, without starting Eli')
    parser.add_argument('--env-file', type=Path)
    options = parser.parse_args(argv)
    if sys.version_info < (3, 11) or sys.platform != 'linux':
        print('Eli Pi launcher requires Linux and Python 3.11 or newer', file=sys.stderr)
        return 2
    try:
        path = options.env_file or Path.home() / '.config/eli/pi.env'
        values = read_env(path) if options.env_file or path.exists() else {}
        for key, value in values.items():
            os.environ.setdefault(key, value)
        args = launch_args(options.profile, os.environ)
        checks = preflight(args, os.environ)
    except (OSError, ValueError) as exc:
        print(f'[Pi] Configuration error: {exc}', file=sys.stderr)
        return 2
    for level, message in checks:
        print(f'[Pi] {level}: {message}', file=sys.stderr)
    if any(level == 'ERROR' for level, _ in checks):
        return 2
    from web.config import ConfigError, WebConfig

    try:
        WebConfig.from_env(host='127.0.0.1')
    except ConfigError as exc:
        print(f'[Pi] Dashboard configuration error: {exc}', file=sys.stderr)
        return 2
    if options.check:
        print(f'[Pi] Preflight complete for {options.profile}. No devices opened.')
        return 0

    from web.__main__ import main as web_main

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        return web_main(args, shutdown_timeout=8)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
