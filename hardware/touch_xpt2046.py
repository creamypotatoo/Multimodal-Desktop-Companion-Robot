"""XPT2046 resistive touch controller (Raspberry Pi, shared SPI0 with the TFT).

Hardware libraries (``board``, ``digitalio``) are imported only inside
``XPT2046Touch.open()``, so this module imports cleanly on Windows and in
tests. Failures raise ``TouchUnavailableError`` with a readable message.

Verified wiring (Raspberry Pi 4, same SPI0 lines as the ILI9341):

  Touch pin  Pi GPIO  Physical pin  Notes
  T_CLK      GPIO11   23            shared SCLK
  T_DIN      GPIO10   19            shared MOSI
  T_OUT      GPIO9    21            shared MISO
  T_CS       GPIO5    29            software chip select (D5)
  T_IRQ      GPIO6    31            PENIRQ, active low, pull-up (D6)

Each conversion is one 3-byte transfer at 500 kHz, SPI mode 0:
command 0xD0 = raw X, 0x90 = raw Y, 12-bit result
``(((rx[1] << 8) | rx[2]) >> 3) & 0xFFF``. Before each retained reading one
conversion on the same axis is discarded (lets the ADC input settle).

Bus discipline (see ``hardware.spi_bus``): every sample takes the shared bus
lock with a bounded wait, reconfigures 500 kHz / mode 0, asserts only T_CS,
and releases T_CS before unlocking. The IRQ pin is a plain GPIO read and is
never read while holding the bus. IRQ is checked only between samples by
the caller, not per conversion (it glitches during conversions).

Configuration (explicit arguments override environment variables):
  ELI_TOUCH_CS      BCM number or board name for T_CS   (default: 5 -> D5)
  ELI_TOUCH_IRQ     BCM number or board name for T_IRQ  (default: 6 -> D6)
  ELI_TOUCH_SPI_HZ  SPI clock in Hz                     (default: 500000)
"""
import os
import re
import time
from dataclasses import dataclass

from hardware.spi_bus import SharedSPIBus, SPIBusTimeout, blinka_lock, blinka_unlock

CMD_X = 0xD0
CMD_Y = 0x90
SAMPLE_TIMEOUT = 0.5      # bounded wait for the bus (a full TFT frame is ~100 ms)
_RESERVED = {'D8', 'CE0', 'D7', 'CE1'}
_PIN = re.compile(r'^(?:GPIO|BCM|D)?(\d{1,2})$')
_NAME = re.compile(r'^[A-Z][A-Z0-9_]*$')


class TouchUnavailableError(RuntimeError):
    """The touch controller cannot be used on this machine right now."""


def decode_adc(rx):
    """12-bit conversion result from the 3 bytes clocked in for one command."""
    return (((rx[1] << 8) | rx[2]) >> 3) & 0xFFF


def normalize_pin(value):
    """'5', 'GPIO5', 'D5' -> 'D5'; other board names pass through upper-cased."""
    text = str(value).strip().upper()
    match = _PIN.match(text)
    if match:
        return f'D{int(match.group(1))}'
    if not _NAME.match(text):
        raise ValueError(f'Invalid pin {value!r}; use a BCM number such as 5')
    return text


@dataclass(frozen=True)
class XPT2046Config:
    cs: str = 'D5'
    irq: str = 'D6'
    baudrate: int = 500_000
    polarity: int = 0
    phase: int = 0

    def __post_init__(self):
        for name in ('cs', 'irq'):
            pin = normalize_pin(getattr(self, name))
            if pin in _RESERVED:
                raise ValueError(f'{pin} is a hardware SPI chip enable owned by the Linux '
                                 'spidev driver; use a free GPIO such as 5')
            object.__setattr__(self, name, pin)
        if self.cs == self.irq:
            raise ValueError('Touch CS and IRQ must be different pins')
        try:
            baudrate = int(self.baudrate)
        except (TypeError, ValueError):
            raise ValueError('Touch SPI baudrate must be an integer') from None
        if not 50_000 <= baudrate <= 2_500_000:
            raise ValueError(f'XPT2046 SPI clock must be 50 kHz - 2.5 MHz, got {baudrate}')
        if self.polarity not in (0, 1) or self.phase not in (0, 1):
            raise ValueError('SPI polarity and phase must be 0 or 1')
        object.__setattr__(self, 'baudrate', baudrate)

    @classmethod
    def from_env(cls, environ=None, **overrides):
        environ = os.environ if environ is None else environ
        mapping = (('cs', 'ELI_TOUCH_CS', str), ('irq', 'ELI_TOUCH_IRQ', str),
                   ('baudrate', 'ELI_TOUCH_SPI_HZ', int))
        for field, var, convert in mapping:
            if field not in overrides and environ.get(var, '').strip():
                try:
                    overrides[field] = convert(environ[var].strip())
                except ValueError:
                    raise ValueError(f'{var}={environ[var]!r} is invalid') from None
        return cls(**overrides)


