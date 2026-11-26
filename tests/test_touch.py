"""Touchscreen driver, calibration, filtering, debouncing and shared-SPI tests (hardware-free)."""
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from display.touch import (TouchCalibration, TouchController, TouchProcessor, TouchTiming,
                           filter_samples, median)
from hardware import tft_display, touch_xpt2046
from hardware.mock_hardware import MockTouch
from hardware.spi_bus import SharedSPIBus, SPIBusTimeout
from hardware.tft_display import TFTDisplay
from hardware.touch_xpt2046 import (CMD_X, CMD_Y, TouchUnavailableError, XPT2046Config,
                                    XPT2046Touch, decode_adc, normalize_pin)


def encode(value):
    """Bytes an XPT2046 clocks out for a 12-bit result (inverse of decode_adc)."""
    word = (value & 0xFFF) << 3
    return bytes((0, word >> 8, word & 0xFF))


def reference_map(raw_x, raw_y):
    """The experimentally verified mapping, verbatim."""
    screen_x = int((raw_y - 652) * 319 / 2783)
    screen_y = int((raw_x - 676) * 239 / 2793)
    return max(0, min(319, screen_x)), max(0, min(239, screen_y))


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeBackend:
    """Scriptable touch backend: ``pressed`` flag plus a sample source."""

    def __init__(self, sample=(2000, 2000)):
        self.pressed = False
        self.sample = sample
        self.reads = 0
        self.opened = self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def is_pressed(self):
        return self.pressed

    def read_raw(self):
        self.reads += 1
        return self.sample() if callable(self.sample) else self.sample


# ------------------------------------------------------------ fake Blinka
class FakePin:
    def __init__(self, name, log):
        self.name, self.log = name, log
        self._value = True
        self.deinited = False
        self.pull = None

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = v
        self.log.append((self.name, v))

    def switch_to_output(self, value=True):
        self._value = value

    def switch_to_input(self, pull=None):
        self.pull = pull

    def deinit(self):
        self.deinited = True


class FakeSPI:
    """Records configure calls; answers X/Y conversions from ``values``."""

    def __init__(self, values=(1500, 2500), on_transfer=None):
        self.values = {CMD_X: values[0], CMD_Y: values[1]}
        self.on_transfer = on_transfer
        self.locked = False
        self.configs = []
        self.commands = []
        self.deinited = False

    def try_lock(self):
        if self.locked:
            return False
        self.locked = True
        return True

    def unlock(self):
        self.locked = False

    def configure(self, **kwargs):
        if not self.locked:
            raise RuntimeError('First call try_lock()')
        self.configs.append(kwargs)

    def write_readinto(self, tx, rx):
        if self.on_transfer:
            self.on_transfer('touch')
        self.commands.append(tx[0])
        rx[:] = encode(self.values[tx[0]])

    def deinit(self):
        self.deinited = True


def fake_blinka(spi, pin_error=None):
    log, pins = [], {}

    def make_pin(pin):
        if pin_error and pin == 'D6':
            raise pin_error
        pins[pin] = FakePin(pin, log)
        return pins[pin]
    board = types.SimpleNamespace(D5='D5', D6='D6', D22='D22', D24='D24', D25='D25',
                                  SPI=MagicMock(return_value=spi))
    digitalio = types.SimpleNamespace(DigitalInOut=make_pin, Pull=types.SimpleNamespace(UP='up'))
    return board, digitalio, pins, log


