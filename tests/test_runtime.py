"""Unified runtime integration tests. All I/O is fake."""
from dataclasses import replace
import os
from threading import Event
from time import monotonic
from unittest import TestCase
from unittest.mock import patch

from behavior.state_machine import State
from controller import Action, Authority, Command, Source
from runtime import Components, UnifiedRuntime
from test_web import FakeClock, PASSWORD, make_config

OPERATOR = Authority('operator', can_move=True, can_wake=True)


class Eyes:
    def __init__(self):
        self.history = []
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def set_expression(self, value):
        self.history.append(value)


class RuntimeTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.eyes = Eyes()
        self.spoken = []
        self.runtime = UnifiedRuntime(Components(
            lambda: self.eyes, lambda: 'recording',
            lambda recording, timeout: 'Hello',
            lambda *args, **kwargs: 'reply',
            self.speak,
        ), clock=self.clock, announce=True)
        self.robot = self.runtime.controller
        self.addCleanup(self.runtime.shutdown)

    def speak(self, text, volume, current, on_start):
        if current():
            if on_start:
                on_start()
            self.spoken.append((text, volume))

    def wait_for(self, condition, timeout=2):
        end = monotonic() + timeout
        while monotonic() < end:
            if condition():
                return
            Event().wait(0.01)
        self.fail('Runtime state did not arrive before deadline')

    def command(self, operation, **args):
        return self.robot.execute(Command(operation, Source.DASHBOARD, args), OPERATOR)

    def start_conversation(self):
        self.assertTrue(self.command(Action.MODE, mode='companion').ok)
        self.assertTrue(self.command(Action.CONVERSATION, action='start').ok)

    def timer(self):
        self.assertTrue(self.command(Action.UTILITY, name='timer', action='start', duration=1).ok)

    def test_ownership_and_shutdown(self):
        self.assertTrue(self.runtime.startup())
        self.assertFalse(self.runtime.startup())
        self.assertTrue(self.eyes.running)
        self.assertEqual(self.robot.status()['mode'], 'standby')
        self.assertFalse(self.robot.utilities.start_scheduler())
        other = UnifiedRuntime(self.runtime.components)
        with self.assertRaisesRegex(RuntimeError, 'already owns'):
            other.startup()
        self.assertTrue(self.command(Action.MODE, mode='movement').ok)
        self.assertTrue(self.command(Action.MOVE, direction='right').ok)
        self.runtime.shutdown()
        self.assertFalse(self.robot.status()['movement']['moving'])
        self.assertFalse(self.eyes.running)
        self.assertTrue(other.startup())
        other.shutdown()

    def test_partial_startup_releases_resource(self):
        class BrokenEyes(Eyes):
            def start(self):
                super().start()
                raise OSError('cannot open')

        eye = BrokenEyes()
        failed = UnifiedRuntime(replace(self.runtime.components, eyes=lambda: eye))
        with self.assertRaises(OSError):
            failed.startup()
        self.assertFalse(eye.running)
        self.assertEqual(failed.controller.status()['runtime']['lifecycle']['detail'], 'Startup failed')
        self.assertTrue(self.runtime.startup())

    def test_stale_ai_and_sleep_priority(self):
        listening, thinking, release_record, release_ai = [Event() for _ in range(4)]
        self.addCleanup(release_ai.set)
        self.addCleanup(release_record.set)

        def record():
            listening.set()
            release_record.wait(3)
            return 'recording'

        def respond(*args, **kwargs):
            thinking.set()
            release_ai.wait(3)
            return 'stale reply'

        self.runtime.components = replace(self.runtime.components, record=record, respond=respond)
        self.runtime.startup()
        self.start_conversation()
        self.assertTrue(listening.wait(2))
        self.assertEqual(self.robot.status()['state'], 'LISTENING')
        self.wait_for(lambda: 'listening' in self.eyes.history)
        release_record.set()
        self.assertTrue(thinking.wait(2))
        self.assertEqual(self.robot.status()['state'], 'THINKING')
        old = self.robot.interaction()
        self.timer()
        self.assertTrue(self.command(Action.MODE, mode='sleep').ok)
        self.clock.t += 2
        release_ai.set()
        self.wait_for(lambda: self.eyes.history[-1:] == ['sleeping'])
        self.assertFalse(self.robot.publish_behavior(old, State.SPEAKING))
        self.assertEqual(self.robot.status()['expression'], 'sleeping')
        self.assertEqual(self.robot.utilities.snapshot()['timer']['state'], 'completed')
        self.runtime.shutdown()
        self.assertEqual(self.spoken, [])

    def test_volume_and_announcements_defer_during_conversation(self):
        received, release = Event(), Event()
        self.addCleanup(release.set)

        def respond(*args, **kwargs):
            received.set()
            release.wait(3)
            return 'reply'

        self.runtime.components = replace(self.runtime.components, respond=respond)
        self.runtime.startup()
        self.assertEqual(self.command(Action.VOLUME, volume=101).status, 400)
        self.assertTrue(self.command(Action.VOLUME, volume=36).ok)
        self.start_conversation()
        self.assertTrue(received.wait(2))
        self.timer()
        self.clock.t += 1
        self.robot.tick()
        self.assertEqual(self.spoken, [])
        release.set()
        self.wait_for(lambda: ('reply', 36) in self.spoken)
        self.command(Action.CONVERSATION, action='end')
        self.wait_for(lambda: ('Your timer is complete.', 36) in self.spoken)
        self.runtime.shutdown()
        self.assertEqual(self.spoken.count(('Your timer is complete.', 36)), 1)

    def test_emergency_and_temporary_expression_priority(self):
        self.runtime.startup()
        self.assertTrue(self.command(Action.EXPRESSION, expression='happy').ok)
        self.assertEqual(self.robot.status()['expression'], 'happy')
        self.clock.t += 3
        self.assertEqual(self.robot.status()['expression'], 'idle')
        self.assertTrue(self.command(Action.EMERGENCY_STOP).ok)
        self.assertEqual(self.robot.status()['expression'], 'confused')
        self.assertTrue(self.command(Action.EXPRESSION, expression='excited').ok)
        self.assertEqual(self.robot.status()['expression'], 'confused')
        self.assertTrue(self.command(Action.MODE, mode='sleep').ok)
        self.assertEqual(self.robot.status()['expression'], 'sleeping')

    def test_ai_failure_is_reported_and_can_be_cancelled(self):
        def broken(*args, **kwargs):
            raise OSError('service offline')

        self.runtime.components = replace(self.runtime.components, respond=broken)
        self.runtime.startup()
        self.start_conversation()
        self.wait_for(lambda: self.robot.companion()['ai']['availability'] == 'unavailable')
        self.assertEqual(self.robot.companion()['ai']['detail'], 'Response generation failed')
        self.assertTrue(self.command(Action.CONVERSATION, action='end').ok)
        self.assertEqual(self.robot.status()['state'], 'IDLE')
        self.assertEqual(self.spoken, [])

    def test_shutdown_invalidates_inflight_work_and_retains_ownership(self):
        requested, released = Event(), Event()
        self.addCleanup(released.set)

        def blocked(*args, **kwargs):
            requested.set()
            released.wait(4)
            return 'late'

        self.runtime.components = replace(self.runtime.components, respond=blocked)
        self.runtime.startup()
        self.start_conversation()
        self.assertTrue(requested.wait(2))
        now = monotonic()
        self.runtime.shutdown()
        self.assertLess(monotonic() - now, 1.5)
        other = UnifiedRuntime(self.runtime.components)
        with self.assertRaises(RuntimeError):
            other.startup()
        self.assertIsNone(self.robot.interaction())
        released.set()
        self.wait_for(lambda: self.robot.status()['runtime']['lifecycle']['detail'] == 'Stopped')
        self.assertEqual(self.spoken, [])
        self.assertFalse(self.eyes.running)

    def test_sleep_announcements_require_opt_in(self):
        self.runtime.announce_in_sleep = True
        self.runtime.startup()
        self.assertTrue(self.command(Action.MODE, mode='sleep').ok)
        self.timer()
        self.clock.t += 1
        self.robot.tick()
        self.wait_for(lambda: ('Your timer is complete.', 70) in self.spoken)
        self.assertEqual(self.robot.status()['expression'], 'sleeping')

    def test_existing_tts_checks_cancellation_after_synthesis(self):
        from audio.text_to_speech import TTSConfig, speak

        current = True
        calls = []

        def run(command, *, stage, **kwargs):
            nonlocal current
            calls.append(stage)
            if stage == 'ffmpeg':
                current = False

        tools = {tool: tool for tool in ('espeak-ng', 'ffmpeg', 'aplay')}
        with patch('audio.text_to_speech._find_pi_tools', return_value=tools), \
                patch('audio.text_to_speech._run', side_effect=run):
            speak('late reply', TTSConfig(backend='espeak'), should_play=lambda: current)
        self.assertEqual(calls, ['espeak-ng', 'ffmpeg'])

    def test_web_auth_csrf_and_shared_state(self):
        from web import create_app

        self.runtime.startup()
        app = create_app(make_config(), controller=self.robot, testing=True)
        self.assertIs(app.extensions['eli_utilities'], self.robot.utilities)
        client = app.test_client()
        self.assertEqual(client.get('/api/v1/status').status_code, 401)
        client.get('/login')
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.assertEqual(client.post('/login', data={'password': PASSWORD, 'csrf_token': token}).status_code, 302)
        self.assertEqual(client.put('/api/v1/mode', json={'mode': 'companion'}).status_code, 403)
        with client.session_transaction() as session:
            token = session['csrf_token']
        response = client.put('/api/v1/mode', json={'mode': 'companion'}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200)
        status = client.get('/api/v1/status').json['data']
        self.assertEqual(status['mode'], 'companion')
        self.assertEqual(status['runtime']['hardware']['availability'], 'simulated')

    def test_launcher_modes_remain_opt_in(self):
        import main
        from web.__main__ import main as web_main

        with self.assertRaises(SystemExit) as legacy:
            main.main(['--help'])
        self.assertEqual(legacy.exception.code, 0)
        with patch('flask.Flask.run') as serve, patch.dict(os.environ, {'ELI_WEB_PASSWORD': PASSWORD}):
            self.assertEqual(web_main(['--host', '127.0.0.1', '--port', '5001']), 0)
            self.assertFalse(serve.call_args.kwargs['use_reloader'])
