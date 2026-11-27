"""Phase 5 voice and audio tests with no hardware, model loading or network."""
from dataclasses import replace
from queue import Empty, Queue
from threading import Event, Thread
import subprocess
from time import monotonic
from unittest import TestCase
from unittest.mock import Mock, patch

from audio.runtime_audio import Microphone, isolated_player
from audio.text_to_speech import TTSConfig
from controller import Action, Authority, Command, Source
from runtime import Components, UnifiedRuntime
from runtime.voice import duration, intent
from test_runtime import Eyes
from test_web import FakeClock, PASSWORD, make_config

OPERATOR = Authority('operator', can_move=True, can_wake=True)


class VoiceParsingTests(TestCase):
    def test_spoken_durations(self):
        for words, seconds in [('ten minutes', 600), ('one hour and two minutes and three seconds', 3723),
                               ('twenty five seconds', 25), ('an hour', 3600),
                               ('one hundred and one seconds', 101), ('23 hours 59 minutes 59 seconds', 86399)]:
            self.assertEqual(duration(words), seconds)
        for words in ('zero seconds', '24 hours', 'two minutes two minutes', 'five', 'two seconds and'):
            self.assertIsNone(duration(words))

    def test_allowlist_and_clarification(self):
        for text, expected in [
            ('Please set a timer for ten minutes.', ('utility', 'timer', 'start', 600)),
            ('Stop the stopwatch', ('utility', 'stopwatch', 'pause', None)),
            ('Resume my Pomodoro', ('utility', 'pomodoro', 'resume', None)),
            ('Set volume to one hundred percent', ('volume', 100)),
            ('Increase volume by five percent', ('relative_volume', 5)),
            ('Enter Sleep Mode', ('mode', 'sleep')),
            ('How much time is left on the timer?', ('remaining',)),
        ]:
            self.assertEqual(intent(text), expected)
        for text in ('Set a timer for -5 minutes', 'Set volume to 2.5 percent', 'Start timer', 'pause',
                     'Set a timer for 99 hours', 'Set volume to 101 percent'):
            self.assertEqual(intent(text)[0], 'clarify')
        self.assertIsNone(intent('Move forward'))
        self.assertIsNone(intent('Tell me about the Moon'))


class AudioAdapterTests(TestCase):
    def test_microphone_has_one_owner_and_cancel_closes_stream(self):
        opened, closed, allowed = Event(), Event(), Event()
        allowed.set()

        class Stream:
            active = True

            def __enter__(self):
                opened.set()
                return self

            def __exit__(self, *exc):
                closed.set()

        driver = Mock()
        driver.InputStream.return_value = Stream()
        microphone = Microphone(input_module=driver, rate=24000, device=3)
        captured = []
        worker = Thread(target=lambda: captured.append(microphone.capture(allowed.is_set)))
        self.addCleanup(worker.join, 2)
        self.addCleanup(allowed.clear)
        worker.start()
        self.assertTrue(opened.wait(2))
        with self.assertRaisesRegex(RuntimeError, 'already owned'):
            microphone.capture(allowed.is_set)
        allowed.clear()
        self.assertTrue(closed.wait(2))
        worker.join(2)
        self.assertEqual(captured, [None])
        self.assertEqual(driver.InputStream.call_args.kwargs['device'], 3)
        self.assertEqual(driver.InputStream.call_args.kwargs['samplerate'], 24000)

    def test_device_error_releases_capture_owner(self):
        driver = Mock()
        driver.InputStream.side_effect = OSError('offline')
        mic = Microphone(input_module=driver)
        for _ in range(2):
            with self.assertRaisesRegex(OSError, 'offline'):
                mic.capture(lambda: True)

    def test_windows_player_kills_and_reaps_before_reporting_cancellation(self):
        process, launch = Mock(), Mock()
        launch.return_value = process
        process.stdout.readline.return_value = b''
        state = Event()
        state.set()

        def wait(timeout):
            if timeout < 1:
                state.clear()
                raise subprocess.TimeoutExpired('player', timeout)
            return -9

        process.wait.side_effect = wait
        with patch('audio.runtime_audio.sys.platform', 'win32'), \
                patch.object(subprocess, 'CREATE_NO_WINDOW', 1, create=True):
            self.assertFalse(isolated_player(TTSConfig(backend='pyttsx3'), 'test', 65,
                                             state.is_set, Mock(), launch=launch))
        process.kill.assert_called_once()
        process.wait.assert_called_with(timeout=1)

    def test_backend_selection_does_not_open_microphone_or_load_model(self):
        from runtime.components import configured_components

        fake_mic = Mock()
        with patch('audio.runtime_audio.Microphone', return_value=fake_mic) as factory, \
                patch('audio.runtime_audio.OfflineWhisper', return_value=Mock()) as model, \
                patch('intelligence.conversation.default_responder', return_value=Mock()) as ai:
            self.assertEqual(configured_components().audio_backend, 'mock')
            factory.assert_not_called()
            model.assert_not_called()
            windows = configured_components('windows', 'mock')
            self.assertEqual(windows.audio_backend, 'windows')
            self.assertTrue(windows.ai_simulated)
            fake_mic.capture.assert_not_called()
            ai.assert_not_called()
            self.assertFalse(configured_components('windows', 'ollama').ai_simulated)
            ai.assert_called_once()


class VoiceRuntimeTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.phrases = Queue()
        self.played = []
        self.captures = []

        def capture(current, *, wake, timeout):
            self.captures.append(wake)
            try:
                text = self.phrases.get(timeout=0.04)
            except Empty:
                return None
            return text if current() else None

        def speak(text, volume, current, on_start):
            if not current():
                return False
            if on_start:
                on_start()
            self.played.append((text, volume))
            return True

        self.runtime = UnifiedRuntime(Components(lambda: Eyes(), lambda: None,
            lambda text, timeout: text, lambda text, name, **kwargs: 'AI reply', speak,
            capture=capture), clock=self.clock, follow_up=3, wake_cooldown=2, announce=True)
        self.robot = self.runtime.controller
        self.addCleanup(self.runtime.shutdown)

    def wait_for(self, condition):
        until = monotonic() + 3
        while monotonic() < until:
            if condition():
                return
            Event().wait(0.01)
        self.fail('Expected runtime action did not arrive')

    def command(self, op, **kwargs):
        return self.robot.execute(Command(op, Source.DASHBOARD, kwargs), OPERATOR)

    def test_wake_cooldown_follow_up_timer_and_inactivity(self):
        self.runtime.configure_wake({'enabled': True})
        self.runtime.startup()
        self.phrases.put('Hey Eli!')
        self.wait_for(lambda: self.robot.interaction() is not None)
        first = self.robot.interaction()
        self.assertFalse(self.robot.activate_voice(*self.robot.wake_context()))
        self.assertEqual(self.robot.get_mode()['mode'], 'companion')
        self.phrases.put('Set a timer for ten minutes')
        self.wait_for(lambda: self.robot.utilities.snapshot()['timer']['duration'] == 600)
        self.phrases.put('What is a dolphin?')
        self.wait_for(lambda: ('AI reply', 70) in self.played)
        self.command(Action.CONVERSATION, action='end')
        # Two samples during cooldown must not open another interaction.
        self.wait_for(lambda: self.runtime.audio_status()['health']['wake']['detail'] == 'Suspended')
        self.assertIsNone(self.robot.interaction())
        self.clock.t += 2
        before = len(self.captures)
        self.phrases.put('Hey Eli')
        self.wait_for(lambda: self.robot.interaction() is not None)
        self.assertNotEqual(self.robot.interaction(), first)
        self.wait_for(lambda: len(self.captures) > before and self.captures[-1] is False)
        self.clock.t += 4
        self.wait_for(lambda: self.robot.interaction() is None)

    def test_wake_is_suppressed_during_speech_and_sleep_stops_it(self):
        playback, stopped = Event(), Event()

        def speak(text, volume, current, on_start):
            on_start()
            playback.set()
            while current():
                Event().wait(0.01)
            stopped.set()
            return False

        self.runtime.components = replace(self.runtime.components, speak=speak)
        self.runtime.configure_wake({'enabled': True})
        self.runtime.startup()
        self.phrases.put('Hey Eli')
        self.wait_for(lambda: self.robot.interaction() is not None)
        self.phrases.put('Hello!')
        self.assertTrue(playback.wait(2))
        calls = len(self.captures)
        self.assertEqual(self.runtime.audio_status()['health']['wake']['detail'], 'Suspended during speech')
        self.assertEqual(len(self.captures), calls)
        self.command(Action.MODE, mode='sleep')
        self.assertTrue(stopped.wait(2))
        self.assertEqual(self.robot.status()['expression'], 'sleeping')
        self.assertEqual(len(self.captures), calls)

    def test_mute_restore_and_wake_in_sleep_opt_in(self):
        self.runtime.configure_wake({'enabled': True, 'in_sleep': True, 'phrase': 'Hello Robot'})
        self.runtime.startup()
        self.command(Action.VOLUME, volume=35)
        self.assertEqual(self.runtime.mute(True)['volume'], 0)
        self.assertEqual(self.runtime.mute(False)['volume'], 35)
        count = len(self.captures)
        self.command(Action.MODE, mode='sleep')
        self.wait_for(lambda: len(self.captures) > count)
        self.phrases.put('Hello Robot')
        self.wait_for(lambda: self.robot.interaction() is not None)
        self.assertEqual(self.robot.get_mode()['mode'], 'companion')

    def test_dashboard_stop_cancels_playback_without_changing_mode(self):
        started, cancelled = Event(), Event()

        def speaker(text, volume, current, on_start):
            if on_start:
                on_start()
            started.set()
            while current():
                Event().wait(0.01)
            cancelled.set()
            return False

        self.runtime.components = replace(self.runtime.components, speak=speaker)
        self.runtime.startup()
        self.assertEqual(self.runtime.test_speaker(), {'queued': True})
        self.assertTrue(started.wait(2))
        self.assertEqual(self.runtime.stop_speaking(), {'requested': True})
        self.assertTrue(cancelled.wait(2))
        self.wait_for(lambda: self.runtime.audio_status()['health']['audio']['detail'] == 'Speech stopped')
        self.assertEqual(self.robot.get_mode()['mode'], 'standby')

    def test_new_audio_routes_require_auth_and_csrf(self):
        from web import create_app

        self.runtime.startup()
        app = create_app(make_config(), controller=self.robot, testing=True)
        app.extensions['eli_runtime'] = self.runtime
        client = app.test_client()
        self.assertEqual(client.get('/api/v1/audio').status_code, 401)
        client.get('/login')
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.assertEqual(client.post('/login', data={'password': PASSWORD, 'csrf_token': token}).status_code, 302)
        self.assertEqual(client.post('/api/v1/audio/stop').status_code, 403)
        with client.session_transaction() as session:
            token = session['csrf_token']
        headers = {'X-CSRF-Token': token}
        self.assertEqual(client.post('/api/v1/audio/stop', headers=headers).json['data'], {'requested': True})
        self.assertEqual(client.put('/api/v1/audio/wake', json={'enabled': 'yes'}, headers=headers).status_code, 400)
