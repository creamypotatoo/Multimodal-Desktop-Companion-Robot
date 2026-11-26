"""Touch event processing for Eli's resistive touchscreen.

Hardware-independent pieces, each testable on its own:

  TouchCalibration  raw ADC -> screen pixels (verified mapping, configurable)
  TouchTiming       settle / sample / debounce parameters
  filter_samples    validity window + median filter
  TouchProcessor    per-press state machine: exactly one tap per physical press
  TouchController   low-priority polling thread delivering taps to a callback

A touch backend is any object with ``open()``, ``is_pressed()`` (IRQ, no
SPI), ``read_raw()`` -> ``(raw_x, raw_y)`` or None, and ``close()``:
``hardware.touch_xpt2046.XPT2046Touch`` or ``hardware.mock_hardware.MockTouch``.

Verified procedure (Raspberry Pi 4 + XPT2046): IRQ low -> wait 80 ms ->
take 15 samples -> keep samples with 100 < raw < 4000 on both axes -> need
at least 8 -> median per axis -> calibrate -> one tap; the press ends after
IRQ has stayed high for 50 ms. All timing uses the monotonic clock.

Calibration environment variables (defaults are the verified values):
  ELI_TOUCH_RAW_X_MIN=676  ELI_TOUCH_RAW_X_MAX=3469
  ELI_TOUCH_RAW_Y_MIN=652  ELI_TOUCH_RAW_Y_MAX=3435
  ELI_TOUCH_SWAP_AXES=1    ELI_TOUCH_INVERT_X=0  ELI_TOUCH_INVERT_Y=0
With swapped axes: screen_x comes from raw Y and screen_y from raw X.
"""
import os
import platform
import threading
import time
from dataclasses import dataclass
from typing import NamedTuple


def _env_bool(value):
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'Expected a boolean (1/0), got {value!r}')


@dataclass(frozen=True)
class TouchCalibration:
    raw_x_min: int = 676
    raw_x_max: int = 3469
    raw_y_min: int = 652
    raw_y_max: int = 3435
    swap_axes: bool = True
    invert_x: bool = False
    invert_y: bool = False
    width: int = 320
    height: int = 240

    def __post_init__(self):
        for name in ('raw_x_min', 'raw_x_max', 'raw_y_min', 'raw_y_max', 'width', 'height'):
            try:
                object.__setattr__(self, name, int(getattr(self, name)))
            except (TypeError, ValueError):
                raise ValueError(f'{name} must be an integer') from None
        for low, high in (('raw_x_min', 'raw_x_max'), ('raw_y_min', 'raw_y_max')):
            if not 0 <= getattr(self, low) < getattr(self, high) <= 4095:
                raise ValueError(f'Calibration needs 0 <= {low} < {high} <= 4095')
        if self.width < 2 or self.height < 2:
            raise ValueError('Screen size must be at least 2x2')
        for name in ('swap_axes', 'invert_x', 'invert_y'):
            object.__setattr__(self, name, bool(getattr(self, name)))

    def _spans(self):
        """(source_min, source_span) for the raw axes feeding screen x and y."""
        x_axis = (self.raw_y_min, self.raw_y_max - self.raw_y_min)
        y_axis = (self.raw_x_min, self.raw_x_max - self.raw_x_min)
        return (x_axis, y_axis) if self.swap_axes else (y_axis, x_axis)

    def map(self, raw_x, raw_y):
        """Screen (x, y) for a raw sample, clamped to the screen."""
        (x_min, x_span), (y_min, y_span) = self._spans()
        src_x, src_y = (raw_y, raw_x) if self.swap_axes else (raw_x, raw_y)
        x = int((src_x - x_min) * (self.width - 1) / x_span)
        y = int((src_y - y_min) * (self.height - 1) / y_span)
        x = max(0, min(self.width - 1, x))
        y = max(0, min(self.height - 1, y))
        if self.invert_x:
            x = self.width - 1 - x
        if self.invert_y:
            y = self.height - 1 - y
        return x, y

    def inverse(self, x, y):
        """Raw (raw_x, raw_y) that maps to screen (x, y); used by the mock backend."""
        if self.invert_x:
            x = self.width - 1 - x
        if self.invert_y:
            y = self.height - 1 - y
        (x_min, x_span), (y_min, y_span) = self._spans()
        # +0.5 px keeps int() truncation from landing one pixel short.
        src_x = round(x_min + (x + 0.5) * x_span / (self.width - 1))
        src_y = round(y_min + (y + 0.5) * y_span / (self.height - 1))
        return (src_y, src_x) if self.swap_axes else (src_x, src_y)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        mapping = (('raw_x_min', 'ELI_TOUCH_RAW_X_MIN', int), ('raw_x_max', 'ELI_TOUCH_RAW_X_MAX', int),
                   ('raw_y_min', 'ELI_TOUCH_RAW_Y_MIN', int), ('raw_y_max', 'ELI_TOUCH_RAW_Y_MAX', int),
                   ('swap_axes', 'ELI_TOUCH_SWAP_AXES', _env_bool),
                   ('invert_x', 'ELI_TOUCH_INVERT_X', _env_bool),
                   ('invert_y', 'ELI_TOUCH_INVERT_Y', _env_bool))
        for field, var, convert in mapping:
            if field not in overrides and environ.get(var, '').strip():
                try:
                    overrides[field] = convert(environ[var].strip())
                except ValueError:
                    raise ValueError(f'{var}={environ[var]!r} is invalid') from None
        return cls(**overrides)


