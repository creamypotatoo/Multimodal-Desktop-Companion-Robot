"""Unified runtime with controller-owned state and one serial conversation/audio worker."""
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic

from behavior.state_machine import RobotStateMachine, State
from controller import RobotController
from controller.adapters import MockAdapter
from controller.errors import LIVE, SIMULATED, UNAVAILABLE
from intelligence.conversation import ConversationSession, MAX_CONSECUTIVE_ERRORS, greeting_for

_OWNER = Lock()
POLL = 0.05
ANNOUNCEMENT_TTL = 20
SUBSCRIBER = 'runtime-audio'
MESSAGES = {
    'timer_done': 'Your timer is complete.',
    'focus_done': 'Your focus session is complete.',
    'break_done': 'Your break is complete.',
}


@dataclass(frozen=True)
class Components:
    eyes: object
    record: object
    transcribe: object
    respond: object
    speak: object
    simulated: bool = True
    profile: object = lambda current: None
    cleanup_recording: object = lambda: None
    initial_volume: int = 70


class RuntimePorts(MockAdapter):
    """Controller-facing ports record intent. Workers own slow component calls."""

    def __init__(self):
        super().__init__()
        self.expression_target = 'idle'
        self.volume_effect = 'Subsequent speech; current playback keeps its volume.'

    def expression(self, value):
        super().expression(value)
        self.expression_target = value


