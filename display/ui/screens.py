"""Eli OS screens and the utility screen registry.

Every screen is static between taps: it is drawn in full once when it
becomes visible, and afterwards only a pressed button or the status pill is
redrawn and resent. The ``eyes`` screen draws nothing; while it is current
the EyeEngine renders the animated eyes as usual.

Phase 1 utility screens are clearly labelled placeholders; no timer, alarm
or other utility logic exists yet.
"""
from dataclasses import dataclass

from display.ui.icons import draw_icon
from display.ui.theme import STATUS, THEME, font, mix
from display.ui.widgets import Button, draw_button, grid, text_center, text_left

WIDTH, HEIGHT = 320, 240
EYES, MENU = 'eyes', 'menu'
HEADER_H = 44
STATUS_RECT = (206, 9, 312, 35)


@dataclass(frozen=True)
class Utility:
    id: str
    title: str
    icon: str
    accent: tuple
    description: str


UTILITIES = (
    Utility('timer', 'Timer', 'timer', (255, 179, 138), 'Countdown timers'),
    Utility('stopwatch', 'Stopwatch', 'stopwatch', (143, 240, 200), 'Lap timing'),
    Utility('focus', 'Focus', 'focus', (255, 143, 163), 'Pomodoro focus sessions'),
    Utility('clock', 'Clock', 'clock', (142, 201, 255), 'Time and date'),
    Utility('alarm', 'Alarm', 'alarm', (255, 224, 138), 'Wake-up alarms'),
    Utility('calendar', 'Calendar', 'calendar', (195, 166, 255), 'Upcoming events'),
    Utility('sleep', 'Sleep', 'sleep', (154, 168, 255), 'Rest mode for Eli'),
    Utility('settings', 'Settings', 'settings', (184, 196, 214), 'Device preferences'),
    Utility('companion', 'Companion', 'companion', THEME.accent, 'Back to Eli'),
)


def status_style(expression):
    """(label, colour) for the conversation status pill, or None."""
    if expression is None:
        return None
    name = getattr(expression, 'name', str(expression)).upper()
    return STATUS.get(name, (name.title(), THEME.accent))


class Screen:
    """Static by default. Live screens override ``view``/``buttons_for``/
    ``draw_region``/``handle``: ``view(now)`` returns ``(mode, {region: value})``;
    a new mode redraws the whole screen, a changed value only its region in
    ``regions``. Buttons with action ``do:<command>`` call ``handle``."""
    id = ''
    title = ''
    buttons = ()
    regions = {}
    status_rect = None     # region redrawn when the conversation status changes

    def view(self, now):
        return None

    def buttons_for(self, mode):
        return self.buttons

    def draw_region(self, draw, theme, name, value):
        pass

    def handle(self, command, now):
        raise ValueError(f'{self.id} has no command {command!r}')

    def draw(self, draw, theme, status, mode=None):
        """Paint the whole screen (buttons unpressed)."""
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=theme.background)
        for button in self.buttons_for(mode):
            draw_button(draw, button, False, theme)
        if self.status_rect:
            self.draw_status(draw, theme, status)

    def draw_status(self, draw, theme, status):
        x0, y0, x1, y1 = self.status_rect
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=theme.background)
        if status is None:
            return
        label, color = status
        draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=(y1 - y0) // 2,
                               fill=mix(theme.background, color, 0.14), outline=mix(theme.background, color, 0.5))
        cy = (y0 + y1) / 2
        draw.ellipse((x0 + 9, cy - 4, x0 + 17, cy + 4), fill=color)
        text_left(draw, (x0 + 24, cy), label, font(12), theme.text)


class EyesScreen(Screen):
    """Companion mode: the animated eyes. Any tap opens the menu."""
    id = EYES
    title = 'Companion'
    buttons = (Button('open-menu', (0, 0, WIDTH, HEIGHT), action=f'open:{MENU}', style='hidden'),)

    def draw(self, draw, theme, status, mode=None):  # the EyeEngine renders this screen
        pass


class MenuScreen(Screen):
    """3 x 3 grid of utilities below a header with the conversation status."""
    id = MENU
    title = 'Menu'
    status_rect = STATUS_RECT

    def __init__(self, utilities=UTILITIES):
        if len(utilities) != 9:
            raise ValueError('The main menu is a 3 x 3 grid of nine entries')
        cells = grid((6, HEADER_H, WIDTH - 6, HEIGHT - 6), 3, 3, 6)
        self.buttons = tuple(
            Button(u.id, rect, u.title, 'home' if u.id == 'companion' else f'open:{u.id}',
                   u.icon, u.accent, 'tile')
            for u, rect in zip(utilities, cells))

    def draw(self, draw, theme, status, mode=None):
        super().draw(draw, theme, status, mode)
        draw.ellipse((14, 18, 22, 26), fill=theme.accent)
        right = text_left(draw, (30, 22), 'Eli', font(19, True), theme.text)
        text_left(draw, (right + 8, 23), 'Menu', font(14), theme.muted)


class PlaceholderScreen(Screen):
    """A labelled placeholder for a utility arriving in a later phase."""

    def __init__(self, utility):
        self.utility = utility
        self.id = utility.id
        self.title = utility.title
        self.buttons = (
            Button('back', (8, 184, 156, 232), 'Back', 'back', 'back', THEME.text, 'bar'),
            Button('home', (164, 184, 312, 232), 'Companion', 'home', 'companion', THEME.accent, 'bar'),
        )

    def draw(self, draw, theme, status, mode=None):
        super().draw(draw, theme, status, mode)
        u = self.utility
        text_center(draw, (WIDTH / 2, 22), u.title, font(19, True), theme.text)
        draw.rounded_rectangle((WIDTH / 2 - 14, 38, WIDTH / 2 + 14, 40), radius=1, fill=u.accent)
        cx, cy = WIDTH / 2, 92
        draw.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), fill=mix(theme.background, u.accent, 0.12),
                     outline=mix(theme.background, u.accent, 0.4), width=2)
        draw_icon(draw, u.icon, cx, cy, 38, u.accent, mix(theme.background, u.accent, 0.12))
        text_center(draw, (cx, 143), u.description, font(15), theme.text)
        text_center(draw, (cx, 164), 'Placeholder · coming in a later phase', font(12), theme.muted)


class ScreenRegistry:
    """Screens by id. ``eyes`` and ``menu`` are always present."""

    def __init__(self, screens=()):
        self._screens = {}
        for screen in screens:
            self.register(screen)

    def register(self, screen):
        if not screen.id:
            raise ValueError('Screen needs an id')
        self._screens[screen.id] = screen
        return screen

    def get(self, screen_id):
        try:
            return self._screens[screen_id]
        except KeyError:
            raise ValueError(f'Unknown screen {screen_id!r}') from None

    def __contains__(self, screen_id):
        return screen_id in self._screens

    def ids(self):
        return tuple(self._screens)


def default_registry(utilities=UTILITIES, apps=None):
    """Eyes, menu, the live Timer/Stopwatch/Focus screens (when ``apps`` is
    given) and labelled placeholders for the remaining utilities."""
    screens = [EyesScreen(), MenuScreen(utilities)]
    live = {}
    if apps is not None:
        from display.ui.app_screens import app_screens
        live = {screen.id: screen for screen in app_screens(apps, {u.id: u for u in utilities})}
    for u in utilities:
        if u.id != 'companion':
            screens.append(live.pop(u.id, None) or PlaceholderScreen(u))
    return ScreenRegistry(screens + list(live.values()))
