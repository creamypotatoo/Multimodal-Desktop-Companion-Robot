"""Live Timer, Stopwatch and Focus (Pomodoro) screens for Eli OS Phase 2.

Each screen renders its model through ``view(now)``: the mode (state, ...)
selects static layout and buttons; region values (digits, progress) change
over time and are the only areas redrawn and resent while a timer runs.
"""
from display.ui.apps import Countdown, Pomodoro, countdown_seconds, format_time
from display.ui.screens import WIDTH, Screen
from display.ui.theme import THEME, font, mix
from display.ui.widgets import Button, grid, text_center, text_left

PHASE_STYLE = {
    Pomodoro.FOCUS: ('Focus', (255, 143, 163)),
    Pomodoro.SHORT: ('Short break', (143, 240, 200)),
    Pomodoro.LONG: ('Long break', (195, 166, 255)),
}
FOCUS_MESSAGES = ("You've got this. One step at a time.", 'Deep focus mode: engaged.',
                  "Stay with it. I'm right here.", 'Small steps add up to big progress.')


def _clear(draw, theme, rect):
    draw.rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=theme.background)


def _text_right(draw, xy, text, fnt, fill):
    x, y = xy
    left, _, right, _ = draw.textbbox((0, 0), text, font=fnt)
    text_left(draw, (x - (right - left), y), text, fnt, fill)


class AppScreen(Screen):
    """Header with icon-only Back and Companion buttons; mode-dependent controls."""

    def __init__(self, utility, model, title=None):
        self.utility, self.model = utility, model
        self.id, self.title = utility.id, title or utility.title
        self.nav = (Button('back', (4, 2, 60, 46), '', 'back', 'back', THEME.text, 'icon'),
                    Button('home', (260, 2, 316, 46), '', 'home', 'companion', THEME.accent, 'icon'))
        self._controls = {}

    @property
    def buttons(self):
        return self.buttons_for(None)

    def buttons_for(self, mode):
        state = self.model.state if mode is None else mode[0]
        return self.nav + self._controls.get(state, ())

    def draw(self, draw, theme, status, mode=None):
        super().draw(draw, theme, status, mode)
        text_center(draw, (WIDTH / 2, 23), self.title, font(18, True), theme.text)
        draw.rounded_rectangle((WIDTH / 2 - 12, 39, WIDTH / 2 + 12, 41), radius=1, fill=self.utility.accent)
        self.draw_static(draw, theme, mode)

    def draw_static(self, draw, theme, mode):
        pass

    def bar(self, draw, theme, rect, filled, color):
        _clear(draw, theme, rect)
        x0, y0, x1, y1 = rect
        r = (y1 - y0) // 2
        draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=r, fill=theme.surface)
        if filled > 0:
            draw.rounded_rectangle((x0, y0, x0 + max(filled, 2 * r), y1 - 1), radius=r, fill=color)


def _control(id, rect, label, action, icon, accent=THEME.text, style='bar'):
    return Button(id, rect, label, action, icon, accent, style)


