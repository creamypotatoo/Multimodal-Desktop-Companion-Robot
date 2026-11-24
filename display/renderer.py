"""Pillow renderer for Eli's eyes.

Rendering happens in two steps so callers can cheaply skip redundant frames:

  ``layout(state)``  pure geometry: integer pixel coordinates for every shape.
                     Two equal layouts produce identical pixels.
  ``draw(layout)``   paints that geometry into a reused RGB buffer.

The returned image is the renderer's own buffer and is overwritten by the next
``draw``; display backends must copy (or finish sending) it before then.
Only rounded rectangles, polygons and ellipses are used, which Pillow draws
quickly on a Raspberry Pi 4.
"""
import math
from typing import NamedTuple, Optional, Tuple

from PIL import Image, ImageDraw

from display.config import EyeConfig

PERSPECTIVE = 0.06      # near eye grows / far eye shrinks with horizontal gaze
CLOSE_DROP = 0.2        # closing eyes sink slightly, as if the upper lid leads
SMILE_SPREAD = 0.22     # how far the smile cut extends past the eye, x eye width
STAR_SIZE = 7


class EyeGeometry(NamedTuple):
    box: Tuple[int, int, int, int]                  # inclusive x0, y0, x1, y1
    radius: int
    top_lid: Optional[Tuple[Tuple[int, int], ...]]  # black polygon or None
    bottom_lid: Optional[Tuple[int, int, int, int]] # black ellipse bbox or None
    sparkles: Tuple[Tuple[int, int, int], ...]      # (cx, cy, r) highlights
    star: Optional[Tuple[Tuple[int, int], ...]]     # twinkle polygon or None
    bounds: Tuple[int, int, int, int]               # exclusive dirty bounds


def union_bounds(*boxes):
    """Union of exclusive ``(x0, y0, x1, y1)`` boxes, ignoring ``None``."""
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _shift_into(low, high, limit_low, limit_high):
    """Offset that moves the span [low, high] inside [limit_low, limit_high]."""
    if high - low > limit_high - limit_low:
        return (limit_low + limit_high) / 2 - (low + high) / 2
    if low < limit_low:
        return limit_low - low
    if high > limit_high:
        return limit_high - high
    return 0.0


def _star(cx, cy, size):
    points = []
    for i in range(8):
        angle = math.pi / 4 * i - math.pi / 2
        r = size if i % 2 == 0 else size * 0.32
        points.append((round(cx + r * math.cos(angle)), round(cy + r * math.sin(angle))))
    return tuple(points)


