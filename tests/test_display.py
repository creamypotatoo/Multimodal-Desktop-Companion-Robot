"""Hardware-free tests for Eli's animated eyes (display package + TFT backend)."""
import contextlib
import io
import math
import random
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from behavior.state_machine import State
from display import demo
from display.animation import EyeAnimator, validate_gaze
from display.config import AnimationConfig, EyeConfig, parse_color
from display.expressions import EXPRESSIONS, Expression
from display.eyes import EyeEngine, create_display, resolve_backend
from display.renderer import EyeRenderer
from hardware import tft_display
from hardware.mock_hardware import MockDisplay
from hardware.tft_display import DisplayUnavailableError, TFTConfig, TFTDisplay, native_origin

QUIET = AnimationConfig(blinking=False, auto_gaze=False)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def settle(animator, start, seconds=3.0, step=0.1):
    t, state = start, None
    for _ in range(int(seconds / step)):
        t += step
        state = animator.update(t)
    return t, state


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ExpressionTests(unittest.TestCase):
    def test_every_expression_has_spec(self):
        self.assertEqual(set(EXPRESSIONS), set(Expression))
        self.assertEqual(len(Expression), 10)

    def test_parse_accepts_names_values_and_state_enum(self):
        self.assertIs(Expression.parse('happy'), Expression.HAPPY)
        self.assertIs(Expression.parse('  Sleeping '), Expression.SLEEPING)
        self.assertIs(Expression.parse(Expression.SAD), Expression.SAD)
        for state in State:  # robot state machine names map directly
            self.assertIs(Expression.parse(state), Expression[state.name])

    def test_invalid_expressions_rejected(self):
        for bad in ('angry', '', None, 42, 1.5, object()):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Expression.parse(bad)


class ConfigTests(unittest.TestCase):
    def test_fps_and_colour_validation(self):
        self.assertEqual(AnimationConfig().fps, 10.0)
        for bad in (0, -1, 61, math.nan, 'fast'):
            with self.subTest(fps=bad), self.assertRaises(ValueError):
                AnimationConfig(fps=bad)
        self.assertEqual(parse_color('#00E5FF'), (0, 229, 255))
        self.assertEqual(EyeConfig(color='ff8000').color, (255, 128, 0))
        for bad in ('#12345', 'zzzzzz', (1, 2), (0, 0, 300)):
            with self.subTest(colour=bad), self.assertRaises(ValueError):
                parse_color(bad)

    def test_env_overrides_and_explicit_precedence(self):
        env = {'ELI_DISPLAY_FPS': '12', 'ELI_EYE_COLOR': '#112233'}
        self.assertEqual(AnimationConfig.from_env(env).fps, 12.0)
        self.assertEqual(AnimationConfig.from_env(env, fps=5).fps, 5.0)
        self.assertEqual(EyeConfig.from_env(env).color, (0x11, 0x22, 0x33))

    def test_geometry_must_fit(self):
        with self.assertRaises(ValueError):
            EyeConfig(eye_width=150, spacing=40)
        with self.assertRaises(ValueError):
            EyeConfig(supersample=0)


class RendererTests(unittest.TestCase):
    def test_frames_are_320x240_rgb_for_every_expression(self):
        renderer = EyeRenderer()
        for expression in Expression:
            with self.subTest(expression=expression.name):
                animator = EyeAnimator(QUIET, rng=random.Random(1), expression=expression)
                _, state = settle(animator, 0.0, 1.0)
                image = renderer.render(state)
                self.assertEqual(image.size, (320, 240))
                self.assertEqual(image.mode, 'RGB')
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
                self.assertIn(EyeConfig().color, [c for _, c in image.getcolors(1 << 16)])

    def test_supersampled_output_is_still_320x240(self):
        renderer = EyeRenderer(EyeConfig(supersample=2))
        _, state = settle(EyeAnimator(QUIET, rng=random.Random(1)), 0.0, 0.5)
        self.assertEqual(renderer.render(state).size, (320, 240))

    def test_buffer_is_reused(self):
        renderer = EyeRenderer()
        state = EyeAnimator(QUIET, rng=random.Random(1)).update(0.1)
        self.assertIs(renderer.render(state), renderer.render(state))

    def test_custom_colour_and_closed_eyes(self):
        renderer = EyeRenderer(EyeConfig(color='#FF0000'))
        animator = EyeAnimator(QUIET, rng=random.Random(1), expression='sleeping')
        _, state = settle(animator, 0.0, 2.0)
        layout = renderer.layout(state)
        for eye in layout:
            self.assertLessEqual(eye.box[3] - eye.box[1], EyeConfig().closed_thickness + 1)
        colours = {c for _, c in renderer.draw(layout).getcolors(1 << 16)}
        self.assertEqual(colours, {(0, 0, 0), (255, 0, 0)})


