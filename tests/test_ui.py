"""Eli OS UI navigation, rendering, display ownership and robot integration tests."""
import contextlib
import io
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import main
from behavior.state_machine import RobotStateMachine, State
from display import robot_eyes, robot_touch, ui_demo
from display.config import AnimationConfig
from display.expressions import Expression
from display.eyes import EyeEngine
from display.touch import TouchCalibration, TouchController
from display.ui import EYES, MENU, UTILITIES, UIManager, default_registry
from display.ui.icons import ICONS, draw_icon
from display.ui.screens import STATUS_RECT
from display.ui.widgets import Button, grid, hit_test
from hardware.mock_hardware import MockDisplay, MockTouch

QUIET = AnimationConfig(blinking=False, auto_gaze=False)
CENTER = lambda r: ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)  # noqa: E731


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def press(ui, button_id, now):
    """Tap a button on the current screen and let its feedback complete."""
    screen = ui.registry.get(ui.current)
    button = next(b for b in screen.buttons if b.id == button_id)
    assert ui.tap(*CENTER(button.rect), now=now) == button_id
    ui.render(now)                         # highlight drawn
    return ui.render(now + 0.2)            # action performed, new screen drawn


class RecordingDisplay(MockDisplay):
    """Mock display that records which thread calls show() and each bbox."""

    def __init__(self, fail_shows=0):
        super().__init__()
        self.threads = set()
        self.bboxes = []
        self.fail_shows = fail_shows

    def show(self, image, bbox=None):
        self.threads.add(threading.current_thread().name)
        if self.fail_shows:
            self.fail_shows -= 1
            return False
        super().show(image, bbox)
        self.bboxes.append(bbox)