@dataclass(frozen=True)
class TouchTiming:
    settle: float = 0.080          # after IRQ goes low, before sampling
    samples: int = 15
    min_valid: int = 8
    valid_low: int = 100           # exclusive bounds for a usable raw value
    valid_high: int = 4000
    release_debounce: float = 0.050
    poll_interval: float = 0.015
    retries: int = 2               # extra sampling bursts if a press reads badly

    def __post_init__(self):
        if self.samples < 1 or not 1 <= self.min_valid <= self.samples:
            raise ValueError('Need 1 <= min_valid <= samples')
        if not 0 <= self.valid_low < self.valid_high <= 4096:
            raise ValueError('Invalid raw validity window')
        for name in ('settle', 'release_debounce', 'poll_interval'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} must not be negative')
        if self.poll_interval <= 0:
            raise ValueError('poll_interval must be positive')
        if self.retries < 0:
            raise ValueError('retries must not be negative')


def median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def filter_samples(samples, timing=TouchTiming()):
    """Median (raw_x, raw_y) of the valid samples, or None if too few are valid.

    A sample is valid when it is not None and both coordinates lie strictly
    between ``valid_low`` and ``valid_high``.
    """
    lo, hi = timing.valid_low, timing.valid_high
    valid = [s for s in samples if s is not None and lo < s[0] < hi and lo < s[1] < hi]
    if len(valid) < timing.min_valid:
        return None
    return median([s[0] for s in valid]), median([s[1] for s in valid]), len(valid)


class TouchEvent(NamedTuple):
    x: int
    y: int
    raw_x: float
    raw_y: float
    samples: int     # valid samples used
    time: float      # monotonic time of the event


IDLE, SETTLING, HELD, RELEASING = 'idle', 'settling', 'held', 'releasing'