class AnimationTests(unittest.TestCase):
    def test_expression_transition_is_smooth_and_converges(self):
        animator = EyeAnimator(QUIET, rng=random.Random(2))
        t, _ = settle(animator, 0.0, 1.0)
        animator.set_expression(Expression.HAPPY, t)
        target = EXPRESSIONS[Expression.HAPPY].left.lid_bottom
        values = []
        for _ in range(30):
            t += 0.1
            values.append(animator.update(t).left.lid_bottom)
        self.assertGreater(values[0], 0.0)
        self.assertLess(values[0], target * 0.9)           # not an abrupt jump
        self.assertEqual(values, sorted(values))            # monotonic approach
        self.assertAlmostEqual(values[-1], target, places=3)
        self.assertIs(animator.expression, Expression.HAPPY)

    def test_transition_is_frame_rate_independent(self):
        results = []
        for step in (0.05, 0.1, 0.25):
            animator = EyeAnimator(QUIET, rng=random.Random(2))
            animator.set_expression('excited', 0.0)
            t = 0.0
            while t < 0.5 - 1e-9:
                t += step
                state = animator.update(t)
            results.append(state.left.scale_y)
        self.assertLess(max(results) - min(results), 0.02)

    def test_manual_blink_closes_then_reopens(self):
        animator = EyeAnimator(QUIET, rng=random.Random(3))
        t, state = settle(animator, 0.0, 1.0)
        self.assertAlmostEqual(state.left.openness, 1.0)
        self.assertTrue(animator.blink(t))
        self.assertFalse(animator.blink(t))  # already blinking
        openness = []
        for _ in range(10):
            t += 0.05
            openness.append(animator.update(t).left.openness)
        self.assertEqual(min(openness), 0.0)
        self.assertAlmostEqual(openness[-1], 1.0)
        self.assertFalse(animator.blinking)

    def test_blink_shows_closed_frame_even_at_low_fps(self):
        animator = EyeAnimator(QUIET, rng=random.Random(3))
        animator.blink(0.0)
        self.assertEqual(animator.update(0.5).left.openness, 0.0)
        self.assertAlmostEqual(animator.update(1.0).left.openness, 1.0)

    def test_natural_blinking_is_randomised_and_not_restless(self):
        def blink_times(seed):
            animator = EyeAnimator(AnimationConfig(auto_gaze=False), rng=random.Random(seed))
            times, was_closed, t = [], False, 0.0
            while t < 60:
                t += 0.05
                closed = animator.update(t).left.openness < 0.05
                if closed and not was_closed:
                    times.append(t)
                was_closed = closed
            return times
        first, second = blink_times(1), blink_times(2)
        self.assertNotEqual(first, second)
        for times in (first, second):
            self.assertGreaterEqual(len(times), 8)   # blinks naturally...
            self.assertLessEqual(len(times), 35)     # ...but not constantly

    def test_sleeping_never_blinks_open(self):
        animator = EyeAnimator(AnimationConfig(), rng=random.Random(4), expression='sleeping')
        t, _ = settle(animator, 0.0, 2.0)
        for _ in range(200):
            t += 0.1
            self.assertLess(animator.update(t).left.openness, 0.05)

    def test_auto_gaze_wanders_within_bounds(self):
        animator = EyeAnimator(AnimationConfig(blinking=False), rng=random.Random(5))
        t, gazes = 0.0, set()
        for _ in range(600):
            t += 0.1
            state = animator.update(t)
            self.assertTrue(-1 <= state.gaze_x <= 1 and -1 <= state.gaze_y <= 1)
            gazes.add((round(state.gaze_x, 2), round(state.gaze_y, 2)))
        self.assertGreater(len(gazes), 5)

    def test_thinking_looks_away_and_up(self):
        animator = EyeAnimator(AnimationConfig(blinking=False), rng=random.Random(6))
        animator.set_expression('thinking', 0.0)
        t, ys, xs = 0.0, [], []
        for _ in range(300):
            t += 0.1
            state = animator.update(t)
            xs.append(state.gaze_x)
            ys.append(state.gaze_y)
        self.assertLess(sum(ys) / len(ys), -0.3)
        self.assertLess(min(xs), -0.3)
        self.assertGreater(max(xs), 0.3)

    def test_manual_gaze_is_clamped_and_held(self):
        animator = EyeAnimator(AnimationConfig(blinking=False), rng=random.Random(7))
        self.assertEqual(animator.set_gaze(5, -7), (1.0, -1.0))
        t, state = settle(animator, 0.0, 10.0)
        self.assertAlmostEqual(state.gaze_x, 1.0, places=3)
        self.assertAlmostEqual(state.gaze_y, -1.0, places=3)
        animator.release_gaze(t)
        self.assertIsNone(animator.manual_gaze)
        _, state = settle(animator, t, 1.0)
        self.assertLess(abs(state.gaze_x), 0.5)

    def test_invalid_gaze_rejected(self):
        for bad in ((math.nan, 0), (0, math.inf), ('left', 0), (None, 0), (True, 0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_gaze(*bad)

    def test_eyes_stay_on_screen_for_extreme_gaze(self):
        renderer = EyeRenderer()
        config = EyeConfig()
        for expression in Expression:
            for gaze in ((-1, -1), (1, 1), (-1, 1), (1, -1), (1, 0), (0, 1)):
                with self.subTest(expression=expression.name, gaze=gaze):
                    animator = EyeAnimator(QUIET, rng=random.Random(8), expression=expression)
                    animator.set_gaze(*gaze)
                    _, state = settle(animator, 0.0, 2.0)
                    for eye in renderer.layout(state):
                        x0, y0, x1, y1 = eye.box
                        self.assertGreaterEqual(min(x0, y0), 0)
                        self.assertLess(x1, config.frame_width)
                        self.assertLess(y1, config.frame_height)
                        bx0, by0, bx1, by1 = eye.bounds
                        self.assertTrue(bx0 >= 0 and by0 >= 0 and bx1 <= 320 and by1 <= 240)
                    left, right = renderer.layout(state)
                    self.assertLess(left.box[2], right.box[0])  # never overlap


class MockDisplayTests(unittest.TestCase):
    def test_open_show_clear_close(self):
        display = MockDisplay()
        with self.assertRaises(RuntimeError):
            display.show(Image.new('RGB', (320, 240)))
        display.open()
        image = Image.new('RGB', (320, 240), (1, 2, 3))
        display.show(image, (0, 0, 10, 10))
        self.assertEqual(display.frames_shown, 1)
        self.assertEqual(display.last_bbox, (0, 0, 10, 10))
        image.paste((9, 9, 9), (0, 0, 320, 240))
        self.assertEqual(display.frame.getpixel((5, 5)), (1, 2, 3))  # copied, not aliased
        with self.assertRaises(ValueError):
            display.show(Image.new('RGB', (240, 320)))
        display.clear()
        self.assertEqual(display.frame.getpixel((5, 5)), (0, 0, 0))
        display.close()
        self.assertFalse(display.is_open)


class EngineTests(unittest.TestCase):
    def make(self, display=None, animation=QUIET, **kwargs):
        clock = FakeClock()
        engine = EyeEngine(display or MockDisplay(), animation=animation, clock=clock,
                           rng=random.Random(9), log=lambda *_: None, **kwargs)
        return engine, clock

    def test_redundant_frames_are_skipped_and_dirty_regions_used(self):
        engine, clock = self.make()
        display = engine.display
        self.assertTrue(engine.render_frame())
        self.assertIsNone(display.last_bbox)  # first frame is full
        for _ in range(40):
            clock.advance(0.1)
            engine.render_frame()
        shown = display.frames_shown
        clock.advance(0.1)
        self.assertFalse(engine.render_frame())   # settled: nothing changed
        self.assertEqual(display.frames_shown, shown)
        engine.set_expression('excited')
        clock.advance(0.1)
        self.assertTrue(engine.render_frame())
        x0, y0, x1, y1 = display.last_bbox
        self.assertTrue(0 <= x0 < x1 <= 320 and 0 <= y0 < y1 <= 240)
        self.assertLess((x1 - x0) * (y1 - y0), 320 * 240)
        stats = engine.stats()
        self.assertGreater(stats['frames_skipped'], 0)
        self.assertEqual(stats['frames_sent'], display.frames_shown)

    def test_engine_commands_validate_without_changing_state(self):
        engine, _ = self.make()
        engine.set_expression('sad')
        with self.assertRaises(ValueError):
            engine.set_expression('furious')
        self.assertIs(engine.expression, Expression.SAD)
        with self.assertRaises(ValueError):
            engine.set_gaze(math.nan, 0)
        self.assertEqual(engine.set_gaze(-3, 0.5), (-1.0, 0.5))

    def test_display_size_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            EyeEngine(MockDisplay(width=240, height=320))

    def test_start_stop_lifecycle(self):
        display = MockDisplay()
        engine = EyeEngine(display, animation=AnimationConfig(fps=30), log=lambda *_: None)
        self.assertFalse(engine.running)
        engine.start()
        thread = engine._thread
        self.assertTrue(engine.running and display.is_open)
        self.assertIs(engine.start()._thread, thread)  # idempotent
        self.assertTrue(wait_for(lambda: display.frames_shown >= 1))
        with self.assertRaises(RuntimeError):
            engine.render_frame()
        engine.stop()
        self.assertFalse(engine.running)
        self.assertFalse(thread.is_alive())
        self.assertFalse(display.is_open)
        self.assertEqual(display.clears, 1)
        engine.stop()  # idempotent
        self.assertEqual(display.clears, 1)
        shown = display.frames_shown
        with engine:  # restart via context manager
            self.assertTrue(engine.running)
            # Settled frames are skipped, so count worker ticks, not frames sent.
            self.assertTrue(wait_for(lambda: engine.stats()['tick_fps'] > 0
                                     and display.frames_shown > shown))
            ticks = lambda: engine.stats()['frames_sent'] + engine.stats()['frames_skipped']
            self.assertTrue(wait_for(lambda: ticks() >= 3))
        self.assertFalse(engine.running or display.is_open)
        self.assertIsNone(engine.last_error)

    def test_worker_sleeps_between_frames(self):
        display = MockDisplay()
        with EyeEngine(display, animation=AnimationConfig(fps=10), log=lambda *_: None) as engine:
            time.sleep(0.55)
            stats = engine.stats()
        self.assertLessEqual(stats['frames_sent'] + stats['frames_skipped'], 8)
        self.assertGreaterEqual(stats['frames_sent'] + stats['frames_skipped'], 3)

    def test_thread_safe_updates_while_running(self):
        display = MockDisplay()
        engine = EyeEngine(display, animation=AnimationConfig(fps=60), log=lambda *_: None)
        errors = []
        names = [e.value for e in Expression]

        def hammer(seed):
            rng = random.Random(seed)
            try:
                for _ in range(300):
                    choice = rng.random()
                    if choice < 0.5:
                        engine.set_expression(rng.choice(names))
                    elif choice < 0.8:
                        engine.set_gaze(rng.uniform(-2, 2), rng.uniform(-2, 2))
                    elif choice < 0.9:
                        engine.release_gaze()
                    else:
                        engine.blink()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with engine:
            threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            engine.set_expression('happy')
            self.assertTrue(wait_for(lambda: display.frames_shown > 3))
            self.assertTrue(engine.running)
        self.assertEqual(errors, [])
        self.assertIsNone(engine.last_error)
        self.assertIs(engine.expression, Expression.HAPPY)

    def test_backend_failure_stops_worker_but_stop_still_cleans_up(self):
        logs = []
        display = MockDisplay(fail_on_show=OSError('SPI write failed'))
        engine = EyeEngine(display, log=logs.append)
        engine.start()
        self.assertTrue(wait_for(lambda: not engine.running))
        self.assertIsInstance(engine.last_error, OSError)
        self.assertTrue(any('SPI write failed' in line for line in logs))
        engine.stop()
        self.assertFalse(display.is_open)

    def test_unavailable_display_raises_without_starting_thread(self):
        display = MockDisplay(fail_on_open=DisplayUnavailableError('no panel'))
        engine = EyeEngine(display)
        with self.assertRaises(DisplayUnavailableError):
            engine.start()
        self.assertFalse(engine.running)
        engine.stop()  # safe even though it never started


class TFTTests(unittest.TestCase):
    def test_config_defaults_match_verified_wiring(self):
        config = TFTConfig()
        self.assertEqual((config.cs, config.dc, config.reset), ('D22', 'D24', 'D25'))
        self.assertEqual((config.baudrate, config.rotation, config.size), (16_000_000, 90, (320, 240)))

    def test_config_rejects_gpio8_and_bad_values(self):
        for kwargs in (dict(cs='D8'), dict(cs='ce0'), dict(rotation=45), dict(baudrate=100),
                       dict(dc='D22'), dict(reset='GPIO 25!')):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TFTConfig(**kwargs)

    def test_config_from_env(self):
        env = {'ELI_DISPLAY_SPI_HZ': '24000000', 'ELI_DISPLAY_ROTATION': '270',
               'ELI_DISPLAY_PARTIAL': '0', 'ELI_DISPLAY_CS': 'd5'}
        config = TFTConfig.from_env(env)
        self.assertEqual((config.baudrate, config.rotation, config.partial_updates, config.cs),
                         (24_000_000, 270, False, 'D5'))
        with self.assertRaises(ValueError):
            TFTConfig.from_env({'ELI_DISPLAY_SPI_HZ': 'fast'})

    def test_native_origin_matches_full_frame_rotation(self):
        rng = random.Random(10)
        frame = Image.effect_noise((320, 240), 64).convert('RGB')
        for rotation in (0, 90, 180, 270):
            full = frame.rotate(rotation, expand=True)
            for _ in range(5):
                x0, y0 = rng.randrange(0, 300), rng.randrange(0, 220)
                bbox = (x0, y0, rng.randrange(x0 + 1, 321), rng.randrange(y0 + 1, 241))
                with self.subTest(rotation=rotation, bbox=bbox):
                    crop = frame.crop(bbox).rotate(rotation, expand=True)
                    nx, ny = native_origin(bbox, rotation, frame.size)
                    expected = full.crop((nx, ny, nx + crop.width, ny + crop.height))
                    self.assertEqual(crop.size, expected.size)
                    self.assertEqual(crop.tobytes(), expected.tobytes())

    def fake_hardware(self, pin_error=None):
        board = types.SimpleNamespace(D22='D22', D24='D24', D25='D25', SPI=MagicMock())
        pins = []

        def make_pin(pin):
            if pin_error and pin == 'D24':
                raise pin_error
            handle = MagicMock(name=f'pin {pin}')
            pins.append(handle)
            return handle
        digitalio = types.SimpleNamespace(DigitalInOut=make_pin)
        ili9341 = types.SimpleNamespace(ILI9341=MagicMock())
        return (board, digitalio, ili9341), pins

    def test_tft_full_and_partial_updates(self):
        modules, pins = self.fake_hardware()
        with patch.object(tft_display, '_import_hardware', return_value=modules):
            display = TFTDisplay().open()
        driver = modules[2].ILI9341
        kwargs = driver.call_args.kwargs
        self.assertEqual((kwargs['baudrate'], kwargs['rotation'], kwargs['width'], kwargs['height']),
                         (16_000_000, 90, 240, 320))
        panel = driver.return_value
        frame = Image.new('RGB', (320, 240))
        display.show(frame)
        panel.image.assert_called_with(frame)  # verified full-frame call
        display.show(frame, (10, 20, 110, 70))
        crop = panel.image.call_args.args[0]
        self.assertEqual(crop.size, (50, 100))  # rotated crop
        self.assertEqual(panel.image.call_args.kwargs, dict(rotation=0, x=20, y=210))
        with self.assertRaises(ValueError):
            display.show(Image.new('RGB', (240, 320)))
        display.close()
        self.assertFalse(display.is_open)
        for pin in pins:
            pin.deinit.assert_called_once()

    def test_partial_updates_can_be_disabled(self):
        modules, _ = self.fake_hardware()
        with patch.object(tft_display, '_import_hardware', return_value=modules):
            display = TFTDisplay(TFTConfig(partial_updates=False)).open()
        frame = Image.new('RGB', (320, 240))
        display.show(frame, (10, 20, 110, 70))
        modules[2].ILI9341.return_value.image.assert_called_with(frame)

    def test_missing_libraries_raise_display_unavailable(self):
        with patch.dict(sys.modules, {'board': None, 'digitalio': None}):
            with self.assertRaises(DisplayUnavailableError) as ctx:
                TFTDisplay().open()
        self.assertIn('adafruit-blinka', str(ctx.exception))

    def test_busy_gpio_is_reported_and_pins_released(self):
        modules, pins = self.fake_hardware(pin_error=RuntimeError('GPIO busy'))
        with patch.object(tft_display, '_import_hardware', return_value=modules):
            with self.assertRaises(DisplayUnavailableError) as ctx:
                TFTDisplay().open()
        self.assertIn('busy', str(ctx.exception))
        self.assertEqual(len(pins), 1)
        pins[0].deinit.assert_called_once()

    def test_engine_with_unavailable_tft(self):
        with patch.dict(sys.modules, {'board': None}):
            engine = EyeEngine(create_display('tft'))
            with self.assertRaises(DisplayUnavailableError):
                engine.start()
        self.assertFalse(engine.running)


class FactoryAndDemoTests(unittest.TestCase):
    def test_resolve_backend(self):
        with patch.dict('os.environ', {'ELI_DISPLAY_BACKEND': ''}):
            with patch('display.eyes.platform.system', return_value='Linux'):
                self.assertEqual(resolve_backend('auto'), 'tft')
            with patch('display.eyes.platform.system', return_value='Windows'):
                self.assertEqual(resolve_backend('auto'), 'preview')
        with patch.dict('os.environ', {'ELI_DISPLAY_BACKEND': 'mock'}):
            self.assertEqual(resolve_backend(), 'mock')
        with self.assertRaises(ValueError):
            resolve_backend('hdmi')
        self.assertIsInstance(create_display('mock'), MockDisplay)

    def test_demo_runs_every_expression_on_mock(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = demo.main(['--backend', 'mock', '--cycles', '1', '--hold', '0.05',
                                '--stats', '0', '--seed', '1'])
        self.assertEqual(status, 0)
        text = out.getvalue()
        for expression in Expression:
            self.assertIn(f'Expression: {expression.name}', text)
        self.assertIn('Eyes stopped', text)

    def test_demo_benchmark_and_invalid_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(demo.main(['--backend', 'mock', '--benchmark', '20']), 0)
        self.assertIn('full-frame ceiling', out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(demo.main(['--backend', 'mock', '--expression', 'angry']), 2)
            self.assertEqual(demo.main(['--backend', 'mock', '--fps', '0']), 2)

    def test_demo_reports_unavailable_tft(self):
        err = io.StringIO()
        with patch.dict(sys.modules, {'board': None}), contextlib.redirect_stderr(err):
            self.assertEqual(demo.main(['--backend', 'tft', '--cycles', '1']), 1)
        self.assertIn('Display unavailable', err.getvalue())

    def test_imports_without_hardware_or_gui_libraries(self):
        code = '''
import importlib.abc, sys
BLOCKED = ('board', 'digitalio', 'busio', 'adafruit_rgb_display', 'tkinter', 'cv2',
           'audio', 'intelligence', 'perception')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in BLOCKED:
            raise ImportError('blocked: ' + fullname)
sys.meta_path.insert(0, Block())
import display.eyes, display.demo, hardware.tft_display, hardware.mock_hardware
engine = display.eyes.EyeEngine(display.eyes.create_display('mock'))
import time
engine.start(); engine.set_expression('happy'); engine.blink()
deadline = time.monotonic() + 3
while engine.display.frames_shown < 1 and time.monotonic() < deadline:
    time.sleep(0.01)
engine.stop()
assert engine.display.frames_shown >= 1
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
