"""Configuration for Eli's web dashboard.

Plain data with validation, read from environment variables in the same style
as ``display.config`` and ``audio.text_to_speech``. Nothing here touches
hardware.

  ELI_WEB_HOST           bind address                    (default: 127.0.0.1)
  ELI_WEB_PORT           TCP port, 1-65535               (default: 5000)
  ELI_WEB_SECRET_KEY     session signing key, >= 32 chars (default: random
                         per process, so logins end on restart)
  ELI_WEB_PASSWORD_HASH  Werkzeug password hash; create with
                         ``python -m web.hash_password``
  ELI_WEB_PASSWORD       plain-text password (local development only;
                         ignored when ELI_WEB_PASSWORD_HASH is set)
  ELI_WEB_COOKIE_SECURE  1 to send the session cookie over HTTPS only
  ELI_WEB_SESSION_HOURS  session lifetime in hours, 1-168 (default: 12)
  ELI_WEB_BACKEND        robot service backend; only ``mock`` in Phase 1

Without a password a random one-time password is generated and printed at
startup. Binding to anything other than localhost requires
ELI_WEB_PASSWORD_HASH and ELI_WEB_SECRET_KEY, so the dashboard is never
reachable on the LAN with a throwaway or plain-text credential.
"""
import os
import secrets
from dataclasses import dataclass, field

from werkzeug.security import generate_password_hash

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')
MIN_SECRET_LENGTH = 32
BACKENDS = ('mock',)
TRUE = ('1', 'true', 'yes', 'on')


class ConfigError(ValueError):
    """Invalid or unsafe dashboard configuration."""


def _int(environ, name, default, low, high):
    raw = environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f'{name} must be an integer, got {raw!r}') from None
    if not low <= value <= high:
        raise ConfigError(f'{name} must be between {low} and {high}, got {value}')
    return value


@dataclass(frozen=True)
class WebConfig:
    host: str = '127.0.0.1'
    port: int = 5000
    secret_key: str = field(default='', repr=False)
    password_hash: str = field(default='', repr=False)
    cookie_secure: bool = False
    session_hours: int = 12
    backend: str = 'mock'
    # Set when no password was configured: shown once on the console.
    generated_password: str = field(default='', repr=False)
    secret_is_ephemeral: bool = False

    @property
    def is_local(self):
        return self.host in LOCAL_HOSTS

    @classmethod
    def from_env(cls, environ=None, **overrides):
        env = os.environ if environ is None else environ
        values = dict(
            host=env.get('ELI_WEB_HOST', '').strip() or '127.0.0.1',
            port=_int(env, 'ELI_WEB_PORT', 5000, 1, 65535),
            cookie_secure=env.get('ELI_WEB_COOKIE_SECURE', '').strip().lower() in TRUE,
            session_hours=_int(env, 'ELI_WEB_SESSION_HOURS', 12, 1, 168),
            backend=env.get('ELI_WEB_BACKEND', '').strip().lower() or 'mock',
        )
        values.update(overrides)
        if values['backend'] not in BACKENDS:
            raise ConfigError(f"ELI_WEB_BACKEND={values['backend']!r} is not available "
                              f"in Phase 1; use: {', '.join(BACKENDS)}")
        local = values['host'] in LOCAL_HOSTS

        secret = values.pop('secret_key', None) or env.get('ELI_WEB_SECRET_KEY', '').strip()
        ephemeral = False
        if secret and len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f'ELI_WEB_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters')
        if not secret:
            if not local:
                raise ConfigError('ELI_WEB_SECRET_KEY is required when ELI_WEB_HOST is not localhost')
            secret, ephemeral = secrets.token_urlsafe(48), True

        pw_hash = values.pop('password_hash', None) or env.get('ELI_WEB_PASSWORD_HASH', '').strip()
        generated = ''
        if not pw_hash:
            if not local:
                raise ConfigError('ELI_WEB_PASSWORD_HASH is required when ELI_WEB_HOST is not '
                                  'localhost (create one with: python -m web.hash_password)')
            plain = env.get('ELI_WEB_PASSWORD', '')
            if not plain:
                plain = generated = secrets.token_urlsafe(12)
            pw_hash = generate_password_hash(plain)
        return cls(secret_key=secret, password_hash=pw_hash, generated_password=generated,
                   secret_is_ephemeral=ephemeral, **values)