class WidgetTests(unittest.TestCase):
    def test_grid_fills_area_exactly(self):
        cells = grid((6, 44, 314, 234), 3, 3, 6)
        self.assertEqual(len(cells), 9)
        self.assertEqual(cells[0][:2], (6, 44))
        self.assertEqual(cells[-1][2:], (314, 234))
        widths = {c[2] - c[0] for c in cells}
        self.assertLessEqual(max(widths) - min(widths), 1)
        with self.assertRaises(ValueError):
            grid((0, 0, 10, 10), 3, 3, 6)

    def test_button_hit_detection(self):
        a = Button('a', (0, 0, 100, 50))
        b = Button('b', (106, 0, 206, 50))
        self.assertIs(hit_test([a, b], 0, 0), a)
        self.assertIs(hit_test([a, b], 99, 49), a)
        self.assertIs(hit_test([a, b], 150, 25), b)
        self.assertIs(hit_test([a, b], 101, 25), a)     # within slop, nearest
        self.assertIs(hit_test([a, b], 105, 25), b)
        self.assertIsNone(hit_test([a, b], 150, 60))
        self.assertIsNone(hit_test([a, b], 103, 25, slop=0))

    def test_menu_targets_are_large_and_on_screen(self):
        menu = default_registry().get(MENU)
        self.assertEqual([b.label for b in menu.buttons], [u.title for u in UTILITIES])
        for button in menu.buttons:
            x0, y0, x1, y1 = button.rect
            self.assertTrue(0 <= x0 < x1 <= 320 and 0 <= y0 < y1 <= 240)
            self.assertGreaterEqual(min(x1 - x0, y1 - y0), 56)
        registry = UIManager().registry
        for screen_id in registry.ids():
            screen = registry.get(screen_id)
            for mode_buttons in [screen.buttons] + [screen.nav + c for c in getattr(screen, '_controls', {}).values()]:
                for button in mode_buttons:
                    x0, y0, x1, y1 = button.rect
                    self.assertTrue(0 <= x0 < x1 <= 320 and 0 <= y0 < y1 <= 240, button)
                    self.assertGreaterEqual(min(x1 - x0, y1 - y0), 38, (screen_id, button.id))

    def test_every_icon_draws(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (40, 40))
        for name in ICONS:
            draw_icon(ImageDraw.Draw(image), name, 20, 20, 26, (255, 0, 0), (0, 0, 0))
            self.assertIsNotNone(image.getbbox(), name)
            image.paste((0, 0, 0), (0, 0, 40, 40))
        with self.assertRaises(ValueError):
            draw_icon(ImageDraw.Draw(image), 'weather', 20, 20, 26, (0, 0, 0), (0, 0, 0))


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.ui = UIManager(clock=lambda: 0.0)

    def test_starts_in_companion_mode_and_tap_opens_menu(self):
        self.assertEqual(self.ui.current, EYES)
        self.assertIsNone(self.ui.render(0.0))           # engine draws the eyes
        self.assertEqual(self.ui.tap(10, 200, now=0.0), 'open-menu')
        self.assertEqual(self.ui.current, MENU)
        image, bbox = self.ui.render(0.0)
        self.assertEqual((image.size, bbox), ((320, 240), None))   # full frame on entry

    def test_every_menu_entry_opens_its_screen_with_back_and_home(self):
        for utility in UTILITIES:
            with self.subTest(utility=utility.id):
                self.ui.home()
                self.ui.open(MENU)
                self.ui.render(0.0)
                press(self.ui, utility.id, 1.0)
                if utility.id == 'companion':
                    self.assertEqual(self.ui.current, EYES)
                    continue
                self.assertEqual(self.ui.stack, (EYES, MENU, utility.id))
                screen = self.ui.registry.get(utility.id)
                self.assertEqual(screen.title, utility.title)
                self.assertLessEqual({'back', 'home'}, {b.action for b in screen.buttons})
                press(self.ui, 'back', 2.0)
                self.assertEqual(self.ui.current, MENU)
                press(self.ui, utility.id, 3.0)
                self.assertIsNone(press(self.ui, 'home', 4.0))
                self.assertEqual(self.ui.stack, (EYES,))

    def test_feedback_is_drawn_before_the_action_runs(self):
        self.ui.open(MENU)
        self.ui.render(0.0)
        timer = self.ui.registry.get(MENU).buttons[0]
        self.ui.tap(*CENTER(timer.rect), now=1.0)
        self.assertEqual(self.ui.current, MENU)           # not yet
        self.assertEqual(self.ui.render(5.0)[1], timer.rect)  # highlight sent first, even if late
        self.assertEqual(self.ui.current, MENU)
        self.assertIsNone(self.ui.render(5.05)[0])        # still showing feedback, nothing to send
        self.assertEqual(self.ui.current, MENU)
        self.ui.render(5.2)
        self.assertEqual(self.ui.current, 'timer')

    def test_second_tap_during_feedback_is_ignored(self):
        self.ui.open(MENU)
        self.ui.render(0.0)
        buttons = self.ui.registry.get(MENU).buttons
        self.ui.tap(*CENTER(buttons[0].rect), now=1.0)
        self.assertIsNone(self.ui.tap(*CENTER(buttons[1].rect), now=1.01))
        self.assertEqual(self.ui.ignored_taps, 1)
        self.ui.render(1.02)
        self.ui.render(1.3)
        self.assertEqual(self.ui.current, 'timer')

    def test_back_at_root_and_unknown_screens(self):
        self.ui.back()
        self.assertEqual(self.ui.stack, (EYES,))
        self.ui.open(MENU)
        self.ui.open(MENU)
        self.assertEqual(self.ui.stack, (EYES, MENU))
        self.ui.open('clock')
        self.ui.open(MENU)                                # reopening pops back to it
        self.assertEqual(self.ui.stack, (EYES, MENU))
        with self.assertRaises(ValueError):
            self.ui.open('weather')
        self.ui.open(EYES)
        self.assertEqual(self.ui.stack, (EYES,))

    def test_unchanged_screen_is_not_redrawn_and_regions_are_minimal(self):
        self.ui.open(MENU)
        self.assertIsNone(self.ui.render(0.0, Expression.IDLE)[1])
        self.assertEqual(self.ui.render(0.1, Expression.IDLE), (None, None))
        self.assertEqual(self.ui.render(0.2, Expression.IDLE), (None, None))
        # Conversation state change: only the status pill is resent.
        self.assertEqual(self.ui.render(0.3, Expression.LISTENING)[1], STATUS_RECT)
        self.assertEqual(self.ui.render(0.4, Expression.LISTENING), (None, None))
        self.assertEqual(self.ui.current, MENU)
        # A tap on empty header space changes nothing.
        self.assertIsNone(self.ui.tap(150, 20, now=0.5))
        self.assertEqual(self.ui.render(0.5, Expression.LISTENING), (None, None))

    def test_invalidate_forces_full_frame(self):
        self.ui.open(MENU)
        self.ui.render(0.0)
        self.ui.invalidate()
        self.assertIsNone(self.ui.render(0.1)[1])

    def test_touch_event_entry_point_and_change_notification(self):
        notified = []
        ui = UIManager(on_change=lambda: notified.append(1))
        event = Mock(x=160, y=120, time=0.0)
        self.assertEqual(ui.handle_touch(event), 'open-menu')
        self.assertEqual(notified, [1])


