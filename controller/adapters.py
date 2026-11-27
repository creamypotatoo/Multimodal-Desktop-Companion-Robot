"""Subsystem contracts. Adapters must return promptly and never call the controller."""
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .errors import AdapterError, SIMULATED, UNAVAILABLE


class Eyes(Protocol):
    def expression(self, value: str) -> None: ...
    def appearance(self, color: tuple, speed: float) -> None: ...


class Audio(Protocol):
    def volume(self, value: int) -> None: ...


class Conversation(Protocol):
    def conversation(self, active: bool) -> None: ...


class Motors(Protocol):
    def move(self, direction: str, speed: int, duration_ms: int) -> None: ...
    def stop(self) -> None: ...


class Servos(Protocol):
    def servo(self, name: str, angle: int) -> None: ...


@dataclass(frozen=True)
class SafetyReading:
    obstacle: bool | None
    cliff: bool | None


class Sensors(Protocol):
    def safety(self) -> SafetyReading: ...


@dataclass(frozen=True)
class Adapters:
    eyes: Eyes
    audio: Audio
    conversation: Conversation
    motors: Motors
    servos: Servos
    sensors: Sensors
    simulated: bool = False


class UnavailableAdapter:
    availability = UNAVAILABLE

    def _unavailable(self, *args):
        raise AdapterError('Subsystem is unavailable')

    expression = appearance = volume = conversation = move = stop = servo = _unavailable

    def safety(self):
        return SafetyReading(None, None)


class MockAdapter:
    """One recorder can serve every port without importing robot hardware."""
    availability = SIMULATED

    def __init__(self):
        self.commands = deque(maxlen=100)
        self.reading = SafetyReading(False, False)

    def expression(self, value):
        self.commands.append(('expression', value))

    def appearance(self, color, speed):
        self.commands.append(('appearance', color, speed))

    def volume(self, value):
        self.commands.append(('volume', value))

    def conversation(self, active):
        self.commands.append(('conversation', active))

    def move(self, direction, speed, duration_ms):
        self.commands.append(('move', direction, speed, duration_ms))

    def stop(self):
        self.commands.append(('stop',))

    def servo(self, name, angle):
        self.commands.append(('servo', name, angle))

    def safety(self):
        return self.reading

    def ports(self):
        return Adapters(self, self, self, self, self, self, simulated=True)
