"""Serial protocol regression tests. No COM or /dev device is opened."""
import json
from types import SimpleNamespace
from threading import Event
from time import monotonic
from unittest import TestCase
from unittest.mock import Mock, patch

from controller import Action, Authority, Command, RobotController, Source
from controller.esp32 import ESP32MotorAdapter, SERIAL_STARTUP_SECONDS, open_serial
from test_web import FakeClock, PASSWORD, make_config

OPERATOR = Authority('a', can_move=True, can_wake=True)
OTHER = Authority('b', can_move=True, can_wake=True)


class Firmware:
    def __init__(self):
        self.commands = []
        self.protocol = 1
        self.generation = 0
        self.session = None
        self.last_id = 0
        self.heartbeat = None
        self.watchdog = 'disarmed'
        self.end = 0
        self.estop = False
        self.direction = 'none'
        self.speed = 0
        self.uptime = 2000
        self.fail_next = None
        self.stale = False
        self.hold_status = False
        self.status_started = Event()
        self.release_status = Event()

    def stop(self):
        self.end = 0
        self.direction = 'none'
        self.speed = 0
        self.session = self.heartbeat = None
        self.watchdog = 'disarmed'

    def receive(self, request):
        now = monotonic()
        if self.heartbeat is not None and now - self.heartbeat >= .75:
            self.stop()
            self.watchdog = 'tripped'
        if self.end and now >= self.end:
            self.end = self.speed = 0
            self.direction = 'none'
        self.commands.append(request)
        cmd = request['cmd']
        result = dict(id=request['id'], ok=True)
        if cmd == 'hello':
            self.stop()
            self.generation += 1
            self.session = f'{self.generation:016x}'
            self.last_id = 0
            self.heartbeat = now
            result.update(session=self.session, protocol_version=self.protocol, firmware_version='1.0.0')
        elif cmd in ('stop', 'estop'):
            self.stop()
            self.estop |= cmd == 'estop'
        elif cmd == 'status':
            duty = round(self.speed * 255 / 100)
            result.update(protocol_version=self.protocol, firmware_version='1.0.0', uptime_ms=self.uptime,
                          communication='ready' if self.session else 'disconnected',
                          direction=self.direction, left_pwm=duty, right_pwm=duty,
                          remaining_ms=max(0, int((self.end-now)*1000)), estop=self.estop,
                          watchdog=self.watchdog, driver_ready=True,
                          last_error=None, sensors=dict(cliff='unavailable', obstacle='unavailable'))
            self.uptime += 1
        elif request.get('session') != self.session or self.session is None:
            result.update(ok=False, error='session_invalid')
        elif request['id'] <= self.last_id:
            result.update(ok=False, error='duplicate_id')
        else:
            self.last_id = request['id']
            if cmd == 'heartbeat':
                self.heartbeat, self.watchdog = now, 'armed'
            elif cmd == 'reset_safety':
                self.stop()
                self.estop = False
            elif cmd == 'move':
                if self.estop:
                    result.update(ok=False, error='estop_latched')
                else:
                    assert 1 <= request['duration_ms'] <= 1000
                    self.speed = request['speed']
                    self.direction = request['direction']
                    self.end = now + request['duration_ms'] / 1000
        result['state'] = 'estopped' if self.estop else 'moving' if self.end else 'stopped'
        return result


class Transport:
    def __init__(self, firmware):
        self.board = firmware
        self.output = b''
        self.delayed = b''
        self.closed = False

    def write(self, packet):
        if self.closed:
            raise OSError('port closed')
        request = json.loads(packet)
        board = self.board
        result = board.receive(request)
        fail, board.fail_next = board.fail_next, None
        if fail == 'disconnect':
            self.closed = True
            raise OSError('cable disconnected')
        data = b'invalid frame\n' if fail == 'malformed' else (json.dumps(result)+'\n').encode()
        if board.stale:
            board.stale = False
            self.output += (json.dumps(dict(id=request['id']-1, ok=True, state='stopped'))+'\n').encode()
        if request['cmd'] == 'status' and board.hold_status:
            board.hold_status = False
            self.delayed = data
            board.status_started.set()
        else:
            self.output += data
        return len(packet)

    def read(self, length):
        if self.delayed and self.board.release_status.is_set():
            self.output += self.delayed
            self.delayed = b''
        if not self.output:
            Event().wait(.002)
        data, self.output = self.output[:length], self.output[length:]
        return data

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        self.output = self.delayed = b''


