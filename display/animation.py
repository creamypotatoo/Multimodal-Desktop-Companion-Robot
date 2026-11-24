"""Time-driven animation state for Eli's eyes (no drawing, no threads).

``EyeAnimator.update(now)`` advances the animation to monotonic time ``now``
and returns an immutable ``FrameState`` that the renderer turns into pixels.
All randomness comes from an injectable ``random.Random`` so tests are
deterministic. The class is not thread-safe by itself; ``display.eyes.EyeEngine``
serialises access with a lock.

Motion model:
  * Expression changes: every pose value moves toward its new target with
    frame-rate independent exponential smoothing (``transition_time``).
  * Gaze: discrete fixation targets reached by a quick smoothed saccade
    (``gaze_time``), then held, which reads as calm rather than floaty.
  * Blinks: close -> hold -> open with eased curves, at randomised intervals,
    sometimes doubled. A blink is guaranteed to render at least one fully
    closed frame even at low FPS.
"""
import math
import random
from typing import NamedTuple

from display.config import AnimationConfig
from display.expressions import EXPRESSIONS, Expression, EyePose

MAX_STEP = 0.5          # seconds; caps dt after a stall
DROWSY_PERIOD = 7.0     # seconds for a sleepy lid to droop before "waking"
DROWSY_TIME = 0.35      # smoothing constant for the drowsy lid
BREATHE_HZ = 0.2
TWINKLE_HZ = 1.4
DOUBLE_BLINK_GAP = 0.08
SACCADE_BLINK_CHANCE = 0.2
MIN_BLINK_SPACING = 1.2


class FrameState(NamedTuple):
    left: EyePose
    right: EyePose
    gaze_x: float
    gaze_y: float
    sparkle: float
    twinkle: float


def _clamp(value, low, high):
    return low if value < low else high if value > high else value


def _smoothstep(x):
    x = _clamp(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _alpha(dt, tau):
    return 1.0 if tau <= 0 else 1.0 - math.exp(-dt / tau)


def validate_gaze(x, y):
    """Return ``(x, y)`` clamped to [-1, 1]; reject non-finite or non-numbers."""
    values = []
    for name, value in (('x', x), ('y', y)):
        if isinstance(value, bool):
            raise ValueError(f'Gaze {name} must be a number, got {value!r}')
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'Gaze {name} must be a number, got {value!r}') from None
        if not math.isfinite(value):
            raise ValueError(f'Gaze {name} must be finite, got {value!r}')
        values.append(_clamp(value, -1.0, 1.0))
    return tuple(values)