class EngineIntegrationTests(unittest.TestCase):
    def make(self, display=None):
        display = display or RecordingDisplay()
        engine = EyeEngine(display, animation=QUIET, log=lambda *_: None)
        ui = UIManager(on_change=engine.request_frame)
        engine.attach_ui(ui)
        return engine, ui, display

    def test_conversation_state_preserved_during_utility_navigation(self):
        engine, ui, display = self.make()
        fsm = RobotStateMachine()
        robot_eyes.attach(fsm, engine)
        engine.render_frame(0.0)
        self.assertIsNone(display.bboxes[-1])
        ui.open(MENU)
        ui.open('timer')
        engine.render_frame(0.1)
        with contextlib.redirect_stdout(io.StringIO()):
            for state in (State.LISTENING, State.THINKING):
                fsm.transition_to(state)
                engine.render_frame(0.2)
                self.assertIs(engine.expression, Expression[state.name])
                self.assertEqual(ui.current, 'timer')     # never interrupted
        ui.home()
        sent = len(display.bboxes)
        self.assertTrue(engine.render_frame(1.0))
        self.assertEqual(len(display.bboxes), sent + 1)
        self.assertIsNone(display.bboxes[-1])             # eyes repainted in full
        self.assertIs(engine.expression, Expression.THINKING)
        # The eye pixels are back: cyan present, no UI navy background left.
        colors = {c for _, c in display.frame.getcolors(320 * 240)}
        self.assertIn(engine.config.color, colors)
        self.assertNotIn(ui.theme.background, colors)

    def test_menu_frames_use_dirty_regions_and_skip_unchanged(self):
        engine, ui, display = self.make()
        ui.open(MENU)
        engine.render_frame(0.0)
        self.assertIsNone(display.bboxes[-1])
        self.assertFalse(engine.render_frame(0.1))        # unchanged: no transfer
        n = len(display.bboxes)
        ui.tap(*CENTER(ui.registry.get(MENU).buttons[4].rect), now=0.2)
        engine.render_frame(0.21)
        self.assertEqual(len(display.bboxes), n + 1)
        self.assertEqual(display.bboxes[-1], ui.registry.get(MENU).buttons[4].rect)

    def test_dropped_ui_frame_is_resent_in_full(self):
        engine, ui, display = self.make(RecordingDisplay(fail_shows=1))
        ui.open(MENU)
        engine.render_frame(0.0)                          # bus busy: dropped
        engine.render_frame(0.1)
        self.assertEqual(display.bboxes, [None])

    def test_dropped_eye_frame_forces_full_eye_frame(self):
        display = RecordingDisplay()
        engine = EyeEngine(display, animation=QUIET, log=lambda *_: None)
        engine.render_frame(0.0)
        engine.set_expression('happy')
        display.fail_shows = 1
        self.assertFalse(engine.render_frame(0.1))
        self.assertTrue(engine.render_frame(0.2))
        self.assertIsNone(display.bboxes[-1])

    def test_failing_ui_is_detached_and_eyes_continue(self):
        logs = []
        display = RecordingDisplay()
        engine = EyeEngine(display, animation=QUIET, log=logs.append)
        broken = Mock()
        broken.render.side_effect = RuntimeError('bad screen')
        engine.attach_ui(broken)
        self.assertTrue(engine.render_frame(0.0))
        self.assertIsNone(engine.ui)
        self.assertIn('Touch UI disabled', logs[0])

    def test_single_display_owner_with_live_touch(self):
        display = RecordingDisplay()
        engine = EyeEngine(display, animation=AnimationConfig(fps=30), log=lambda *_: None)
        ui = UIManager(on_change=engine.request_frame)
        cal = TouchCalibration()
        touch = MockTouch(calibration=cal)
        controller = TouchController(touch, ui.handle_touch, cal, log=lambda *_: None)
        engine.start()
        controller.start()
        engine.attach_ui(ui)
        try:
            for x, y, expect in ((160, 120, MENU), (*CENTER(ui.registry.get(MENU).buttons[3].rect), 'clock'),
                                 (*CENTER(ui.registry.get('clock').buttons[1].rect), EYES)):
                touch.press_screen(x, y)
                touch.release()
                self.assertTrue(wait_for(lambda: ui.current == expect), expect)
                self.assertTrue(wait_for(lambda: controller.processor.state == 'idle'))
            names = {t.name for t in threading.enumerate()}
            self.assertEqual(sum(n.startswith('eli-eyes') for n in names), 1)
        finally:
            controller.stop()
            engine.stop()
        self.assertEqual(display.threads, {'eli-eyes'})   # only the engine worker draws
        self.assertFalse(touch.is_open or display.is_open)


