"""Typed controller messages. Authority is provided separately by the caller."""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class RobotMode(StrEnum):
    STANDBY = 'standby'
    COMPANION = 'companion'
    MOVEMENT = 'movement'
    SLEEP = 'sleep'


class Source(StrEnum):
    DASHBOARD = 'dashboard'
    VOICE = 'voice'
    SYSTEM = 'internal_system'
    AUTONOMOUS = 'autonomous'


class Action(StrEnum):
    STATUS = 'status'
    MODE = 'mode'
    EXPRESSION = 'expression'
    VOLUME = 'volume'
    SETTINGS = 'settings'
    SPEED = 'speed'
    MOVE = 'move'
    STOP = 'stop'
    SERVO = 'servo'
    CONVERSATION = 'conversation'
    EMERGENCY_STOP = 'emergency_stop'
    RESET_SAFETY = 'reset_safety'
    UTILITY = 'utility'
    UTILITY_SETTINGS = 'utility_settings'


@dataclass(frozen=True)
class Command:
    action: Action
    source: Source
    args: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Authority:
    owner: str | None = None
    can_move: bool = False
    can_wake: bool = False


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    status: int
    state: dict | None = None
    message: str | None = None
    code: str | None = None
    field: str | None = None
