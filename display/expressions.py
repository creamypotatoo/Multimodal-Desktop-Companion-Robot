"""Expression definitions for Eli's eyes.

An expression is pure data: a target pose for each eye plus behaviour settings
(blink rate, gaze wandering, periodic motion). ``display.animation`` smoothly
interpolates between these targets; nothing here draws or times anything.

Pose fields (per eye), all relative to ``EyeConfig``:
  scale_x, scale_y  size multipliers
  offset_y          vertical shift as a fraction of eye height (+ is down)
  openness          1 = fully open, 0 = closed (blinks multiply this)
  lid_top           fraction of the eye hidden by a straight upper lid
  lid_tilt          upper-lid slope; + drops the outer corner (sad droop),
                    - drops the inner corner
  lid_bottom        curved lower cut that turns the eye into a smiling arc
"""
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple, Optional, Tuple


class Expression(Enum):
    IDLE = 'idle'
    LISTENING = 'listening'
    THINKING = 'thinking'
    SPEAKING = 'speaking'
    HAPPY = 'happy'
    EXCITED = 'excited'
    SAD = 'sad'
    CONFUSED = 'confused'
    SLEEPY = 'sleepy'
    SLEEPING = 'sleeping'

    @classmethod
    def parse(cls, value):
        """Accept an Expression, its name/value (any case) or any Enum member
        with a matching name, such as ``behavior.state_machine.State.IDLE``."""
        if isinstance(value, cls):
            return value
        if isinstance(value, Enum):
            value = value.name
        if isinstance(value, str):
            key = value.strip().upper()
            if key in cls.__members__:
                return cls[key]
        names = ', '.join(e.value for e in cls)
        raise ValueError(f'Unknown expression {value!r}; expected one of: {names}')


class EyePose(NamedTuple):
    scale_x: float = 1.0
    scale_y: float = 1.0
    offset_y: float = 0.0
    openness: float = 1.0
    lid_top: float = 0.0
    lid_tilt: float = 0.0
    lid_bottom: float = 0.0


@dataclass(frozen=True)
class ExpressionSpec:
    """Targets (smoothly interpolated) and behaviour (applied immediately)."""
    left: EyePose
    right: EyePose
    # --- interpolated targets ---
    gaze_bias: Tuple[float, float] = (0.0, 0.0)
    sparkle: float = 0.0        # 0..1 highlight/twinkle strength
    bounce: float = 0.0         # scale_y pulse amplitude (speaking)
    hop: float = 0.0            # vertical hop amplitude, fraction of eye height
    breathe: float = 0.0        # slow vertical drift amplitude (sleeping)
    drowsy: float = 0.0         # extra lid droop that builds up then resets
    # --- behaviour ---
    motion_hz: float = 0.0      # frequency of bounce/hop
    blink_interval: Optional[Tuple[float, float]] = (2.5, 6.0)
    double_blink_chance: float = 0.15
    blink_speed: float = 1.0    # >1 = slower blinks
    wander_interval: Optional[Tuple[float, float]] = (1.5, 4.0)
    wander_amplitude: float = 0.3
    gaze_points: Optional[Tuple[Tuple[float, float], ...]] = None

    def __post_init__(self):
        for interval in (self.blink_interval, self.wander_interval):
            if interval is not None and not 0 < interval[0] <= interval[1]:
                raise ValueError('Intervals must be (low, high) with 0 < low <= high')


def _both(**kwargs):
    pose = EyePose(**kwargs)
    return dict(left=pose, right=pose)


EXPRESSIONS = {
    Expression.IDLE: ExpressionSpec(
        **_both(scale_y=0.96),
        blink_interval=(2.5, 6.0), wander_interval=(1.8, 4.5), wander_amplitude=0.35),
    Expression.LISTENING: ExpressionSpec(
        **_both(scale_x=1.08, scale_y=1.12, offset_y=-0.03),
        blink_interval=(3.0, 7.0), double_blink_chance=0.05,
        wander_interval=(2.5, 5.0), wander_amplitude=0.12),
    Expression.THINKING: ExpressionSpec(
        **_both(scale_x=0.96, scale_y=0.92, lid_top=0.16),
        gaze_bias=(0.0, -0.35), blink_interval=(3.0, 6.0), double_blink_chance=0.05,
        wander_interval=(1.2, 2.6), wander_amplitude=0.2,
        gaze_points=((-0.75, -0.5), (0.75, -0.5), (0.0, -0.8), (0.55, -0.7))),
    Expression.SPEAKING: ExpressionSpec(
        **_both(scale_x=1.03, scale_y=1.02, lid_bottom=0.18),
        bounce=0.05, motion_hz=2.2, blink_interval=(2.5, 5.0),
        wander_interval=(1.5, 3.5), wander_amplitude=0.2),
    Expression.HAPPY: ExpressionSpec(
        **_both(scale_x=1.06, scale_y=1.0, offset_y=-0.04, lid_bottom=0.5),
        hop=0.015, motion_hz=1.2, blink_interval=(3.0, 6.0),
        wander_interval=(2.5, 5.0), wander_amplitude=0.12),
    Expression.EXCITED: ExpressionSpec(
        **_both(scale_x=1.16, scale_y=1.2, offset_y=-0.05, lid_bottom=0.12),
        sparkle=1.0, hop=0.035, motion_hz=2.6, blink_interval=(3.0, 6.5),
        double_blink_chance=0.25, wander_interval=(1.2, 2.5), wander_amplitude=0.15),
    Expression.SAD: ExpressionSpec(
        **_both(scale_x=0.98, scale_y=0.84, offset_y=0.14, lid_top=0.22, lid_tilt=0.5),
        gaze_bias=(0.0, 0.45), blink_interval=(3.5, 7.0), double_blink_chance=0.05,
        blink_speed=1.4, wander_interval=(3.0, 6.0), wander_amplitude=0.12),
    Expression.CONFUSED: ExpressionSpec(
        left=EyePose(scale_x=1.06, scale_y=1.1, offset_y=-0.05),
        right=EyePose(scale_x=0.9, scale_y=0.8, offset_y=0.04, lid_top=0.3, lid_tilt=-0.25),
        gaze_bias=(0.15, -0.1), blink_interval=(2.5, 5.0), double_blink_chance=0.3,
        wander_interval=(0.9, 2.0), wander_amplitude=0.18),
    Expression.SLEEPY: ExpressionSpec(
        **_both(scale_y=0.92, offset_y=0.06, lid_top=0.5, lid_tilt=0.12),
        drowsy=0.3, gaze_bias=(0.0, 0.3), blink_interval=(3.0, 6.0),
        double_blink_chance=0.0, blink_speed=2.6, wander_interval=(4.0, 8.0),
        wander_amplitude=0.08),
    Expression.SLEEPING: ExpressionSpec(
        **_both(scale_x=1.04, offset_y=0.12, openness=0.0),
        breathe=0.035, gaze_bias=(0.0, 0.25), blink_interval=None,
        wander_interval=None, wander_amplitude=0.0),
}

if set(EXPRESSIONS) != set(Expression):  # pragma: no cover - definition guard
    raise RuntimeError('Every expression needs an ExpressionSpec')
