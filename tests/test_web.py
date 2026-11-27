"""Web dashboard (Phase 1): app factory, auth, CSRF, API contract, mock robot.

Runs on Windows and the Pi without hardware. Skipped if Flask is not installed
(``python -m pip install -r requirements-web.txt``).
"""
import subprocess
import sys
import unittest
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ImportError:  # pragma: no cover - optional dependency
    flask = None

if flask:
    from werkzeug.security import generate_password_hash

    from web import create_app
    from web.auth import LoginThrottle
    from web.config import ConfigError, WebConfig
    from web.services import MockRobotService, RobotService, create_service
    from web.services.base import ConflictError, ValidationError
    from web.services.host_metrics import HostMetrics

PASSWORD = 'correct horse battery staple'
SECRET = 'x' * 40


def make_config(**overrides):
    values = dict(secret_key=SECRET, password_hash=generate_password_hash(PASSWORD))
    values.update(overrides)
    return WebConfig.from_env({}, **values)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@unittest.skipIf(flask is None, 'Flask not installed')
class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = MockRobotService(clock=self.clock, wall_clock=lambda: 1.7e9)
        self.app = create_app(make_config(), service=self.service, testing=True)
        self.client = self.app.test_client()

    def csrf(self):
        with self.client.session_transaction() as sess:
            if 'csrf_token' not in sess:
                sess['csrf_token'] = 'test-token'
            return sess['csrf_token']

    def login(self, password=PASSWORD):
        self.client.get('/login')
        return self.client.post('/login', data={'password': password, 'csrf_token': self.csrf()})

    def call(self, method, path, json=None, csrf=True, **kwargs):
        headers = kwargs.pop('headers', {})
        if csrf:
            headers['X-CSRF-Token'] = self.csrf()
        return self.client.open(path, method=method, json=json, headers=headers, **kwargs)

    def assert_ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        body = response.get_json()
        self.assertIs(body['ok'], True)
        self.assertIn('data', body)
        return body['data']

    def assert_error(self, response, status, code=None, field=None):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        body = response.get_json()
        self.assertIs(body['ok'], False)
        self.assertEqual(set(body), {'ok', 'error'})
        self.assertTrue(body['error']['message'])
        if code:
            self.assertEqual(body['error']['code'], code)
        if field:
            self.assertEqual(body['error']['field'], field)
        return body['error']


