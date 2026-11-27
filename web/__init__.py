"""Eli's web control dashboard (Phase 1: mock mode).

``create_app()`` builds the Flask application. It never imports or opens
hardware: the robot is a ``web.services.RobotService`` (``MockRobotService`` in
Phase 1). Run it with ``python -m web``; see WEB_DASHBOARD.md.
"""
from datetime import timedelta

from flask import Flask, request

from web import api, auth, pages
from web.config import WebConfig
from web.responses import error
from web.services import create_service

MAX_REQUEST_BYTES = 16 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def create_app(config=None, *, service=None, testing=False):
    config = config or WebConfig.from_env()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.secret_key,
        ELI_PASSWORD_HASH=config.password_hash,
        ELI_WEB_CONFIG=config,
        TESTING=testing,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        SESSION_COOKIE_NAME='eli_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=config.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.session_hours),
    )
    app.json.sort_keys = False
    app.extensions['eli_robot'] = service or create_service(config.backend)
    app.extensions['eli_login_throttle'] = auth.LoginThrottle()

    @app.before_request
    def _api_auth_first():
        # Unauthenticated API calls get 401 before any CSRF check (403).
        if request.path.startswith('/api/') and not auth.is_authenticated():
            return error('unauthorized', 'Login required', 401)
        return None

    app.before_request(auth.check_csrf)

    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api.bp)

    @app.context_processor
    def _template_globals():
        return {'csrf_token': auth.csrf_token, 'nav_pages': pages.PAGES,
                'mobile_nav': pages.MOBILE_NAV, 'backend': config.backend}

    @app.after_request
    def _security_headers(response):
        headers = response.headers
        headers.setdefault('Content-Security-Policy', CSP)
        headers['X-Content-Type-Options'] = 'nosniff'
        headers['X-Frame-Options'] = 'DENY'
        headers['Referrer-Policy'] = 'no-referrer'
        headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if not request.path.startswith('/static/'):
            headers['Cache-Control'] = 'no-store'
        return response

    def _api_or_page(code, message, status):
        def handler(exc):
            if request.path.startswith('/api/'):
                return error(code, message, status)
            return exc
        return handler

    app.register_error_handler(400, _api_or_page('bad_request', 'Malformed request', 400))
    app.register_error_handler(404, _api_or_page('not_found', 'No such endpoint', 404))
    app.register_error_handler(405, _api_or_page('method_not_allowed',
                                                 'Method not allowed for this endpoint', 405))
    app.register_error_handler(413, _api_or_page('payload_too_large', 'Request body too large',
                                                 413))
    app.register_error_handler(500, _api_or_page('internal_error', 'Internal server error', 500))
    return app
