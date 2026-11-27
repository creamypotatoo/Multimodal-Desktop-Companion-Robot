"""Reusable layout and button components for Eli OS screens.

Rectangles are exclusive ``(x0, y0, x1, y1)`` tuples, the same convention as
the display backends' dirty regions, so a button's rect is directly the
region to resend after its pressed state changes.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

from display.ui.icons import draw_icon
from display.ui.theme import THEME, font, mix

TOUCH_SLOP = 3   # px of tolerance around buttons for a fingertip on a resistive panel


@dataclass(frozen=True)
class Button:
    """A tappable region. ``action`` is ``'back'``, ``'home'`` or ``'open:<screen>'``."""
    id: str
    rect: Tuple[int, int, int, int]
    label: str = ''
    action: str = ''
    icon: Optional[str] = None
    accent: tuple = THEME.accent
    style: str = 'tile'           # 'tile' (icon above label), 'bar' (icon left of label), 'hidden'

    def contains(self, x, y, slop=0):
        x0, y0, x1, y1 = self.rect
        return x0 - slop <= x < x1 + slop and y0 - slop <= y < y1 + slop


def grid(area, rows, cols, gap):
    """Split ``area`` into rows x cols cells with ``gap`` px between them.

    Remainders are spread over the first cells, so cells differ by <= 1 px and
    the grid fills the area exactly. Returns rects row by row.
    """
    x0, y0, x1, y1 = area

    def edges(start, end, count):
        usable = end - start - gap * (count - 1)
        if usable < count:
            raise ValueError('Grid area too small')
        size, extra = divmod(usable, count)
        result, pos = [], start
        for i in range(count):
            length = size + (1 if i < extra else 0)
            result.append((pos, pos + length))
            pos += length + gap
        return result

    return [(cx0, cy0, cx1, cy1) for cy0, cy1 in edges(y0, y1, rows)
            for cx0, cx1 in edges(x0, x1, cols)]


def hit_test(buttons, x, y, slop=TOUCH_SLOP):
    """The button under (x, y): exact hits first, then the nearest within ``slop``."""
    for button in buttons:
        if button.contains(x, y):
            return button
    near = [b for b in buttons if b.contains(x, y, slop)]
    if not near:
        return None

    def distance(b):
        x0, y0, x1, y1 = b.rect
        return ((x0 + x1) / 2 - x) ** 2 + ((y0 + y1) / 2 - y) ** 2
    return min(near, key=distance)


def _line_middle(draw, fnt):
    """Vertical middle of a text line for ``fnt``, the same for every word, so
    labels with and without descenders share a baseline."""
    _, top, _, bottom = draw.textbbox((0, 0), 'Hg', font=fnt)
    return (top + bottom) / 2


def text_center(draw, xy, text, fnt, fill):
    """Draw ``text`` centred on ``xy``."""
    x, y = xy
    left, _, right, _ = draw.textbbox((0, 0), text, font=fnt)
    draw.text((x - (left + right) / 2, y - _line_middle(draw, fnt)), text, font=fnt, fill=fill)


def text_left(draw, xy, text, fnt, fill):
    """Draw ``text`` starting at x, vertically centred on y; returns its right edge."""
    x, y = xy
    left, _, right, _ = draw.textbbox((0, 0), text, font=fnt)
    draw.text((x - left, y - _line_middle(draw, fnt)), text, font=fnt, fill=fill)
    return x + right - left


def fit_font(draw, text, size, max_width, bold=False, min_size=10):
    """Largest font <= ``size`` whose rendering of ``text`` fits ``max_width``."""
    while size > min_size:
        fnt = font(size, bold)
        left, _, right, _ = draw.textbbox((0, 0), text, font=fnt)
        if right - left <= max_width:
            return fnt
        size -= 1
    return font(min_size, bold)


def draw_button(draw, button, pressed=False, theme=THEME):
    """Paint ``button`` (including its own background area) in its state."""
    if button.style == 'hidden':
        return
    x0, y0, x1, y1 = button.rect
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=theme.background)
    fill = mix(theme.surface, button.accent, theme.press_mix) if pressed else theme.surface
    edge = button.accent if pressed else theme.surface_edge
    radius = min(theme.radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=radius, fill=fill,
                           outline=edge, width=2 if pressed else 1)
    label_color = theme.text
    w, h = x1 - x0, y1 - y0
    if button.style == 'tile':
        icon_size = min(26, h // 2 - 2)
        cy_icon = y0 + h * 0.38
        if button.icon:
            draw_icon(draw, button.icon, (x0 + x1) / 2, cy_icon, icon_size, button.accent, fill)
        fnt = fit_font(draw, button.label, 13, w - 8)
        text_center(draw, ((x0 + x1) / 2, y0 + h * 0.78), button.label, fnt, label_color)
    else:  # 'bar'
        fnt = fit_font(draw, button.label, 15, w - 44, bold=True)
        left, _, right, _ = draw.textbbox((0, 0), button.label, font=fnt)
        icon_size = min(20, h - 16) if button.icon else 0
        total = (right - left) + (icon_size + 8 if button.icon else 0)
        start = (x0 + x1) / 2 - total / 2
        if button.icon:
            draw_icon(draw, button.icon, start + icon_size / 2, (y0 + y1) / 2, icon_size,
                      button.accent, fill)
            start += icon_size + 8
        text_center(draw, (start + (right - left) / 2, (y0 + y1) / 2), button.label, fnt, label_color)