class SerialTests(TestCase):
    def setUp(self):
        self.firmware = Firmware()
        self.opened = []
        self.adapter = ESP32MotorAdapter('COM9', opener=self.open,
                                         startup_seconds=0,
                                         discover=lambda: [dict(port='COM9', description='CP210x')])
        self.adapter.startup()
        self.addCleanup(self.adapter.shutdown)

    def open(self, port, baud):
        self.opened.append((port, baud))
        return Transport(self.firmware)

    def wait(self, predicate):
        until = monotonic() + 2
        while monotonic() < until:
            if predicate():
                return
            Event().wait(.003)
        self.fail(f'Serial condition timed out: {self.adapter.snapshot()}')

    def connect(self):
        self.adapter.connect('COM9')
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'connected')

    def ack(self, command, *, after=0):
        self.wait(lambda: (ack := self.adapter.snapshot()['last_ack']) is not None
                  and ack['command'] == command and ack['id'] > after
                  and not self.adapter.snapshot()['command_pending'])
        return self.adapter.snapshot()['last_ack']['id']

    def test_handshake_direction_and_heartbeat_do_not_extend_pulse(self):
        self.connect()
        self.assertEqual(self.opened, [('COM9', 115200)])
        self.assertEqual(self.adapter.ports()[0]['description'], 'CP210x')
        self.assertEqual([item['cmd'] for item in self.firmware.commands[:3]], ['hello', 'heartbeat', 'status'])
        self.assertEqual(self.adapter.snapshot()['telemetry']['protocol_version'], 1)
        previous = 0
        for direction in ('forward', 'backward', 'left', 'right'):
            self.adapter.move(direction, 80, 800)
            previous = self.ack('move', after=previous)
            self.assertEqual(self.firmware.direction, direction)
        due = self.firmware.end
        self.wait(lambda: any(req['id'] > previous and req['cmd'] == 'heartbeat'
                              for req in self.firmware.commands))
        self.assertEqual(self.firmware.end, due)
        self.assertEqual(len({req['id'] for req in self.firmware.commands}), len(self.firmware.commands))

    def test_wrong_protocol_cannot_move(self):
        self.firmware.protocol = 2
        self.adapter.connect('COM9')
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('Incompatible', self.adapter.snapshot()['error'])
        self.assertNotIn('move', [item['cmd'] for item in self.firmware.commands])

    def test_stop_estop_prioritize_queue_and_reset_requires_new_session(self):
        self.connect()
        for command in ('stop', 'estop'):
            self.firmware.hold_status = True
            self.firmware.status_started.clear()
            self.firmware.release_status.clear()
            self.assertTrue(self.firmware.status_started.wait(1))
            self.adapter.move('forward', 80, 250)
            (self.adapter.stop if command == 'stop' else self.adapter.emergency_stop)()
            self.firmware.release_status.set()
            self.ack(command)
            self.assertNotIn('move', [item['cmd'] for item in self.firmware.commands])
        self.assertTrue(self.firmware.estop)
        self.adapter.reset_safety()
        self.ack('reset_safety')
        self.assertFalse(self.firmware.estop)
        self.assertIsNone(self.firmware.session)
        self.assertEqual(self.adapter.snapshot()['reset_count'], 1)

    def test_stale_reply_ignored_and_malformed_reply_requires_explicit_reconnect(self):
        self.connect()
        self.firmware.stale = True
        self.adapter.move('left', 55, 200)
        self.ack('move')
        self.firmware.fail_next = 'malformed'
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('Malformed', self.adapter.snapshot()['error'])
        self.assertIsNone(self.adapter.snapshot()['telemetry'])
        sent = sum(item['cmd'] == 'move' for item in self.firmware.commands)
        self.connect()
        self.assertEqual(sum(item['cmd'] == 'move' for item in self.firmware.commands), sent)
        self.assertEqual(len(self.opened), 2)

    def test_cable_loss_watchdog_and_firmware_reset(self):
        self.connect()
        self.firmware.fail_next = 'disconnect'
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('cable disconnected', self.adapter.snapshot()['error'])
        self.connect()
        self.firmware.heartbeat -= 1
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn(self.firmware.watchdog, ('tripped', 'disarmed'))
        self.connect()
        self.firmware.uptime = 0
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('firmware reset', self.adapter.snapshot()['error'])
        self.assertNotIn('move', [item['cmd'] for item in self.firmware.commands])


