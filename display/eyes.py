"""Eli's animated eye engine: lifecycle, scheduling and the public interface.

    from display.eyes import EyeEngine, create_display
    eyes = EyeEngine(create_display('tft'))
    eyes.start()                      # opens the display, starts the worker
    eyes.set_expression('listening')  # any thread; returns immediately
    eyes.set_gaze(0.4, -0.2)          # -1..1 each, clamped; held until released
    eyes.release_gaze()               # back to natural wandering
    eyes.blink()
    eyes.stop()                       # joins the worker, blanks and closes

One daemon worker thread renders at the configured FPS. Commands only update
animation state under a lock, so they never block on rendering or SPI. There
is no command queue: the latest expression/gaze wins, so memory is bounded.

A display backend is any object with ``width``, ``height``, ``open()``,
``show(image, bbox=None)``, ``clear()`` and ``close()``. ``show`` receives the
renderer's reused buffer plus an optional changed region; it must finish with
the image before returning. Backends: ``hardware.tft_display.TFTDisplay``,
``hardware.mock_hardware.MockDisplay`` and ``display.preview.PreviewDisplay``.

Frames whose integer geometry is unchanged are skipped entirely (no drawing,
no transfer). Otherwise only the union of the old and new eye bounds is sent.
"""
import os
import platform
import threading
import time

from display.animation import EyeAnimator
from display.config import AnimationConfig, EyeConfig
from display.expressions import Expression
from display.renderer import EyeRenderer, union_bounds
from hardware.tft_display import DisplayUnavailableError

BACKENDS = ('auto', 'tft', 'preview', 'mock')
DIRTY_PADDING = 2


def resolve_backend(name='auto'):
    """Turn ``auto`` into a concrete backend name; validate the others.

    ``auto`` uses ``ELI_DISPLAY_BACKEND`` if set, else ``tft`` on Linux and
    ``preview`` elsewhere.
    """
    name = (name or 'auto').strip().lower()
    if name == 'auto':
        name = os.environ.get('ELI_DISPLAY_BACKEND', '').strip().lower() or \
            ('tft' if platform.system() == 'Linux' else 'preview')
    if name not in BACKENDS or name == 'auto':
        raise ValueError(f'Unknown display backend {name!r}; expected one of: '
                         + ', '.join(BACKENDS))
    return name


def create_display(name='auto', **kwargs):
    """Build a display backend by name (see ``resolve_backend``).

    Hardware/GUI modules load lazily. Extra keyword arguments go to the backend.
    """
    name = resolve_backend(name)
    if name == 'tft':
        from hardware.tft_display import TFTConfig, TFTDisplay
        config = kwargs.pop('config', None) or TFTConfig.from_env(**kwargs)
        return TFTDisplay(config)
    if name == 'preview':
        from display.preview import PreviewDisplay
        return PreviewDisplay(**kwargs)
    if name == 'mock':
        from hardware.mock_hardware import MockDisplay
        return MockDisplay(**kwargs)


