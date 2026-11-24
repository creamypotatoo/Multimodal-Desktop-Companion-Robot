from enum import Enum, auto

class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()

class RobotStateMachine:
    def __init__(self):
        self.state = State.IDLE
        self._listeners = []

    def add_listener(self, callback):
        """Call ``callback(old_state, new_state)`` after every transition.

        Listeners must be quick and non-blocking; their errors are ignored so
        an observer (e.g. the eye display) can never break the robot.
        """
        self._listeners.append(callback)

    def transition_to(self, new_state):
        print(f"[State change] {self.state.name} -> {new_state.name}")
        old_state, self.state = self.state, new_state
        for callback in list(self._listeners):
            try:
                callback(old_state, new_state)
            except Exception:
                pass

    def current_state(self):
        return self.state