class SerialStartupTests(TestCase):
    wait = SerialTests.wait
    ack = SerialTests.ack

    def setUp(self):
        self.firmware = Firmware()
        self.opened = Event()
        self.transports = []
        self.fail_flush = False

    def start(self, seconds=SERIAL_STARTUP_SECONDS):
        self.adapter = ESP32MotorAdapter('COM9', opener=self.open, startup_seconds=seconds)
        self.addCleanup(self.adapter.shutdown)
        self.adapter.startup()
        self.adapter.connect('COM9')
        self.assertTrue(self.opened.wait(1))

    def open(self, port, baud):
        transport = Transport(self.firmware)
        transport.output = b'ESP32 startup\n{"incomplete":'
        transport.opened_at = monotonic()
        reset_input_buffer = transport.reset_input_buffer

        def flush():
            transport.flushed_at = monotonic()
            if self.fail_flush:
                raise OSError('startup buffer reset failed')
            reset_input_buffer()

        transport.reset_input_buffer = flush
        self.transports.append(transport)
        self.opened.set()
        return transport

    def test_wait_then_flush_stale_startup_data_before_strict_negotiation(self):
        self.start(.04)
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'connected')
        transport = self.transports[0]
        self.assertGreaterEqual(transport.flushed_at - transport.opened_at, .039)
        self.assertEqual([item['cmd'] for item in self.firmware.commands], ['hello', 'heartbeat', 'status'])

    def test_disconnect_cancels_initialization(self):
        self.start()
        self.adapter.disconnect()
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'disconnected')
        self.assertTrue(self.transports[0].closed)
        self.assertFalse(hasattr(self.transports[0], 'flushed_at'))
        self.assertEqual([item['cmd'] for item in self.firmware.commands], ['stop'])

    def test_shutdown_cancels_initialization_and_honors_pending_estop(self):
        self.start()
        self.adapter.emergency_stop()
        self.adapter.shutdown()
        self.assertFalse(self.adapter._thread.is_alive())
        self.assertTrue(self.transports[0].closed)
        self.assertTrue(self.firmware.estop)
        self.assertEqual([item['cmd'] for item in self.firmware.commands], ['estop'])

    def test_buffer_reset_failure_closes_port_and_reconnects_without_replay(self):
        self.start(.04)
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'connected')
        self.adapter.move('left', 50, 250)
        self.ack('move')
        self.adapter.disconnect()
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'disconnected')
        self.fail_flush = True
        self.adapter.connect('COM9')
        self.wait(lambda: self.transports[-1].closed and self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('buffer reset failed', self.adapter.snapshot()['error'])
        self.fail_flush = False
        self.adapter.connect('COM9')
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'connected')
        self.assertEqual([item['cmd'] for item in self.firmware.commands].count('move'), 1)
        self.assertEqual(self.firmware.speed, 0)

    def test_malformed_response_after_flush_is_rejected(self):
        self.firmware.fail_next = 'malformed'
        self.start(.01)
        self.wait(lambda: self.transports[0].closed and self.adapter.snapshot()['connection'] == 'fault')
        self.assertIn('Malformed', self.adapter.snapshot()['error'])

    def test_failed_open_closes_partially_initialized_handle(self):
        link = Mock()
        link.open.side_effect = OSError('open failed')
        fake_serial = SimpleNamespace(Serial=Mock(return_value=link))
        with patch.dict('sys.modules', {'serial': fake_serial}):
            with self.assertRaisesRegex(OSError, 'open failed'):
                open_serial('COM9', 115200)
        link.close.assert_called_once()


