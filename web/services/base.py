"""The robot service contract used by the dashboard API.

The dashboard uses a ``RobotService`` facade for controller commands and
host diagnostics. Robot state belongs to the controller.

Every method returns plain JSON-serialisable dicts. Invalid input raises
``ValidationError``; a valid request the robot cannot perform right now raises
``ConflictError``. Each capability reports one of the ``AVAILABILITY`` values
so the UI can say honestly whether something is real, simulated or missing.
"""
from abc import ABC, abstractmethod

from controller.errors import (LIVE, SIMULATED, UNAVAILABLE, ConflictError,
                               ServiceError, ValidationError)

AVAILABILITY = (LIVE, SIMULATED, UNAVAILABLE)


class RobotService(ABC):
    """Operations the dashboard can request. Implementations must be thread-safe."""

    backend = 'abstract'

    @abstractmethod
    def status(self): ...

    @abstractmethod
    def get_mode(self): ...

    @abstractmethod
    def set_mode(self, mode, *, authority): ...

    @abstractmethod
    def get_expression(self): ...

    @abstractmethod
    def set_expression(self, expression): ...

    @abstractmethod
    def get_volume(self): ...

    @abstractmethod
    def set_volume(self, volume): ...

    @abstractmethod
    def get_settings(self): ...

    @abstractmethod
    def update_settings(self, changes): ...

    @abstractmethod
    def movement(self): ...

    @abstractmethod
    def move(self, direction, duration_ms=None, *, authority): ...

    @abstractmethod
    def set_speed(self, speed, *, authority): ...

    @abstractmethod
    def stop(self): ...

    @abstractmethod
    def set_servo(self, servo, angle, *, authority): ...

    @abstractmethod
    def companion(self): ...

    @abstractmethod
    def conversation(self, action): ...

    @abstractmethod
    def perception(self): ...

    @abstractmethod
    def diagnostics(self): ...
