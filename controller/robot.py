"""Authoritative robot state and command execution."""
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from inspect import signature
import math
import threading
import time

from behavior.state_machine import State
from display.config import DEFAULT_EYE_COLOR, parse_color
from display.expressions import Expression
from productivity import UtilityError, UtilityService

from .adapters import MockAdapter, SafetyReading
from .commands import Action, Authority, Command, CommandResult, RobotMode, Source
from .errors import (LIVE, SIMULATED, UNAVAILABLE, AdapterError, ConflictError, ServiceError,
                     ValidationError)
from .events import EventStream
from .esp32 import MAX_PULSE_MS, validate_port

MODES = tuple(mode.value for mode in RobotMode)
MODE_ALIASES = {'focus': 'standby', 'manual': 'movement'}
DIRECTIONS = ('forward', 'backward', 'left', 'right')
SERVOS = {'head_pan': (0, 180), 'head_tilt': (45, 135)}
SERVO_HOME = 90
VOLUME_RANGE = (0, 100)
SPEED_RANGE = (0, 100)
DURATION_RANGE = (100, 5000)   # ms a single movement pulse lasts
DEFAULT_DURATION_MS = 500
ANIMATION_SPEED_RANGE = (0.5, 2.0)
SETTINGS_FIELDS = ('eye_color', 'animation_speed')
CONVERSATION_ACTIONS = ('start', 'end')
HISTORY_LENGTH = 20

NOT_LINKED = 'Subsystem is unavailable in the mock dashboard.'


def _hex(rgb):
    return '#{:02X}{:02X}{:02X}'.format(*rgb)