# ------------------------------------------------------------------ tests
class DecodeTests(unittest.TestCase):
    def test_raw_adc_decoding(self):
        self.assertEqual(decode_adc(bytes((0, 0x7F, 0xF8))), 0xFFF)
        self.assertEqual(decode_adc(bytes((0, 0, 0x07))), 0)          # low 3 bits ignored
        self.assertEqual(decode_adc(bytes((0xFF, 0x80, 0x00))), 0)     # bit 15 / rx[0] ignored
        for value in (0, 1, 100, 676, 2048, 3469, 4095):
            self.assertEqual(decode_adc(encode(value)), value)
        rx = bytes((0x12, 0x3C, 0x5D))
        self.assertEqual(decode_adc(rx), (((rx[1] << 8) | rx[2]) >> 3) & 0xFFF)

    def test_pin_and_config_validation(self):
        self.assertEqual(normalize_pin('5'), 'D5')
        self.assertEqual(normalize_pin('GPIO6'), 'D6')
        self.assertEqual(normalize_pin('d5'), 'D5')
        cfg = XPT2046Config.from_env({'ELI_TOUCH_CS': '5', 'ELI_TOUCH_IRQ': '6',
                                      'ELI_TOUCH_SPI_HZ': '500000'})
        self.assertEqual((cfg.cs, cfg.irq, cfg.baudrate, cfg.polarity, cfg.phase),
                         ('D5', 'D6', 500_000, 0, 0))
        self.assertEqual(XPT2046Config(), cfg)
        for bad in (dict(cs='8'), dict(cs='5', irq='5'), dict(baudrate=16_000_000)):
            with self.assertRaises(ValueError):
                XPT2046Config(**bad)
        with self.assertRaises(ValueError):
            XPT2046Config.from_env({'ELI_TOUCH_SPI_HZ': 'fast'})


class CalibrationTests(unittest.TestCase):
    def test_defaults_match_verified_mapping_exactly(self):
        cal = TouchCalibration()
        for raw_x in range(0, 4096, 37):
            for raw_y in range(0, 4096, 41):
                self.assertEqual(cal.map(raw_x, raw_y), reference_map(raw_x, raw_y))

    def test_axes_are_swapped_not_inverted(self):
        cal = TouchCalibration()
        # Raw Y drives screen x; raw X drives screen y; both increase together.
        self.assertEqual(cal.map(676, 652), (0, 0))
        self.assertEqual(cal.map(676, 3435), (319, 0))
        self.assertEqual(cal.map(3469, 652), (0, 239))
        self.assertEqual(cal.map(3469, 3435), (319, 239))

    def test_coordinates_are_clamped(self):
        cal = TouchCalibration()
        self.assertEqual(cal.map(0, 0), (0, 0))
        self.assertEqual(cal.map(4095, 4095), (319, 239))
        self.assertEqual(cal.map(-50, 9000), (319, 0))

    def test_inversion_and_unswapped_options(self):
        cal = TouchCalibration(invert_x=True, invert_y=True)
        self.assertEqual(cal.map(676, 652), (319, 239))
        plain = TouchCalibration(swap_axes=False, raw_x_min=0, raw_x_max=4095,
                                 raw_y_min=0, raw_y_max=4095)
        self.assertEqual(plain.map(4095, 0), (319, 0))

    def test_inverse_round_trips_every_pixel(self):
        for cal in (TouchCalibration(), TouchCalibration(invert_x=True, swap_axes=False)):
            for x in range(0, 320, 7):
                for y in range(0, 240, 7):
                    self.assertEqual(cal.map(*cal.inverse(x, y)), (x, y))

    def test_env_configuration_and_validation(self):
        env = {'ELI_TOUCH_RAW_X_MIN': '600', 'ELI_TOUCH_RAW_X_MAX': '3500',
               'ELI_TOUCH_RAW_Y_MIN': '700', 'ELI_TOUCH_RAW_Y_MAX': '3400',
               'ELI_TOUCH_SWAP_AXES': '0', 'ELI_TOUCH_INVERT_X': '1', 'ELI_TOUCH_INVERT_Y': 'no'}
        cal = TouchCalibration.from_env(env)
        self.assertEqual((cal.raw_x_min, cal.raw_x_max, cal.raw_y_min, cal.raw_y_max),
                         (600, 3500, 700, 3400))
        self.assertEqual((cal.swap_axes, cal.invert_x, cal.invert_y), (False, True, False))
        self.assertEqual(TouchCalibration.from_env({}), TouchCalibration())
        with self.assertRaises(ValueError):
            TouchCalibration.from_env({'ELI_TOUCH_RAW_X_MIN': 'abc'})
        with self.assertRaises(ValueError):
            TouchCalibration(raw_x_min=3000, raw_x_max=2000)