class RobotTouchTests(unittest.TestCase):
    def eyes(self):
        return EyeEngine(MockDisplay(), animation=QUIET, log=lambda *_: None).start()

    def test_backend_selection_and_off_by_default(self):
        for value in (None, '', 'off', 'none', '0'):
            env = {} if value is None else {'ELI_TOUCH_BACKEND': value}
            self.assertIsNone(robot_touch.selected_touch_backend(env))
        self.assertEqual(robot_touch.selected_touch_backend({'ELI_TOUCH_BACKEND': ' XPT2046 '}), 'xpt2046')
        logs = []
        eyes = self.eyes()
        try:
            self.assertIsNone(robot_touch.start_robot_touch(eyes, {}, log=logs.append))
            self.assertEqual(logs, [])
            self.assertIsNone(eyes.ui)
        finally:
            eyes.stop()

    def test_mock_touch_starts_attaches_and_cleans_up(self):
        logs = []
        eyes = self.eyes()
        try:
            touch = robot_touch.start_robot_touch(eyes, {'ELI_TOUCH_BACKEND': 'mock'}, log=logs.append)
            self.assertIsNotNone(touch)
            self.assertIs(eyes.ui, touch.ui)
            self.assertTrue(touch.controller.running)
            self.assertIn('Touch input running on mock', logs[0])
            robot_touch.stop_robot_touch(touch)
            self.assertFalse(touch.controller.running)
            self.assertFalse(touch.backend.is_open)
            self.assertIsNone(eyes.ui)
            self.assertTrue(eyes.running)                  # eyes unaffected
        finally:
            eyes.stop()

    def test_touch_initialisation_failure_keeps_eyes_running(self):
        logs = []
        eyes = self.eyes()
        try:
            with patch.dict(sys.modules, {'board': None, 'digitalio': None}):
                touch = robot_touch.start_robot_touch(eyes, {'ELI_TOUCH_BACKEND': 'xpt2046'},
                                                      log=logs.append)
            self.assertIsNone(touch)
            self.assertEqual(len(logs), 1)
            self.assertIn('Touch disabled', logs[0])
            self.assertIsNone(eyes.ui)
            self.assertTrue(eyes.running)
        finally:
            eyes.stop()

    def test_invalid_settings_and_missing_display_are_isolated(self):
        eyes = self.eyes()
        try:
            for env in ({'ELI_TOUCH_BACKEND': 'capacitive'},
                        {'ELI_TOUCH_BACKEND': 'mock', 'ELI_TOUCH_RAW_X_MIN': 'x'},
                        {'ELI_TOUCH_BACKEND': 'xpt2046', 'ELI_TOUCH_SPI_HZ': '99000000'}):
                logs = []
                self.assertIsNone(robot_touch.start_robot_touch(eyes, env, log=logs.append))
                self.assertEqual(len(logs), 1, env)
        finally:
            eyes.stop()
        logs = []
        self.assertIsNone(robot_touch.start_robot_touch(None, {'ELI_TOUCH_BACKEND': 'mock'},
                                                        log=logs.append))
        self.assertIn('needs the eye display', logs[0])
        robot_touch.stop_robot_touch(None)                 # no-op