class AppFactoryTests(WebTestCase):
    def test_app_initialises_with_mock_service_and_blueprints(self):
        self.assertIsInstance(self.app.extensions['eli_robot'], RobotService)
        self.assertEqual(set(self.app.blueprints), {'auth', 'pages', 'api'})
        self.assertTrue(self.app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(self.app.config['SESSION_COOKIE_SAMESITE'], 'Strict')

    def test_default_service_is_mock(self):
        app = create_app(make_config())
        self.assertIsInstance(app.extensions['eli_robot'], MockRobotService)
        with self.assertRaises(ValueError):
            create_service('real')

    def test_security_headers(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('camera=()', response.headers['Permissions-Policy'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_all_pages_render_after_login(self):
        self.login()
        for path in ('/', '/companion', '/movement', '/productivity', '/perception',
                     '/diagnostics', '/settings'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            self.assertIn('name="csrf-token"', html)
            self.assertIn('Mock mode', html)

    def test_static_assets_are_local(self):
        for path in ('/static/css/dashboard.css', '/static/js/api.js', '/static/js/eyes.js',
                     '/static/js/pages/home.js', '/static/img/favicon.svg'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()
        self.login()
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('http://', html.replace('http://www.w3.org', ''))
        self.assertNotIn('https://', html)

    def test_unknown_api_route_and_method_are_json(self):
        self.login()
        self.assert_error(self.client.get('/api/v1/nope'), 404, 'not_found')
        self.assert_error(self.call('DELETE', '/api/v1/status'), 405, 'method_not_allowed')


class ConfigTests(unittest.TestCase):
    @unittest.skipIf(flask is None, 'Flask not installed')
    def test_defaults_bind_localhost_with_generated_password(self):
        config = WebConfig.from_env({})
        self.assertEqual((config.host, config.port), ('127.0.0.1', 5000))
        self.assertTrue(config.is_local)
        self.assertTrue(config.generated_password)
        self.assertTrue(config.secret_is_ephemeral)
        self.assertNotIn(config.generated_password, repr(config))
        self.assertNotIn(config.secret_key, repr(config))

    @unittest.skipIf(flask is None, 'Flask not installed')
    def test_lan_binding_requires_hash_and_secret(self):
        with self.assertRaisesRegex(ConfigError, 'SECRET_KEY'):
            WebConfig.from_env({'ELI_WEB_HOST': '0.0.0.0', 'ELI_WEB_PASSWORD': 'pw'})
        with self.assertRaisesRegex(ConfigError, 'PASSWORD_HASH'):
            WebConfig.from_env({'ELI_WEB_HOST': '0.0.0.0', 'ELI_WEB_SECRET_KEY': SECRET,
                                'ELI_WEB_PASSWORD': 'plain text is not enough'})
        config = WebConfig.from_env({'ELI_WEB_HOST': '0.0.0.0', 'ELI_WEB_SECRET_KEY': SECRET,
                                     'ELI_WEB_PASSWORD_HASH': generate_password_hash('pw')})
        self.assertFalse(config.is_local)
        self.assertFalse(config.generated_password)

    @unittest.skipIf(flask is None, 'Flask not installed')
    def test_invalid_values_rejected(self):
        for env in ({'ELI_WEB_PORT': 'abc'}, {'ELI_WEB_PORT': '70000'},
                    {'ELI_WEB_SECRET_KEY': 'short'}, {'ELI_WEB_BACKEND': 'real'},
                    {'ELI_WEB_SESSION_HOURS': '0'}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                WebConfig.from_env(env)
        self.assertTrue(WebConfig.from_env({'ELI_WEB_COOKIE_SECURE': '1'}).cookie_secure)

    @unittest.skipIf(flask is None, 'Flask not installed')
    def test_cli_reports_config_errors_without_starting(self):
        from web.__main__ import main
        with patch.dict('os.environ', {'ELI_WEB_HOST': '0.0.0.0'}, clear=False), \
                patch('web.__main__.create_app') as factory, \
                patch('sys.stderr'):
            self.assertEqual(main([]), 2)
            self.assertEqual(main(['--host', '127.0.0.1', '--port', '0']), 2)
        factory.assert_not_called()


class AuthTests(WebTestCase):
    def test_pages_redirect_to_login(self):
        response = self.client.get('/movement')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=/movement', response.location)

    def test_api_requires_login(self):
        for method, path in (('GET', '/api/v1/status'), ('PUT', '/api/v1/mode'),
                             ('POST', '/api/v1/movement/stop'), ('GET', '/api/v1/diagnostics')):
            with self.subTest(path=path):
                self.assert_error(self.call(method, path, json={'mode': 'sleep'}), 401,
                                  'unauthorized')
        self.assertEqual(self.service.get_mode()['mode'], 'companion')

    def test_login_success_rotates_session_and_redirects(self):
        self.client.get('/login')
        with self.client.session_transaction() as sess:
            sess['planted'] = 'attacker'
            token = sess['csrf_token']
        response = self.client.post('/login', data={'password': PASSWORD, 'csrf_token': token,
                                                    'next': '/settings'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/settings')
        with self.client.session_transaction() as sess:
            self.assertTrue(sess['authenticated'])
            self.assertNotIn('planted', sess)
            self.assertNotEqual(sess['csrf_token'], token)
        self.assert_ok(self.client.get('/api/v1/status'))

    def test_wrong_password_rejected(self):
        response = self.login('wrong')
        self.assertEqual(response.status_code, 401)
        self.assert_error(self.client.get('/api/v1/status'), 401)

    def test_open_redirects_blocked(self):
        for target in ('https://evil.example', '//evil.example', '/\\evil', 'javascript:x'):
            self.client.get('/login')
            response = self.client.post('/login', data={'password': PASSWORD, 'next': target,
                                                        'csrf_token': self.csrf()})
            self.assertEqual(response.location, '/', target)
            self.client.post('/logout', data={'csrf_token': self.csrf()})

    def test_login_requires_csrf(self):
        self.client.get('/login')
        response = self.client.post('/login', data={'password': PASSWORD})
        self.assertEqual(response.status_code, 403)
        self.assert_error(self.client.get('/api/v1/status'), 401)

    def test_login_throttled_after_repeated_failures(self):
        for _ in range(5):
            self.assertEqual(self.login('nope').status_code, 401)
        self.assertEqual(self.login().status_code, 429)  # even the right password
        self.assert_error(self.client.get('/api/v1/status'), 401)

    def test_throttle_window_expires(self):
        clock = FakeClock()
        throttle = LoginThrottle(limit=2, window=60, clock=clock)
        throttle.fail('a'); throttle.fail('a')
        self.assertTrue(throttle.blocked('a'))
        self.assertFalse(throttle.blocked('b'))
        clock.t += 61
        self.assertFalse(throttle.blocked('a'))

    def test_logout_requires_post_and_csrf_then_clears_session(self):
        self.login()
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.client.post('/logout').status_code, 403)
        self.assert_ok(self.client.get('/api/v1/status'))
        self.assertEqual(self.client.post('/logout', data={'csrf_token': self.csrf()}).status_code,
                         302)
        self.assert_error(self.client.get('/api/v1/status'), 401)

    def test_state_changes_require_csrf(self):
        self.login()
        self.assert_error(self.call('PUT', '/api/v1/mode', json={'mode': 'sleep'}, csrf=False),
                          403, 'csrf_failed')
        self.assert_error(self.call('PUT', '/api/v1/mode', json={'mode': 'sleep'}, csrf=False,
                                    headers={'X-CSRF-Token': 'forged'}), 403, 'csrf_failed')
        self.assertEqual(self.service.get_mode()['mode'], 'companion')
        self.assert_ok(self.call('PUT', '/api/v1/mode', json={'mode': 'sleep'}))


class ApiFormatTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_get_endpoints_return_envelope(self):
        for path in ('status', 'mode', 'expression', 'volume', 'settings', 'movement',
                     'companion', 'perception', 'diagnostics'):
            with self.subTest(path=path):
                response = self.client.get(f'/api/v1/{path}')
                self.assertEqual(response.mimetype, 'application/json')
                self.assertIsInstance(self.assert_ok(response), dict)

    def test_status_shape(self):
        data = self.assert_ok(self.client.get('/api/v1/status'))
        for key in ('name', 'backend', 'mode', 'state', 'expression', 'volume',
                    'conversation_active', 'movement', 'uptime_s', 'capabilities', 'history'):
            self.assertIn(key, data)
        self.assertEqual(data['backend'], 'mock')
        self.assertEqual(data['state'], 'IDLE')

    def test_unsupported_hardware_is_never_reported_live(self):
        caps = self.assert_ok(self.client.get('/api/v1/status'))['capabilities']
        self.assertTrue(all(v in ('simulated', 'unavailable') for v in caps.values()))
        for key in ('camera', 'microphone', 'face_recognition', 'ai', 'esp32'):
            self.assertEqual(caps[key], 'unavailable')
        perception = self.assert_ok(self.client.get('/api/v1/perception'))
        self.assertIsNone(perception['camera']['preview'])
        self.assertIsNone(perception['recognition']['identity'])
        diag = self.assert_ok(self.client.get('/api/v1/diagnostics'))
        self.assertFalse(diag['esp32']['connected'])
        self.assertTrue(all(s['availability'] == 'unavailable' for s in diag['sensors'].values()))

    def test_body_validation(self):
        self.assert_error(self.call('PUT', '/api/v1/volume', data='{"volume": 5}',
                                    content_type='text/plain'), 415, 'unsupported_media_type')
        self.assert_error(self.call('PUT', '/api/v1/volume', data='{bad json',
                                    content_type='application/json'), 400, 'bad_request')
        self.assert_error(self.call('PUT', '/api/v1/volume', json=[1, 2]), 400, 'bad_request')
        self.assert_error(self.call('PUT', '/api/v1/volume', json={}), 400, 'bad_request', 'volume')
        self.assert_error(self.call('PUT', '/api/v1/volume', json={'volume': 5, 'x': 1}), 400,
                          'bad_request', 'x')

    def test_oversized_body_rejected(self):
        response = self.call('PUT', '/api/v1/expression', json={'expression': 'a' * 20000})
        self.assert_error(response, 413, 'payload_too_large')


class ModeTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def set_mode(self, mode):
        return self.call('PUT', '/api/v1/mode', json={'mode': mode})

    def test_transitions(self):
        data = self.assert_ok(self.set_mode('manual'))
        self.assertEqual((data['mode'], data['previous']), ('manual', 'companion'))
        data = self.assert_ok(self.set_mode('SLEEP '))
        self.assertEqual((data['mode'], data['expression']), ('sleep', 'sleeping'))
        data = self.assert_ok(self.set_mode('focus'))
        self.assertEqual((data['mode'], data['expression']), ('focus', 'idle'))
        self.assertEqual(self.assert_ok(self.client.get('/api/v1/mode'))['mode'], 'focus')
        modes = [h['mode'] for h in self.service.status()['history'] if h['command'] == 'mode']
        self.assertEqual(modes, ['focus', 'sleep', 'manual'])

    def test_same_mode_is_noop(self):
        data = self.assert_ok(self.set_mode('companion'))
        self.assertEqual(data['previous'], 'companion')
        self.assertEqual(self.service.status()['history'], [])

    def test_invalid_modes(self):
        for value in ('turbo', '', 3, None, ['manual'], True):
            with self.subTest(value=value):
                self.assert_error(self.set_mode(value), 400, 'validation_error', 'mode')
        self.assertEqual(self.service.get_mode()['mode'], 'companion')

    def test_leaving_companion_ends_conversation(self):
        self.assert_ok(self.call('POST', '/api/v1/conversation', json={'action': 'start'}))
        self.assertTrue(self.service.status()['conversation_active'])
        self.assertEqual(self.service.status()['state'], 'LISTENING')
        data = self.assert_ok(self.set_mode('focus'))
        self.assertFalse(data['conversation_active'])

    def test_conversation_requires_companion_mode(self):
        self.assert_ok(self.set_mode('manual'))
        self.assert_error(self.call('POST', '/api/v1/conversation', json={'action': 'start'}),
                          409, 'conflict')
        self.assert_error(self.call('POST', '/api/v1/conversation', json={'action': 'chat'}),
                          400, 'validation_error', 'action')
        # Ending is always allowed.
        self.assert_ok(self.call('POST', '/api/v1/conversation', json={'action': 'end'}))


class ExpressionVolumeSettingsTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_expression_changes(self):
        data = self.assert_ok(self.call('PUT', '/api/v1/expression', json={'expression': 'Happy'}))
        self.assertEqual(data['expression'], 'happy')
        self.assertEqual(len(data['expressions']), 10)
        self.assertEqual(self.service.status()['expression'], 'happy')

    def test_invalid_expressions(self):
        for value in ('angry', '', 5, None):
            with self.subTest(value=value):
                self.assert_error(self.call('PUT', '/api/v1/expression',
                                            json={'expression': value}), 400, 'validation_error')
        self.assertEqual(self.service.status()['expression'], 'idle')

    def test_expression_blocked_while_asleep(self):
        self.assert_ok(self.call('PUT', '/api/v1/mode', json={'mode': 'sleep'}))
        self.assert_error(self.call('PUT', '/api/v1/expression', json={'expression': 'happy'}),
                          409, 'conflict')
        self.assert_ok(self.call('PUT', '/api/v1/expression', json={'expression': 'sleeping'}))

    def test_volume_changes_and_bounds(self):
        self.assertEqual(self.assert_ok(self.call('PUT', '/api/v1/volume',
                                                  json={'volume': 35}))['volume'], 35)
        self.assertEqual(self.assert_ok(self.call('PUT', '/api/v1/volume',
                                                  json={'volume': 100.0}))['volume'], 100)
        for value in (-1, 101, 50.5, '50', True, None, float('nan')):
            with self.subTest(value=value):
                self.assert_error(self.call('PUT', '/api/v1/volume', json={'volume': value}),
                                  400, 'validation_error', 'volume')
        self.assertEqual(self.service.get_volume()['volume'], 100)

    def test_settings_update_is_atomic(self):
        data = self.assert_ok(self.call('PATCH', '/api/v1/settings',
                                        json={'eye_color': '#ff00aa', 'animation_speed': 1.5}))
        self.assertEqual((data['eye_color'], data['animation_speed']), ('#FF00AA', 1.5))
        bad = ({'eye_color': 'red'}, {'eye_color': '#12345'}, {'animation_speed': 3},
               {'animation_speed': '1'}, {'eye_color': '#000000', 'animation_speed': 0},
               {'glow': 1}, {})
        for body in bad:
            with self.subTest(body=body):
                self.assert_error(self.call('PATCH', '/api/v1/settings', json=body), 400)
        settings = self.service.get_settings()
        self.assertEqual((settings['eye_color'], settings['animation_speed']), ('#FF00AA', 1.5))


class MovementTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def manual(self):
        self.assert_ok(self.call('PUT', '/api/v1/mode', json={'mode': 'manual'}))

    def test_movement_requires_manual_mode(self):
        self.assert_error(self.call('POST', '/api/v1/movement/move', json={'direction': 'forward'}),
                          409, 'conflict')
        self.assert_error(self.call('PUT', '/api/v1/movement/servo',
                                    json={'servo': 'head_pan', 'angle': 10}), 409, 'conflict')
        self.assertFalse(self.assert_ok(self.client.get('/api/v1/movement'))['enabled'])

    def test_move_pulse_expires(self):
        self.manual()
        data = self.assert_ok(self.call('POST', '/api/v1/movement/move',
                                        json={'direction': 'left', 'duration_ms': 1000}))
        self.assertEqual((data['moving'], data['direction'], data['duration_ms']),
                         (True, 'left', 1000))
        self.clock.t += 0.5
        self.assertTrue(self.service.movement()['moving'])
        self.clock.t += 0.6
        self.assertFalse(self.service.movement()['moving'])

    def test_stop_always_allowed(self):
        self.manual()
        self.assert_ok(self.call('POST', '/api/v1/movement/move', json={'direction': 'forward'}))
        data = self.assert_ok(self.call('POST', '/api/v1/movement/stop'))
        self.assertFalse(data['moving'])
        self.assert_ok(self.call('PUT', '/api/v1/mode', json={'mode': 'companion'}))
        self.assert_ok(self.call('POST', '/api/v1/movement/stop'))

    def test_sleep_and_leaving_manual_stop_motion(self):
        for mode in ('sleep', 'focus'):
            self.manual()
            self.assert_ok(self.call('POST', '/api/v1/movement/move', json={'direction': 'right'}))
            self.assert_ok(self.call('PUT', '/api/v1/mode', json={'mode': mode}))
            self.assertFalse(self.service.movement()['moving'], mode)

    def test_movement_validation(self):
        self.manual()
        for body in ({'direction': 'up'}, {'direction': 1},
                     {'direction': 'left', 'duration_ms': 50},
                     {'direction': 'left', 'duration_ms': 6000},
                     {'direction': 'left', 'duration_ms': '500'}):
            with self.subTest(body=body):
                self.assert_error(self.call('POST', '/api/v1/movement/move', json=body), 400)
        self.assert_error(self.call('POST', '/api/v1/movement/move', json={}), 400, field='direction')
        self.assertFalse(self.service.movement()['moving'])

    def test_speed(self):
        data = self.assert_ok(self.call('PUT', '/api/v1/movement/speed', json={'speed': 80}))
        self.assertEqual(data['speed'], 80)
        for value in (-5, 101, 'fast', None):
            self.assert_error(self.call('PUT', '/api/v1/movement/speed', json={'speed': value}),
                              400, field='speed')
        self.manual()
        self.assert_ok(self.call('PUT', '/api/v1/movement/speed', json={'speed': 0}))
        self.assert_error(self.call('POST', '/api/v1/movement/move', json={'direction': 'forward'}),
                          409)

    def test_servos(self):
        self.manual()
        data = self.assert_ok(self.call('PUT', '/api/v1/movement/servo',
                                        json={'servo': 'head_tilt', 'angle': 120}))
        self.assertEqual(data['servos']['head_tilt']['angle'], 120)
        for body in ({'servo': 'head_tilt', 'angle': 150}, {'servo': 'arm', 'angle': 90},
                     {'servo': 'head_pan', 'angle': 181}, {'servo': 'head_pan'}):
            with self.subTest(body=body):
                self.assert_error(self.call('PUT', '/api/v1/movement/servo', json=body), 400)

    def test_mock_service_drives_no_motors(self):
        with patch('hardware.mock_hardware.MockMotors') as motors:
            self.manual()
            self.assert_ok(self.call('POST', '/api/v1/movement/move', json={'direction': 'left'}))
            self.assert_ok(self.call('POST', '/api/v1/movement/stop'))
        motors.assert_not_called()


@unittest.skipIf(flask is None, 'Flask not installed')
class ServiceUnitTests(unittest.TestCase):
    def test_errors_carry_status(self):
        self.assertEqual(ValidationError('x').status, 400)
        self.assertEqual(ConflictError('x').status, 409)

    def test_history_is_bounded(self):
        service = MockRobotService()
        for i in range(30):
            service.set_volume(i)
        history = service.status()['history']
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]['volume'], 29)

    def test_host_metrics_without_procfs_reports_unavailable(self):
        snap = HostMetrics(reader=lambda path: None).snapshot()
        for key in ('cpu_percent', 'memory_percent', 'temperature', 'uptime'):
            self.assertIsNone(snap[key]['value'])
            self.assertEqual(snap[key]['availability'], 'unavailable')

    def test_host_metrics_parse_linux_files(self):
        stats = iter(['cpu  100 0 100 800 0 0 0 0\n', 'cpu  150 0 150 900 0 0 0 0\n'])
        files = {
            '/proc/meminfo': 'MemTotal:  4000000 kB\nMemAvailable: 1000000 kB\n',
            '/sys/class/thermal/thermal_zone0/temp': '48312\n',
            '/proc/uptime': '3600.5 100.0\n',
        }
        metrics = HostMetrics(reader=lambda p: next(stats) if p == '/proc/stat' else files.get(p))
        self.assertIsNone(metrics.cpu_percent())  # needs two samples
        self.assertEqual(metrics.cpu_percent(), 50.0)
        self.assertEqual(metrics.memory()['percent'], 75.0)
        self.assertEqual(metrics.temperature_c(), 48.3)
        self.assertEqual(metrics.uptime_s(), 3600)


class RegressionTests(unittest.TestCase):
    """The dashboard must not touch hardware or the AI pipeline."""

    @unittest.skipIf(flask is None, 'Flask not installed')
    def test_dashboard_runs_with_hardware_and_ai_modules_blocked(self):
        code = '''
import importlib.abc, sys
BLOCKED = ('cv2', 'board', 'busio', 'digitalio', 'RPi', 'spidev', 'gpiozero', 'picamera2',
           'adafruit_rgb_display', 'sounddevice', 'pyttsx3', 'faster_whisper', 'onnxruntime',
           'serial', 'PIL', 'numpy', 'audio', 'perception', 'intelligence')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in BLOCKED or fullname in ('main', 'hardware.tft_display',
                                                             'display.eyes', 'display.robot_eyes'):
            raise AssertionError('Forbidden import: ' + fullname)
sys.meta_path.insert(0, Block())
from web import create_app
from web.config import WebConfig
app = create_app(WebConfig.from_env({'ELI_WEB_PASSWORD': 'pw'}))
client = app.test_client()
client.get('/login')
with client.session_transaction() as s:
    token = s['csrf_token']
assert client.post('/login', data={'password': 'pw', 'csrf_token': token}).status_code == 302
with client.session_transaction() as s:
    token = s['csrf_token']
h = {'X-CSRF-Token': token}
assert client.put('/api/v1/mode', json={'mode': 'manual'}, headers=h).status_code == 200
assert client.post('/api/v1/movement/move', json={'direction': 'left'}, headers=h).status_code == 200
assert client.put('/api/v1/expression', json={'expression': 'happy'}, headers=h).status_code == 200
for path in ('/', '/settings', '/api/v1/diagnostics', '/api/v1/perception'):
    assert client.get(path).status_code == 200, path
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_does_not_import_web(self):
        code = ('import sys, main\n'
                'assert not any(m == "web" or m.startswith("web.") for m in sys.modules)\n'
                'assert not any(m == "flask" or m.startswith("flask.") for m in sys.modules)\n')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_interfaces_unchanged(self):
        from behavior.state_machine import RobotStateMachine, State
        from display.expressions import Expression
        from hardware.mock_hardware import MockMotors
        self.assertEqual([s.name for s in State], ['IDLE', 'LISTENING', 'THINKING', 'SPEAKING'])
        self.assertIs(RobotStateMachine().current_state(), State.IDLE)
        for name in ('move_forward', 'move_backward', 'turn_left', 'turn_right', 'stop'):
            self.assertTrue(callable(getattr(MockMotors, name)))
        self.assertEqual(len(Expression), 10)


if __name__ == '__main__':
    unittest.main()