class TimerScreen(AppScreen):
    COLUMNS = (('h', (10, 78)), ('m', (86, 154)), ('s', (162, 230)))
    TIME, BAR = (8, 50, 312, 114), (24, 124, 296, 136)

    def __init__(self, utility, model):
        super().__init__(utility, model)
        accent = utility.accent
        setup = []
        for field, (x0, x1) in self.COLUMNS:
            setup.append(_control(f'{field}+', (x0, 48, x1, 86), '', f'do:{field}+1', 'plus', accent, 'icon'))
            setup.append(_control(f'{field}-', (x0, 128, x1, 166), '', f'do:{field}-1', 'minus', accent, 'icon'))
        setup.append(_control('start', (238, 48, 312, 166), 'Start', 'do:toggle', 'play', accent, 'tile'))
        for seconds, rect in zip(Countdown.PRESETS, grid((8, 174, 312, 232), 1, 5, 6)):
            setup.append(_control(f'preset-{seconds // 60}', rect, f'{seconds // 60} min',
                                  f'do:preset:{seconds}', None, accent))
        self.regions = {field: (x0, 88, x1, 126) for field, (x0, x1) in self.COLUMNS}
        self.regions.update(time=self.TIME, bar=self.BAR)
        rest = (_control('reset', (130, 180, 218, 232), 'Reset', 'do:reset', 'reset'),
                _control('cancel', (224, 180, 312, 232), 'Cancel', 'do:cancel', 'cancel'))
        primary = {Countdown.READY: ('Start', 'play'), Countdown.RUNNING: ('Pause', 'pause'),
                   Countdown.PAUSED: ('Resume', 'play'), Countdown.DONE: ('Restart', 'play')}
        self._controls = {Countdown.SETUP: tuple(setup)}
        for state, (label, icon) in primary.items():
            self._controls[state] = (_control('toggle', (8, 180, 124, 232), label, 'do:toggle',
                                              icon, accent),) + rest

    def view(self, now):
        m = self.model
        if m.state == Countdown.SETUP:
            h, mi, s = m.fields
            return (m.state,), {'h': h, 'm': mi, 's': s}
        width = self.BAR[2] - self.BAR[0]
        return (m.state, m.duration), {
            'time': format_time(countdown_seconds(m.remaining(now))),
            'bar': int(m.progress(now) * width)}

    def handle(self, command, now):
        m = self.model
        if command[0] in 'hms' and command[1] in '+-':
            m.adjust(command[0], int(command[1:]))
        elif command.startswith('preset:'):
            m.cancel()
            m.start(now, int(command[7:]))
        elif command == 'toggle':
            m.toggle(now)
        elif command == 'reset':
            m.reset()
        elif command == 'cancel':
            m.cancel()
        else:
            super().handle(command, now)

    def draw_static(self, draw, theme, mode):
        m = self.model
        if m.state == Countdown.SETUP:
            return
        text = {Countdown.READY: f'Ready · {format_time(m.duration)}',
                Countdown.RUNNING: f'Counting down from {format_time(m.duration)}',
                Countdown.PAUSED: 'Paused',
                Countdown.DONE: "Time's up!"}[m.state]
        color = self.utility.accent if m.state == Countdown.DONE else theme.muted
        text_center(draw, (WIDTH / 2, 156), text, font(14, m.state == Countdown.DONE), color)

    def draw_region(self, draw, theme, name, value):
        rect = self.regions[name]
        if name == 'bar':
            self.bar(draw, theme, rect, value, self.utility.accent)
            return
        _clear(draw, theme, rect)
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        if name == 'time':
            color = {Countdown.DONE: self.utility.accent, Countdown.PAUSED: theme.muted}.get(
                self.model.state, theme.text)
            text_center(draw, (cx, cy), value, font(46, True), color)
        else:  # h / m / s setup digits
            text_center(draw, (cx - 7, cy), f'{value:02d}', font(30, True), theme.text)
            text_left(draw, (cx + 17, cy + 5), name, font(13), theme.muted)


