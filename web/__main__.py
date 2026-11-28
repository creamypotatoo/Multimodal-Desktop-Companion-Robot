"""Run the dashboard: ``python -m web`` (localhost:5000, mock robot).

Uses Flask's built-in threaded server with debug mode off, which is adequate
for a single operator on a home LAN. Configuration: see ``web.config``.
"""
import argparse
import os
import sys

from controller import RobotController
from controller.esp32 import ESP32MotorAdapter
from web import create_app
from web.config import ConfigError, WebConfig


def main(argv=None, *, shutdown_timeout=0):
    parser = argparse.ArgumentParser(description='Eli web dashboard')
    parser.add_argument('--host', help='bind address (overrides ELI_WEB_HOST)')
    parser.add_argument('--port', type=int, help='port (overrides ELI_WEB_PORT)')
    parser.add_argument('--unified', action='store_true', help='enable the unified conversation runtime')
    parser.add_argument('--local-io', action='store_true', help='use local audio, AI and configured eyes')
    parser.add_argument('--local-camera', action='store_true', help='enable explicit camera controls in Perception')
    parser.add_argument('--face-threshold', type=float, default=50.0, help='maximum LBPH distance (exclusive)')
    parser.add_argument('--face-model', default='data/trained_model.yml')
    parser.add_argument('--face-labels', default='data/labels.txt')
    parser.add_argument('--expression-model', help='optional local FER+ ONNX model')
    parser.add_argument('--audio', choices=('mock', 'windows', 'pi'), help='select audio independently')
    parser.add_argument('--ai', choices=('mock', 'ollama'), help='select the conversation responder')
    parser.add_argument('--motors', choices=('mock', 'esp32'), default='mock')
    parser.add_argument('--serial-port', default=os.environ.get('ELI_SERIAL_PORT'))
    parser.add_argument('--serial-baud', type=int, default=os.environ.get('ELI_SERIAL_BAUD', '115200'))
    parser.add_argument('--wake', action='store_true', help='enable wake phrase listening at startup')
    parser.add_argument('--wake-phrase', default='Hey Eli')
    parser.add_argument('--wake-in-sleep', action='store_true')
    parser.add_argument('--wake-cooldown', type=float, default=3)
    parser.add_argument('--follow-up', type=float, default=20, help='inactivity window in seconds')
    parser.add_argument('--announce', action='store_true', help='enable spoken productivity announcements')
    parser.add_argument('--announce-in-sleep', action='store_true', help='also announce in Sleep')
    args = parser.parse_args(argv)
    if (args.local_io or args.announce or args.announce_in_sleep or args.audio or args.ai
            or args.wake or args.wake_in_sleep) and not args.unified:
        parser.error('Runtime options require --unified')
    if args.local_camera and not args.unified:
        parser.error('--local-camera requires --unified')
    if args.announce_in_sleep and not args.announce:
        parser.error('--announce-in-sleep requires --announce')
    overrides = {k: v for k, v in (('host', args.host), ('port', args.port)) if v is not None}
    try:
        if 'port' in overrides and not 1 <= overrides['port'] <= 65535:
            raise ConfigError('--port must be between 1 and 65535')
        config = WebConfig.from_env(**overrides)
        serial_motors = ESP32MotorAdapter(args.serial_port, args.serial_baud)
    except (ConfigError, ValueError) as exc:
        print(f'[Web] Configuration error: {exc}', file=sys.stderr, flush=True)
        return 2
    runtime = None
    if args.unified:
        from runtime import UnifiedRuntime
        from runtime.components import configured_components
        from controller.errors import ValidationError
        try:
            audio_backend = args.audio or ('pi' if args.local_io else 'mock')
            ai_backend = args.ai or ('ollama' if args.local_io else 'mock')
            components = configured_components(audio_backend, ai_backend,
                                               configured_eyes=args.local_io, camera=args.local_camera)
            if components.camera is not None:
                from functools import partial
                from math import isfinite
                from intelligence.recognize import load_recognizer
                if not isfinite(args.face_threshold) or args.face_threshold <= 0:
                    raise ValueError('Face threshold must be positive and finite')
                components.camera.recognizer = partial(load_recognizer, args.face_model,
                                                       args.face_labels, args.face_threshold)
                if args.expression_model:
                    from intelligence.emotion import load_classifier
                    components.camera.classifier = partial(load_classifier, args.expression_model)
            runtime = UnifiedRuntime(components, announce=args.announce,
                                     announce_in_sleep=args.announce_in_sleep,
                                     follow_up=args.follow_up, wake_cooldown=args.wake_cooldown,
                                     serial_motors=serial_motors, motor_backend=args.motors)
            runtime.configure_wake(dict(enabled=args.wake, phrase=args.wake_phrase,
                                        in_sleep=args.wake_in_sleep))
        except (ImportError, OSError, ValueError, ValidationError) as exc:
            print(f'[Runtime] Configuration error: {exc}', file=sys.stderr, flush=True)
            return 2
    app = create_app(config, controller=runtime.controller if runtime else
                     RobotController(serial_motors=serial_motors, motor_backend=args.motors))
    if runtime is not None:
        app.extensions['eli_runtime'] = runtime
    print(f'[Web] Eli dashboard ({args.motors} motors): '
          f'http://{"localhost" if config.is_local else config.host}:{config.port}/', flush=True)
    if config.generated_password:
        print(f'[Web] One-time password for this run: {config.generated_password}', flush=True)
        print('[Web] Set ELI_WEB_PASSWORD_HASH (python -m web.hash_password) to choose your own.',
              flush=True)
    if config.secret_is_ephemeral:
        print('[Web] ELI_WEB_SECRET_KEY not set: sessions end when the server restarts.', flush=True)
    if not config.is_local:
        print('[Web] LAN access enabled; the dashboard is reachable by other devices.', flush=True)
    controller = runtime or app.extensions['eli_controller']
    try:
        controller.startup()
        app.run(host=config.host, port=config.port, debug=False, threaded=True, use_reloader=False)
    finally:
        controller.shutdown()
        if runtime is not None and shutdown_timeout and not runtime.wait_for_shutdown(shutdown_timeout):
            print('[Runtime] Shutdown timed out waiting for a device or service call', file=sys.stderr, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