class EyeEngine:
    def __init__(self, display=None, config=None, animation=None, *, clock=time.monotonic,
                 rng=None, clear_on_stop=True, niceness=5, log=print):
        if display is None:
            from hardware.mock_hardware import MockDisplay
            display = MockDisplay()
        self.config = config or EyeConfig()
        self.animation = animation or AnimationConfig()
        if (display.width, display.height) != self.config.size:
            raise ValueError(f'Display is {display.width}x{display.height} but eyes render '
                             f'{self.config.frame_width}x{self.config.frame_height}')
        self.display = display
        self.clear_on_stop = clear_on_stop
        self.niceness = niceness
        self._clock = clock
        self._log = log
        self._renderer = EyeRenderer(self.config)
        self._state_lock = threading.Lock()      # animator state
        self._lifecycle_lock = threading.Lock()  # start/stop
        self._stats_lock = threading.Lock()
        self._animator = EyeAnimator(self.animation, rng=rng, now=clock())
        self._thread = None
        self._stop = threading.Event()
        self._open = False
        self._last_layout = None
        self._last_bounds = None
        self.last_error = None
        self._reset_stats()

    # -------------------------------------------------------- public interface
    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def expression(self):
        with self._state_lock:
            return self._animator.expression

    def start(self):
        """Open the display and start animating. Raises DisplayUnavailableError."""
        with self._lifecycle_lock:
            if self.running:
                return self
            if not self._open:
                self.display.open()
                self._open = True
            self.last_error = None
            self._last_layout = self._last_bounds = None  # first frame is full
            self._stop = threading.Event()
            with self._state_lock:
                self._animator.update(self._clock())  # avoid a dt jump after idle time
            self._reset_stats()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name='eli-eyes', daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        """Stop the worker, blank the screen and close the display. Idempotent."""
        with self._lifecycle_lock:
            self._stop.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
                if thread.is_alive():
                    self._log('[Display] Eye worker did not stop within timeout')
            if self._open:
                try:
                    if self.clear_on_stop:
                        self.display.clear()
                except Exception as exc:
                    self._log(f'[Display] Clear failed: {exc}')
                finally:
                    try:
                        self.display.close()
                    finally:
                        self._open = False

    def set_expression(self, expression):
        """Change expression (Expression, name or matching Enum). ValueError if unknown."""
        expression = Expression.parse(expression)
        with self._state_lock:
            return self._animator.set_expression(expression, self._clock())

    def set_gaze(self, x, y):
        """Look toward (x, y), each -1..1 (clamped); +x right, +y down."""
        with self._state_lock:
            return self._animator.set_gaze(x, y, self._clock())

    def release_gaze(self):
        with self._state_lock:
            self._animator.release_gaze(self._clock())

    def blink(self, double=False):
        """Blink now; returns False if a blink is already in progress."""
        with self._state_lock:
            return self._animator.blink(self._clock(), double=double)

    def render_frame(self, now=None):
        """Advance and present one frame synchronously (tests, benchmarks).

        Returns True if a frame was sent, False if it was skipped as unchanged.
        Must not be called while the worker thread is running.
        """
        if self.running:
            raise RuntimeError('render_frame() cannot be used while the engine is running')
        if not self._open:
            self.display.open()
            self._open = True
        return self._tick(self._clock() if now is None else now)

    def stats(self, reset=False):
        """Measured performance since start (or the last reset)."""
        with self._stats_lock:
            s = dict(self._stats)
            if reset:
                self._reset_stats_locked()
        elapsed = max(1e-9, time.perf_counter() - s.pop('since'))
        sent = s['frames_sent']
        return dict(
            elapsed=elapsed, frames_sent=sent, frames_skipped=s['frames_skipped'],
            overruns=s['overruns'], target_fps=self.animation.fps,
            sent_fps=sent / elapsed, tick_fps=(sent + s['frames_skipped']) / elapsed,
            render_ms=1000 * s['render_total'] / sent if sent else 0.0,
            render_ms_max=1000 * s['render_max'],
            transfer_ms=1000 * s['transfer_total'] / sent if sent else 0.0,
            transfer_ms_max=1000 * s['transfer_max'],
            pixels_per_frame=s['pixels'] / sent if sent else 0.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------- internals
    def _reset_stats(self):
        with self._stats_lock:
            self._reset_stats_locked()

    def _reset_stats_locked(self):
        self._stats = dict(since=time.perf_counter(), frames_sent=0, frames_skipped=0,
                           overruns=0, render_total=0.0, render_max=0.0,
                           transfer_total=0.0, transfer_max=0.0, pixels=0)

    def _tick(self, now):
        with self._state_lock:
            state = self._animator.update(now)
        t0 = time.perf_counter()
        layout = self._renderer.layout(state)
        if layout == self._last_layout:
            with self._stats_lock:
                self._stats['frames_skipped'] += 1
            return False
        image = self._renderer.draw(layout)
        t1 = time.perf_counter()
        bounds = self._renderer.bounds(layout)
        width, height = self.config.size
        if self._last_layout is None:
            dirty = None
            pixels = width * height
        else:
            x0, y0, x1, y1 = union_bounds(bounds, self._last_bounds)
            dirty = (max(0, x0 - DIRTY_PADDING), max(0, y0 - DIRTY_PADDING),
                     min(width, x1 + DIRTY_PADDING), min(height, y1 + DIRTY_PADDING))
            pixels = (dirty[2] - dirty[0]) * (dirty[3] - dirty[1])
        self.display.show(image, dirty)
        t2 = time.perf_counter()
        self._last_layout, self._last_bounds = layout, bounds
        with self._stats_lock:
            s = self._stats
            s['frames_sent'] += 1
            s['render_total'] += t1 - t0
            s['render_max'] = max(s['render_max'], t1 - t0)
            s['transfer_total'] += t2 - t1
            s['transfer_max'] = max(s['transfer_max'], t2 - t1)
            s['pixels'] += pixels
        return True

    def _lower_priority(self):
        # Linux applies niceness per thread: keep audio/STT ahead of the eyes.
        if not self.niceness or platform.system() != 'Linux' or not hasattr(os, 'setpriority'):
            return
        try:
            tid = threading.get_native_id()
            os.setpriority(os.PRIO_PROCESS, tid, os.getpriority(os.PRIO_PROCESS, tid) + self.niceness)
        except OSError:
            pass

    def _run(self, stop):
        self._lower_priority()
        period = self.animation.frame_period
        min_sleep = self.animation.min_sleep
        next_time = time.monotonic()
        try:
            while not stop.is_set():
                self._tick(self._clock())
                next_time += period
                delay = next_time - time.monotonic()
                if delay < min_sleep:
                    with self._stats_lock:
                        self._stats['overruns'] += 1
                    if delay < -period:
                        next_time = time.monotonic()  # fell behind: do not try to catch up
                    delay = min_sleep                 # always yield the CPU
                stop.wait(delay)
        except Exception as exc:
            self.last_error = exc
            self._log(f'[Display] Eye animation stopped: {type(exc).__name__}: {exc}')


__all__ = ['EyeEngine', 'create_display', 'resolve_backend', 'DisplayUnavailableError', 'Expression', 'BACKENDS']
