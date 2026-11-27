"""In-memory robot used by the dashboard in Phase 1.

``MockRobotService`` validates and remembers every request but drives nothing:
no GPIO, SPI, camera, microphone, motors or ESP32 are opened, and nothing from
the AI pipeline is imported. Like ``hardware.mock_hardware.MockMotors`` it only
records what it *would* do. Expression names and eye colour parsing reuse the
pure-data ``display.expressions`` / ``display.config`` modules so the dashboard
accepts exactly what the real eye engine accepts.

Mode rules (simulated, but enforced so the UI behaves like the real robot):
  companion  default; conversations may be started
  focus      productivity mode; any conversation ends
  manual     movement and servo commands are accepted
  sleep      motion stops, eyes show ``sleeping``; leaving sleep returns to ``idle``
Movement and servo commands outside ``manual`` mode raise ``ConflictError``.
"""
import math
import threading
import time
from collections import deque

from behavior.state_machine import State
from display.config import DEFAULT_EYE_COLOR, parse_color
from display.expressions import Expression
from web.services.base import (SIMULATED, UNAVAILABLE, ConflictError, RobotService,
                               ValidationError)
from web.services.host_metrics import HostMetrics

MODES = ('companion', 'focus', 'manual', 'sleep')
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

# The parts of Eli the dashboard is not linked to yet (Phase 1).
NOT_LINKED = 'Not linked to the dashboard in Phase 1.'


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
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


