"""The robot service contract used by the dashboard API.

The API layer only talks to a ``RobotService``. Phase 1 ships
``MockRobotService``; a later phase can add a service backed by the real robot
controller (state machine, eye engine, ESP32 link) with the same methods and
return shapes, and the frontend will not change.

Every method returns plain JSON-serialisable dicts. Invalid input raises
``ValidationError``; a valid request the robot cannot perform right now raises
``ConflictError``. Each capability reports one of the ``AVAILABILITY`` values
so the UI can say honestly whether something is real, simulated or missing.
"""
from abc import ABC, abstractmethod

# How the dashboard labels every capability.
LIVE = 'live'                # backed by real data from the robot or host
SIMULATED = 'simulated'      # accepted and remembered, but nothing physical happens
UNAVAILABLE = 'unavailable'  # not connected in this phase; no data is shown
AVAILABILITY = (LIVE, SIMULATED, UNAVAILABLE)


class ServiceError(Exception):
    status = 500
    code = 'service_error'

    def __init__(self, message, *, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class ValidationError(ServiceError):
    status = 400
    code = 'validation_error'


class ConflictError(ServiceError):
    status = 409
    code = 'conflict'


class RobotService(ABC):
    """Operations the dashboard can request. Implementations must be thread-safe."""

    backend = 'abstract'

    @abstractmethod
    def status(self): ...

    @abstractmethod
    def get_mode(self): ...

    @abstractmethod
    def set_mode(self, mode): ...

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
    def move(self, direction, duration_ms=None): ...

    @abstractmethod
    def set_speed(self, speed): ...

    @abstractmethod
    def stop(self): ...

    @abstractmethod
    def set_servo(self, servo, angle): ...

    @abstractmethod
    def companion(self): ...

    @abstractmethod
    def conversation(self, action): ...

    @abstractmethod
    def perception(self): ...

    @abstractmethod
    def diagnostics(self): ...
