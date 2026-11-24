"""Configuration for Eli's animated eyes: visual style and animation timing.

Everything here is plain data with validation; nothing imports hardware or GUI
libraries. Explicit arguments override environment variables:

  ELI_DISPLAY_FPS   target frames per second, 0 < fps <= 60   (default: 10)
  ELI_EYE_COLOR     eye colour as #RRGGBB                     (default: #00E5FF)
"""
import math
import os
from dataclasses import dataclass, replace

DEFAULT_FPS = 10.0
MAX_FPS = 60.0
DEFAULT_EYE_COLOR = (0, 229, 255)  # cyan


def parse_color(value):
    """Return an ``(r, g, b)`` tuple from ``#RRGGBB``, ``RRGGBB`` or a 3-sequence."""
    if isinstance(value, str):
        text = value.strip().lstrip('#')
        if len(text) != 6:
            raise ValueError(f'Colour must be #RRGGBB, got {value!r}')
        try:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise ValueError(f'Colour must be #RRGGBB, got {value!r}') from None
    try:
        rgb = tuple(int(c) for c in value)
    except (TypeError, ValueError):
        raise ValueError(f'Colour must be #RRGGBB or (r, g, b), got {value!r}') from None
    if len(rgb) != 3 or not all(0 <= c <= 255 for c in rgb):
        raise ValueError(f'Colour must have three 0-255 components, got {value!r}')
    return rgb


def validate_fps(fps):
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        raise ValueError(f'FPS must be a number, got {fps!r}') from None
    if not (math.isfinite(fps) and 0 < fps <= MAX_FPS):
        raise ValueError(f'FPS must be > 0 and <= {MAX_FPS:g}, got {fps}')
    return fps


@dataclass(frozen=True)
class EyeConfig:
    """Visual style. Sizes are in output pixels at the base (IDLE) pose.

    ``spacing`` is the gap between the inner edges of the two eyes. ``offset_x``
    and ``offset_y`` move the pair relative to the frame centre. ``max_gaze_x``
    and ``max_gaze_y`` are the pixel shift at gaze +/-1. ``supersample`` > 1
    draws at a larger size and downsamples for smoother edges (costs CPU).
    """
    frame_width: int = 320
    frame_height: int = 240
    color: tuple = DEFAULT_EYE_COLOR
    background: tuple = (0, 0, 0)
    sparkle_color: tuple = (255, 255, 255)
    eye_width: int = 78
    eye_height: int = 96
    spacing: int = 64
    corner_radius: int = 22
    offset_x: int = 0
    offset_y: int = -6
    max_gaze_x: int = 34
    max_gaze_y: int = 22
    closed_thickness: int = 6
    margin: int = 4
    supersample: int = 1

    def __post_init__(self):
        for name in ('color', 'background', 'sparkle_color'):
            object.__setattr__(self, name, parse_color(getattr(self, name)))
        positive = ('frame_width', 'frame_height', 'eye_width', 'eye_height',
                    'closed_thickness', 'supersample')
        for name in positive:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f'{name} must be positive')
        for name in ('spacing', 'corner_radius', 'max_gaze_x', 'max_gaze_y', 'margin'):
            if int(getattr(self, name)) < 0:
                raise ValueError(f'{name} must not be negative')
        if self.supersample > 4:
            raise ValueError('supersample must be between 1 and 4')
        if 2 * self.eye_width + self.spacing + 2 * self.margin > self.frame_width:
            raise ValueError('Two eyes plus spacing do not fit the frame width')
        if self.eye_height + 2 * self.margin > self.frame_height:
            raise ValueError('eye_height does not fit the frame height')

    @property
    def size(self):
        return (self.frame_width, self.frame_height)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        if 'color' not in overrides and environ.get('ELI_EYE_COLOR'):
            overrides['color'] = environ['ELI_EYE_COLOR']
        return cls(**overrides)


@dataclass(frozen=True)
class AnimationConfig:
    """Timing. All durations are seconds of monotonic time.

    ``transition_time`` and ``gaze_time`` are exponential smoothing time
    constants: after one constant a change is ~63% complete, after three ~95%.
    Smoothing is frame-rate independent, so lowering ``fps`` keeps the same
    motion, just sampled less often.
    """
    fps: float = DEFAULT_FPS
    transition_time: float = 0.14
    gaze_time: float = 0.07
    blink_close: float = 0.09
    blink_hold: float = 0.05
    blink_open: float = 0.14
    blinking: bool = True
    auto_gaze: bool = True
    min_sleep: float = 0.002

    def __post_init__(self):
        object.__setattr__(self, 'fps', validate_fps(self.fps))
        for name in ('transition_time', 'gaze_time', 'blink_close', 'blink_hold',
                     'blink_open', 'min_sleep'):
            value = float(getattr(self, name))
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f'{name} must be a non-negative number')
            object.__setattr__(self, name, value)
        if self.blink_close + self.blink_hold + self.blink_open <= 0:
            raise ValueError('Blink duration must be positive')

    @property
    def frame_period(self):
        return 1.0 / self.fps

    def with_fps(self, fps):
        return replace(self, fps=fps)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        if 'fps' not in overrides and environ.get('ELI_DISPLAY_FPS'):
            overrides['fps'] = environ['ELI_DISPLAY_FPS']
        return cls(**overrides)
