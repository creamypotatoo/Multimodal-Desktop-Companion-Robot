"""Monotonic timing models adapted from the original Eli productivity tools."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Completion:
    kind: str
    phase: str | None = None
    next_phase: str | None = None


class Countdown:
    def __init__(self, duration=300):
        self.configure(duration)

    def configure(self, duration):
        self.duration = duration
        self.reset()

    def reset(self):
        self.state = 'idle'
        self.frozen = self.duration
        self.deadline = None

    def cancel(self):
        self.configure(0)

    def remaining(self, now):
        if self.state == 'running':
            return max(0.0, self.deadline - now)
        return self.frozen

    def start(self, now):
        self.frozen = self.duration
        self.resume(now)

    def resume(self, now):
        self.state = 'running'
        self.deadline = now + self.frozen

    def pause(self, now):
        self.frozen = self.remaining(now)
        self.state = 'paused'
        self.deadline = None

    def complete(self):
        self.state = 'completed'
        self.frozen = 0
        self.deadline = None
        return Completion('timer_done')

    def snapshot(self, now):
        remaining = self.remaining(now)
        return dict(state=self.state, duration=self.duration, remaining=remaining,
                    progress=100 * (1 - remaining / self.duration) if self.duration else 0)


class Stopwatch:
    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 'idle'
        self.accumulated = 0.0
        self.started = None
        self.clear_laps()

    def elapsed(self, now):
        return self.accumulated + (now - self.started if self.state == 'running' else 0)

    def start(self, now):
        self.state, self.started = 'running', now

    resume = start

    def pause(self, now):
        self.accumulated = self.elapsed(now)
        self.state = 'paused'

    def clear_laps(self, now=0):
        self.laps = []
        self.last_total = self.elapsed(now)
        self.lap_count = 0
        self.fastest = self.slowest = None

    def lap(self, now):
        total = self.elapsed(now)
        self.lap_count += 1
        lap = dict(number=self.lap_count, duration=total - self.last_total, total=total)
        self.last_total = total
        self.laps.append(lap)
        del self.laps[:-99]
        if self.fastest is None or lap['duration'] < self.fastest['duration']:
            self.fastest = lap
        if self.slowest is None or lap['duration'] > self.slowest['duration']:
            self.slowest = lap

    def snapshot(self, now):
        return dict(state=self.state, elapsed=self.elapsed(now),
                    laps=[dict(lap) for lap in self.laps],
                    fastest=dict(self.fastest) if self.fastest else None,
                    slowest=dict(self.slowest) if self.slowest else None)


class Pomodoro(Countdown):
    LIMITS = dict(focus=(1, 90), short_break=(1, 30), long_break=(1, 60),
                  long_every=(1, 12))

    def __init__(self):
        self.settings = dict(focus=25, short_break=5, long_break=15,
                             long_every=4, automatic=False)
        self.reset()

    def configure(self, settings):
        self.settings.update(settings)
        self.prepare(self.phase)

    def prepare(self, phase):
        self.phase = phase
        self.duration = self.settings[phase] * 60
        super().reset()

    def reset(self):
        self.completed = self.total_focus = self.sessions_in_cycle = 0
        self.prepare('focus')

    def skip(self, now):
        self.advance(now, completed=False)

    def complete(self):
        return self.advance(self.deadline, completed=True)

    def advance(self, at, *, completed):
        previous = self.phase
        next_phase = 'focus'
        if previous == 'focus':
            next_phase = 'short_break'
            if completed:
                self.completed += 1
                self.total_focus += self.duration
                self.sessions_in_cycle += 1
                if self.sessions_in_cycle >= self.settings['long_every']:
                    self.sessions_in_cycle = 0
                    next_phase = 'long_break'
        self.prepare(next_phase)
        if self.settings['automatic']:
            self.start(at)
        if completed:
            return Completion('focus_done' if previous == 'focus' else 'break_done',
                              previous, next_phase)

    def snapshot(self, now):
        return dict(super().snapshot(now), phase=self.phase, settings=dict(self.settings),
                    completed=self.completed, total_focus=self.total_focus,
                    sessions_in_cycle=self.sessions_in_cycle)