def _integer(value, field, low, high):
    """An int (or integral float) in [low, high]; booleans and strings are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f'{field} must be a number', field=field)
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise ValidationError(f'{field} must be a whole number', field=field)
        value = int(value)
    if not low <= value <= high:
        raise ValidationError(f'{field} must be between {low} and {high}', field=field)
    return value


def _number(value, field, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or isinstance(value, float) and not math.isfinite(value)):
        raise ValidationError(f'{field} must be a number', field=field)
    if not low <= value <= high:
        raise ValidationError(f'{field} must be between {low:g} and {high:g}', field=field)
    return round(float(value), 2)


def _choice(value, field, options):
    if not isinstance(value, str) or value.strip().lower() not in options:
        raise ValidationError(f"{field} must be one of: {', '.join(options)}", field=field)
    return value.strip().lower()


def _unavailable(detail=NOT_LINKED):
    return {'availability': UNAVAILABLE, 'detail': detail}


class RobotController:
    backend = 'mock'

    def __init__(self, clock=time.monotonic, wall_clock=time.time, *, adapters=None,
                 utilities=None, lease_seconds=2.0, managed_conversation=False, initial_volume=70,
                 serial_motors=None, motor_backend='mock'):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('Control lease must have a positive, finite duration')
        self._clock = clock
        self._wall = wall_clock
        self.adapters = adapters if adapters is not None else MockAdapter().ports()
        self._mock_motors = self.adapters.motors
        if motor_backend not in ('mock', 'esp32') or (motor_backend == 'esp32' and serial_motors is None):
            raise ValueError('Invalid motor backend')
        self._serial_motors = serial_motors
        self._serial_fault_count = self._serial_reset_count = 0
        self._serial_reset_pending = False
        if motor_backend == 'esp32':
            self.adapters = replace(self.adapters, motors=serial_motors)
        self.backend = 'mock' if self.adapters.simulated else 'adapter'
        self.utilities = utilities if utilities is not None else UtilityService(clock=clock)
        self.events = EventStream()
        self._condition = threading.Condition(threading.RLock())
        self._lock = self._condition
        self._lifecycle = threading.Lock()
        self._worker = None
        self._closed = False
        self._started = clock()
        self._mode = RobotMode.STANDBY
        self._mode_revision = 0
        self._expression = Expression.IDLE
        self._volume = _integer(initial_volume, 'volume', *VOLUME_RANGE)
        self._unmuted_volume = self._volume or 70
        self._speed = 80 if motor_backend == 'esp32' else 50
        self._eye_color = DEFAULT_EYE_COLOR
        self._animation_speed = 1.0
        self._servos = dict.fromkeys(SERVOS, SERVO_HOME)
        self._motion = None
        self._stop_unknown = not self.adapters.simulated
        self._lease_seconds = lease_seconds
        self._lease = None
        self._estop = False
        self._fault = None
        self._sensors = SafetyReading(None, None)
        self._conversation = False
        self._managed_conversation = managed_conversation
        self._behavior = State.IDLE
        self._generation = 0
        self._temporary_expression = None
        self._runtime_health = {}
        self._history = deque(maxlen=HISTORY_LENGTH)
        self._availability = self.capabilities()
        self._last_result = None
        self._errors = deque(maxlen=10)
        self._utility_cursor = 0

    def execute(self, command: Command, authority: Authority = Authority()) -> CommandResult:
        """Validate a known command. Source identifies origin, never permissions."""
        if (not isinstance(command, Command) or not isinstance(command.action, Action)
                or not isinstance(command.source, Source) or not isinstance(authority, Authority)):
            return CommandResult(False, 400, message='Invalid command or source', code='validation_error')
        with self._condition:
            try:
                if (command.source is not Source.DASHBOARD and self._uses_serial()
                        and command.action in (Action.MOVE, Action.SPEED, Action.RESET_SAFETY)):
                    raise ConflictError('Physical movement requires an authenticated manual control session')
                if command.action is Action.MOTOR_LINK and command.source is not Source.DASHBOARD:
                    raise ConflictError('Motor backend controls require the dashboard')
                if self._closed and command.action not in (Action.STATUS, Action.STOP, Action.EMERGENCY_STOP):
                    raise AdapterError('Controller is shut down')
                handler = self.status if command.action is Action.STATUS else getattr(
                    self, '_cmd_' + command.action.value)
                try:
                    args = dict(command.args)
                    if not isinstance(command.args, Mapping) or 'authority' in args:
                        raise TypeError
                    parameters = signature(handler)
                    if 'authority' in parameters.parameters:
                        args['authority'] = authority
                    parameters.bind(**args)
                except (TypeError, ValueError):
                    raise ValidationError('Invalid command arguments') from None
                if command.action not in (Action.STATUS, Action.STOP, Action.EMERGENCY_STOP):
                    self._tick(self._clock())
                data = handler(**args)
                result = CommandResult(True, 200, data)
            except UtilityError as exc:
                code = {400: 'validation_error', 404: 'not_found', 409: 'conflict'}[exc.status]
                result = CommandResult(False, exc.status, message=str(exc), code=code, field=exc.field)
            except ServiceError as exc:
                result = CommandResult(False, exc.status, message=exc.message,
                                       code=exc.code, field=exc.field)
            if command.action is not Action.STATUS or not result.ok:
                self._last_result = dict(action=command.action.value, source=command.source.value,
                                         ok=result.ok, status=result.status, message=result.message)
                if not result.ok:
                    self._errors.appendleft(dict(self._last_result))
            self._condition.notify()
            return result

    def startup(self):
        with self._lifecycle:
            with self._condition:
                if self._worker is not None:
                    return False
                self._closed = False
                if self._serial_motors:
                    self._serial_motors.startup()
                self._stop_motion('startup')
                self._mode = RobotMode.STANDBY
                self._mode_revision += 1
                self._temporary_expression = None
                for action, value in ((self._change_conversation, False),
                                      (self._change_expression, Expression.IDLE)):
                    try:
                        action(value, force=True)
                    except AdapterError as exc:
                        self._errors.appendleft(dict(action='startup', message=exc.message,
                                                     ok=False, status=exc.status))
                self.utilities.start_scheduler()
                self._worker = threading.Thread(target=self._run, name='eli-controller', daemon=True)
                self._worker.start()
                return True

    def shutdown(self):
        with self._lifecycle:
            with self._condition:
                if self._closed:
                    return
                self._closed = True
                if self._managed_conversation:
                    self._change_conversation(False)
                self._stop_motion('shutdown')
                self._condition.notify()
            if self._worker is not None:
                self._worker.join()
                self._worker = None
            self.utilities.stop_scheduler()
            if self._serial_motors:
                self._serial_motors.shutdown()
            self.events.close()

    def _run(self):
        with self._condition:
            while not self._closed:
                now = self._clock()
                self._tick(now)
                delay = 0.1 if self._motion else 0.25
                if self._motion:
                    delay = max(0, min(delay, self._motion['until'] - now, self._lease[1] - now))
                self._condition.wait(delay)

    def tick(self):
        """Process safety and utility events once, including with a fake clock."""
        with self._lock:
            if not self._closed:
                self._tick(self._clock())

    def _tick(self, now):
        if self._closed:
            return
        self._read_safety()
        if self._uses_serial():
            self._sync_serial_safety()
        caps = self.capabilities()
        for key, value in caps.items():
            if self._availability.get(key) != value:
                self.events.publish('availability_changed', subsystem=key, availability=value)
        self._availability = caps
        if self._motion:
            unsafe = self._unsafe_reason()
            if unsafe:
                self._fault_stop(unsafe)
            elif now >= self._lease[1]:
                self._stop_motion('lease_expired')
            elif now >= self._motion['until']:
                self._stop_motion('command_expired')
        if self._lease and now >= self._lease[1]:
            self._lease = None
        if self._managed_conversation:
            self._refresh_expression(now)
        feed = self.utilities.notifications(self._utility_cursor)
        for event in feed['events']:
            self.events.publish('utility_completed', stream=feed['stream'], observed_at=now, **event)
        self._utility_cursor = feed['cursor']

    def _read_safety(self):
        if self._uses_serial():
            self._sensors = SafetyReading(None, None)
            return
        try:
            sample = self.adapters.sensors.safety()
            if not isinstance(sample, SafetyReading):
                raise ValueError
            self._sensors = sample
        except Exception:
            self._sensors = SafetyReading(None, None)

    def _unsafe_reason(self):
        if self._uses_serial():
            status = self._serial_motors.snapshot()
            if status['connection'] != 'connected':
                return 'ESP32 serial link is unavailable'
            telemetry = status['telemetry']
            if telemetry and not telemetry['driver_ready']:
                return 'ESP32 driver reported a PWM fault'
            return None
        if self._sensors.cliff is True:
            return 'Cliff detected'
        if self._sensors.obstacle is True:
            return 'Obstacle detected'
        if self._sensors.obstacle is not False or self._sensors.cliff is not False:
            return 'Required safety telemetry is unavailable'
        sensor_availability = self._availability_of(self.adapters.sensors)
        if sensor_availability == UNAVAILABLE or (not self.adapters.simulated and
                                                   sensor_availability != LIVE):
            return 'Required safety telemetry is unavailable'
        if self._availability_of(self.adapters.motors) == UNAVAILABLE:
            return 'Motor subsystem is unavailable'
        return None

    def _record(self, command, **details):
        self._history.appendleft({'command': command, 'at': round(self._wall(), 3), **details})

    def _invoke(self, subsystem, adapter, method, *args):
        try:
            getattr(adapter, method)(*args)
        except AdapterError:
            self.events.publish('subsystem_failed', subsystem=subsystem, operation=method)
            raise
        except Exception:
            self.events.publish('subsystem_failed', subsystem=subsystem, operation=method)
            raise AdapterError(f'{subsystem.capitalize()} could not {method}') from None

    def _stop_motion(self, reason):
        self._motion = None
        # An ordinary pulse expiration retains ownership until the lease expires.
        if reason != 'command_expired':
            self._lease = None
        try:
            self._invoke('motors', self.adapters.motors, 'stop')
        except AdapterError as exc:
            self._stop_unknown = True
            self._latch_fault(exc.message)
            return False
        self._stop_unknown = False
        self.events.publish('movement_stopped', reason=reason)
        return True

    def _latch_fault(self, reason):
        if reason != self._fault:
            self._fault = reason
            self.events.publish('safety_fault', reason=reason)
            self._errors.appendleft(dict(action='safety', message=reason, ok=False))

    def _fault_stop(self, reason):
        self._latch_fault(reason)
        self._stop_motion('safety_fault')

    def _motion_state(self):
        if self._uses_serial():
            return {'moving': None, 'direction': None, 'remaining_ms': 0}
        if self._stop_unknown:
            return {'moving': None, 'direction': None, 'remaining_ms': 0}
        if not self._motion:
            return {'moving': False, 'direction': None, 'remaining_ms': 0}
        deadline = min(self._motion['until'], self._lease[1])
        remaining = max(0, math.ceil((deadline - self._clock()) * 1000))
        return {'moving': True, 'direction': self._motion['direction'], 'remaining_ms': remaining}

    def _robot_state(self):
        if self._managed_conversation:
            return self._behavior.name
        if not self.adapters.simulated:
            return None
        return (State.LISTENING if self._conversation else State.IDLE).name

    def _movement_block(self):
        if self._mode is not RobotMode.MOVEMENT:
            return 'Movement requires movement mode'
        if self._closed or (not self.adapters.simulated and self._worker is None):
            return 'Movement safety worker is inactive'
        if self._estop:
            return 'Emergency stop is latched'
        if self._uses_serial():
            if self._fault:
                return self._fault
            unsafe = self._unsafe_reason()
            if unsafe:
                return unsafe
            if self._motion or self._serial_motors.snapshot()['command_pending']:
                return 'Wait for the current pulse or serial command'
        return self._fault or self._unsafe_reason()

    def _uses_serial(self):
        return self._serial_motors is not None and self.adapters.motors is self._serial_motors

    def serial_ports(self):
        return self._serial_motors.ports() if self._serial_motors else []

    def _sync_serial_safety(self):
        status = self._serial_motors.snapshot()
        telemetry = status['telemetry']
        if status['fault_count'] != self._serial_fault_count:
            self._serial_fault_count = status['fault_count']
            self._serial_reset_pending = False
            self._motion = self._lease = None
            self._latch_fault(status['error'] or 'ESP32 communication failed')
        if telemetry and telemetry['estop']:
            self._estop = True
            self._motion = self._lease = None
        if status['reset_count'] != self._serial_reset_count:
            self._serial_reset_count = status['reset_count']
            if self._serial_reset_pending and telemetry and not telemetry['estop'] and telemetry['driver_ready']:
                self._estop = False
                self._fault = None
                self.events.publish('safety_reset')
            self._serial_reset_pending = False

    def _cmd_motor_link(self, operation, port=None, baud=None, backend=None, *, authority):
        if not self._serial_motors:
            raise ConflictError('Serial motor backend is unavailable')
        if not authority.can_move or not authority.owner:
            raise ConflictError('Motor setup requires an authenticated control session')
        status = self._serial_motors.snapshot()
        if operation == 'select':
            backend = _choice(backend, 'backend', ('mock', 'esp32'))
            if self._mode is not RobotMode.STANDBY or status['connection'] != 'disconnected':
                raise ConflictError('Disconnect the serial link and select Standby before switching backend')
            self.adapters = replace(self.adapters, motors=self._serial_motors if backend == 'esp32'
                                    else self._mock_motors)
            self._speed = 80 if backend == 'esp32' else 50
        elif not self._uses_serial():
            raise ConflictError('Select the ESP32 motor backend first')
        elif operation == 'connect':
            if self._mode is not RobotMode.STANDBY:
                raise ConflictError('Select Standby before connecting the ESP32')
            port = status['port'] if port is None else port
            if not validate_port(port):
                raise ValidationError('Select a valid serial port', field='port')
            if baud is not None:
                baud = _integer(baud, 'baud', 1200, 1000000)
            self._serial_motors.connect(port, baud)
        elif operation == 'disconnect':
            self._serial_reset_pending = False
            self._motion = self._lease = None
            self._serial_motors.disconnect()
        else:
            raise ValidationError('Invalid motor link operation', field='operation')
        return self._movement_snapshot()

    def _authorize_drive(self, authority):
        if not authority.can_move or not isinstance(authority.owner, str) or not authority.owner:
            raise ConflictError('Movement requires an authorized control session')
        if self._lease and self._lease[0] != authority.owner:
            raise ConflictError('Movement is reserved by another control session')

    def _change_expression(self, expression, *, force=False):
        changed = expression is not self._expression
        if not changed and not force:
            return
        self._invoke('eyes', self.adapters.eyes, 'expression', expression.value)
        self._expression = expression
        if changed:
            self.events.publish('expression_changed', expression=expression.value)

    def _change_conversation(self, active, *, force=False):
        changed = active != self._conversation
        if not changed and not force:
            return
        self._invoke('conversation', self.adapters.conversation, 'conversation', active)
        self._conversation = active
        if self._managed_conversation:
            self._generation += 1
            self._behavior = State.IDLE
        if changed:
            self.events.publish('conversation_changed', active=active)

    def interaction(self):
        with self._lock:
            return self._generation if (self._managed_conversation and not self._closed
                                         and self._conversation and self._mode is RobotMode.COMPANION) else None

    def wake_context(self):
        with self._lock:
            return self._mode.value, self._mode_revision, self._generation

    def activate_voice(self, expected_mode, revision, generation):
        """Atomically enter Companion and open a session for a current wake event."""
        with self._lock:
            if (self._closed or not self._managed_conversation or self._conversation
                    or self.wake_context() != (expected_mode, revision, generation)
                    or self._mode is RobotMode.MOVEMENT
                    or self._estop or self._fault):
                return False
            if self._mode is not RobotMode.COMPANION:
                self._cmd_mode(RobotMode.COMPANION, authority=Authority(can_wake=True))
            self._cmd_conversation('start')
            now = self._clock()
            self._temporary_expression = (Expression.EXCITED, now + 0.35)
            self._refresh_expression(now)
            return True

    def execute_voice(self, generation, operation, **args):
        """Accept an allowlisted intent only while its interaction still owns audio."""
        with self._lock:
            if generation is None or generation != self.interaction():
                return None
            return self.execute(Command(operation, Source.VOICE, args))

    def publish_behavior(self, generation, state, *, finished=False):
        with self._lock:
            if generation is None or generation != self.interaction():
                return False
            if finished:
                self._change_conversation(False)
            elif state is not self._behavior:
                self._behavior = state
                self.events.publish('behavior_changed', state=state.name)
            self._refresh_expression(self._clock())
            return True

    def update_runtime_health(self, component, availability, detail):
        with self._lock:
            self._runtime_health[component] = dict(availability=availability, detail=detail)

    def can_announce(self, in_sleep=False):
        with self._lock:
            return (not self._closed and not self._conversation and not self._estop and not self._fault
                    and (self._mode in (RobotMode.STANDBY, RobotMode.COMPANION)
                         or in_sleep and self._mode is RobotMode.SLEEP))

    def _refresh_expression(self, now):
        from display.robot_eyes import STATE_EXPRESSIONS

        if self._temporary_expression and now >= self._temporary_expression[1]:
            self._temporary_expression = None
        expression = STATE_EXPRESSIONS[self._behavior]
        if self._temporary_expression:
            expression = self._temporary_expression[0]
        if self._estop or self._fault:
            expression = Expression.CONFUSED
        if self._mode is RobotMode.SLEEP:
            expression = Expression.SLEEPING
        self._change_expression(expression)

    def _availability_of(self, adapter):
        return getattr(adapter, 'availability', SIMULATED if self.adapters.simulated else UNAVAILABLE)

    # ----- status -----
    def status(self):
        with self._lock:
            self._tick(self._clock())
            utilities = self.utilities.snapshot()
            runtime = deepcopy(self._runtime_health)
            if self._uses_serial():
                runtime['hardware'] = dict(availability=self._serial_motors.availability,
                                           detail='ESP32 serial motors; obstacle and cliff sensors unavailable')
            return {
                'name': 'Eli',
                'backend': self.backend,
                'mode': self._mode.value,
                'state': self._robot_state(),
                'expression': self._expression.value,
                'volume': self._volume,
                'conversation_active': self._conversation,
                'movement': self._movement_snapshot(),
                'connection': {'controller': 'stopped' if self._closed else 'available',
                               'hardware': (self._serial_motors.snapshot()['connection']
                                            if self._uses_serial() else 'unavailable')},
                'state_provenance': 'simulated' if self.adapters.simulated else 'requested',
                'active_utilities': {name: utilities[name] for name in ('timer', 'stopwatch', 'pomodoro')
                                     if utilities[name]['state'] in ('running', 'paused')},
                'last_command': deepcopy(self._last_result),
                'errors': deepcopy(list(self._errors)),
                'uptime_s': int(self._clock() - self._started),
                'capabilities': self.capabilities(),
                'history': deepcopy(list(self._history)),
                **({'runtime': runtime} if self._managed_conversation else {}),
            }

    def capabilities(self):
        return {
            'eyes': self._runtime_health.get('eyes', {}).get(
                'availability', self._availability_of(self.adapters.eyes)),
            'volume': self._runtime_health.get('audio', {}).get(
                'availability', self._availability_of(self.adapters.audio)),
            'movement': self._availability_of(self.adapters.motors),
            'servos': UNAVAILABLE if self._uses_serial() else self._availability_of(self.adapters.servos),
            'conversation': self._runtime_health.get('conversation', {}).get(
                'availability', self._availability_of(self.adapters.conversation)),
            'camera': self._runtime_health.get('camera', {}).get('availability', UNAVAILABLE),
            'microphone': self._runtime_health.get('microphone', {}).get('availability', UNAVAILABLE),
            'face_recognition': UNAVAILABLE,
            'ai': self._runtime_health.get('ai', {}).get('availability', UNAVAILABLE),
            'esp32': self._serial_motors.availability if self._uses_serial() else UNAVAILABLE,
            'safety_sensors': (self._availability_of(self.adapters.sensors)
                               if self._sensors.obstacle is not None and self._sensors.cliff is not None
                               else UNAVAILABLE),
        }

    def get_mode(self):
        with self._lock:
            return {'mode': self._mode.value, 'modes': list(MODES), 'availability': SIMULATED
                    if self.adapters.simulated else LIVE}

    def _cmd_mode(self, mode, *, authority):
        if isinstance(mode, str):
            mode = MODE_ALIASES.get(mode.strip().lower(), mode)
        mode = RobotMode(_choice(mode, 'mode', MODES))
        previous = self._mode
        if mode is not previous:
            if (previous is RobotMode.SLEEP and not authority.can_wake
                    or mode is RobotMode.MOVEMENT and not authority.can_move):
                raise ConflictError('This mode transition requires authorization')
            if previous is RobotMode.MOVEMENT or mode is RobotMode.SLEEP:
                if not self._stop_motion('mode_transition') and not self._uses_serial():
                    raise AdapterError('Motor stop could not be confirmed')
            self._change_conversation(False)
            self._mode = mode
            if self._managed_conversation:
                self._temporary_expression = None
                self._refresh_expression(self._clock())
            else:
                try:
                    self._change_expression(Expression.SLEEPING if mode is RobotMode.SLEEP else Expression.IDLE)
                except AdapterError:
                    self._mode = previous
                    raise
            self._record('mode', mode=mode.value, previous=previous.value)
            self._mode_revision += 1
            self.events.publish('mode_changed', mode=mode.value, previous=previous.value)
        return dict(self.get_mode(), previous=previous.value, expression=self._expression.value,
                    conversation_active=self._conversation)

    # ----- eyes -----
    def get_expression(self):
        with self._lock:
            return {'expression': self._expression.value,
                    'expressions': [e.value for e in Expression], 'availability': self.capabilities()['eyes']}

    def _cmd_expression(self, expression):
        if not isinstance(expression, str):
            raise ValidationError('expression must be a string', field='expression')
        try:
            parsed = Expression.parse(expression)
        except ValueError as exc:
            raise ValidationError(str(exc), field='expression') from None
        with self._lock:
            if self._mode is RobotMode.SLEEP and parsed is not Expression.SLEEPING:
                raise ConflictError('Eli is asleep; change the mode before setting an expression')
            if self._managed_conversation:
                self._temporary_expression = (parsed, self._clock() + 3)
                self._refresh_expression(self._clock())
            else:
                self._change_expression(parsed)
            self._record('expression', expression=parsed.value)
            return self.get_expression()

    # ----- audio -----
    def get_volume(self):
        with self._lock:
            return {'volume': self._volume, 'min': VOLUME_RANGE[0], 'max': VOLUME_RANGE[1],
                    'availability': self.capabilities()['volume'],
                    **({'effect': self.adapters.audio.volume_effect, 'restore': self._unmuted_volume}
                       if self._managed_conversation else {})}

    def _cmd_volume(self, volume):
        volume = _integer(volume, 'volume', *VOLUME_RANGE)
        with self._lock:
            self._invoke('audio', self.adapters.audio, 'volume', volume)
            self._volume = volume
            if volume:
                self._unmuted_volume = volume
            self.events.publish('volume_changed', volume=volume)
            self._record('volume', volume=volume)
            return self.get_volume()

    # ----- settings -----
    def get_settings(self):
        with self._lock:
            return {
                'eye_color': _hex(self._eye_color),
                'animation_speed': self._animation_speed,
                'animation_speed_range': list(ANIMATION_SPEED_RANGE),
                'volume': self._volume,
                'expression': self._expression.value,
                'mode': self._mode.value,
                'availability': self.capabilities()['eyes'],
            }

    def _cmd_settings(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValidationError(f"Provide at least one of: {', '.join(SETTINGS_FIELDS)}")
        unknown = sorted(set(changes) - set(SETTINGS_FIELDS))
        if unknown:
            raise ValidationError(f"Unknown setting(s): {', '.join(unknown)}", field=unknown[0])
        updates = {}
        if 'eye_color' in changes:
            value = changes['eye_color']
            if not isinstance(value, str):
                raise ValidationError('eye_color must be #RRGGBB', field='eye_color')
            try:
                updates['eye_color'] = parse_color(value)
            except ValueError:
                raise ValidationError('eye_color must be #RRGGBB', field='eye_color') from None
        if 'animation_speed' in changes:
            updates['animation_speed'] = _number(changes['animation_speed'], 'animation_speed',
                                                 *ANIMATION_SPEED_RANGE)
        with self._lock:
            self._invoke('eyes', self.adapters.eyes, 'appearance',
                         updates.get('eye_color', self._eye_color),
                         updates.get('animation_speed', self._animation_speed))
            if 'eye_color' in updates:
                self._eye_color = updates['eye_color']
            if 'animation_speed' in updates:
                self._animation_speed = updates['animation_speed']
            self._record('settings', **{k: (_hex(v) if k == 'eye_color' else v)
                                        for k, v in updates.items()})
            return self.get_settings()

    # ----- movement -----
    def _movement_snapshot(self):
        block = self._movement_block()
        capabilities = self.capabilities()
        return {
            **self._motion_state(), 'speed': self._speed,
            'servos': {name: {'angle': angle, 'min': SERVOS[name][0], 'max': SERVOS[name][1]}
                       for name, angle in self._servos.items()},
            'directions': list(DIRECTIONS), 'enabled': block is None,
            'availability': capabilities['movement'],
            'servo_availability': capabilities['servos'],
            'serial': (self._serial_motors.snapshot() if self._serial_motors else None),
            'motor_backend': 'esp32' if self._uses_serial() else 'mock',
            'physical_motion': 'unavailable',
            'safety': {'emergency_stop': self._estop, 'fault': self._fault,
                       'blocked_reason': block, 'control_leased': self._lease is not None,
                       'obstacle': self._sensors.obstacle, 'cliff': self._sensors.cliff,
                       'availability': capabilities['safety_sensors']},
        }

    def movement(self):
        with self._lock:
            self._tick(self._clock())
            return self._movement_snapshot()

    def _cmd_move(self, direction, duration_ms=None, *, authority):
        direction = _choice(direction, 'direction', DIRECTIONS)
        duration = (250 if self._uses_serial() else DEFAULT_DURATION_MS) if duration_ms is None else (
            _integer(duration_ms, 'duration_ms', DURATION_RANGE[0],
                     MAX_PULSE_MS if self._uses_serial() else DURATION_RANGE[1]))
        self._authorize_drive(authority)
        block = self._movement_block()
        if block:
            if (self._mode is RobotMode.MOVEMENT and not self._fault and not self._estop
                    and block == self._unsafe_reason()):
                self._fault_stop(block)
            raise ConflictError(block)
        if self._speed == 0:
            raise ConflictError('Motor speed is 0; raise the speed before moving')
        now = self._clock()
        # The adapter receives a hard limit no later than either server deadline.
        limit = min(duration, max(1, int(self._lease_seconds * 1000)))
        try:
            self._invoke('motors', self.adapters.motors, 'move', direction, self._speed, limit)
        except AdapterError:
            self._fault_stop('Motor move failed')
            raise
        self._motion = {'direction': direction, 'until': now + (limit if self._uses_serial() else duration) / 1000}
        self._lease = (authority.owner, now + self._lease_seconds)
        self._record('move', direction=direction, duration_ms=limit, speed=self._speed)
        self.events.publish('movement_requested', direction=direction, duration_ms=limit, speed=self._speed)
        return dict(self.movement(), duration_ms=limit)

    def _cmd_speed(self, speed, *, authority):
        speed = _integer(speed, 'speed', *SPEED_RANGE)
        if self._lease and speed != 0:
            self._authorize_drive(authority)
        if speed == 0 and not self._stop_motion('zero_speed'):
            raise AdapterError('Motor stop could not be confirmed')
        self._speed = speed
        self._record('speed', speed=speed)
        return self.movement()

    def _cmd_stop(self):
        self._record('stop')
        if not self._stop_motion('operator_stop'):
            raise AdapterError('Motor stop could not be confirmed')
        if self._uses_serial() and self._serial_motors.snapshot()['connection'] == 'disconnected':
            raise AdapterError('STOP could not be sent; ESP32 is disconnected')
        return self.movement()

    def _cmd_emergency_stop(self):
        self._estop = True
        self._serial_reset_pending = False
        self.events.publish('emergency_stop', active=True)
        if self._uses_serial():
            self._motion = self._lease = None
            self._invoke('motors', self._serial_motors, 'emergency_stop')
            return self._movement_snapshot()
        if not self._stop_motion('emergency_stop'):
            raise AdapterError('Emergency stop requested; motor stop could not be confirmed')
        return self.movement()

    def _cmd_reset_safety(self, *, authority):
        if not authority.can_move:
            raise ConflictError('Safety reset requires an authorized operator')
        block = self._unsafe_reason()
        if block:
            raise ConflictError(block)
        if self._uses_serial():
            self._motion = self._lease = None
            self._serial_motors.reset_safety()
            self._serial_reset_pending = True
            return self._movement_snapshot()
        if not self._stop_motion('safety_reset'):
            raise AdapterError('Motor stop could not be confirmed')
        self._estop = False
        self._fault = None
        self.events.publish('safety_reset')
        return self.movement()

    def _cmd_servo(self, servo, angle, *, authority):
        servo = _choice(servo, 'servo', tuple(SERVOS))
        angle = _integer(angle, 'angle', *SERVOS[servo])
        self._authorize_drive(authority)
        if self._uses_serial():
            raise ConflictError('The ESP32 motor backend does not control servos')
        block = self._movement_block()
        if block:
            raise ConflictError(block)
        self._invoke('servos', self.adapters.servos, 'servo', servo, angle)
        self._servos[servo] = angle
        self._record('servo', servo=servo, angle=angle)
        return self.movement()

    # ----- companion -----
    def companion(self):
        with self._lock:
            return {
                'conversation': {'active': self._conversation, 'state': self._robot_state(),
                                 'availability': self.capabilities()['conversation'],
                                 'detail': ('Unified runtime conversation' if self._managed_conversation
                                            else 'Simulated conversation' if self.adapters.simulated
                                            else 'Conversation adapter state')},
                'face_recognition': _unavailable(),
                'microphone': deepcopy(self._runtime_health.get('microphone',
                                       _unavailable('Microphone capture is unavailable.'))),
                'ai': deepcopy(self._runtime_health.get('ai',
                               _unavailable('Ollama connection status is unavailable.'))),
                'integrated': self._managed_conversation,
            }

    def _cmd_conversation(self, action):
        action = _choice(action, 'action', CONVERSATION_ACTIONS)
        if action == 'start' and self._mode is not RobotMode.COMPANION:
            raise ConflictError(f'Conversations require companion mode (current mode: {self._mode})')
        if (action == 'start' and self._managed_conversation and
                self._runtime_health.get('lifecycle', {}).get('detail') != 'Running'):
            raise ConflictError('Conversation runtime is not running')
        self._change_conversation(action == 'start')
        if self._managed_conversation:
            self._refresh_expression(self._clock())
        elif self._mode is RobotMode.COMPANION:
            self._change_expression(Expression.LISTENING if self._conversation else Expression.IDLE)
        self._record('conversation', action=action)
        return self.companion()

    def _cmd_utility(self, name, action, duration=None):
        if not isinstance(name, str):
            raise ValidationError('Utility name must be a string', field='name')
        result = self.utilities.act(name, action, duration=duration)
        self._tick(self._clock())
        return result

    def _cmd_utility_settings(self, name, settings):
        if not isinstance(name, str) or not isinstance(settings, dict):
            raise ValidationError('Expected a utility name and settings object')
        result = self.utilities.configure(name, settings)
        self._tick(self._clock())
        return result

    # ----- perception and diagnostics -----
    def perception(self):
        return {
            'camera': {**_unavailable('Camera preview is not available. The dashboard never '
                                      'streams or stores camera frames.'), 'preview': None},
            'recognition': {**_unavailable(), 'identity': None},
        }