def _import_hardware():
    """Import Blinka modules lazily. Patched in tests."""
    try:
        import board
        import digitalio
    except Exception as exc:  # ImportError, or Blinka's platform detection errors
        raise TouchUnavailableError(
            f'Touch libraries unavailable ({exc}). On the Raspberry Pi install '
            'adafruit-blinka in the venv; on Windows use ELI_TOUCH_BACKEND=mock.') from exc
    return board, digitalio


class XPT2046Touch:
    """Touch backend: ``open()``, ``is_pressed()``, ``read_raw()``, ``close()``.

    ``bus`` is the ``SharedSPIBus`` of the TFT (``TFTDisplay.spi_bus``). If it
    is None, ``open()`` creates and owns a bus of its own.
    """

    def __init__(self, config=None, bus=None, *, sample_timeout=SAMPLE_TIMEOUT):
        self.config = config or XPT2046Config()
        self.sample_timeout = sample_timeout
        self._external_bus = bus
        self.bus = None
        self._cs = self._irq = None
        self.bus_timeouts = 0

    @property
    def is_open(self):
        return self.bus is not None

    def open(self):
        if self.bus is not None:
            return self
        board, digitalio = _import_hardware()
        cfg = self.config
        try:
            cs_pin, irq_pin = getattr(board, cfg.cs), getattr(board, cfg.irq)
        except AttributeError as exc:
            raise TouchUnavailableError(f'Unknown board pin: {exc}') from exc
        try:
            self._cs = digitalio.DigitalInOut(cs_pin)
            self._cs.switch_to_output(value=True)        # deselected (active low)
            self._irq = digitalio.DigitalInOut(irq_pin)
            self._irq.switch_to_input(pull=digitalio.Pull.UP)
            if self._external_bus is not None:
                self.bus = self._external_bus
            else:
                self.bus = SharedSPIBus(board.SPI(), owner=True)
        except Exception as exc:
            self._release()
            hint = ' Is another process using GPIO5/GPIO6?' if 'busy' in str(exc).lower() else ''
            raise TouchUnavailableError(f'Could not initialise XPT2046: {exc}.{hint}') from exc
        return self

    def is_pressed(self):
        """True while the panel is touched (PENIRQ low). No SPI traffic."""
        if self._irq is None:
            raise RuntimeError('Touch controller is not open')
        return not self._irq.value

    def read_raw(self):
        """One (raw_x, raw_y) sample, or None if the bus stayed busy.

        Holds the shared bus only for these four short conversions.
        """
        if self.bus is None:
            raise RuntimeError('Touch controller is not open')
        cfg = self.config
        try:
            with self.bus.transaction(self.sample_timeout) as spi:
                blinka_lock(spi, time.monotonic() + self.sample_timeout)
                try:
                    # Reconfigure every time: the TFT leaves the bus at 16 MHz.
                    spi.configure(baudrate=cfg.baudrate, polarity=cfg.polarity,
                                  phase=cfg.phase, bits=8)
                    self._convert(spi, CMD_X)               # discarded
                    raw_x = self._convert(spi, CMD_X)
                    self._convert(spi, CMD_Y)               # discarded
                    raw_y = self._convert(spi, CMD_Y)
                finally:
                    blinka_unlock(spi)
        except SPIBusTimeout:
            self.bus_timeouts += 1
            return None
        return raw_x, raw_y

    def _convert(self, spi, command):
        tx = bytes((command, 0, 0))
        rx = bytearray(3)
        self._cs.value = False
        try:
            spi.write_readinto(tx, rx)
        finally:
            self._cs.value = True
        return decode_adc(rx)

    def close(self):
        self._release()

    def _release(self):
        bus, self.bus = self.bus, None
        if bus is not None and bus is not self._external_bus:
            bus.close()
        for pin in (self._cs, self._irq):
            if pin is not None:
                try:
                    pin.deinit()
                except Exception:
                    pass
        self._cs = self._irq = None