class FilterTests(unittest.TestCase):
    def test_invalid_samples_rejected(self):
        timing = TouchTiming()
        good = [(2000, 2000)] * 7
        bad = [None, (100, 2000), (2000, 4000), (50, 50), (4095, 2000), (2000, 100), (3999, 99)]
        self.assertIsNone(filter_samples(good + bad, timing))        # only 7 valid < 8
        self.assertEqual(filter_samples(good + [(101, 3999)] + bad, timing)[2], 8)

    def test_median_filter_removes_outliers(self):
        samples = [(2000, 1500)] * 10 + [(3900, 200), (150, 3950), (3000, 3000), None, None]
        raw_x, raw_y, count = filter_samples(samples)
        self.assertEqual((raw_x, raw_y, count), (2000, 1500, 13))
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.proc = TouchProcessor(self.backend)

    def run_for(self, start, seconds, step=0.015):
        events, t = [], start
        while t < start + seconds - 1e-9:
            event = self.proc.poll(t)
            if event:
                events.append(event)
            t += step
        return events, t

    def test_settle_then_fifteen_samples_then_one_event(self):
        self.backend.pressed = True
        self.assertIsNone(self.proc.poll(0.0))
        self.assertIsNone(self.proc.poll(0.079))
        self.assertEqual(self.backend.reads, 0)       # no SPI during the settle delay
        event = self.proc.poll(0.080)
        self.assertEqual(self.backend.reads, 15)
        self.assertEqual((event.x, event.y), TouchCalibration().map(2000, 2000))
        self.assertEqual(event.samples, 15)

    def test_one_event_per_physical_press_despite_long_hold_and_bounce(self):
        self.backend.pressed = True
        events, t = self.run_for(0.0, 2.0)
        self.assertEqual(len(events), 1)
        reads = self.backend.reads
        # IRQ bounces high for 30 ms (< 50 ms debounce) and back: same press.
        self.backend.pressed = False
        more, t = self.run_for(t, 0.03)
        self.backend.pressed = True
        more2, t = self.run_for(t, 0.5)
        self.assertEqual(more + more2, [])
        self.assertEqual(self.backend.reads, reads)   # no re-sampling for the same press

    def test_release_debounce_then_next_press_is_new_event(self):
        self.backend.pressed = True
        events, t = self.run_for(0.0, 0.3)
        self.backend.pressed = False
        self.run_for(t, 0.045)
        self.assertEqual(self.proc.state, 'releasing')
        _, t = self.run_for(t + 0.045, 0.03)
        self.assertEqual(self.proc.state, 'idle')
        self.backend.pressed = True
        more, _ = self.run_for(t, 0.3)
        self.assertEqual((len(events), len(more)), (1, 1))

    def test_glitch_shorter_than_settle_is_ignored(self):
        self.backend.pressed = True
        self.proc.poll(0.0)
        self.backend.pressed = False
        self.proc.poll(0.03)
        self.assertEqual(self.proc.state, 'idle')
        self.assertEqual(self.backend.reads, 0)

    def test_bad_press_is_retried_then_rejected_without_event(self):
        self.backend.sample = None
        self.backend.pressed = True
        events, _ = self.run_for(0.0, 1.0)
        self.assertEqual(events, [])
        self.assertEqual(self.backend.reads, 15 * 3)   # first burst + 2 retries
        self.assertEqual(self.proc.state, 'held')
        self.assertEqual(self.proc.rejected_presses, 1)

    def test_recovers_when_a_retry_reads_well(self):
        samples = iter([None] * 15 + [(1000, 3000)] * 15)
        self.backend.sample = lambda: next(samples)
        self.backend.pressed = True
        events, _ = self.run_for(0.0, 0.3)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].raw_x, events[0].raw_y), (1000, 3000))


