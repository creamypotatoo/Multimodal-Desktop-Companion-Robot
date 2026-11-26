"""UIManager: screen navigation, touch feedback and dirty-region rendering.

Threading model (one authoritative display owner):

  * ``tap()`` / ``handle_touch()`` / ``open()`` / ``back()`` / ``home()`` may
    be called from any thread (touch thread, preview GUI). They only update
    navigation state under a lock and return immediately.
  * ``render(now, expression)`` is called only by the EyeEngine worker, the
    single thread that talks to the display. It returns
      None            -> companion mode: the engine draws the eyes;
      (None, None)    -> a UI screen is showing and nothing changed (no SPI);
      (image, bbox)   -> send ``image``; ``bbox`` None = full frame, else the
                         exclusive changed region.

Touch feedback: a tapped button is drawn highlighted first and its action
runs once the highlight has been on screen for ``feedback_time`` seconds
(measured from when it was drawn, so a late frame never shortens it).
Only one press is in flight at a time, so a double tap cannot trigger two
actions. Navigation never depends on the conversation state, and conversation
updates never change the current screen.
"""
import threading
import time

from PIL import Image, ImageDraw

from display.renderer import union_bounds
from display.ui.screens import EYES, MENU, default_registry, status_style
from display.ui.theme import THEME
from display.ui.widgets import draw_button, hit_test

FEEDBACK_TIME = 0.12


class UIManager:
    def __init__(self, registry=None, *, theme=THEME, size=(320, 240),
                 feedback_time=FEEDBACK_TIME, clock=time.monotonic, on_change=None):
        self.registry = registry or default_registry()
        for required in (EYES, MENU):
            self.registry.get(required)
        self.theme = theme
        self.size = tuple(size)
        self.feedback_time = feedback_time
        self.on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._stack = [EYES]
        self._pressed = None          # (button, monotonic time of tap)
        self._pressed_drawn = None    # monotonic time the highlight was first drawn
        self._full = True
        self._dirty = []
        self._status_drawn = None
        self._canvas = Image.new('RGB', self.size, theme.background)
        self._draw = ImageDraw.Draw(self._canvas)
        self.taps = 0
        self.ignored_taps = 0

    # ----------------------------------------------------------- navigation
    @property
    def current(self):
        with self._lock:
            return self._stack[-1]

    @property
    def active(self):
        """True while a UI screen (not the eyes) is showing."""
        return self.current != EYES

    @property
    def stack(self):
        with self._lock:
            return tuple(self._stack)

    def open(self, screen_id):
        self.registry.get(screen_id)   # ValueError if unknown
        self._navigate(f'open:{screen_id}')

    def back(self):
        self._navigate('back')

    def home(self):
        self._navigate('home')

    def handle_touch(self, event):
        """TouchController callback."""
        return self.tap(event.x, event.y, getattr(event, 'time', None))

    def tap(self, x, y, now=None):
        """Handle a tap at screen (x, y). Returns the button id hit, or None."""
        now = self._clock() if now is None else now
        with self._lock:
            self.taps += 1
            if self._pressed is not None:          # previous press still showing feedback
                self.ignored_taps += 1
                return None
            screen = self.registry.get(self._stack[-1])
            button = hit_test(screen.buttons, x, y)
            if button is None:
                return None
            if screen.id == EYES:
                self._perform(button.action)       # no highlight over the eyes
            else:
                self._pressed, self._pressed_drawn = (button, now), None
                self._dirty.append(button.rect)
        self._notify()
        return button.id

    def invalidate(self):
        """Force the next UI frame to be sent in full (e.g. after a dropped frame)."""
        with self._lock:
            self._full = True

    def _navigate(self, action):
        with self._lock:
            self._perform(action)
        self._notify()

    def _perform(self, action):
        """Apply ``action``; caller holds the lock."""
        stack = self._stack
        if action == 'home':
            stack[:] = [EYES]
        elif action == 'back':
            if len(stack) > 1:
                stack.pop()
        elif action.startswith('open:'):
            target = action[5:]
            self.registry.get(target)
            if target == EYES:
                stack[:] = [EYES]
            elif target in stack:
                del stack[stack.index(target) + 1:]
            else:
                stack.append(target)
        else:
            raise ValueError(f'Unknown UI action {action!r}')
        self._pressed, self._pressed_drawn = None, None
        self._dirty = []
        self._full = True
        self._status_drawn = None

    def _notify(self):
        callback = self.on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    # ------------------------------------------------------------ rendering
    def render(self, now=None, expression=None):
        """Called by the display owner only. See the module docstring."""
        now = self._clock() if now is None else now
        with self._lock:
            if (self._pressed is not None and self._pressed_drawn is not None
                    and now - self._pressed_drawn >= self.feedback_time):
                self._perform(self._pressed[0].action)
            screen_id = self._stack[-1]
            if screen_id == EYES:
                return None
            screen = self.registry.get(screen_id)
            full, dirty = self._full, self._dirty
            pressed = self._pressed[0] if self._pressed is not None else None
            self._full, self._dirty = False, []
            status = status_style(expression)
            status_changed = screen.status_rect is not None and status != self._status_drawn
            self._status_drawn = status

        draw, theme = self._draw, self.theme
        if full:
            screen.draw(draw, theme, status)
            if pressed is not None:
                draw_button(draw, pressed, True, theme)
            rects = None
        else:
            rects = []
            for rect in dirty:
                for button in screen.buttons:
                    if button.rect == rect:
                        draw_button(draw, button, button is pressed, theme)
                        rects.append(rect)
            if status_changed:
                screen.draw_status(draw, theme, status)
                rects.append(screen.status_rect)
            if not rects:
                return None, None
        if pressed is not None:
            with self._lock:
                if (self._pressed is not None and self._pressed[0] is pressed
                        and self._pressed_drawn is None):
                    self._pressed_drawn = now
        return self._canvas, (None if rects is None else union_bounds(*rects))