class TouchProcessor:
    """One tap per physical press, from IRQ state and raw samples.

    ``poll(now)`` is called periodically; it reads the IRQ (no SPI) and only
    samples once per press, after the settle delay. States:

      idle      -> IRQ low                         -> settling
      settling  -> IRQ high (shorter than settle)  -> idle, no event
                -> settle elapsed, good samples    -> held + TouchEvent
                -> bad samples, retries exhausted  -> held, no event
      held      -> IRQ high                        -> releasing
      releasing -> IRQ low again (bounce)          -> held (same press)
                -> high for release_debounce       -> idle
    """

    def __init__(self, backend, calibration=None, timing=None):
        self.backend = backend
        self.calibration = calibration or TouchCalibration()
        self.timing = timing or TouchTiming()
        self.state = IDLE
        self._since = 0.0
        self._attempts = 0
        self.rejected_presses = 0

    def reset(self):
        self.state = IDLE
        self._attempts = 0

    def poll(self, now):
        pressed = self.backend.is_pressed()
        state, timing = self.state, self.timing
        if state == IDLE:
            if pressed:
                self.state, self._since, self._attempts = SETTLING, now, 0
            return None
        if state == SETTLING:
            if not pressed:
                self.state = IDLE
                return None
            if now - self._since < timing.settle:
                return None
            result = filter_samples([self.backend.read_raw() for _ in range(timing.samples)], timing)
            if result is None:
                self._attempts += 1
                if self._attempts > timing.retries:
                    self.rejected_presses += 1
                    self.state = HELD          # wait for release; never a second event
                return None
            self.state = HELD
            raw_x, raw_y, count = result
            x, y = self.calibration.map(raw_x, raw_y)
            return TouchEvent(x, y, raw_x, raw_y, count, now)
        if state == HELD:
            if not pressed:
                self.state, self._since = RELEASING, now
            return None
        # RELEASING
        if pressed:
            self.state = HELD
        elif now - self._since >= timing.release_debounce:
            self.state = IDLE
        return None


class TouchController:
    """Polls a touch backend on its own daemon thread and delivers taps.

    ``on_tap(event)`` runs on the touch thread and must be quick (the UI
    manager only records it under a lock). Errors from the backend stop the
    thread after one log line; the rest of the robot carries on.
    """

    def __init__(self, backend, on_tap, calibration=None, timing=None, *,
                 clock=time.monotonic, niceness=5, log=print):
        self.backend = backend
        self.on_tap = on_tap
        self.processor = TouchProcessor(backend, calibration, timing)
        self.timing = self.processor.timing
        self.niceness = niceness
        self._clock = clock
        self._log = log
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._open = False
        self.last_error = None
        self.taps = 0

    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self):
        """Open the backend and start polling. Raises if the hardware is unavailable."""
        with self._lock:
            if self.running:
                return self
            if not self._open:
                self.backend.open()
                self._open = True
            self.processor.reset()
            self.last_error = None
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name='eli-touch', daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        """Stop polling and release GPIO/SPI. Idempotent."""
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
                if thread.is_alive():
                    self._log('[Touch] Touch worker did not stop within timeout')
            if self._open:
                try:
                    self.backend.close()
                finally:
                    self._open = False

    def poll_once(self, now=None):
        """Advance the processor once synchronously (tests); returns the event."""
        event = self.processor.poll(self._clock() if now is None else now)
        if event is not None:
            self._deliver(event)
        return event

    def _deliver(self, event):
        self.taps += 1
        try:
            self.on_tap(event)
        except Exception as exc:
            self._log(f'[Touch] Tap handler failed: {type(exc).__name__}: {exc}')

    def _lower_priority(self):
        if not self.niceness or platform.system() != 'Linux' or not hasattr(os, 'setpriority'):
            return
        try:
            tid = threading.get_native_id()
            os.setpriority(os.PRIO_PROCESS, tid, os.getpriority(os.PRIO_PROCESS, tid) + self.niceness)
        except OSError:
            pass

    def _run(self, stop):
        self._lower_priority()
        interval = self.timing.poll_interval
        try:
            while not stop.is_set():
                self.poll_once()
                stop.wait(interval)
        except Exception as exc:
            self.last_error = exc
            self._log(f'[Touch] Touch input stopped: {type(exc).__name__}: {exc}')
