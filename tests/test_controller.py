"""Controller policy, lifecycle and dashboard integration with deterministic clocks."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest import TestCase, skipIf

from controller import Action, Authority, Command, RobotController, RobotMode, Source
from controller.adapters import MockAdapter, SafetyReading, UnavailableAdapter
from test_web import FakeClock, PASSWORD, flask, make_config

OPERATOR = Authority('operator', can_move=True, can_wake=True)
OTHER = Authority('other', can_move=True, can_wake=True)


class ControllerTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.adapter = MockAdapter()
        self.robot = RobotController(clock=self.clock, adapters=self.adapter.ports())

    def command(self, operation, *, authority=OPERATOR, source=Source.DASHBOARD, **args):
        return self.robot.execute(Command(operation, source, args), authority)

    def mode(self, mode):
        self.assertTrue(self.command(Action.MODE, mode=mode).ok)

    def drive(self, **args):
        return self.command(Action.MOVE, direction='forward', **args)

    def test_startup_shutdown_is_idempotent_and_removes_subscriptions(self):
        started = Event()

        class MotorRecorder(MockAdapter):
            def stop(self):
                super().stop()
                started.set()

        adapter = MotorRecorder()
        robot = RobotController(clock=self.clock, adapters=adapter.ports())
        robot.events.subscribe('subscriber')
        self.addCleanup(robot.shutdown)
        self.assertEqual(robot.status()['mode'], RobotMode.STANDBY)
        self.assertFalse(robot.status()['movement']['moving'])
        self.assertTrue(robot.startup())
        self.assertTrue(started.is_set())
        self.assertFalse(robot.startup())
        self.assertFalse(robot.utilities.start_scheduler())
        worker = robot._worker
        robot.shutdown()
        robot.shutdown()
        self.assertFalse(worker.is_alive())
        self.assertEqual(robot.status()['connection']['controller'], 'stopped')
        self.assertEqual(robot.execute(Command(Action.VOLUME, Source.SYSTEM, {'volume': 1})).status, 503)
        with self.assertRaises(KeyError):
            robot.events.read('subscriber')
        self.assertTrue(robot.startup())

    def test_modes_conversation_eyes_and_always_available_stop(self):
        self.assertEqual(self.command(Action.CONVERSATION, action='start').status, 409)
        self.mode(RobotMode.COMPANION)
        self.assertTrue(self.command(Action.CONVERSATION, action='start').ok)
        self.assertEqual(self.robot.status()['state'], 'LISTENING')
        self.assertIn(('expression', 'listening'), self.adapter.commands)
        self.mode(RobotMode.MOVEMENT)
        self.assertFalse(self.robot.status()['conversation_active'])
        self.assertFalse(self.robot.status()['movement']['moving'])
        self.assertTrue(self.drive().ok)
        self.mode(RobotMode.SLEEP)
        self.assertEqual(self.robot.status()['expression'], 'sleeping')
        self.assertTrue(self.command(Action.CONVERSATION, action='end').ok)
        self.assertEqual(self.robot.status()['expression'], 'sleeping')
        self.assertEqual(self.command(Action.MODE, mode='turbo').status, 400)
        for mode in RobotMode:
            self.mode(mode)
            self.assertTrue(self.command(Action.STOP).ok)

    def test_source_is_metadata_and_typed_arguments_are_validated(self):
        self.assertEqual(self.command(Action.MODE, authority=Authority(), source=Source.SYSTEM,
                                      mode='movement').status, 409)
        self.mode(RobotMode.MOVEMENT)
        self.assertEqual(self.drive(authority=Authority(), source=Source.AUTONOMOUS).status, 409)
        self.mode(RobotMode.SLEEP)
        self.assertEqual(self.command(Action.MODE, authority=Authority(), source=Source.VOICE,
                                      mode='standby').status, 409)
        invalid = [Command('move', Source.DASHBOARD), Command(Action.STOP, 'internal_system'),
                   Command(Action.STOP, Source.SYSTEM, []),
                   Command(Action.STOP, Source.SYSTEM, {'authority': OPERATOR}),
                   Command(Action.VOLUME, Source.SYSTEM)]
        for command in invalid:
            self.assertEqual(self.robot.execute(command).status, 400)
        self.assertEqual(self.command(Action.SPEED, speed=True).field, 'speed')
        self.assertEqual(self.drive(duration_ms=5001).field, 'duration_ms')

    def test_lease_conflicts_and_command_expiration(self):
        self.assertEqual(self.drive().status, 409)
        self.mode(RobotMode.MOVEMENT)
        self.assertTrue(self.drive().ok)
        self.assertEqual(self.drive(authority=OTHER).status, 409)
        self.assertEqual(self.command(Action.SPEED, speed=70, authority=OTHER).status, 409)
        self.clock.t += 0.5
        self.robot.tick()
        self.assertEqual(self.adapter.commands[-1], ('stop',))
        self.assertFalse(self.robot.movement()['moving'])
        self.assertEqual(self.drive(authority=OTHER).status, 409)
        self.clock.t += 1.5
        self.assertTrue(self.drive(authority=OTHER, duration_ms=5000).ok)
        self.assertEqual(self.adapter.commands[-1], ('move', 'forward', 50, 2000))
        self.clock.t += 2
        self.robot.tick()
        self.assertFalse(self.robot.movement()['moving'])
        self.assertEqual(self.adapter.commands[-1], ('stop',))
        self.assertFalse(self.robot.movement()['safety']['control_leased'])

    def test_worker_stops_expired_motion_without_a_client_request(self):
        stopped = Event()

        class MotorRecorder(MockAdapter):
            def stop(self):
                super().stop()
                stopped.set()

        adapter = MotorRecorder()
        self.robot = RobotController(clock=self.clock, adapters=adapter.ports())
        self.addCleanup(self.robot.shutdown)
        self.robot.startup()
        self.mode(RobotMode.MOVEMENT)
        self.assertTrue(self.drive(duration_ms=100).ok)
        stopped.clear()
        with self.robot._condition:
            self.clock.t += 0.11
            self.robot._condition.notify()
        self.assertTrue(stopped.wait(timeout=2))
        self.assertEqual(adapter.commands[-1], ('stop',))

    def test_emergency_and_obstacle_cliff_faults_require_explicit_reset(self):
        self.mode(RobotMode.MOVEMENT)
        self.assertTrue(self.drive().ok)
        self.assertTrue(self.command(Action.EMERGENCY_STOP, authority=OTHER).ok)
        self.assertTrue(self.robot.movement()['safety']['emergency_stop'])
        self.assertEqual(self.drive().status, 409)
        self.assertTrue(self.command(Action.RESET_SAFETY).ok)
        self.assertFalse(self.robot.movement()['moving'])
        for reading, label in [(SafetyReading(True, False), 'Obstacle'),
                               (SafetyReading(False, True), 'Cliff'),
                               (SafetyReading(None, False), 'Required safety')]:
            self.assertTrue(self.drive().ok)
            self.adapter.reading = reading
            self.robot.tick()
            self.assertFalse(self.robot.movement()['moving'])
            self.assertIn(label, self.robot.movement()['safety']['fault'])
            self.assertEqual(self.command(Action.RESET_SAFETY).status, 409)
            self.adapter.reading = SafetyReading(False, False)
            self.assertEqual(self.drive().status, 409)
            self.assertTrue(self.command(Action.RESET_SAFETY).ok)
            self.assertFalse(self.robot.movement()['moving'])

    def test_unavailable_sensors_fail_closed_in_adapter_mode(self):
        self.robot = RobotController(clock=self.clock, adapters=replace(
            self.adapter.ports(), simulated=False, sensors=UnavailableAdapter()))
        self.addCleanup(self.robot.shutdown)
        self.robot.startup()
        self.mode(RobotMode.MOVEMENT)
        result = self.drive()
        self.assertEqual(result.status, 409)
        self.assertIn('telemetry is unavailable', result.message)
        self.assertNotIn('move', [command[0] for command in self.adapter.commands])

    def test_adapter_failures_surface_and_stop_is_unconfirmed(self):
        class FailingMotors(MockAdapter):
            failed = True

            def move(self, *args):
                raise OSError('private adapter details')

            def stop(self):
                if self.failed:
                    raise OSError('private adapter details')
                super().stop()

        motors = FailingMotors()
        self.robot = RobotController(clock=self.clock, adapters=replace(
            self.adapter.ports(), motors=motors, audio=UnavailableAdapter()))
        self.mode(RobotMode.MOVEMENT)
        self.assertEqual(self.drive().status, 503)
        state = self.robot.status()
        self.assertIsNone(state['movement']['moving'])
        self.assertEqual(state['last_command']['message'], 'Motors could not move')
        self.assertEqual(self.command(Action.RESET_SAFETY).status, 503)
        self.assertEqual(self.command(Action.VOLUME, volume=13).status, 503)
        self.assertEqual(self.robot.get_volume()['volume'], 70)
        motors.failed = False
        self.assertTrue(self.command(Action.RESET_SAFETY).ok)
        self.assertFalse(self.robot.movement()['moving'])

    def test_event_subscriptions_and_productivity_across_modes(self):
        self.robot.events.subscribe('observer')
        with self.assertRaises(ValueError):
            self.robot.events.subscribe('observer')
        self.mode(RobotMode.COMPANION)
        self.assertTrue(self.command(Action.CONVERSATION, action='start').ok)
        self.assertTrue(self.command(Action.VOLUME, volume=12).ok)
        self.assertTrue(self.command(Action.UTILITY, name='timer', action='start', duration=7).ok)
        self.mode(RobotMode.MOVEMENT)
        self.assertTrue(self.drive().ok)
        self.mode(RobotMode.SLEEP)
        self.assertIn('timer', self.robot.status()['active_utilities'])
        self.adapter.reading = SafetyReading(None, None)
        self.clock.t += 7
        self.robot.tick()
        events = self.robot.events.read('observer')
        kinds = [event['event'] for event in events]
        for kind in ('mode_changed', 'conversation_changed', 'expression_changed',
                     'volume_changed', 'movement_requested', 'movement_stopped',
                     'availability_changed', 'utility_completed'):
            self.assertIn(kind, kinds)
        self.assertEqual(kinds.count('utility_completed'), 1)
        self.assertEqual(self.robot.events.read('observer'), [])
        self.robot.events.unsubscribe('observer')

    def test_simultaneous_drive_only_one_owner_and_snapshots_are_detached(self):
        self.mode(RobotMode.MOVEMENT)
        barrier = Barrier(2)

        def move(authority):
            barrier.wait(timeout=3)
            return self.drive(authority=authority).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(move, [OPERATOR, OTHER])), [200, 409])
        self.assertEqual(sum(item[0] == 'move' for item in self.adapter.commands), 1)
        snapshot = self.robot.status()
        snapshot['movement']['servos']['head_pan']['angle'] = -1
        snapshot['history'][0]['command'] = 'invalid'
        self.assertEqual(self.robot.status()['movement']['servos']['head_pan']['angle'], 90)
        self.assertEqual(self.robot.status()['history'][0]['command'], 'move')


@skipIf(flask is None, 'Flask not installed')
class ControllerWebTests(TestCase):
    def test_clients_share_state_and_cannot_spoof_authority(self):
        from web import create_app

        app = create_app(make_config(), testing=True)
        controller = app.extensions['eli_controller']
        self.assertIs(app.extensions['eli_utilities'], controller.utilities)
        self.assertIs(app.extensions['eli_robot'].controller, controller)

        def login():
            client = app.test_client()
            client.get('/login')
            with client.session_transaction() as session:
                token = session['csrf_token']
            response = client.post('/login', data={'password': PASSWORD, 'csrf_token': token})
            self.assertEqual(response.status_code, 302)
            with client.session_transaction() as session:
                return client, {'X-CSRF-Token': session['csrf_token']}

        a, headers_a = login()
        b, headers_b = login()
        base = '/api/v1'
        self.assertEqual(app.test_client().post(base + '/movement/emergency-stop').status_code, 401)
        self.assertEqual(a.post(base + '/movement/emergency-stop').status_code, 403)
        self.assertTrue(a.put(base + '/mode', json={'mode': 'movement'}, headers=headers_a).json['ok'])
        self.assertEqual(b.get(base + '/status').json['data']['mode'], 'movement')
        move = base + '/movement/move'
        self.assertTrue(a.post(move, json={'direction': 'right'}, headers=headers_a).json['ok'])
        self.assertEqual(b.post(move, json={'direction': 'left'}, headers=headers_b).status_code, 409)
        self.assertEqual(b.post(move, json={'direction': 'left', 'source': 'internal_system'},
                                headers=headers_b).status_code, 400)
        self.assertTrue(b.post(base + '/movement/emergency-stop', headers=headers_b).json['ok'])
        self.assertTrue(a.get(base + '/movement').json['data']['safety']['emergency_stop'])
        self.assertTrue(a.post(base + '/movement/reset-safety', headers=headers_a).json['ok'])
        self.assertFalse(b.get(base + '/movement').json['data']['moving'])
        self.assertTrue(a.put(base + '/volume', json={'volume': 21}, headers=headers_a).json['ok'])
        self.assertEqual(b.get(base + '/volume').json['data']['volume'], 21)
        self.assertTrue(b.put(base + '/expression', json={'expression': 'happy'}, headers=headers_b).json['ok'])
        self.assertEqual(a.get(base + '/expression').json['data']['expression'], 'happy')
