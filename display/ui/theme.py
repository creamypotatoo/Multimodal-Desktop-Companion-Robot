"""Colours and fonts for Eli OS screens (320x240, 2.4" resistive panel).

Dark navy background, the eyes' cyan as the primary accent, and one soft
pastel per utility. Fonts are local TrueType files (DejaVu on Raspberry Pi
OS, Segoe UI/Arial on Windows); Pillow's built-in font is the last resort,
so nothing is ever downloaded.

  ELI_UI_FONT       path to a regular .ttf to use instead
  ELI_UI_FONT_BOLD  path to a bold .ttf to use instead
"""
import os
from dataclasses import dataclass
from functools import lru_cache

from PIL import ImageFont

_REGULAR = ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/TTF/DejaVuSans.ttf',
            'segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf')
_BOLD = ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
         '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
         'segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf')


@dataclass(frozen=True)
class Theme:
    background: tuple = (6, 10, 20)        # near-black navy
    surface: tuple = (16, 24, 40)          # tiles and buttons
    surface_edge: tuple = (32, 46, 70)
    text: tuple = (230, 240, 255)
    muted: tuple = (124, 139, 165)
    accent: tuple = (0, 229, 255)          # matches the eyes
    radius: int = 12
    press_mix: float = 0.35                # how strongly a pressed button takes its accent


THEME = Theme()

# Conversation state -> (label, colour) for the small status pill.
STATUS = {
    'IDLE': ('Idle', (124, 139, 165)),
    'LISTENING': ('Listening', (143, 240, 200)),
    'THINKING': ('Thinking', (195, 166, 255)),
    'SPEAKING': ('Speaking', (0, 229, 255)),
}


def mix(a, b, t):
    """Blend colour ``a`` toward ``b`` by ``t`` (0..1)."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


@lru_cache(maxsize=16)
def font(size, bold=False):
    """A cached local font of ``size`` px; never raises."""
    override = os.environ.get('ELI_UI_FONT_BOLD' if bold else 'ELI_UI_FONT', '').strip()
    for candidate in ((override,) if override else ()) + (_BOLD if bold else _REGULAR):
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()