class MainTouchIntegrationTests(unittest.TestCase):
    def run_main(self, env):
        camera = Mock()
        camera.read.side_effect = KeyboardInterrupt
        captured = []
        real_stop = robot_touch.stop_robot_touch
        log = io.StringIO()
        with patch.dict('os.environ', env), \
                patch('main.Camera', return_value=camera), \
                patch('main.load_vision', return_value=(Mock(), Mock())), \
                patch('main.stop_robot_touch', side_effect=lambda t: (captured.append(t), real_stop(t))), \
                contextlib.redirect_stdout(log):
            status = main.main(['--headless', '--vision-only'])
        return status, captured, log.getvalue()

    def test_mock_touch_runs_with_mock_eyes_and_shuts_down_first(self):
        status, captured, log = self.run_main({'ELI_DISPLAY_BACKEND': 'mock',
                                               'ELI_TOUCH_BACKEND': 'mock'})
        self.assertEqual(status, 0)
        touch = captured[0]
        self.assertFalse(touch.controller.running)
        self.assertFalse(touch.backend.is_open)
        self.assertFalse(touch.eyes.running)
        self.assertLess(log.index('Touch input running'), log.index('Eli is online'))

    def test_touch_off_by_default_and_without_display(self):
        status, captured, log = self.run_main({'ELI_DISPLAY_BACKEND': 'mock'})
        self.assertEqual((status, captured), (0, [None]))
        self.assertNotIn('[Touch]', log)
        status, captured, log = self.run_main({'ELI_DISPLAY_BACKEND': '', 'ELI_TOUCH_BACKEND': 'mock'})
        self.assertEqual((status, captured), (0, [None]))
        self.assertEqual(log.count('Touch disabled'), 1)
        self.assertIn('Eli is online', log)


class DemoTests(unittest.TestCase):
    def test_headless_tour_visits_every_screen(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = ui_demo.main(['--backend', 'mock', '--tour', '--fps', '30'])
        self.assertEqual(status, 0, out.getvalue())
        text = out.getvalue()
        self.assertIn('all screens ok', text)
        for utility in UTILITIES:
            if utility.id != 'companion':
                self.assertIn(f'-> {utility.id}', text)

    def test_invalid_options(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ui_demo.main(['--backend', 'mock', '--tour', '--touch', 'xpt2046']), 2)
            self.assertEqual(ui_demo.main(['--backend', 'mock', '--fps', '0']), 2)


if __name__ == '__main__':
    unittest.main()