class EyeAnimator:
    def __init__(self, config=None, rng=None, now=0.0, expression=Expression.IDLE):
        self.config = config or AnimationConfig()
        self._rng = rng or random.Random()
        self._expression = Expression.parse(expression)
        self._spec = EXPRESSIONS[self._expression]
        self._left = list(self._spec.left)
        self._right = list(self._spec.right)
        self._extras = self._extra_targets()
        self._last = float(now)
        self._motion_phase = 0.0
        self._breathe_phase = 0.0
        self._twinkle_phase = 0.0
        self._drowsy_elapsed = 0.0
        self._drowsy = 0.0
        self._gaze_target = list(self._spec.gaze_bias)
        self._gaze = list(self._gaze_target)
        self._manual_gaze = None
        self._blink_start = None
        self._closed_shown = False
        self._double_pending = False
        self._last_blink = -math.inf
        self._next_blink = self._schedule(self._spec.blink_interval, now)
        self._next_wander = self._schedule(self._spec.wander_interval, now)

    # ------------------------------------------------------------- commands
    @property
    def expression(self):
        return self._expression

    @property
    def manual_gaze(self):
        return None if self._manual_gaze is None else tuple(self._manual_gaze)

    @property
    def blinking(self):
        return self._blink_start is not None

    def set_expression(self, expression, now):
        expression = Expression.parse(expression)
        if expression is self._expression:
            return expression
        self._expression = expression
        self._spec = EXPRESSIONS[expression]
        self._next_blink = (self._schedule(self._spec.blink_interval, now)
                            if self._spec.blink_interval else None)
        self._drowsy_elapsed = 0.0
        self._pick_gaze(now, initial=True)
        return expression

    def set_gaze(self, x, y, now=None):
        """Fix the gaze (e.g. toward a tracked face) until ``release_gaze``."""
        self._manual_gaze = list(validate_gaze(x, y))
        self._gaze_target = list(self._manual_gaze)
        return tuple(self._manual_gaze)

    def release_gaze(self, now):
        self._manual_gaze = None
        self._pick_gaze(now, initial=True)

    def blink(self, now, double=False):
        """Start a blink now. Returns False if one is already in progress."""
        if self._blink_start is not None:
            if double:
                self._double_pending = True
            return False
        self._blink_start = float(now)
        self._closed_shown = False
        self._double_pending = bool(double)
        self._last_blink = float(now)
        return True

    # --------------------------------------------------------------- update
    def update(self, now):
        now = float(now)
        dt = _clamp(now - self._last, 0.0, MAX_STEP)
        self._last = now
        spec = self._spec
        cfg = self.config

        # Expression pose and extras.
        a = _alpha(dt, cfg.transition_time)
        for current, target in ((self._left, spec.left), (self._right, spec.right)):
            for i, value in enumerate(target):
                current[i] += (value - current[i]) * a
        for i, value in enumerate(self._extra_targets()):
            self._extras[i] += (value - self._extras[i]) * a
        sparkle, bounce, hop, breathe, drowsy = self._extras

        # Periodic motion uses accumulated phase so frequency changes stay smooth.
        self._motion_phase = (self._motion_phase + 2 * math.pi * spec.motion_hz * dt) % (2 * math.pi)
        self._breathe_phase = (self._breathe_phase + 2 * math.pi * BREATHE_HZ * dt) % (2 * math.pi)
        self._twinkle_phase = (self._twinkle_phase + 2 * math.pi * TWINKLE_HZ * dt) % (2 * math.pi)
        self._drowsy_elapsed = (self._drowsy_elapsed + dt) % DROWSY_PERIOD
        droop = drowsy * (self._drowsy_elapsed / DROWSY_PERIOD) ** 2
        self._drowsy += (droop - self._drowsy) * _alpha(dt, DROWSY_TIME)

        # Gaze.
        if self._manual_gaze is None and cfg.auto_gaze and self._next_wander is not None \
                and now >= self._next_wander:
            self._pick_gaze(now)
        ga = _alpha(dt, cfg.gaze_time)
        for i in (0, 1):
            self._gaze[i] += (self._gaze_target[i] - self._gaze[i]) * ga

        # Blinks.
        if cfg.blinking and self._next_blink is not None and now >= self._next_blink \
                and self._blink_start is None:
            self.blink(now, double=self._rng.random() < spec.double_blink_chance)
        blink = self._blink_factor(now)

        sin_motion = math.sin(self._motion_phase)
        pulse = 1.0 + bounce * sin_motion
        lift = -hop * abs(sin_motion) + breathe * math.sin(self._breathe_phase)
        left = self._pose(self._left, pulse, lift, blink)
        right = self._pose(self._right, pulse, lift, blink)
        twinkle = 0.5 + 0.5 * math.sin(self._twinkle_phase)
        return FrameState(left, right, _clamp(self._gaze[0], -1.0, 1.0),
                          _clamp(self._gaze[1], -1.0, 1.0), _clamp(sparkle, 0.0, 1.0), twinkle)

    # -------------------------------------------------------------- helpers
    def _extra_targets(self):
        s = self._spec
        return [s.sparkle, s.bounce, s.hop, s.breathe, s.drowsy]

    def _pose(self, values, pulse, lift, blink):
        scale_x, scale_y, offset_y, openness, lid_top, lid_tilt, lid_bottom = values
        return EyePose(
            scale_x=max(0.1, scale_x),
            scale_y=max(0.1, scale_y * pulse),
            offset_y=offset_y + lift,
            openness=_clamp(openness * blink, 0.0, 1.0),
            lid_top=_clamp(lid_top + self._drowsy, 0.0, 0.95),
            lid_tilt=_clamp(lid_tilt, -1.0, 1.0),
            lid_bottom=_clamp(lid_bottom, 0.0, 0.9))

    def _schedule(self, interval, now):
        if not interval:
            return None
        return now + self._rng.uniform(*interval)

    def _pick_gaze(self, now, initial=False):
        spec = self._spec
        self._next_wander = self._schedule(spec.wander_interval, now)
        if self._manual_gaze is not None:
            return
        bias_x, bias_y = spec.gaze_bias
        if spec.gaze_points:
            points = [p for p in spec.gaze_points if list(p) != self._gaze_target] \
                or list(spec.gaze_points)
            px, py = self._rng.choice(points)
            jitter = spec.wander_amplitude * 0.3
            target = (px + self._rng.uniform(-jitter, jitter), py + self._rng.uniform(-jitter, jitter))
        elif initial or not self.config.auto_gaze or self._rng.random() < 0.35:
            target = (bias_x, bias_y)   # return to a resting centre
        else:
            amp = spec.wander_amplitude
            target = (bias_x + self._rng.uniform(-amp, amp),
                      bias_y + self._rng.uniform(-amp, amp) * 0.6)
        self._gaze_target = [_clamp(target[0], -1.0, 1.0), _clamp(target[1], -1.0, 1.0)]
        # Real eyes often blink during a larger gaze shift.
        if not initial and self.config.blinking and spec.blink_interval \
                and now - self._last_blink >= MIN_BLINK_SPACING \
                and self._rng.random() < SACCADE_BLINK_CHANCE:
            self.blink(now)

    def _blink_factor(self, now):
        if self._blink_start is None:
            return 1.0
        speed = self._spec.blink_speed
        close = self.config.blink_close * speed
        hold = self.config.blink_hold * speed
        opening = self.config.blink_open * speed
        elapsed = now - self._blink_start
        if elapsed >= close and not self._closed_shown:
            # Guarantee one fully closed frame, then open from here.
            self._closed_shown = True
            self._blink_start = now - close - hold
            return 0.0
        if elapsed < close:
            return 1.0 - _smoothstep(elapsed / close if close else 1.0)
        if elapsed < close + hold:
            return 0.0
        if elapsed < close + hold + opening:
            return _smoothstep((elapsed - close - hold) / opening)
        # Finished.
        self._blink_start = None
        if self._double_pending:
            self._double_pending = False
            self._next_blink = now + DOUBLE_BLINK_GAP
        else:
            self._next_blink = self._schedule(self._spec.blink_interval, now)
        return 1.0
