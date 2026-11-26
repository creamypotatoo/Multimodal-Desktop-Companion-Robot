"""Countdown timer, stopwatch and Pomodoro models (Eli OS Phase 2).

All timing is derived from monotonic timestamps passed in as ``now``; nothing
counts frames or decrements per tick, so accuracy does not depend on the
display frame rate. The models are polled (``Apps.poll``) on every display
tick, whatever screen is showing, and report completions as ``UtilityEvent``.
They are mutated only on the display worker thread (UIManager.render).
"""
import math
from typing import NamedTuple, Optional

MAX_TIMER = 23 * 3600 + 59 * 60 + 59
MAX_LAPS = 99


class UtilityEvent(NamedTuple):
    kind: str                 # 'timer_done' | 'focus_done' | 'break_done'
    detail: Optional[str] = None   # focus_done: next phase ('short_break'/'long_break')


def format_time(seconds, tenths=False):
    """M:SS / H:MM:SS; countdowns round up (pass a ceil'd value), stopwatches floor."""
    seconds = max(0.0, seconds)
    if tenths:
        whole = int(seconds)
        h, rem = divmod(whole, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f'{h}:{m:02d}:{s:02d}'
        return f'{m:02d}:{s:02d}.{int((seconds - whole) * 10)}'
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def countdown_seconds(remaining):
    """Displayed whole seconds of a countdown: 0.2 s left still shows 00:01."""
    return int(math.ceil(remaining - 1e-9))


class Countdown:
    SETUP, READY, RUNNING, PAUSED, DONE = 'setup', 'ready', 'running', 'paused', 'done'
    PRESETS = (60, 300, 600, 900, 1800)

    def __init__(self):
        self.state = self.SETUP
        self.duration = 0
        self._end = None
        self._remaining = 0.0

    @property
    def fields(self):
        h, rem = divmod(self.duration, 3600)
        return (h,) + divmod(rem, 60)

    def adjust(self, field, delta):
        """Change hours/minutes/seconds (wrapping) while setting up."""
        if self.state != self.SETUP:
            return
        h, m, s = self.fields
        if field == 'h':
            h = (h + delta) % 24
        elif field == 'm':
            m = (m + delta) % 60
        elif field == 's':
            s = (s + delta) % 60
        else:
            raise ValueError(f'Unknown field {field!r}')
        self.duration = h * 3600 + m * 60 + s

    def start(self, now, duration=None):
        if duration is not None:
            self.duration = max(0, min(MAX_TIMER, int(duration)))
        if self.duration <= 0 or self.state in (self.RUNNING, self.PAUSED):
            return False
        self.state, self._end = self.RUNNING, now + self.duration
        return True

    def pause(self, now):
        if self.state == self.RUNNING:
            self._remaining = max(0.0, self._end - now)
            self.state = self.PAUSED

    def resume(self, now):
        if self.state == self.PAUSED:
            self._end = now + self._remaining
            self.state = self.RUNNING

    def toggle(self, now):
        if self.state == self.RUNNING:
            self.pause(now)
        elif self.state == self.PAUSED:
            self.resume(now)
        else:
            self.start(now)

    def reset(self):
        """Back to the full duration, stopped (Start runs it again)."""
        if self.state != self.SETUP:
            self.state, self._remaining = self.READY, float(self.duration)

    def cancel(self):
        """Stop and return to the duration setup."""
        self.state = self.SETUP

    def remaining(self, now):
        if self.state == self.RUNNING:
            return max(0.0, self._end - now)
        if self.state in (self.PAUSED, self.READY):
            return self._remaining
        return 0.0 if self.state == self.DONE else float(self.duration)

    def progress(self, now):
        """Fraction elapsed, 0..1."""
        if not self.duration:
            return 0.0
        return min(1.0, max(0.0, 1.0 - self.remaining(now) / self.duration))

    def poll(self, now):
        if self.state == self.RUNNING and now >= self._end:
            self.state = self.DONE
            return UtilityEvent('timer_done')
        return None


class Stopwatch:
    IDLE, RUNNING, PAUSED = 'idle', 'running', 'paused'

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = self.IDLE
        self._accumulated = 0.0
        self._started = None
        self.laps = []            # (number, split, total), oldest first
        self._lap_count = 0
        self._last_total = 0.0

    def elapsed(self, now):
        if self.state == self.RUNNING:
            return self._accumulated + max(0.0, now - self._started)
        return self._accumulated

    def start(self, now):
        if self.state == self.IDLE:
            self.state, self._started = self.RUNNING, now

    def pause(self, now):
        if self.state == self.RUNNING:
            self._accumulated = self.elapsed(now)
            self.state = self.PAUSED

    def resume(self, now):
        if self.state == self.PAUSED:
            self.state, self._started = self.RUNNING, now

    def toggle(self, now):
        {self.IDLE: self.start, self.RUNNING: self.pause, self.PAUSED: self.resume}[self.state](now)

    def lap(self, now):
        if self.state != self.RUNNING:
            return None
        total = self.elapsed(now)
        self._lap_count += 1
        lap = (self._lap_count, total - self._last_total, total)
        self._last_total = total
        self.laps.append(lap)
        del self.laps[:-MAX_LAPS]
        return lap


class Pomodoro:
    READY, RUNNING, PAUSED = 'ready', 'running', 'paused'
    FOCUS, SHORT, LONG = 'focus', 'short_break', 'long_break'
    LIMITS = {'focus': (5, 90, 5), 'short': (1, 30, 1), 'long': (5, 60, 5)}  # minutes: min, max, step

    def __init__(self, focus=25, short=5, long=15, long_every=4):
        self.minutes = {'focus': focus, 'short': short, 'long': long}
        self.long_every = long_every
        self.reset()

    def reset(self):
        self.state, self.phase = self.READY, self.FOCUS
        self.completed = 0          # focus sessions completed since reset
        self._end = None
        self._duration = self.phase_seconds(self.FOCUS)
        self._remaining = float(self._duration)

    def phase_seconds(self, phase):
        key = {self.FOCUS: 'focus', self.SHORT: 'short', self.LONG: 'long'}[phase]
        return self.minutes[key] * 60

    def adjust(self, key, delta_steps):
        low, high, step = self.LIMITS[key]
        self.minutes[key] = max(low, min(high, self.minutes[key] + delta_steps * step))
        if self.state == self.READY:          # not started yet: show the new length
            self._duration = self.phase_seconds(self.phase)
            self._remaining = float(self._duration)

    @property
    def cycle_position(self):
        """Focus sessions completed in the current round of ``long_every``."""
        return self.completed % self.long_every

    def remaining(self, now):
        if self.state == self.RUNNING:
            return max(0.0, self._end - now)
        return self._remaining

    def progress(self, now):
        return min(1.0, max(0.0, 1.0 - self.remaining(now) / self._duration)) if self._duration else 0.0

    def start(self, now):
        if self.state == self.READY:
            self._begin(now)

    def _begin(self, start_time):
        self._duration = self.phase_seconds(self.phase)
        self.state, self._end = self.RUNNING, start_time + self._duration

    def pause(self, now):
        if self.state == self.RUNNING:
            self._remaining = max(0.0, self._end - now)
            self.state = self.PAUSED

    def resume(self, now):
        if self.state == self.PAUSED:
            self.state, self._end = self.RUNNING, now + self._remaining

    def toggle(self, now):
        {self.READY: self.start, self.RUNNING: self.pause, self.PAUSED: self.resume}[self.state](now)

    def skip(self, now):
        """End the current phase early (not counted as a completed session)."""
        self._advance(now, completed=False)

    def _advance(self, now, completed):
        if self.phase == self.FOCUS:
            if completed:
                self.completed += 1
            long = completed and self.completed % self.long_every == 0
            self.phase = self.LONG if long else self.SHORT
            # Breaks start automatically, from the exact end of focus (no drift).
            self._begin(self._end if completed else now)
            return UtilityEvent('focus_done', self.phase) if completed else None
        self.phase, self.state = self.FOCUS, self.READY   # focus waits for the user
        self._duration = self.phase_seconds(self.FOCUS)
        self._remaining = float(self._duration)
        return UtilityEvent('break_done') if completed else None

    def poll(self, now):
        if self.state == self.RUNNING and now >= self._end:
            return self._advance(now, completed=True)
        return None


class Apps:
    """The running utility models, shared by their screens."""

    def __init__(self):
        self.timer = Countdown()
        self.stopwatch = Stopwatch()
        self.pomodoro = Pomodoro()

    def poll(self, now):
        events = []
        for model in (self.timer, self.pomodoro):
            event = model.poll(now)
            if event is not None:
                events.append(event)
        return events