class MockRobotService(RobotService):
    backend = 'mock'

    def __init__(self, clock=time.monotonic, wall_clock=time.time, metrics=None):
        self._clock = clock
        self._wall = wall_clock
        self._metrics = metrics or HostMetrics()
        self._lock = threading.RLock()
        self._started = clock()
        self._mode = 'companion'
        self._expression = Expression.IDLE
        self._volume = 70
        self._speed = 50
        self._eye_color = DEFAULT_EYE_COLOR
        self._animation_speed = 1.0
        self._servos = {name: SERVO_HOME for name in SERVOS}
        self._motion = None  # {'direction', 'until'} while a simulated pulse runs
        self._conversation = False
        self._history = deque(maxlen=HISTORY_LENGTH)

    # ----- helpers (call with the lock held) -----
    def _record(self, command, **details):
        self._history.appendleft({'command': command, 'at': round(self._wall(), 3), **details})

    def _motion_state(self):
        if self._motion and self._clock() >= self._motion['until']:
            self._motion = None
        if not self._motion:
            return {'moving': False, 'direction': None, 'remaining_ms': 0}
        remaining = int((self._motion['until'] - self._clock()) * 1000)
        return {'moving': True, 'direction': self._motion['direction'], 'remaining_ms': remaining}

    def _robot_state(self):
        # Simulated conversation pins the state machine view to LISTENING.
        return (State.LISTENING if self._conversation else State.IDLE).name

    def _require_manual(self, action):
        if self._mode != 'manual':
            raise ConflictError(f'{action} requires manual mode (current mode: {self._mode})')

    # ----- status -----
    def status(self):
        with self._lock:
            return {
                'name': 'Eli',
                'backend': self.backend,
                'mode': self._mode,
                'state': self._robot_state(),
                'expression': self._expression.value,
                'volume': self._volume,
                'conversation_active': self._conversation,
                'movement': self._motion_state(),
                'uptime_s': int(self._clock() - self._started),
                'capabilities': self.capabilities(),
                'history': list(self._history),
            }

    @staticmethod
    def capabilities():
        return {
            'eyes': SIMULATED, 'volume': SIMULATED, 'movement': SIMULATED,
            'servos': SIMULATED, 'conversation': SIMULATED,
            'camera': UNAVAILABLE, 'microphone': UNAVAILABLE,
            'face_recognition': UNAVAILABLE, 'ai': UNAVAILABLE, 'esp32': UNAVAILABLE,
        }

    # ----- mode -----
    def get_mode(self):
        with self._lock:
            return {'mode': self._mode, 'modes': list(MODES), 'availability': SIMULATED}

    def set_mode(self, mode):
        mode = _choice(mode, 'mode', MODES)
        with self._lock:
            previous = self._mode
            if mode != previous:
                if mode == 'sleep':
                    self._motion = None
                    self._expression = Expression.SLEEPING
                elif previous == 'sleep':
                    self._expression = Expression.IDLE
                if mode != 'companion':
                    self._conversation = False
                if previous == 'manual':
                    self._motion = None
                self._mode = mode
                self._record('mode', mode=mode, previous=previous)
            result = self.get_mode()
            result.update(previous=previous, expression=self._expression.value,
                          conversation_active=self._conversation)
            return result

    # ----- eyes -----
    def get_expression(self):
        with self._lock:
            return {'expression': self._expression.value,
                    'expressions': [e.value for e in Expression], 'availability': SIMULATED}

    def set_expression(self, expression):
        if not isinstance(expression, str):
            raise ValidationError('expression must be a string', field='expression')
        try:
            parsed = Expression.parse(expression)
        except ValueError as exc:
            raise ValidationError(str(exc), field='expression') from None
        with self._lock:
            if self._mode == 'sleep' and parsed is not Expression.SLEEPING:
                raise ConflictError('Eli is asleep; change the mode before setting an expression')
            self._expression = parsed
            self._record('expression', expression=parsed.value)
            return self.get_expression()

    # ----- audio -----
    def get_volume(self):
        with self._lock:
            return {'volume': self._volume, 'min': VOLUME_RANGE[0], 'max': VOLUME_RANGE[1],
                    'availability': SIMULATED}

    def set_volume(self, volume):
        volume = _integer(volume, 'volume', *VOLUME_RANGE)
        with self._lock:
            self._volume = volume
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
                'mode': self._mode,
                'availability': SIMULATED,
            }

    def update_settings(self, changes):
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
        with self._lock:  # validated first, so a bad field changes nothing
            if 'eye_color' in updates:
                self._eye_color = updates['eye_color']
            if 'animation_speed' in updates:
                self._animation_speed = updates['animation_speed']
            self._record('settings', **{k: (_hex(v) if k == 'eye_color' else v)
                                        for k, v in updates.items()})
            return self.get_settings()

    # ----- movement -----
    def movement(self):
        with self._lock:
            return {
                **self._motion_state(),
                'speed': self._speed,
                'servos': {name: {'angle': angle, 'min': SERVOS[name][0], 'max': SERVOS[name][1]}
                           for name, angle in self._servos.items()},
                'directions': list(DIRECTIONS),
                'enabled': self._mode == 'manual',
                'availability': SIMULATED,
            }

    def move(self, direction, duration_ms=None):
        direction = _choice(direction, 'direction', DIRECTIONS)
        duration = DEFAULT_DURATION_MS if duration_ms is None else \
            _integer(duration_ms, 'duration_ms', *DURATION_RANGE)
        with self._lock:
            self._require_manual('Movement')
            if self._speed == 0:
                raise ConflictError('Motor speed is 0; raise the speed before moving')
            self._motion = {'direction': direction, 'until': self._clock() + duration / 1000}
            self._record('move', direction=direction, duration_ms=duration, speed=self._speed)
            result = self.movement()
            result['duration_ms'] = duration
            return result

    def set_speed(self, speed):
        speed = _integer(speed, 'speed', *SPEED_RANGE)
        with self._lock:
            self._speed = speed
            self._record('speed', speed=speed)
            return self.movement()

    def stop(self):
        # Always accepted, in every mode: stopping must never be refused.
        with self._lock:
            self._motion = None
            self._record('stop')
            return self.movement()

    def set_servo(self, servo, angle):
        servo = _choice(servo, 'servo', tuple(SERVOS))
        angle = _integer(angle, 'angle', *SERVOS[servo])
        with self._lock:
            self._require_manual('Servo control')
            self._servos[servo] = angle
            self._record('servo', servo=servo, angle=angle)
            return self.movement()

    # ----- companion -----
    def companion(self):
        with self._lock:
            return {
                'conversation': {'active': self._conversation, 'state': self._robot_state(),
                                 'availability': SIMULATED,
                                 'detail': 'Simulated; the voice pipeline in main.py is not '
                                           'controlled from the dashboard yet.'},
                'face_recognition': _unavailable(),
                'microphone': _unavailable('Not linked; the dashboard never accesses '
                                           'microphone audio.'),
                'ai': _unavailable('Ollama connection status is not linked in Phase 1.'),
            }

    def conversation(self, action):
        action = _choice(action, 'action', CONVERSATION_ACTIONS)
        with self._lock:
            if action == 'start':
                if self._mode != 'companion':
                    raise ConflictError(f'Conversations require companion mode '
                                        f'(current mode: {self._mode})')
                self._conversation = True
                self._expression = Expression.LISTENING
            else:
                self._conversation = False
                self._expression = Expression.IDLE
            self._record('conversation', action=action)
            return self.companion()

    # ----- perception and diagnostics -----
    def perception(self):
        return {
            'camera': {**_unavailable('Camera preview is not available. The dashboard never '
                                      'streams or stores camera frames.'), 'preview': None},
            'recognition': {**_unavailable(), 'identity': None},
        }

    def diagnostics(self):
        components = ('camera', 'microphone', 'speaker', 'tft_display')
        return {
            'host': self._metrics.snapshot(),
            'esp32': {**_unavailable('No ESP32 link in Phase 1.'), 'connected': False},
            'sensors': {name: _unavailable() for name in components},
            'service': {'backend': self.backend, 'availability': SIMULATED,
                        'uptime_s': int(self._clock() - self._started)},
        }
