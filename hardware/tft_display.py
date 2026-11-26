"""ILI9341 2.4" SPI TFT backend (Raspberry Pi) for Eli's eyes.

Hardware libraries (``board``, ``digitalio``, ``adafruit_rgb_display``) are
imported only inside ``TFTDisplay.open()``, so this module imports cleanly on
Windows and in tests. Missing libraries, unknown pins or busy GPIOs raise
``DisplayUnavailableError`` with a readable message.

Verified wiring (Raspberry Pi 4, SPI0, landscape rotation=90, 16 MHz):

  TFT pin  Pi GPIO  Physical pin  board name
  CS       GPIO22   15            D22   (software CS; do NOT use GPIO8/CE0)
  DC       GPIO24   18            D24
  RESET    GPIO25   22            D25
  MOSI     GPIO10   19            (SPI0 MOSI)
  SCK      GPIO11   23            (SPI0 SCLK)
  MISO     GPIO9    21            (optional; unused for writes)
  VCC/LED  3.3 V    1 or 17
  GND      GND      9

GPIO8 (CE0) is claimed by the Linux spidev driver, so requesting it as a
software chip select fails with "resource busy"; ``TFTConfig`` rejects it.

Configuration (explicit arguments override environment variables):
  ELI_DISPLAY_SPI_HZ     SPI clock in Hz              (default: 16000000)
  ELI_DISPLAY_CS         board pin name for CS        (default: D22)
  ELI_DISPLAY_DC         board pin name for DC        (default: D24)
  ELI_DISPLAY_RST        board pin name for RESET     (default: D25)
  ELI_DISPLAY_ROTATION   0, 90, 180 or 270            (default: 90)
  ELI_DISPLAY_PARTIAL    1 = send only changed region (default: 1)

The SPI bus is wrapped in ``hardware.spi_bus.SharedSPIBus`` and exposed as
``TFTDisplay.spi_bus`` so the XPT2046 touch controller (same bus, CS GPIO5)
can share it. Every panel transfer holds that lock; if the bus stays busy
beyond its timeout the frame is dropped (``show`` returns False) and the next
frame is sent in full, so the panel never keeps a stale region.
"""
import os
import re
from dataclasses import dataclass

from PIL import Image

from hardware.spi_bus import SharedSPIBus, SPIBusTimeout

ROTATIONS = (0, 90, 180, 270)
# Pillow transposes equal to Image.rotate(angle, expand=True), as used by
# adafruit_rgb_display (counter-clockwise).
_T = getattr(Image, 'Transpose', Image)  # Pillow < 9.1 keeps constants on Image
_TRANSPOSE = {90: _T.ROTATE_90, 180: _T.ROTATE_180, 270: _T.ROTATE_270}
_RESERVED_CS = {'D8', 'CE0'}
_PIN_NAME = re.compile(r'^[A-Z][A-Z0-9_]*$')


class DisplayUnavailableError(RuntimeError):
    """The display backend cannot be used on this machine right now."""


def _env_bool(value):
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'Expected a boolean (1/0), got {value!r}')


@dataclass(frozen=True)
class TFTConfig:
    cs: str = 'D22'
    dc: str = 'D24'
    reset: str = 'D25'
    baudrate: int = 16_000_000
    rotation: int = 90
    native_width: int = 240
    native_height: int = 320
    partial_updates: bool = True

    def __post_init__(self):
        for name in ('cs', 'dc', 'reset'):
            pin = str(getattr(self, name)).strip().upper()
            if not _PIN_NAME.match(pin):
                raise ValueError(f'{name} must be a board pin name such as D22, got {pin!r}')
            object.__setattr__(self, name, pin)
        if self.cs in _RESERVED_CS:
            raise ValueError('GPIO8/CE0 is owned by the Linux SPI driver and fails as a '
                             'software CS ("resource busy"); use D22 (GPIO22, pin 15)')
        if len({self.cs, self.dc, self.reset}) != 3:
            raise ValueError('CS, DC and RESET must be different pins')
        try:
            baudrate = int(self.baudrate)
            rotation = int(self.rotation)
        except (TypeError, ValueError):
            raise ValueError('baudrate and rotation must be integers') from None
        if not 1_000_000 <= baudrate <= 80_000_000:
            raise ValueError(f'SPI baudrate must be 1-80 MHz, got {baudrate}')
        if rotation not in ROTATIONS:
            raise ValueError(f'Rotation must be one of {ROTATIONS}, got {rotation}')
        if self.native_width <= 0 or self.native_height <= 0:
            raise ValueError('Native display dimensions must be positive')
        object.__setattr__(self, 'baudrate', baudrate)
        object.__setattr__(self, 'rotation', rotation)

    @property
    def size(self):
        """Frame size (width, height) the caller renders, after rotation."""
        if self.rotation in (90, 270):
            return (self.native_height, self.native_width)
        return (self.native_width, self.native_height)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        mapping = (('baudrate', 'ELI_DISPLAY_SPI_HZ', int), ('cs', 'ELI_DISPLAY_CS', str),
                   ('dc', 'ELI_DISPLAY_DC', str), ('reset', 'ELI_DISPLAY_RST', str),
                   ('rotation', 'ELI_DISPLAY_ROTATION', int),
                   ('partial_updates', 'ELI_DISPLAY_PARTIAL', _env_bool))
        for field, var, convert in mapping:
            if field not in overrides and environ.get(var, '').strip():
                try:
                    overrides[field] = convert(environ[var])
                except ValueError:
                    raise ValueError(f'{var}={environ[var]!r} is invalid') from None
        return cls(**overrides)


