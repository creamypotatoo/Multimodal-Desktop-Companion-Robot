"""Session authentication and CSRF protection for the dashboard.

- A single operator password, checked against a Werkzeug hash with a
  constant-time comparison. The session is cleared and re-issued at login.
- Signed, HttpOnly, SameSite=Strict session cookies (see ``web.create_app``).
- Every state-changing request (POST/PUT/PATCH/DELETE) must carry the session's
  CSRF token in the ``X-CSRF-Token`` header (API) or ``csrf_token`` form field.
- Failed logins are throttled per client address.
"""
import functools
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import (Blueprint, current_app, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

from web.responses import error

bp = Blueprint('auth', __name__)

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')
CSRF_HEADER = 'X-CSRF-Token'
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
MAX_PASSWORD_LENGTH = 256


class LoginThrottle:
    """At most ``MAX_FAILURES`` failed logins per address per ``LOCKOUT_SECONDS``."""

    def __init__(self, limit=MAX_FAILURES, window=LOCKOUT_SECONDS, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures = {}
        self._lock = threading.Lock()

    def _recent(self, key):
        cutoff = self.clock() - self.window
        recent = [t for t in self._failures.get(key, ()) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def fail(self, key):
        with self._lock:
            self._failures.setdefault(key, []).append(self.clock())

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)


def csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = session['csrf_token'] = secrets.token_urlsafe(32)
    return token


def is_authenticated():
    return session.get('authenticated') is True


def _wants_json():
    return request.path.startswith('/api/')


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            if _wants_json():
                return error('unauthorized', 'Login required', 401)
            return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))
        return view(*args, **kwargs)
    return wrapper


def check_csrf():
    """before_request hook: reject state-changing requests without a valid token."""
    if request.method in SAFE_METHODS:
        return None
    expected = session.get('csrf_token')
    supplied = request.headers.get(CSRF_HEADER) or request.form.get('csrf_token', '')
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        if _wants_json():
            return error('csrf_failed', 'Missing or invalid CSRF token', 403)
        return render_template('login.html', error='Your session expired. Please sign in again.',
                               next_url=''), 403
    return None


def _safe_next(target):
    """Only allow redirects to local paths (no open redirects)."""
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return url_for('pages.home')
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return url_for('pages.home')
    return target


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if is_authenticated():
            return redirect(url_for('pages.home'))
        csrf_token()
        return render_template('login.html', next_url=request.args.get('next', ''))

    throttle = current_app.extensions['eli_login_throttle']
    client = request.remote_addr or 'unknown'
    next_url = request.form.get('next', '')
    if throttle.blocked(client):
        return render_template('login.html', next_url=next_url,
                               error='Too many attempts. Try again in a few minutes.'), 429
    password = request.form.get('password', '')
    valid = (0 < len(password) <= MAX_PASSWORD_LENGTH and
             check_password_hash(current_app.config['ELI_PASSWORD_HASH'], password))
    if not valid:
        throttle.fail(client)
        return render_template('login.html', next_url=next_url,
                               error='Incorrect password.'), 401
    throttle.reset(client)
    session.clear()  # prevent session fixation
    session.permanent = True
    session['authenticated'] = True
    csrf_token()
    return redirect(_safe_next(next_url))


@bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been signed out.')
    return redirect(url_for('auth.login'))