class UnifiedRuntime:
    def __init__(self, components, *, clock=monotonic, announce=False, announce_in_sleep=False):
        self.components = components
        self.clock = clock
        self.announce = announce
        self.announce_in_sleep = announce_in_sleep
        self.ports = RuntimePorts()
        self.controller = RobotController(clock=clock, adapters=self.ports.ports(),
                                          managed_conversation=True,
                                          initial_volume=components.initial_volume)
        self._lifecycle = Lock()
        self._stop = Event()
        self._workers = []
        self._eyes = None
        self._owns_resources = False
        self._active = False
        self._closing = False
        self._cleanup_worker = None
        self._eye_cleanup = None
        self._health('lifecycle', 'Stopped', UNAVAILABLE)
        self._health('hardware', 'Mock motors, sensors and servos', SIMULATED)
        self._health('ai', 'Fake responder' if components.simulated else 'Ollama not yet checked',
                     SIMULATED if components.simulated else UNAVAILABLE)
        self._health('audio', 'Simulated audio' if components.simulated else 'Local audio, on demand',
                     SIMULATED if components.simulated else UNAVAILABLE)
        self._health('conversation', 'Ready on request')

    def _health(self, component, detail, availability=None):
        if availability is None:
            availability = SIMULATED if self.components.simulated else LIVE
        self.controller.update_runtime_health(component, availability, detail)

    def startup(self):
        with self._lifecycle:
            if self._active:
                return False
            if self._closing or not _OWNER.acquire(blocking=False):
                raise RuntimeError('A unified runtime already owns the process resources')
            self._owns_resources = True
            self._stop.clear()
            try:
                self._eyes = self.components.eyes()
                if self._eyes is not None and not getattr(self._eyes, 'running', False):
                    self._eyes.start()
                self._health('eyes', 'EyeEngine connected' if self._eyes else 'Eye display disabled',
                             (getattr(self._eyes, 'availability', None) or
                              (SIMULATED if self.components.simulated else LIVE))
                             if self._eyes else UNAVAILABLE)
                if not self.controller.startup():
                    raise RuntimeError('Controller already started')
                self.controller.events.subscribe(SUBSCRIBER)
                for target, name in ((self._render_eyes, 'eli-runtime-eyes'),
                                     (self._interact, 'eli-runtime-audio')):
                    worker = Thread(target=target, name=name, daemon=True)
                    worker.start()
                    self._workers.append(worker)
                self._active = True
                self._health('lifecycle', 'Running', LIVE)
                return True
            except Exception:
                self._stop.set()
                self.controller.shutdown()
                self._closing = True
                self._health('lifecycle', 'Startup failed', UNAVAILABLE)
                self._finish_shutdown()
                self._schedule_cleanup()
                raise

    def shutdown(self):
        with self._lifecycle:
            if not self._owns_resources:
                return
            self._active = False
            self._closing = True
            self._stop.set()
            self.controller.shutdown()
            if self._eye_cleanup is not None and not self._eye_cleanup.is_alive():
                self._eye_cleanup = None
            self._finish_shutdown()
            if self._owns_resources:
                self._health('lifecycle', 'Stopping; waiting for in-flight I/O', UNAVAILABLE)
                self._schedule_cleanup()
            else:
                self._health('lifecycle', 'Stopped', UNAVAILABLE)

    def _schedule_cleanup(self):
        pending = self._workers or (self._eye_cleanup is not None and self._eye_cleanup.is_alive())
        if pending and (self._cleanup_worker is None or not self._cleanup_worker.is_alive()):
            self._cleanup_worker = Thread(target=self._reap_workers,
                                          name='eli-runtime-cleanup', daemon=True)
            self._cleanup_worker.start()

    def _finish_shutdown(self):
        for worker in self._workers:
            worker.join(timeout=0.5)
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        if self._workers:
            return
        if self._eyes is not None:
            if self._eye_cleanup is None:
                self._eye_cleanup = Thread(target=self._close_eyes, name='eli-runtime-eye-close',
                                           daemon=True)
                self._eye_cleanup.start()
            self._eye_cleanup.join(timeout=0.5)
            if self._eyes is not None:
                return
        self._eye_cleanup = None
        if self._owns_resources:
            self._owns_resources = False
            self._closing = False
            _OWNER.release()

    def _close_eyes(self):
        try:
            self._eyes.stop()
            if getattr(self._eyes, 'running', False):
                raise RuntimeError('Eye engine is still running')
        except Exception:
            self._health('eyes', 'Eye shutdown incomplete; ownership retained', UNAVAILABLE)
        else:
            self._eyes = None

    def _reap_workers(self):
        while self._owns_resources:
            with self._lifecycle:
                self._finish_shutdown()
                if not self._owns_resources:
                    self._health('lifecycle', 'Stopped', UNAVAILABLE)
                    return
            if not self._workers and (self._eye_cleanup is None or not self._eye_cleanup.is_alive()):
                return  # An eye resource reported failure. Hold ownership until explicit retry.
            Event().wait(POLL)

    def _render_eyes(self):
        previous = None
        while not self._stop.is_set():
            expression = self.ports.expression_target
            if self._eyes is not None and previous != expression:
                try:
                    self._eyes.set_expression(expression)
                except Exception:
                    self._health('eyes', 'Eye expression update failed', UNAVAILABLE)
                previous = expression
            if self._eyes is not None and getattr(self._eyes, 'last_error', None):
                self._health('eyes', 'Eye engine reported a display error', UNAVAILABLE)
            self._stop.wait(POLL)

    def _record(self):
        try:
            recording = self.components.record()
        except Exception:
            self._health('microphone', 'Recording failed', UNAVAILABLE)
            raise
        self._health('microphone', 'Simulated input' if self.components.simulated else 'Capture succeeded')
        return recording

    def _respond(self, text, person_name, history=(), context=None):
        try:
            response = self.components.respond(text, person_name, history=history, context=context)
            if not isinstance(response, str) or not response.strip():
                raise ValueError('Missing AI response')
        except Exception:
            self._health('ai', 'Response generation failed', UNAVAILABLE)
            raise RuntimeError('AI response generation failed') from None
        if self.components.simulated:
            self._health('ai', 'Fake responder')
        elif getattr(self.components.respond, 'last_source', None) == 'ollama':
            self._health('ai', 'Ollama responded')
        else:
            self._health('ai', 'Ollama unavailable or disabled; using existing fallback', UNAVAILABLE)
        return response

    def _speak(self, text, current, on_start=None):
        if not current():
            return
        volume = self.controller.get_volume()['volume']
        try:
            if volume:
                self.components.speak(text, volume, current, on_start)
            self._health('audio', 'Muted' if not volume else 'Speech output completed')
        except Exception:
            self._health('audio', 'Speech output failed', UNAVAILABLE)
            raise RuntimeError('Speech output failed') from None

    def _new_session(self, generation, current):
        try:
            profile = self.components.profile(current)
        except Exception:
            self._health('camera', 'Camera or face recognition unavailable', UNAVAILABLE)
            profile = {}
        else:
            if profile is not None:
                self._health('camera', 'Last session camera capture completed')
        fsm = RobotStateMachine()
        fsm.add_listener(lambda old, new: self.controller.publish_behavior(generation, new))
        session = ConversationSession(
            fsm=fsm, record=self._record, transcriber=self.components.transcribe, responder=self._respond,
            speaker=lambda text, on_start=None: self._speak(text, current, on_start),
            sleep=lambda seconds: self._stop.wait(seconds),
            silence_rms=0 if self.components.simulated else None, is_current=current,
            **(profile or {}),
        )
        if session.person_name:
            session.say(greeting_for(session.person_name, session.visit_count))
        return session

    def _interact(self):
        pending = deque(maxlen=16)
        generation = session = None
        while not self._stop.is_set():
            if self.announce:
                try:
                    for event in self.controller.events.read(SUBSCRIBER):
                        if event['event'] == 'utility_completed':
                            pending.append(event['data'])
                except KeyError:
                    return
            current_generation = self.controller.interaction()
            if current_generation is not None:
                def current(token=current_generation):
                    return not self._stop.is_set() and self.controller.interaction() == token

                try:
                    if generation != current_generation:
                        generation = current_generation
                        self._health('conversation', 'Session running')
                        session = self._new_session(generation, current)
                    session.turn()
                    if session.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        self.controller.publish_behavior(generation, State.IDLE, finished=True)
                except Exception:
                    self._health('conversation', 'Session failed', UNAVAILABLE)
                    self.controller.publish_behavior(generation, State.IDLE, finished=True)
                finally:
                    try:
                        self.components.cleanup_recording()
                    except Exception:
                        self._health('microphone', 'Could not clean up recording', UNAVAILABLE)
            else:
                if generation is not None:
                    self._health('conversation', 'Ready on request')
                session = generation = None
                if pending:
                    item = pending.popleft()
                    if self._announcement_allowed() and self.clock() - item['observed_at'] < ANNOUNCEMENT_TTL:
                        try:
                            self._speak(MESSAGES[item['kind']], self._announcement_allowed)
                        except Exception:
                            pass  # Audio health is recorded by the shared speech path.
            self._stop.wait(POLL)

    def _announcement_allowed(self):
        return not self._stop.is_set() and self.controller.can_announce(self.announce_in_sleep)