class ControllerSerialTests(TestCase):
    setUp = SerialTests.setUp
    wait = SerialTests.wait
    connect = SerialTests.connect
    open = SerialTests.open
    ack = SerialTests.ack

    def controller(self):
        self.clock = FakeClock()
        robot = RobotController(clock=self.clock, serial_motors=self.adapter, motor_backend='esp32')
        robot.startup()
        self.addCleanup(robot.shutdown)
        return robot

    def command(self, robot, op, *, authority=OPERATOR, source=Source.DASHBOARD, **args):
        return robot.execute(Command(op, source, args), authority)

    def test_manual_owner_pulse_expiration_mode_and_estop(self):
        robot = self.controller()
        self.connect()
        self.assertFalse(self.command(robot, Action.MOVE, direction='forward').ok)
        self.assertTrue(self.command(robot, Action.MODE, mode='movement').ok)
        self.assertTrue(robot.movement()['enabled'])
        self.assertFalse(self.command(robot, Action.MOVE, source=Source.VOICE, direction='forward').ok)
        self.assertFalse(self.command(robot, Action.MOVE, source=Source.AUTONOMOUS, direction='forward').ok)
        self.assertEqual(self.command(robot, Action.MOVE, direction='forward', duration_ms=1001).status, 400)
        result = self.command(robot, Action.MOVE, direction='forward')
        self.assertTrue(result.ok)
        self.assertEqual(result.state['duration_ms'], 250)
        self.assertIsNone(result.state['moving'])
        self.assertIsNone(result.state['safety']['obstacle'])
        self.ack('move')
        competing = self.command(robot, Action.MOVE, authority=OTHER, direction='forward')
        self.assertFalse(competing.ok)
        self.assertIn('reserved', competing.message)
        self.clock.t += 3
        robot.tick()
        self.ack('stop')
        self.assertFalse(robot.movement()['safety']['control_leased'])
        self.assertTrue(self.command(robot, Action.EMERGENCY_STOP).ok)
        self.ack('estop')
        self.assertTrue(robot.movement()['safety']['emergency_stop'])
        self.assertFalse(self.command(robot, Action.MOVE, direction='forward').ok)
        self.assertTrue(self.command(robot, Action.RESET_SAFETY).ok)
        self.ack('reset_safety')
        self.assertFalse(robot.movement()['safety']['emergency_stop'])
        self.assertTrue(self.command(robot, Action.MOVE, direction='forward').ok)
        previous = self.ack('move')
        self.assertTrue(self.command(robot, Action.MODE, mode='standby').ok)
        self.ack('stop', after=previous)
        self.assertFalse(robot.movement()['enabled'])

    def test_repeated_manual_pulses_after_stop_renew_session_without_replaying(self):
        robot = self.controller()
        self.connect()
        self.assertTrue(self.command(robot, Action.MODE, mode='movement').ok)
        previous = 0
        for direction in ('forward', 'left', 'backward'):
            self.assertTrue(robot.movement()['enabled'])
            self.assertTrue(self.command(robot, Action.MOVE, direction=direction).ok)
            move_id = self.ack('move', after=previous)
            self.assertTrue(self.command(robot, Action.STOP).ok)
            previous = self.ack('stop', after=move_id)
            self.assertIsNone(self.firmware.session)
        moves = [packet for packet in self.firmware.commands if packet['cmd'] == 'move']
        self.assertEqual([packet['direction'] for packet in moves], ['forward', 'left', 'backward'])
        self.assertEqual(len({packet['session'] for packet in moves}), 3)
        for move in moves:
            self.assertTrue(any(packet['cmd'] == 'heartbeat' and packet['session'] == move['session']
                                and packet['id'] < move['id'] for packet in self.firmware.commands))

    def test_dashboard_connect_and_mock_selection_are_authenticated(self):
        from web import create_app
        robot = self.controller()
        client = create_app(make_config(), controller=robot, testing=True).test_client()
        url = '/api/v1/movement/link'
        self.assertEqual(client.post(url, json={'operation': 'connect'}).status_code, 401)
        client.get('/login')
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.assertEqual(client.post('/login', data={'password': PASSWORD, 'csrf_token': token}).status_code, 302)
        self.assertEqual(client.post(url, json={'operation': 'connect'}).status_code, 403)
        with client.session_transaction() as session:
            token = session['csrf_token']
        headers = {'X-CSRF-Token': token}
        self.assertEqual(client.get('/api/v1/movement/ports').json['data']['ports'][0]['port'], 'COM9')
        self.assertEqual(client.post(url, headers=headers, json={'operation': 'connect'}).status_code, 200)
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'connected')
        self.assertEqual(client.post(url, headers=headers, json={'operation': 'disconnect'}).status_code, 200)
        self.wait(lambda: self.adapter.snapshot()['connection'] == 'disconnected')
        response = client.post(url, headers=headers, json={'operation': 'select', 'backend': 'mock'})
        self.assertEqual(response.json['data']['motor_backend'], 'mock')
        self.assertEqual(response.json['data']['availability'], 'simulated')