class StopwatchScreen(AppScreen):
    TIME, LAPS = (8, 50, 312, 104), (12, 108, 308, 178)

    def __init__(self, utility, model):
        super().__init__(utility, model)
        a = utility.accent
        left, right = (8, 182, 156, 232), (164, 182, 312, 232)
        reset = _control('reset', right, 'Reset', 'do:reset', 'reset')
        self._controls = {
            'idle': (_control('toggle', left, 'Start', 'do:toggle', 'play', a), reset),
            'running': (_control('toggle', left, 'Pause', 'do:toggle', 'pause', a),
                        _control('lap', right, 'Lap', 'do:lap', 'lap', a)),
            'paused': (_control('toggle', left, 'Resume', 'do:toggle', 'play', a), reset),
        }
        self.regions = {'time': self.TIME, 'laps': self.LAPS}

    def view(self, now):
        m = self.model
        return (m.state,), {'time': format_time(m.elapsed(now), tenths=True),
                            'laps': tuple(m.laps[-3:])}

    def handle(self, command, now):
        actions = {'toggle': self.model.toggle, 'lap': self.model.lap,
                   'reset': lambda now: self.model.reset()}
        if command not in actions:
            super().handle(command, now)
        actions[command](now)

    def draw_region(self, draw, theme, name, value):
        rect = self.regions[name]
        _clear(draw, theme, rect)
        if name == 'time':
            color = theme.muted if self.model.state == 'paused' else theme.text
            text_center(draw, ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), value,
                        font(44, True), color)
            return
        if not value:
            text_center(draw, (WIDTH / 2, 142), 'Tap Lap while running to record splits',
                        font(12), theme.muted)
            return
        fnt, bold = font(14), font(14, True)
        for i, (number, split, total) in enumerate(reversed(value)):
            y = rect[1] + 12 + i * 23
            text_left(draw, (22, y), f'Lap {number}', fnt, self.utility.accent if i == 0 else theme.muted)
            _text_right(draw, (196, y), format_time(split, tenths=True), bold, theme.text)
            _text_right(draw, (298, y), format_time(total, tenths=True), fnt, theme.muted)


class FocusScreen(AppScreen):
    RING = (14, 48, 130, 164)

    def __init__(self, utility, model, settings_id):
        super().__init__(utility, model)
        a = utility.accent
        rest = (_control('skip', (106, 184, 176, 232), 'Skip', 'do:skip', None),
                _control('reset', (182, 184, 252, 232), 'Reset', 'do:reset', None),
                _control('durations', (258, 184, 312, 232), '', f'open:{settings_id}', 'settings',
                         THEME.text, 'icon'))
        primary = {'ready': ('Start', 'play'), 'running': ('Pause', 'pause'), 'paused': ('Resume', 'play')}
        self._controls = {state: (_control('toggle', (8, 184, 100, 232), label, 'do:toggle', icon, a),) + rest
                          for state, (label, icon) in primary.items()}
        self.regions = {'ring': self.RING}

    def view(self, now):
        m = self.model
        return (m.state, m.phase, m.completed), {
            'ring': (format_time(countdown_seconds(m.remaining(now))), int(m.progress(now) * 120) * 3)}

    def handle(self, command, now):
        m = self.model
        actions = {'toggle': m.toggle, 'skip': m.skip, 'reset': lambda now: m.reset()}
        if command not in actions:
            super().handle(command, now)
        actions[command](now)

    def message(self):
        m = self.model
        if m.state == 'paused':
            return 'Paused. Take a breath, then carry on.'
        if m.phase == Pomodoro.LONG:
            return f'{m.long_every} sessions done. Enjoy a long rest!'
        if m.phase == Pomodoro.SHORT:
            return 'Stretch, sip some water, breathe.'
        if m.state == 'ready':
            return 'Ready when you are. Tap Start.' if not m.completed else \
                f'Ready for session {m.completed + 1}?'
        return FOCUS_MESSAGES[m.completed % len(FOCUS_MESSAGES)]

    def draw_static(self, draw, theme, mode):
        m = self.model
        label, color = PHASE_STYLE[m.phase]
        text_center(draw, (226, 64), label, font(17, True), color)
        filled = m.long_every if m.phase == Pomodoro.LONG else m.cycle_position
        for i in range(m.long_every):
            x = 226 + (i - (m.long_every - 1) / 2) * 20
            if i < filled:
                draw.ellipse((x - 5, 86, x + 5, 96), fill=color)
            else:
                draw.ellipse((x - 5, 86, x + 5, 96), outline=mix(theme.background, color, 0.6), width=2)
        session = f'Session {m.completed + 1}' if m.phase == Pomodoro.FOCUS else f'{m.completed} completed'
        text_center(draw, (226, 112), session, font(13), theme.muted)
        self.draw_face(draw, theme, 226, 142)
        text_center(draw, (WIDTH / 2, 172), self.message(), font(12), theme.text)

    def draw_face(self, draw, theme, cx, cy):
        """A tiny robot face that mirrors the session: focused, happy, sleepy, calm."""
        c, bg, m = theme.accent, theme.background, self.model
        for side in (-1, 1):
            x = cx + side * 14
            if m.state == 'paused':
                draw.rounded_rectangle((x - 9, cy - 1, x + 9, cy + 3), radius=2, fill=c)
            elif m.phase != Pomodoro.FOCUS:          # happy ^ ^
                draw.arc((x - 9, cy - 6, x + 9, cy + 12), 200, 340, fill=c, width=4)
            else:
                draw.rounded_rectangle((x - 9, cy - 9, x + 9, cy + 9), radius=5, fill=c)
                if m.state == 'running':             # determined brows
                    inner, outer = (cy - 2, cy - 10) if side < 0 else (cy - 10, cy - 2)
                    left, right = (outer, inner) if side < 0 else (inner, outer)
                    draw.polygon(((x - 11, cy - 11), (x + 11, cy - 11), (x + 11, right), (x - 11, left)),
                                 fill=bg)

    def draw_region(self, draw, theme, name, value):
        text, degrees = value
        x0, y0, x1, y1 = self.RING
        _clear(draw, theme, self.RING)
        color = PHASE_STYLE[self.model.phase][1]
        box = (x0 + 4, y0 + 4, x1 - 5, y1 - 5)
        draw.ellipse(box, outline=theme.surface_edge, width=8)
        if degrees:
            draw.arc(box, -90, -90 + degrees, fill=color, width=8)
        text_center(draw, ((x0 + x1) / 2, (y0 + y1) / 2), text, font(24, True),
                    theme.muted if self.model.state == 'paused' else theme.text)


