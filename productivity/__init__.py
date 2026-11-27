"""Thread-safe productivity commands, snapshots and completion notifications."""
from collections import deque
from dataclasses import asdict, dataclass
from threading import Condition, Lock, Thread
from time import monotonic
from typing import Protocol
from uuid import uuid4

from .models import Countdown, Pomodoro, Stopwatch

MAX_TIMER = 24 * 3600 - 1
PRESETS = (60, 300, 600, 900, 1500, 1800, 3600)
ACTIVE = ('running', 'paused')
TRANSITIONS = {
    'start': ('idle', 'completed'), 'pause': ('running',), 'resume': ('paused',),
    'lap': ('running',), 'cancel': ('idle', 'running', 'paused', 'completed'),
}
ACTIONS = {
    'timer': {'start', 'pause', 'resume', 'reset', 'cancel'},
    'stopwatch': {'start', 'pause', 'resume', 'reset', 'lap', 'clear_laps'},
    'pomodoro': {'start', 'pause', 'resume', 'reset', 'skip'},
}


class UtilityError(ValueError):
    def __init__(self, message, *, status=400, field=None):
        super().__init__(message)
        self.status = status
        self.field = field


@dataclass(frozen=True)
class UtilityEvent:
    id: int
    kind: str
    phase: str | None
    next_phase: str | None


class NotificationSource(Protocol):
    def notifications(self, after: int = 0) -> dict:
        """Return available completion events after the consumer's cursor."""
        ...


def integer(value, minimum, maximum, field):
    if type(value) is not int or not minimum <= value <= maximum:
        raise UtilityError(f'{field} must be an integer from {minimum} to {maximum}', field=field)
    return value


class UtilityService:
    def __init__(self, *, clock=monotonic):
        self._clock = clock
        self._models = dict(timer=Countdown(), stopwatch=Stopwatch(), pomodoro=Pomodoro())
        self._timed = (self._models['timer'], self._models['pomodoro'])
        self._changed = Condition()
        self._lifecycle = Lock()
        self._thread = None
        self._stopping = False
        self._events = deque(maxlen=128)
        self._sequence = 0
        self._epoch = uuid4().hex

    def _advance(self, now):
        # Apply due deadlines in order, even when the scheduler wakes late.
        deadlines = self._deadlines()
        while deadlines:
            model = min(deadlines, key=lambda item: item.deadline)
            if model.deadline > now:
                break
            completion = model.complete()
            self._sequence += 1
            self._events.append(UtilityEvent(self._sequence, **asdict(completion)))
            deadlines = self._deadlines()

    def _deadlines(self):
        return [model for model in self._timed if model.deadline is not None]

    def _snapshot(self, now):
        return dict(**{name: model.snapshot(now) for name, model in self._models.items()},
                    stream=self._epoch, presets=PRESETS,
                    events=[asdict(event) for event in self._events],
                    cursor=self._sequence)

    def snapshot(self):
        with self._changed:
            now = self._clock()
            self._advance(now)
            return self._snapshot(now)

    def notifications(self, after=0):
        with self._changed:
            self._advance(self._clock())
            return dict(stream=self._epoch, cursor=self._sequence,
                        events=[asdict(event) for event in self._events if event.id > after])

    def _model(self, name):
        if name not in self._models:
            raise UtilityError('Unknown utility', status=404)
        return self._models[name]

    def configure(self, name, settings):
        with self._changed:
            model = self._model(name)
            now = self._clock()
            self._advance(now)
            if name == 'timer':
                if settings.keys() != {'duration'}:
                    raise UtilityError('Provide duration in seconds', field='duration')
                value = integer(settings['duration'], 1, MAX_TIMER, 'duration')
            elif name == 'pomodoro':
                if not settings:
                    raise UtilityError('Provide at least one Pomodoro setting')
                value = {}
                for field, setting in settings.items():
                    if field in Pomodoro.LIMITS:
                        value[field] = integer(setting, *Pomodoro.LIMITS[field], field)
                    elif field == 'automatic' and type(setting) is bool:
                        value[field] = setting
                    else:
                        raise UtilityError('Invalid Pomodoro setting', field=field)
            else:
                raise UtilityError('This utility has no duration settings', status=404)

            if model.state in ACTIVE:
                raise UtilityError('Reset or finish the active utility before configuring it', status=409)
            model.configure(value)
            self._changed.notify()
            return self._snapshot(now)

    def act(self, name, action, *, duration=None):
        with self._changed:
            model = self._model(name)
            if not isinstance(action, str) or action not in ACTIONS[name]:
                raise UtilityError('Unsupported action for this utility', field='action')
            if duration is not None:
                if (name, action) != ('timer', 'start'):
                    raise UtilityError('Duration is only supported when starting the timer',
                                       field='duration')
                integer(duration, 1, MAX_TIMER, 'duration')
            now = self._clock()
            self._advance(now)
            if action in TRANSITIONS and model.state not in TRANSITIONS[action]:
                raise UtilityError(f'Cannot {action} while {model.state}', status=409)
            if (name, action) == ('timer', 'start'):
                if duration is not None:
                    model.configure(duration)
                if not model.duration:
                    raise UtilityError('Set a duration before starting', status=409)
            method = getattr(model, action)
            if action in ('reset', 'cancel'):
                method()
            else:
                method(now)
            self._changed.notify()
            return self._snapshot(now)

    def start_scheduler(self):
        """Start one worker for this service. Return whether one was created."""
        with self._lifecycle:
            if self._thread is not None:
                return False
            with self._changed:
                self._stopping = False
                self._thread = Thread(target=self._run, name='eli-productivity', daemon=True)
                self._thread.start()
            return True

    def stop_scheduler(self):
        with self._lifecycle:
            if self._thread is None:
                return
            with self._changed:
                self._stopping = True
                self._changed.notify()
            self._thread.join()
            self._thread = None

    def _run(self):
        with self._changed:
            while not self._stopping:
                now = self._clock()
                self._advance(now)
                deadlines = self._deadlines()
                delay = min((model.deadline - now for model in deadlines), default=None)
                self._changed.wait(delay)
