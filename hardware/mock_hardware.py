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