class ControllerTests(unittest.TestCase):
    def test_thread_delivers_one_tap_per_mock_press_and_cleans_up(self):
        cal = TouchCalibration()
        touch = MockTouch(calibration=cal)
        taps = []
        controller = TouchController(touch, taps.append, cal, log=lambda *_: None)
        controller.start()
        try:
            self.assertTrue(controller.running)
            touch.press_screen(200, 100)
            touch.release()
            deadline = time.monotonic() + 2
            while (not taps or controller.processor.state != 'idle') and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            self.assertEqual([(t.x, t.y) for t in taps], [(200, 100)])
        finally:
            controller.stop()
        self.assertFalse(controller.running)
        self.assertFalse(touch.is_open)
        controller.stop()                              # idempotent
        self.assertEqual(touch.closes, 1)

    def test_handler_errors_do_not_stop_polling(self):
        backend = FakeBackend()
        logs = []
        controller = TouchController(backend, MagicMock(side_effect=ValueError('boom')),
                                     log=logs.append)
        backend.pressed = True
        controller.poll_once(0.0)
        controller.poll_once(0.1)
        self.assertIn('boom', logs[0])
        self.assertEqual(controller.taps, 1)

    def test_backend_failure_stops_thread_and_logs_once(self):
        backend = FakeBackend()
        backend.is_pressed = MagicMock(side_effect=OSError('GPIO gone'))
        logs = []
        controller = TouchController(backend, lambda e: None, log=logs.append).start()
        deadline = time.monotonic() + 2
        while controller.running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(controller.running)
        self.assertEqual(len(logs), 1)
        self.assertIsInstance(controller.last_error, OSError)
        controller.stop()
        self.assertEqual(backend.closed, 1)

    def test_open_failure_raises_and_starts_no_thread(self):
        touch = MockTouch(fail_on_open=TouchUnavailableError('no panel'))
        controller = TouchController(touch, lambda e: None)
        with self.assertRaises(TouchUnavailableError):
            controller.start()
        self.assertFalse(controller.running)
        controller.stop()
        self.assertEqual(touch.closes, 0)


class XPT2046Tests(unittest.TestCase):
    def open_touch(self, spi, bus=None, **kwargs):
        board, digitalio, pins, log = fake_blinka(spi, **kwargs)
        with patch.object(touch_xpt2046, '_import_hardware', return_value=(board, digitalio)):
            touch = XPT2046Touch(bus=bus).open()
        return touch, board, pins, log

    def test_sample_protocol_configures_and_selects_every_time(self):
        spi = FakeSPI(values=(1500, 2500))
        touch, board, pins, log = self.open_touch(spi)
        self.assertEqual(pins['D6'].pull, 'up')
        self.assertTrue(pins['D5'].value)                         # deselected at rest
        pins['D6']._value = False
        self.assertTrue(touch.is_pressed())                       # active low
        pins['D6']._value = True
        self.assertFalse(touch.is_pressed())
        self.assertEqual(touch.read_raw(), (1500, 2500))
        self.assertEqual(touch.read_raw(), (1500, 2500))
        # Discard-then-keep on each axis, X (0xD0) then Y (0x90).
        self.assertEqual(spi.commands, [CMD_X, CMD_X, CMD_Y, CMD_Y] * 2)
        self.assertEqual(spi.configs, [dict(baudrate=500_000, polarity=0, phase=0, bits=8)] * 2)
        self.assertEqual(log, [('D5', False), ('D5', True)] * 8)  # CS per conversion
        self.assertFalse(spi.locked)                              # Blinka lock released
        self.assertFalse(touch.bus.locked)

    def test_bus_timeout_returns_none_without_blocking(self):
        spi = FakeSPI()
        bus = SharedSPIBus(spi, owner=False)
        touch, *_ = self.open_touch(spi, bus=bus)
        touch.sample_timeout = 0.05
        with bus.transaction():
            t0 = time.monotonic()
            self.assertIsNone(touch.read_raw())
            self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(touch.bus_timeouts, 1)
        self.assertEqual(spi.commands, [])

    def test_close_releases_pins_and_owned_bus_only(self):
        spi = FakeSPI()
        touch, board, pins, _ = self.open_touch(spi)
        touch.close()
        self.assertTrue(pins['D5'].deinited and pins['D6'].deinited)
        self.assertTrue(spi.deinited)                             # it created the bus
        shared_spi = FakeSPI()
        shared = SharedSPIBus(shared_spi, owner=False)
        touch, board, pins, _ = self.open_touch(shared_spi, bus=shared)
        board.SPI.assert_not_called()                             # reuses the TFT bus
        touch.close()
        touch.close()
        self.assertFalse(shared_spi.deinited)                     # the TFT owns it
        self.assertFalse(touch.is_open)

    def test_initialisation_failures(self):
        with patch.dict(sys.modules, {'board': None, 'digitalio': None}):
            with self.assertRaises(TouchUnavailableError) as ctx:
                XPT2046Touch().open()
        self.assertIn('adafruit-blinka', str(ctx.exception))
        spi = FakeSPI()
        with self.assertRaises(TouchUnavailableError) as ctx:
            _, _, pins, _ = self.open_touch(spi, pin_error=RuntimeError('GPIO busy'))
        self.assertIn('busy', str(ctx.exception))


