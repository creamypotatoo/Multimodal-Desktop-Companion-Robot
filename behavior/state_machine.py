from enum import Enum, auto

class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()

class RobotStateMachine:
    def __init__(self):
        self.state = State.IDLE

    def transition_to(self, new_state):
        print(f"[State change] {self.state.name} -> {new_state.name}")
        self.state = new_state

    def current_state(self):
        return self.state