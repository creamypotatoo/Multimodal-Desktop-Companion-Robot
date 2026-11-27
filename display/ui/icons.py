"""Simple vector icons drawn with Pillow primitives (no image assets).

``draw_icon(draw, name, cx, cy, size, color, background)`` paints the icon
centred on (cx, cy) inside a ``size`` x ``size`` square. ``background`` is
the colour underneath, used for cut-outs (moon, gear hole).
"""
import math


def _r(cx, cy, r):
    return (cx - r, cy - r, cx + r, cy + r)


def _timer(d, cx, cy, s, c, bg, w):
    h, half = s // 2, s // 3
    d.line((cx - half, cy - h, cx + half, cy - h), fill=c, width=w)
    d.line((cx - half, cy + h, cx + half, cy + h), fill=c, width=w)
    top = ((cx - half + 2, cy - h + 2), (cx + half - 2, cy - h + 2), (cx, cy))
    d.line(top + top[:1], fill=c, width=w, joint='curve')   # polygon(width=) needs Pillow 9.1
    d.polygon(((cx, cy), (cx + half - 2, cy + h - 2), (cx - half + 2, cy + h - 2)), fill=c)


def _stopwatch(d, cx, cy, s, c, bg, w):
    r = s * 0.42
    cy2 = cy + s * 0.06
    d.ellipse(_r(cx, cy2, r), outline=c, width=w)
    d.rectangle((cx - w, cy2 - r - w * 2.5, cx + w, cy2 - r), fill=c)
    d.line((cx + r * 0.75, cy2 - r * 0.9, cx + r * 0.95, cy2 - r * 0.7), fill=c, width=w)
    d.line((cx, cy2, cx + r * 0.45, cy2 - r * 0.5), fill=c, width=w)


def _focus(d, cx, cy, s, c, bg, w):
    r = s * 0.46
    d.ellipse(_r(cx, cy, r), outline=c, width=w)
    d.ellipse(_r(cx, cy, r * 0.58), outline=c, width=w)
    d.ellipse(_r(cx, cy, r * 0.2), fill=c)


def _clock(d, cx, cy, s, c, bg, w):
    r = s * 0.46
    d.ellipse(_r(cx, cy, r), outline=c, width=w)
    d.line((cx, cy, cx, cy - r * 0.62), fill=c, width=w)
    d.line((cx, cy, cx + r * 0.45, cy + r * 0.12), fill=c, width=w)
    d.ellipse(_r(cx, cy, w), fill=c)


def _alarm(d, cx, cy, s, c, bg, w):
    r = s * 0.34
    top = cy - s * 0.38
    d.pieslice((cx - r, top, cx + r, top + 2 * r), 180, 360, fill=c)
    d.rectangle((cx - r, top + r, cx + r, cy + s * 0.2), fill=c)
    d.rounded_rectangle((cx - r - s * 0.1, cy + s * 0.16, cx + r + s * 0.1, cy + s * 0.26),
                        radius=2, fill=c)
    d.ellipse(_r(cx, cy + s * 0.36, s * 0.08), fill=c)


def _calendar(d, cx, cy, s, c, bg, w):
    h = s * 0.42
    box = (cx - h, cy - h * 0.8, cx + h, cy + h)
    d.rounded_rectangle(box, radius=max(2, s // 10), outline=c, width=w)
    d.rectangle((box[0], box[1], box[2], box[1] + h * 0.45), fill=c)
    for dx in (-h * 0.5, h * 0.5):
        d.line((cx + dx, box[1] - s * 0.1, cx + dx, box[1] + 2), fill=c, width=w)
    dot = max(1, s // 16)
    for row in range(2):
        for col in range(3):
            x = cx + (col - 1) * h * 0.55
            y = cy + h * 0.15 + row * h * 0.42
            d.ellipse(_r(x, y, dot), fill=c)


def _sleep(d, cx, cy, s, c, bg, w):
    r = s * 0.36
    mx, my = cx - s * 0.06, cy + s * 0.04
    d.ellipse(_r(mx, my, r), fill=c)
    d.ellipse(_r(mx + r * 0.55, my - r * 0.4, r * 0.85), fill=bg)
    zs = s * 0.16
    zx, zy = cx + s * 0.2, cy - s * 0.38
    d.line(((zx, zy), (zx + zs, zy), (zx, zy + zs), (zx + zs, zy + zs)), fill=c, width=max(1, w - 1))


def _settings(d, cx, cy, s, c, bg, w):
    outer, inner = s * 0.46, s * 0.33
    points = []
    teeth = 8
    for i in range(teeth * 4):
        angle = 2 * math.pi * i / (teeth * 4)
        r = outer if (i % 4) in (1, 2) else inner
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=c)
    d.ellipse(_r(cx, cy, s * 0.14), fill=bg)


def _companion(d, cx, cy, s, c, bg, w):
    ew, eh, gap = s * 0.3, s * 0.5, s * 0.14
    for side in (-1, 1):
        x = cx + side * (gap / 2 + ew / 2)
        d.rounded_rectangle((x - ew / 2, cy - eh / 2, x + ew / 2, cy + eh / 2),
                            radius=max(2, int(ew / 3)), fill=c)


def _back(d, cx, cy, s, c, bg, w):
    h = s * 0.3
    d.line(((cx + h * 0.5, cy - h), (cx - h * 0.5, cy), (cx + h * 0.5, cy + h)),
           fill=c, width=w + 1, joint='curve')


ICONS = {
    'timer': _timer, 'stopwatch': _stopwatch, 'focus': _focus, 'clock': _clock,
    'alarm': _alarm, 'calendar': _calendar, 'sleep': _sleep, 'settings': _settings,
    'companion': _companion, 'back': _back,
}


def draw_icon(draw, name, cx, cy, size, color, background):
    if name not in ICONS:
        raise ValueError(f'Unknown icon {name!r}')
    width = max(2, round(size / 11))
    ICONS[name](draw, cx, cy, size, color, background, width)