class SharedSPITests(unittest.TestCase):
    def fake_tft(self, spi):
        board, digitalio, pins, _ = fake_blinka(spi)
        ili9341 = types.SimpleNamespace(ILI9341=MagicMock())
        return (board, digitalio, ili9341), board, digitalio

    def test_tft_and_touch_never_overlap_on_the_bus(self):
        active, overlaps, counts = [0], [], {'tft': 0, 'touch': 0}
        guard = threading.Lock()

        def transfer(who):
            with guard:
                active[0] += 1
                if active[0] > 1:
                    overlaps.append(who)
            time.sleep(0.0005)
            with guard:
                active[0] -= 1
                counts[who] += 1

        spi = FakeSPI(on_transfer=transfer)
        modules, board, digitalio = self.fake_tft(spi)
        with patch.object(tft_display, '_import_hardware', return_value=modules):
            display = TFTDisplay().open()
        modules[2].ILI9341.return_value.image.side_effect = lambda *a, **k: transfer('tft')
        with patch.object(touch_xpt2046, '_import_hardware', return_value=(board, digitalio)):
            touch = XPT2046Touch(bus=display.spi_bus).open()
        self.assertIs(touch.bus, display.spi_bus)
        frame = Image.new('RGB', (320, 240))
        stop = threading.Event()

        def tft_loop():
            while not stop.is_set():
                display.show(frame, (10, 10, 60, 60))

        thread = threading.Thread(target=tft_loop)
        thread.start()
        try:
            for _ in range(40):
                self.assertEqual(touch.read_raw(), (1500, 2500))
        finally:
            stop.set()
            thread.join()
        self.assertEqual(overlaps, [])
        self.assertGreater(counts['tft'], 0)
        self.assertEqual(counts['touch'], 40 * 4)
        self.assertEqual(len(spi.configs), 40)                    # reconfigured every sample
        touch.close()
        display.close()

    def test_tft_drops_frame_on_bus_timeout_and_resends_full(self):
        spi = FakeSPI()
        modules, *_ = self.fake_tft(spi)
        with patch.object(tft_display, '_import_hardware', return_value=modules):
            display = TFTDisplay().open()
        panel = modules[2].ILI9341.return_value
        display.spi_bus.timeout = 0.05
        frame = Image.new('RGB', (320, 240))
        panel.image.reset_mock()
        with display.spi_bus.transaction():
            self.assertFalse(display.show(frame, (10, 10, 50, 50)))
        panel.image.assert_not_called()
        self.assertEqual(display.dropped_frames, 1)
        self.assertTrue(display.show(frame, (10, 10, 50, 50)))
        panel.image.assert_called_once_with(frame)               # full frame, not a crop
        self.assertTrue(display.show(frame, (10, 10, 50, 50)))
        self.assertEqual(panel.image.call_args.kwargs, dict(rotation=0, x=10, y=270))
        display.close()
        self.assertIsNone(display.spi_bus)

    def test_bus_lock_is_bounded(self):
        bus = SharedSPIBus(object(), owner=False)
        with bus.transaction():
            t0 = time.monotonic()
            with self.assertRaises(SPIBusTimeout):
                with bus.transaction(timeout=0.02):
                    pass
            self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(bus.timeouts, 1)
        with bus.transaction(timeout=0.02):
            self.assertTrue(bus.locked)
        self.assertFalse(bus.locked)


class ImportTests(unittest.TestCase):
    def test_imports_without_hardware_or_gui_libraries(self):
        import subprocess
        code = '''
import importlib.abc, sys
BLOCKED = ('board', 'digitalio', 'busio', 'adafruit_rgb_display', 'tkinter', 'cv2', 'spidev')
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in BLOCKED:
            raise ImportError('blocked: ' + fullname)
sys.meta_path.insert(0, Block())
import display.touch, display.ui, display.robot_touch, display.ui_demo
import hardware.touch_xpt2046, hardware.spi_bus, hardware.mock_hardware
assert display.ui_demo.main(['--backend', 'mock', '--tour', '--fps', '30']) == 0
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