def native_origin(bbox, rotation, frame_size):
    """Native (unrotated) panel x, y for the top-left of a rotated crop.

    ``bbox`` is an exclusive ``(x0, y0, x1, y1)`` in frame coordinates and
    ``frame_size`` the frame (width, height), matching ``Image.rotate``.
    """
    x0, y0, x1, y1 = bbox
    width, height = frame_size
    if rotation == 0:
        return x0, y0
    if rotation == 90:
        return y0, width - x1
    if rotation == 180:
        return width - x1, height - y1
    if rotation == 270:
        return height - y1, x0
    raise ValueError(f'Rotation must be one of {ROTATIONS}, got {rotation}')


def _import_hardware():
    """Import Blinka/Adafruit modules lazily. Patched in tests."""
    try:
        import board
        import digitalio
        from adafruit_rgb_display import ili9341
    except Exception as exc:  # ImportError, or Blinka's platform detection errors
        raise DisplayUnavailableError(
            f'TFT libraries unavailable ({exc}). On the Raspberry Pi install '
            'adafruit-blinka and adafruit-circuitpython-rgb-display in the venv; '
            'on Windows use the preview or mock backend.') from exc
    return board, digitalio, ili9341


class TFTDisplay:
    """Physical ILI9341 display. ``show`` is called only from one thread."""

    def __init__(self, config=None):
        self.config = config or TFTConfig()
        self.width, self.height = self.config.size
        self.partial_updates = self.config.partial_updates
        self._disp = None
        self._pins = []
        self._spi = None
        self.spi_bus = None
        self.dropped_frames = 0
        self._force_full = False

    @property
    def is_open(self):
        return self._disp is not None

    def open(self):
        if self._disp is not None:
            return self
        board, digitalio, ili9341 = _import_hardware()
        cfg = self.config
        try:
            pins = {name: getattr(board, getattr(cfg, name)) for name in ('cs', 'dc', 'reset')}
        except AttributeError as exc:
            raise DisplayUnavailableError(f'Unknown board pin: {exc}') from exc
        try:
            for name in ('cs', 'dc', 'reset'):
                self._pins.append(digitalio.DigitalInOut(pins[name]))
            cs, dc, reset = self._pins
            self._spi = board.SPI()
            self.spi_bus = SharedSPIBus(self._spi, owner=False)  # this display deinits it
            with self.spi_bus.transaction():
                self._disp = ili9341.ILI9341(
                    self._spi, cs=cs, dc=dc, rst=reset, baudrate=cfg.baudrate,
                    width=cfg.native_width, height=cfg.native_height, rotation=cfg.rotation)
                self._disp.fill(0)
        except Exception as exc:
            self._release()
            hint = ' Is another process using the display or GPIO?' \
                if 'busy' in str(exc).lower() else ''
            raise DisplayUnavailableError(f'Could not initialise ILI9341: {exc}.{hint}') from exc
        return self

    def show(self, image, bbox=None):
        """Send ``image`` (frame-sized RGB). ``bbox`` limits the transfer to a
        changed region (exclusive x0, y0, x1, y1) when partial updates are on.

        Returns False (frame dropped, next frame forced full) if the shared SPI
        bus could not be acquired in time; True otherwise."""
        if self._disp is None:
            raise RuntimeError('TFT display is not open')
        if image.size != (self.width, self.height):
            raise ValueError(f'Expected a {self.width}x{self.height} image, got {image.size}')
        if self._force_full:
            bbox = None
        try:
            if bbox is None or not self.partial_updates or \
                    tuple(bbox) == (0, 0, self.width, self.height):
                # Same call as the verified full-screen hardware test.
                with self.spi_bus.transaction():
                    self._disp.image(image)
                self._force_full = False
                return True
            x0, y0, x1, y1 = (max(0, bbox[0]), max(0, bbox[1]),
                              min(self.width, bbox[2]), min(self.height, bbox[3]))
            if x1 <= x0 or y1 <= y0:
                return True
            crop = image.crop((x0, y0, x1, y1))
            rotation = self.config.rotation
            if rotation:
                crop = crop.transpose(_TRANSPOSE[rotation])
            nx, ny = native_origin((x0, y0, x1, y1), rotation, (self.width, self.height))
            with self.spi_bus.transaction():
                self._disp.image(crop, rotation=0, x=nx, y=ny)
            return True
        except SPIBusTimeout:
            self.dropped_frames += 1
            self._force_full = True
            return False

    def clear(self):
        if self._disp is not None:
            try:
                with self.spi_bus.transaction():
                    self._disp.fill(0)
            except SPIBusTimeout:
                self._force_full = True

    def close(self):
        self._release()

    def _release(self):
        self._disp = None
        self.spi_bus = None
        self._force_full = False
        for pin in self._pins:
            try:
                pin.deinit()
            except Exception:
                pass
        self._pins = []
        if self._spi is not None:
            try:
                self._spi.deinit()
            except Exception:
                pass
            self._spi = None
