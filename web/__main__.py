"""Run the dashboard: ``python -m web`` (localhost:5000, mock robot).

Uses Flask's built-in threaded server with debug mode off, which is adequate
for a single operator on a home LAN. Configuration: see ``web.config``.
"""
import argparse
import sys

from web import create_app
from web.config import ConfigError, WebConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description='Eli web dashboard (mock robot mode)')
    parser.add_argument('--host', help='bind address (overrides ELI_WEB_HOST)')
    parser.add_argument('--port', type=int, help='port (overrides ELI_WEB_PORT)')
    args = parser.parse_args(argv)
    overrides = {k: v for k, v in (('host', args.host), ('port', args.port)) if v is not None}
    try:
        if 'port' in overrides and not 1 <= overrides['port'] <= 65535:
            raise ConfigError('--port must be between 1 and 65535')
        config = WebConfig.from_env(**overrides)
    except ConfigError as exc:
        print(f'[Web] Configuration error: {exc}', file=sys.stderr, flush=True)
        return 2
    app = create_app(config)
    print(f'[Web] Eli dashboard ({config.backend} robot): '
          f'http://{"localhost" if config.is_local else config.host}:{config.port}/', flush=True)
    if config.generated_password:
        print(f'[Web] One-time password for this run: {config.generated_password}', flush=True)
        print('[Web] Set ELI_WEB_PASSWORD_HASH (python -m web.hash_password) to choose your own.',
              flush=True)
    if config.secret_is_ephemeral:
        print('[Web] ELI_WEB_SECRET_KEY not set: sessions end when the server restarts.', flush=True)
    if not config.is_local:
        print('[Web] LAN access enabled; the dashboard is reachable by other devices.', flush=True)
    controller = app.extensions['eli_controller']
    try:
        controller.startup()
        app.run(host=config.host, port=config.port, debug=False, threaded=True, use_reloader=False)
    finally:
        controller.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