class EyeRenderer:
    def __init__(self, config=None):
        self.config = config or EyeConfig()
        k = self.config.supersample
        self._canvas = Image.new('RGB', (self.config.frame_width * k,
                                         self.config.frame_height * k), self.config.background)
        self._draw = ImageDraw.Draw(self._canvas)

    @property
    def size(self):
        return self.config.size

    # --------------------------------------------------------------- layout
    def layout(self, state):
        cfg = self.config
        width, height = cfg.size
        dx = state.gaze_x * cfg.max_gaze_x
        dy = state.gaze_y * cfg.max_gaze_y
        frames = []
        for side, pose in ((-1, state.left), (1, state.right)):
            persp = 1.0 + PERSPECTIVE * state.gaze_x * side
            w = cfg.eye_width * pose.scale_x * persp
            full_h = cfg.eye_height * pose.scale_y * persp
            h = max(float(cfg.closed_thickness), full_h * pose.openness)
            cx = width / 2 + cfg.offset_x + side * (cfg.spacing + cfg.eye_width) / 2 + dx
            cy = (height / 2 + cfg.offset_y + pose.offset_y * cfg.eye_height + dy
                  + (full_h - h) * CLOSE_DROP)
            frames.append([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, pose,
                           full_h * pose.openness <= cfg.closed_thickness + 0.5])

        # Keep both eyes on screen: shift the pair horizontally (preserving the
        # spacing), and each eye vertically (preserving asymmetric poses).
        m = cfg.margin
        sx = _shift_into(min(f[0] for f in frames), max(f[2] for f in frames), m, width - 1 - m)
        for f in frames:
            sy = _shift_into(f[1], f[3], m, height - 1 - m)
            f[0] += sx
            f[2] += sx
            f[1] += sy
            f[3] += sy
        gap = frames[1][0] - frames[0][2]

        result = []
        for index, (x0, y0, x1, y1, pose, closed) in enumerate(frames):
            side = -1 if index == 0 else 1
            box = (round(x0), round(y0), round(x1), round(y1))
            w, h = x1 - x0, y1 - y0
            if closed:
                radius = max(0, min(box[3] - box[1], box[2] - box[0]) // 2)
                result.append(EyeGeometry(box, radius, None, None, (), None,
                                          (box[0], box[1], box[2] + 1, box[3] + 1)))
                continue
            scale = min(pose.scale_x, pose.scale_y)
            radius = max(0, min(round(cfg.corner_radius * scale),
                                (box[2] - box[0]) // 2, (box[3] - box[1]) // 2))

            top_lid = None
            if pose.lid_top > 0.005 or abs(pose.lid_tilt) > 0.005:
                inner = y0 + h * pose.lid_top
                outer = y0 + h * (pose.lid_top + 0.5 * pose.lid_tilt)
                inner, outer = (min(max(v, y0 - 3), y1 + 1) for v in (inner, outer))
                if max(inner, outer) > y0 + 0.5:
                    left_y, right_y = (outer, inner) if side < 0 else (inner, outer)
                    top = round(y0) - 3
                    top_lid = ((round(x0) - 3, top), (round(x1) + 3, top),
                               (round(x1) + 3, round(right_y)), (round(x0) - 3, round(left_y)))

            bottom_lid = None
            if pose.lid_bottom > 0.005:
                spread = max(1.0, min(w * SMILE_SPREAD, gap / 2 - 2))
                bottom_lid = (round(x0 - spread), round(y1 - h * pose.lid_bottom),
                              round(x1 + spread), round(y1 + h * 0.9))

            sparkles, star = (), None
            bounds = (box[0], box[1], box[2] + 1, box[3] + 1)
            if state.sparkle > 0.05:
                twinkle = state.twinkle if side > 0 else 1.0 - state.twinkle
                r = w * 0.1 * state.sparkle * (0.8 + 0.2 * twinkle)
                lid_floor = y0 + h * (pose.lid_top + max(0.0, 0.5 * pose.lid_tilt))
                hx, hy = x0 + w * 0.3, max(y0 + h * 0.3, lid_floor + r + 2)
                if r >= 1.5 and hy + r < y1 - h * pose.lid_bottom:
                    sparkles = ((round(hx), round(hy), round(r)),
                                (round(x0 + w * 0.47), round(hy + r * 1.6), max(1, round(r * 0.45))))
                size = STAR_SIZE * state.sparkle * (0.35 + 0.65 * twinkle)
                if size >= 2:
                    scx = x0 - 11 if side < 0 else x1 + 11
                    scx = min(max(scx, size + 1), width - 2 - size)
                    scy = max(y0 + 4, size + 1)
                    star = _star(scx, scy, size)
                    xs, ys = [p[0] for p in star], [p[1] for p in star]
                    bounds = union_bounds(bounds, (min(xs), min(ys), max(xs) + 1, max(ys) + 1))
            result.append(EyeGeometry(box, radius, top_lid, bottom_lid, sparkles, star, bounds))
        return tuple(result)

    @staticmethod
    def bounds(layout):
        """Exclusive bounding box of everything visible in ``layout``."""
        return union_bounds(*(eye.bounds for eye in layout))

    # ----------------------------------------------------------------- draw
    def draw(self, layout):
        cfg = self.config
        k = cfg.supersample
        draw = self._draw
        draw.rectangle((0, 0, self._canvas.width - 1, self._canvas.height - 1), fill=cfg.background)

        def s(points):
            return [v * k for v in points] if k > 1 else points

        for eye in layout:
            draw.rounded_rectangle(s(eye.box), radius=eye.radius * k, fill=cfg.color)
            if eye.top_lid:
                draw.polygon([(x * k, y * k) for x, y in eye.top_lid], fill=cfg.background)
            if eye.bottom_lid:
                draw.ellipse(s(eye.bottom_lid), fill=cfg.background)
            for cx, cy, r in eye.sparkles:
                draw.ellipse(s((cx - r, cy - r, cx + r, cy + r)), fill=cfg.sparkle_color)
            if eye.star:
                draw.polygon([(x * k, y * k) for x, y in eye.star], fill=cfg.sparkle_color)
        if k > 1:
            return self._canvas.reduce(k)
        return self._canvas

    def render(self, state):
        return self.draw(self.layout(state))