class FocusSettingsScreen(AppScreen):
    ROWS = (('focus', 'Focus'), ('short', 'Short break'), ('long', 'Long break'))

    def __init__(self, utility, model):
        super().__init__(utility, model, 'Focus durations')
        self.id = 'focus_settings'
        a = utility.accent
        controls, self.regions = [], {}
        for i, (key, _) in enumerate(self.ROWS):
            y0 = 52 + i * 46
            controls += [_control(f'{key}-', (150, y0, 198, y0 + 40), '', f'do:{key}:-1', 'minus', a, 'icon'),
                         _control(f'{key}+', (264, y0, 312, y0 + 40), '', f'do:{key}:1', 'plus', a, 'icon')]
            self.regions[key] = (200, y0, 262, y0 + 40)
        controls.append(_control('done', (8, 194, 312, 234), 'Done', 'back', None, a))
        self._controls = {'settings': tuple(controls)}

    def buttons_for(self, mode):
        return self.nav + self._controls['settings']

    def view(self, now):
        return ('settings',), dict(self.model.minutes)

    def handle(self, command, now):
        key, _, delta = command.partition(':')
        if key not in self.model.LIMITS:
            super().handle(command, now)
        self.model.adjust(key, int(delta))

    def draw_static(self, draw, theme, mode):
        for i, (_, label) in enumerate(self.ROWS):
            text_left(draw, (16, 72 + i * 46), label, font(15), theme.text)

    def draw_region(self, draw, theme, name, value):
        rect = self.regions[name]
        _clear(draw, theme, rect)
        text_center(draw, ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), f'{value} min',
                    font(15, True), theme.text)


def app_screens(apps, utilities):
    settings = FocusSettingsScreen(utilities['focus'], apps.pomodoro)
    return [TimerScreen(utilities['timer'], apps.timer),
            StopwatchScreen(utilities['stopwatch'], apps.stopwatch),
            FocusScreen(utilities['focus'], apps.pomodoro, settings.id),
            settings]
