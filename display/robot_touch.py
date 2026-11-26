"""Connect the touchscreen and Eli OS menu to the running eyes (main.py).

``start_robot_touch(eyes)`` builds the touch backend, a ``UIManager`` and a
``TouchController`` from environment variables, attaches the UI to the eye
engine (which stays the single display owner) and starts polling. It
returns a ``RobotTouch`` handle, or ``None`` after one concise warning when
touch is off or unavailable; the eyes and conversation are unaffected.

  ELI_TOUCH_BACKEND  off | xpt2046 | mock   (default: off)
  ELI_TOUCH_CS, ELI_TOUCH_IRQ, ELI_TOUCH_SPI_HZ       see hardware/touch_xpt2046.py
  ELI_TOUCH_RAW_{X,Y}_{MIN,MAX}, ELI_TOUCH_SWAP_AXES,
  ELI_TOUCH_INVERT_X, ELI_TOUCH_INVERT_Y              see display/touch.py

With ``xpt2046`` and the TFT display, the touch controller shares the TFT's
SPI bus lock (``TFTDisplay.spi_bus``).
"""
import os

DISABLED = ('', 'off', 'none', 'disabled', '0', 'false')
TOUCH_BACKENDS = ('xpt2046', 'mock')


def _log(message):
    print(f'[Touch] {message}', flush=True)


def selected_touch_backend(environ=None):
    """The configured touch backend name, or None when touch is off."""
    environ = os.environ if environ is None else environ
    name = environ.get('ELI_TOUCH_BACKEND', '').strip().lower()
    return None if name in DISABLED else name


def create_touch(name, environ=None, *, bus=None, calibration=None):
    """Build a touch backend by name (hardware imports stay lazy)."""
    if name == 'xpt2046':
        from hardware.touch_xpt2046 import XPT2046Config, XPT2046Touch
        return XPT2046Touch(XPT2046Config.from_env(environ), bus=bus)
    if name == 'mock':
        from hardware.mock_hardware import MockTouch
        return MockTouch(calibration=calibration)
    raise ValueError(f'ELI_TOUCH_BACKEND={name!r} is not supported; use xpt2046, mock or off')


class RobotTouch:
    """Running touch input + UI. ``stop()`` detaches the UI and frees GPIO."""

    def __init__(self, eyes, ui, controller, alerts=None):
        self.eyes, self.ui, self.controller, self.alerts = eyes, ui, controller, alerts

    @property
    def backend(self):
        return self.controller.backend

    def stop(self):
        try:
            self.controller.stop()   # joins the touch thread, releases T_CS/T_IRQ
        finally:
            self.eyes.detach_ui()
            if self.alerts is not None:
                self.alerts.stop()


def start_robot_touch(eyes, environ=None, *, fsm=None, log=_log, backend_factory=None,
                      speak=None):
    """Start touch input and the menu once; return RobotTouch or None. Never raises."""
    environ = os.environ if environ is None else environ
    try:
        name = selected_touch_backend(environ)
        if name is None:
            return None
        if name not in TOUCH_BACKENDS:
            raise ValueError(f'ELI_TOUCH_BACKEND={name!r} is not supported; use xpt2046, mock or off')
        if eyes is None:
            raise RuntimeError('the touch menu needs the eye display (set ELI_DISPLAY_BACKEND)')
        from display.touch import TouchCalibration, TouchController
        from display.ui import UIManager
        width, height = eyes.display.width, eyes.display.height
        calibration = TouchCalibration.from_env(environ, width=width, height=height)
        bus = getattr(eyes.display, 'spi_bus', None)
        backend = (backend_factory or create_touch)(name, environ, bus=bus, calibration=calibration)
        from display.ui.alerts import AlertPlayer
        alerts = AlertPlayer(eyes, fsm, speak)
        ui = UIManager(size=(width, height), on_change=eyes.request_frame, on_event=alerts.handle)
        controller = TouchController(backend, ui.handle_touch, calibration)
        controller.start()           # raises TouchUnavailableError if the hardware is absent
    except Exception as exc:
        log(f'Touch disabled: {exc}')
        return None
    alerts.start()
    eyes.attach_ui(ui)
    log(f'Touch input running on {name}; tap the eyes to open the menu.')
    return RobotTouch(eyes, ui, controller, alerts)


def stop_robot_touch(touch, *, log=_log):
    """Stop touch polling and detach the menu; never raises."""
    if touch is None:
        return
    try:
        touch.stop()
    except Exception as exc:
        log(f'Touch shutdown failed: {exc}')
