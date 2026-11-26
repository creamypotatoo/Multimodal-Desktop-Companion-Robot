"""Shared SPI0 bus coordination for the ILI9341 TFT and the XPT2046 touch panel.

Both devices sit on the same SCLK/MOSI/MISO lines with independent software
chip selects (TFT GPIO22, touch GPIO5). They run at different clocks
(16 MHz vs 500 kHz), so every transaction must:

  1. hold the bus exclusively (``SharedSPIBus.transaction``, bounded wait),
  2. reconfigure clock/mode for its own device,
  3. assert only its own chip select, and release it before unlocking.

The TFT's Adafruit driver already reconfigures (via ``SPIDevice``) and toggles
its CS on every write; the touch driver does the same explicitly. This lock
is the single authority: Blinka's own ``try_lock`` is a plain flag and is not
safe between threads, so it is only taken while this lock is held.

Never hold the bus while waiting for anything except the transfer itself.
"""
import threading
import time
from contextlib import contextmanager

DEFAULT_TIMEOUT = 0.5   # longer than a full 320x240 frame at 16 MHz (~80-150 ms)


class SPIBusTimeout(RuntimeError):
    """The shared SPI bus could not be acquired within the timeout."""


class SharedSPIBus:
    """A Blinka ``busio.SPI`` (or a test double) plus a thread lock.

    ``owner`` decides whether ``close()`` deinitialises the underlying SPI
    object; the device that created it owns it.
    """

    def __init__(self, spi, *, owner=True, timeout=DEFAULT_TIMEOUT):
        self.spi = spi
        self.owner = owner
        self.timeout = timeout
        self._lock = threading.Lock()
        self.timeouts = 0

    @property
    def locked(self):
        return self._lock.locked()

    @contextmanager
    def transaction(self, timeout=None):
        """Hold the bus for one short transaction; raise SPIBusTimeout if busy."""
        timeout = self.timeout if timeout is None else timeout
        if not self._lock.acquire(timeout=timeout):
            self.timeouts += 1
            raise SPIBusTimeout(f'SPI bus busy for more than {timeout:.3f} s')
        try:
            yield self.spi
        finally:
            self._lock.release()

    def close(self):
        spi, self.spi = self.spi, None
        if spi is not None and self.owner:
            try:
                spi.deinit()
            except Exception:
                pass


def blinka_lock(spi, deadline):
    """Take Blinka's per-object SPI lock (needed before ``configure``).

    Only called while ``SharedSPIBus`` is held, so it normally succeeds at
    once; the deadline guarantees it can never spin forever.
    """
    try_lock = getattr(spi, 'try_lock', None)
    if try_lock is None:
        return
    while not try_lock():
        if time.monotonic() >= deadline:
            raise SPIBusTimeout('Blinka SPI lock not released')
        time.sleep(0.0005)


def blinka_unlock(spi):
    unlock = getattr(spi, 'unlock', None)
    if unlock is not None:
        unlock()
