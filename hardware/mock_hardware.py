class MockMotors:
    """
    A fake motor controller.
    Prints what it WOULD do instead of actually moving anything.
    Later, RealMotors will have these exact same method names,
    but will send commands to the ESP32 instead of printing.
    """

    def move_forward(self):
        print("[SIMULATION] Moving forward")

    def move_backward(self):
        print("[SIMULATION] Moving backward")

    def turn_left(self):
        print("[SIMULATION] Turning left")

    def turn_right(self):
        print("[SIMULATION] Turning right")

    def stop(self):
        print("[SIMULATION] Stopping")

class MockDisplay:
    """
    A fake TFT display for Eli's eyes.
    Keeps the latest frame in memory instead of sending it over SPI, so the
    eye engine runs on Windows and in tests. It has the same methods as
    hardware.tft_display.TFTDisplay: open(), show(image, bbox), clear(), close().
    ``transfer_delay`` simulates SPI time; ``fail_on_open``/``fail_on_show``
    are exceptions to raise, for testing error handling.
    """

    def __init__(self, width=320, height=240, transfer_delay=0.0,
                 fail_on_open=None, fail_on_show=None):
        self.width = width
        self.height = height
        self.transfer_delay = transfer_delay
        self.fail_on_open = fail_on_open
        self.fail_on_show = fail_on_show
        self.is_open = False
        self.frames_shown = 0
        self.clears = 0
        self.last_bbox = None
        self.frame = None

    def open(self):
        if self.fail_on_open is not None:
            raise self.fail_on_open
        if self.frame is None:
            from PIL import Image  # lazy: keep this module light to import
            self.frame = Image.new('RGB', (self.width, self.height))
        self.is_open = True
        return self

    def show(self, image, bbox=None):
        if not self.is_open:
            raise RuntimeError('Mock display is not open')
        if image.size != (self.width, self.height):
            raise ValueError(f'Expected a {self.width}x{self.height} image, got {image.size}')
        if self.fail_on_show is not None:
            raise self.fail_on_show
        if self.transfer_delay:
            import time
            time.sleep(self.transfer_delay)
        self.frame.paste(image)  # reuse one buffer instead of copying per frame
        self.last_bbox = bbox
        self.frames_shown += 1

    def clear(self):
        if self.frame is not None:
            self.frame.paste((0, 0, 0), (0, 0, self.width, self.height))
        self.clears += 1

    def close(self):
        self.is_open = False


class MockTouch:
    """
    A fake XPT2046 touch controller for Windows, the preview and tests.
    Same methods as hardware.touch_xpt2046.XPT2046Touch: open(),
    is_pressed(), read_raw(), close(). Thread-safe: press()/release() may be
    called from a GUI thread while the touch thread polls.

    ``press(raw_x, raw_y)`` simulates a finger at raw ADC values;
    ``press_screen(x, y)`` uses ``calibration.inverse`` to get them. A press
    reads as held for at least ``min_hold`` seconds from the first time it is
    polled, even if released sooner, so quick mouse clicks survive the 80 ms
    settle delay like real taps, however late the polling thread runs.
    ``samples`` (optional) is a list of raw samples (or None) returned in
    order before falling back to the current press position.
    """

    def __init__(self, calibration=None, min_hold=0.15, clock=None,
                 fail_on_open=None, samples=None):
        import threading
        import time
        self.calibration = calibration
        self.min_hold = min_hold
        self._clock = clock or time.monotonic
        self.fail_on_open = fail_on_open
        self.samples = list(samples or [])
        self.is_open = False
        self.opens = self.closes = 0
        self.reads = 0
        self._lock = threading.Lock()
        self._raw = None
        self._down_at = None      # first time is_pressed() observed this press
        self._pressed = False
        self._released = False

    def open(self):
        if self.fail_on_open is not None:
            raise self.fail_on_open
        self.is_open = True
        self.opens += 1
        return self

    def press(self, raw_x, raw_y):
        with self._lock:
            self._raw = (raw_x, raw_y)
            self._pressed, self._released, self._down_at = True, False, None

    def press_screen(self, x, y):
        if self.calibration is None:
            raise RuntimeError('press_screen() needs a calibration')
        self.press(*self.calibration.inverse(x, y))

    def release(self):
        with self._lock:
            if self._pressed:
                self._released = True

    def is_pressed(self):
        if not self.is_open:
            raise RuntimeError('Mock touch is not open')
        with self._lock:
            if not self._pressed:
                return False
            now = self._clock()
            if self._down_at is None:
                self._down_at = now
            if self._released and now - self._down_at >= self.min_hold:
                self._pressed = self._released = False
                self._down_at = None
                return False
            return True

    def read_raw(self):
        if not self.is_open:
            raise RuntimeError('Mock touch is not open')
        with self._lock:
            self.reads += 1
            if self.samples:
                return self.samples.pop(0)
            return self._raw

    def close(self):
        self.is_open = False
        self.closes += 1
