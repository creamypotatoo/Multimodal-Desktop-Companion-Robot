"""Phase 2 service and API tests adapted from the original timing scenarios."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import unittest

from productivity import UtilityError, UtilityService
from test_web import FakeClock, PASSWORD, flask, make_config


class UtilityTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(0)
        self.service = UtilityService(clock=self.clock)
        self.act = self.service.act

    def test_countdown_accuracy_pause_resume_and_exactly_once(self):
        self.act('timer', 'start', duration=300)
        self.clock.t = 123.4
        self.assertAlmostEqual(self.act('timer', 'pause')['timer']['remaining'], 176.6)
        self.clock.t = 1000
        self.assertAlmostEqual(self.act('timer', 'resume')['timer']['remaining'], 176.6)
        self.clock.t = 1176.599
        self.assertFalse(self.service.notifications()['events'])
        self.clock.t = 1176.6
        data = self.service.snapshot()
        self.assertEqual(data['timer'], dict(state='completed', duration=300, remaining=0, progress=100))
        self.assertEqual(data['events'], [dict(id=1, kind='timer_done', phase=None, next_phase=None)])
        self.assertEqual(data['events'], self.service.snapshot()['events'])
        self.assertFalse(self.service.notifications(after=1)['events'])

    def test_countdown_reset_cancel_and_validation(self):
        self.act('timer', 'start', duration=90)
        self.assertEqual(self.act('timer', 'reset')['timer']['remaining'], 90)
        self.act('timer', 'start')
        with self.assertRaises(UtilityError) as conflict:
            self.act('timer', 'start', duration=15)
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(self.service.snapshot()['timer']['duration'], 90)
        self.assertEqual(self.act('timer', 'cancel')['timer']['duration'], 0)
        self.clock.t = 1000
        self.assertFalse(self.service.notifications()['events'])
        with self.assertRaises(UtilityError):
            self.act('timer', 'start')
        for value in (True, '5', 0, -1, 1.0, 86400):
            with self.subTest(value=value), self.assertRaises(UtilityError):
                self.service.configure('timer', {'duration': value})

    def test_stopwatch_splits_extrema_and_clear(self):
        self.act('stopwatch', 'start')
        self.clock.t = 12.5
        self.act('stopwatch', 'lap')
        self.clock.t = 20
        self.act('stopwatch', 'pause')
        with self.assertRaises(UtilityError):
            self.act('stopwatch', 'lap')
        self.clock.t = 100
        self.act('stopwatch', 'resume')
        self.clock.t = 102.5
        data = self.act('stopwatch', 'lap')['stopwatch']
        self.assertEqual(data['laps'], [dict(number=1, duration=12.5, total=12.5),
                                        dict(number=2, duration=10, total=22.5)])
        self.assertEqual(data['fastest'], data['laps'][1])
        self.assertEqual(data['slowest'], data['laps'][0])
        self.act('stopwatch', 'clear_laps')
        self.clock.t = 104
        data = self.act('stopwatch', 'lap')['stopwatch']
        self.assertEqual(data['laps'], [dict(number=1, duration=1.5, total=24)])
        self.assertEqual(self.act('stopwatch', 'reset')['stopwatch']['elapsed'], 0)

    def test_stopwatch_bounded_history_retains_extrema(self):
        self.act('stopwatch', 'start')
        self.clock.t = 0.5
        self.act('stopwatch', 'lap')
        for i in range(1, 101):
            self.clock.t = 0.5 + i
            data = self.act('stopwatch', 'lap')['stopwatch']
        self.assertEqual(len(data['laps']), 99)
        self.assertEqual(data['fastest']['number'], 1)

    def test_pomodoro_manual_default_cycle(self):
        self.assertEqual(self.service.snapshot()['pomodoro']['settings'],
                         dict(focus=25, short_break=5, long_break=15, long_every=4, automatic=False))
        for count in range(1, 5):
            self.act('pomodoro', 'start')
            self.clock.t += 1500
            state = self.service.snapshot()['pomodoro']
            phase, duration = ('long_break', 900) if count == 4 else ('short_break', 300)
            self.assertEqual((state['state'], state['phase'], state['remaining']),
                             ('idle', phase, duration))
            self.assertEqual((state['completed'], state['total_focus']), (count, count * 1500))
            self.act('pomodoro', 'start')
            self.clock.t += duration
            self.assertEqual(self.service.snapshot()['pomodoro']['phase'], 'focus')
        self.assertEqual([event['kind'] for event in self.service.notifications()['events']],
                         ['focus_done', 'break_done'] * 4)

    def test_pomodoro_automatic_catch_up_skips_and_settings(self):
        self.service.configure('pomodoro', dict(focus=1, short_break=1, long_break=2,
                                                long_every=2, automatic=True))
        self.act('pomodoro', 'start')
        self.clock.t = 30
        self.act('pomodoro', 'pause')
        self.clock.t = 100
        self.act('pomodoro', 'resume')
        self.clock.t = 120
        state = self.act('pomodoro', 'skip')['pomodoro']
        self.assertEqual((state['phase'], state['completed'], state['total_focus']), ('short_break', 0, 0))
        self.assertFalse(self.service.notifications()['events'])
        self.clock.t = 305
        data = self.service.snapshot()
        self.assertEqual([event['kind'] for event in data['events']], ['break_done', 'focus_done', 'break_done'])
        self.assertEqual((data['pomodoro']['phase'], data['pomodoro']['remaining']), ('focus', 55))
        self.clock.t = 360
        state = self.service.snapshot()['pomodoro']
        self.assertEqual((state['phase'], state['completed'], state['total_focus']), ('long_break', 2, 120))
        self.assertEqual(self.act('pomodoro', 'reset')['pomodoro']['completed'], 0)

    def test_scheduler_completes_in_background_and_can_restart(self):
        class ObservedService(UtilityService):
            completed = Event()

            def _advance(self, now):
                super()._advance(now)
                if self._events:
                    self.completed.set()

        service = ObservedService(clock=self.clock)
        service.act('timer', 'start', duration=1)
        self.clock.t = 1
        self.addCleanup(service.stop_scheduler)
        self.assertTrue(service.start_scheduler())
        self.assertFalse(service.start_scheduler())
        self.assertTrue(service.completed.wait(timeout=2))
        self.assertEqual(service.notifications()['cursor'], 1)
        worker = service._thread
        service.stop_scheduler()
        self.assertFalse(worker.is_alive())
        service.stop_scheduler()
        self.assertTrue(service.start_scheduler())
        service.stop_scheduler()
        self.assertEqual(service.notifications()['cursor'], 1)


@unittest.skipIf(flask is None, 'Flask not installed')
class UtilityApiTests(unittest.TestCase):
    def setUp(self):
        from web import create_app
        self.clock = FakeClock()
        self.app = create_app(make_config(), utilities=UtilityService(clock=self.clock), testing=True)
        self.client = self.app.test_client()
        self.base = '/api/v1/utilities'

    def login(self, client):
        client.get('/login')
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.assertEqual(client.post('/login', data={'password': PASSWORD, 'csrf_token': token}).status_code, 302)
        with client.session_transaction() as session:
            return {'X-CSRF-Token': session['csrf_token']}

    def test_auth_csrf_and_validation(self):
        self.assertEqual(self.client.get(self.base).status_code, 401)
        self.assertEqual(self.client.get(self.base + '/events').status_code, 401)
        headers = self.login(self.client)
        timer = self.base + '/timer/actions'
        self.assertEqual(self.client.post(timer, json={'action': 'start'}).status_code, 403)
        for method, suffix, body, code in (
            ('POST', '/timer/actions', {'action': 'resume'}, 409),
            ('POST', '/timer/actions', {'action': ['start']}, 400),
            ('POST', '/timer/actions', {'action': 'start', 'duration': None}, 400),
            ('POST', '/timer/actions', {'action': 'start', 'extra': True}, 400),
            ('PUT', '/timer/settings', {'duration': 999999}, 400),
            ('PUT', '/pomodoro/settings', {'automatic': 1}, 400),
            ('PUT', '/pomodoro/settings', {'long_every': 0}, 400),
            ('PUT', '/stopwatch/settings', {}, 404),
            ('POST', '/other/actions', {'action': 'start'}, 404),
        ):
            with self.subTest(suffix=suffix, body=body):
                response = self.client.open(self.base + suffix, method=method, json=body, headers=headers)
                self.assertEqual(response.status_code, code)
                self.assertFalse(response.json['ok'])
        self.assertEqual(self.client.get(self.base + '/events?after=-1').status_code, 400)

    def test_multiple_clients_and_concurrent_requests(self):
        clients = [self.app.test_client() for _ in range(4)]
        headers = [self.login(client) for client in clients]
        barrier = Barrier(len(clients))

        def start(index):
            barrier.wait(timeout=3)
            return clients[index].post(self.base + '/timer/actions', headers=headers[index],
                                       json={'action': 'start', 'duration': 10 + index})

        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            responses = list(pool.map(start, range(len(clients))))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409, 409, 409])
        duration = next(response.json['data']['timer']['duration'] for response in responses if response.status_code == 200)
        for client in clients:
            self.assertEqual(client.get(self.base).json['data']['timer']['duration'], duration)
        self.clock.t += duration
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            records = list(pool.map(lambda client: client.get(self.base + '/events').json['data'], clients))
        self.assertTrue(all(record == records[0] for record in records))
        self.assertEqual(len(records[0]['events']), 1)
