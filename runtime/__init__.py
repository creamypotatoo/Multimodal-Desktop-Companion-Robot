"""Unified runtime with controller-owned state and one serial conversation/audio worker."""
from collections import deque
from dataclasses import dataclass
from math import ceil
from threading import Event, Lock, Thread
from time import monotonic

from behavior.state_machine import RobotStateMachine, State
from audio.runtime_audio import PlaybackNotStopped
from controller import Action, Command, RobotController, Source
from controller.adapters import MockAdapter
from controller.errors import LIVE, SIMULATED, UNAVAILABLE
from controller.errors import ConflictError, ValidationError
from intelligence.conversation import ConversationSession, MAX_CONSECUTIVE_ERRORS, greeting_for
from runtime.voice import intent, matches_wake, normalize

_OWNER = Lock()
POLL = 0.05
ANNOUNCEMENT_TTL = 20
TRANSCRIPT_TTL = 120
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
    capture: object = None
    microphone: object = None
    wake_detector: object = None
    audio_backend: str = 'mock'
    ai_simulated: bool | None = None
    audio_info: object = None
    camera: object = None
    optional_eyes: bool = False
    camera_problem: str | None = None
    input_problem: str | None = None
    stt_problem: str | None = None
    output_problem: str | None = None
    cancellable_stt: bool = False


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
    def __init__(self, components, *, clock=monotonic, announce=False, announce_in_sleep=False,
                 follow_up=20.0, wake_cooldown=3.0, serial_motors=None, motor_backend='mock'):
        self.components = components
        self.camera = components.camera
        self.clock = clock
        if not 1 <= follow_up <= 60 or not 0.5 <= wake_cooldown <= 60:
            raise ValueError('Follow-up window must be 1–60s and wake cooldown 0.5–60s')
        self.follow_up = follow_up
        self.wake_cooldown = wake_cooldown
        self._audio_lock = Lock()
        self._wake = dict(enabled=False, phrase='hey eli', in_sleep=False)
        self._wake_revision = 0
        self._last_wake = float('-inf')
        self._speech_epoch = 0
        self._turn_epoch = 0
        self._speaker_test = False
        self._mic_test = None
        self._mic_test_status = 'Not run'
        self._recognitions = {}
        self._audio_fault = None
        self.announce = announce
        self.announce_in_sleep = announce_in_sleep
        self.ports = RuntimePorts()
        self.controller = RobotController(clock=clock, adapters=self.ports.ports(),
                                          managed_conversation=True,
                                          initial_volume=components.initial_volume,
                                          serial_motors=serial_motors, motor_backend=motor_backend)
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
        self._health('hardware', 'See motor link and safety status', SIMULATED)
        self._health('ai', 'Fake responder' if self.mock_ai else 'Ollama not yet checked',
                     SIMULATED if self.mock_ai else UNAVAILABLE)
        self._health('audio', 'Simulated audio' if components.simulated else 'Local audio, on demand',
                     SIMULATED if components.simulated else UNAVAILABLE)
        self._health('conversation', 'Ready on request')
        for subsystem, problem in (('camera', components.camera_problem), ('microphone', components.input_problem),
                                   ('stt', components.stt_problem), ('audio', components.output_problem)):
            if problem:
                self._health(subsystem, problem, UNAVAILABLE)

    @property
    def mock_ai(self):
        return self.components.simulated if self.components.ai_simulated is None else self.components.ai_simulated

    def audio_status(self):
        with self._audio_lock:
            wake = dict(self._wake)
            self._expire_transcripts()
            recognitions = {key: dict(value) for key, value in self._recognitions.items()}
            mic_test = self._mic_test_status
        health = self.controller.status().get('runtime', {})
        microphone = self.components.microphone
        devices = dict(self.components.audio_info or {})
        if microphone:
            devices['selected_input'] = microphone.device
            devices['input_name'] = next((item['name'] for item in devices.get('inputs', [])
                                          if item['id'] == microphone.device),
                                         microphone.device or devices.get('default_input') or 'System default')
        stt = self.components.transcribe
        model_path = getattr(stt, 'path', None)
        return dict(wake=wake, backend=self.components.audio_backend,
                    detector='custom model' if self.components.wake_detector else 'offline phrase detection',
                    input_level=microphone.level if microphone else 0,
                    capture=(dict(peak=microphone.peak, speech=microphone.speech,
                                  noise=microphone.noise, onset=microphone.onset,
                                  threshold=microphone.settings.threshold,
                                  noise_ratio=microphone.settings.noise_ratio,
                                  max_gain=microphone.max_gain) if microphone else None),
                    recognition=recognitions, microphone_test=mic_test,
                    model=(dict(name=model_path.name, loaded=stt.model is not None) if model_path else None),
                    devices=devices,
                    volume=self.controller.get_volume()['volume'],
                    health={key: health[key] for key in ('microphone', 'wake', 'stt', 'audio', 'ai')
                            if key in health})

    def _expire_transcripts(self):
        now = self.clock()
        for key in list(self._recognitions):
            if now - self._recognitions[key]['at'] >= TRANSCRIPT_TTL:
                del self._recognitions[key]

    def configure_sensitivity(self, threshold, noise_ratio):
        microphone = self.components.microphone
        if not microphone:
            raise ConflictError('Sensitivity requires a real microphone')
        for field, value, low, high in (('threshold', threshold, 5, 5000),
                                        ('noise_ratio', noise_ratio, 1.2, 8)):
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValidationError(f'{field} must be from {low} to {high}', field=field)
        with self._audio_lock:
            microphone.configure(threshold, noise_ratio)
            self._wake_revision += 1
        return self.audio_status()

    def test_microphone(self):
        with self._audio_lock:
            if not self.components.microphone or self._audio_fault:
                raise ConflictError('Microphone test requires an available real audio backend')
            if not self._active or not self.controller.can_announce() or self._mic_test is not None:
                raise ConflictError('Microphone test requires an idle runtime in Standby or Companion')
            self._mic_test = self.controller.wake_context()
            self._mic_test_status = 'Queued'
        return {'queued': True}

    def _run_microphone_test(self, context):
        deadline = monotonic() + 5

        def permitted():
            return (not self._stop.is_set() and self.controller.can_announce()
                    and self.controller.wake_context() == context)

        self._mic_test_status = 'Listening for speech'
        try:
            speech = self.components.microphone.capture(lambda: permitted() and monotonic() < deadline,
                                                        timeout=4, test=True)
            self._mic_test_status = ('Cancelled' if not permitted() else
                                     'Speech detected' if speech else 'No speech detected')
        except Exception as exc:
            self._mic_test_status = f'Input failed ({type(exc).__name__})'
            self._health('microphone', self._mic_test_status, UNAVAILABLE)
        finally:
            with self._audio_lock:
                self._mic_test = None

    def select_input(self, device):
        microphone = self.components.microphone
        if microphone is None:
            raise ConflictError('Microphone selection is unavailable for this backend')
        devices = (self.components.audio_info or {}).get('inputs', [])
        if device is not None and (type(device) is not int or device not in {item['id'] for item in devices}):
            raise ValidationError('Choose an enumerated microphone ID or the system default', field='device')
        with self._audio_lock:
            microphone.device = device
            self._wake_revision += 1
            self._speech_epoch += 1
        return self.audio_status()

    def configure_wake(self, settings):
        if not settings or set(settings) - {'enabled', 'phrase', 'in_sleep'}:
            raise ValidationError('Provide enabled, phrase, or in_sleep')
        changes = {}
        for field, value in settings.items():
            if field == 'phrase':
                if not isinstance(value, str) or len(value) > 64 or not normalize(value):
                    raise ValidationError('Wake phrase must contain letters or numbers, up to 64 characters', field=field)
                changes[field] = normalize(value)
            elif type(value) is bool:
                changes[field] = value
            else:
                raise ValidationError('Expected a boolean', field=field)
        with self._audio_lock:
            self._wake.update(changes)
            self._wake_revision += 1
        return self.audio_status()

    def stop_speaking(self):
        with self._audio_lock:
            self._speech_epoch += 1
            self._speaker_test = False
        return {'requested': True}

    def test_speaker(self):
        with self._audio_lock:
            if self.components.output_problem:
                raise ConflictError(self.components.output_problem)
            if self._audio_fault or self._stop.is_set() or not self._active or not self.controller.can_announce():
                raise ConflictError('Speaker test requires an idle runtime in Standby or Companion')
            if not self.controller.get_volume()['volume']:
                raise ConflictError('Unmute audio before testing the speaker')
            self._speaker_test = (self.controller.wake_context(), self._speech_epoch)
        return {'queued': True}

    def mute(self, muted):
        if type(muted) is not bool:
            raise ValidationError('muted must be a boolean', field='muted')
        with self._audio_lock:
            volume = self.controller.get_volume()
            target = 0 if muted else volume['restore']
            result = self.controller.execute(Command(Action.VOLUME, Source.DASHBOARD, {'volume': target}))
            if muted:
                self._speech_epoch += 1
        return result.state

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
                self._start_eyes()
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

    def _start_eyes(self):
        try:
            self._eyes = self.components.eyes()
            if self._eyes is not None and not getattr(self._eyes, 'running', False):
                self._eyes.start()
        except (ImportError, OSError, RuntimeError) as exc:
            if not self.components.optional_eyes:
                raise
            if self._eyes is not None:
                self._eyes.stop()
                self._eyes = None
            self._health('eyes', f'TFT unavailable: {exc}', UNAVAILABLE)
            return
        self._health('eyes', 'EyeEngine connected' if self._eyes else 'Eye display disabled',
                     (getattr(self._eyes, 'availability', None) or
                      (SIMULATED if self.components.simulated else LIVE))
                     if self._eyes else UNAVAILABLE)

    def shutdown(self):
        with self._lifecycle:
            if not self._owns_resources:
                return
            self._active = False
            self._closing = True
            self._stop.set()
            with self._audio_lock:
                self._recognitions.clear()
                self._mic_test = None
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
        pending = self.camera is not None or self._workers or (
            self._eye_cleanup is not None and self._eye_cleanup.is_alive())
        if pending and (self._cleanup_worker is None or not self._cleanup_worker.is_alive()):
            self._cleanup_worker = Thread(target=self._reap_workers,
                                          name='eli-runtime-cleanup', daemon=True)
            self._cleanup_worker.start()

    def wait_for_shutdown(self, timeout):
        worker = self._cleanup_worker
        if worker is not None:
            worker.join(timeout=timeout)
        return not self._owns_resources

    def _finish_shutdown(self):
        camera_closed = self.camera is None or self.camera.shutdown()
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
        if not camera_closed:
            return
        if self._audio_fault and self._audio_fault.process.poll() is None:
            return
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
            if self.camera is None and not self._workers and (self._eye_cleanup is None or not self._eye_cleanup.is_alive()):
                return  # An eye resource reported failure. Hold ownership until explicit retry.
            Event().wait(POLL)

    def _render_eyes(self):
        previous = None
        gaze = None
        camera_health = None
        while not self._stop.is_set():
            with self._audio_lock:
                self._expire_transcripts()
            expression = self.ports.expression_target
            if self._eyes is not None and previous != expression:
                try:
                    self._eyes.set_expression(expression)
                except Exception:
                    self._health('eyes', 'Eye expression update failed', UNAVAILABLE)
                previous = expression
            target = None
            if self.camera is not None:
                snapshot = self.camera.snapshot()
                camera = snapshot['camera']
                health = (camera['detail'], (SIMULATED if camera['simulated'] else LIVE)
                          if camera['state'] == 'active' else UNAVAILABLE)
                if health != camera_health:
                    self._health('camera', *health)
                    camera_health = health
                if expression in ('idle', 'listening'):
                    target = snapshot['recognition']['gaze']
            if self._eyes is not None and target != gaze:
                try:
                    if target is None:
                        self._eyes.release_gaze()
                    else:
                        self._eyes.set_gaze(*target)
                    gaze = target
                except Exception:
                    self._health('eyes', 'Eye gaze update failed', UNAVAILABLE)
            if self._eyes is not None and getattr(self._eyes, 'last_error', None):
                self._health('eyes', 'Eye engine reported a display error', UNAVAILABLE)
            self._stop.wait(POLL)

    def _record(self, current=None, *, wake=False, timeout=None):
        try:
            recording = (self.components.capture(current, wake=wake, timeout=timeout)
                         if self.components.capture else self.components.record())
        except Exception as exc:
            self._health('microphone', f'Recording failed ({type(exc).__name__})', UNAVAILABLE)
            raise
        self._health('microphone', 'Simulated input' if self.components.simulated else 'Capture succeeded')
        return recording

    def _transcribe(self, recording, *, timeout, current=lambda: True, wake=False):
        start = self.clock()
        channel = 'wake' if wake else 'command'
        detail = None
        text = ''
        try:
            options = {'current': current} if self.components.cancellable_stt else {}
            text = self.components.transcribe(recording, timeout=timeout, **options)
            if not isinstance(text, str):
                raise TypeError('Expected transcript text')
        except Exception as exc:
            detail = ('Incompatible PyAV decoder (metadata_errors)' if 'metadata_errors' in str(exc)
                      else f'Transcription failed ({type(exc).__name__})')
            text = ''
            self._health('stt', detail, UNAVAILABLE)
        finally:
            now = self.clock()
            with self._audio_lock:
                if current():
                    self._recognitions[channel] = dict(text=text[:160], latency_ms=round((now-start)*1000),
                                                       at=now, error=detail)
        if detail:
            raise RuntimeError(detail)
        self._health('stt', 'Transcription completed' if text.strip() else 'No speech transcribed')
        return text

    def _respond(self, text, person_name, history=(), context=None):
        try:
            response = self.components.respond(text, person_name, history=history, context=context)
            if not isinstance(response, str) or not response.strip():
                raise ValueError('Missing AI response')
        except Exception:
            self._health('ai', 'Response generation failed', UNAVAILABLE)
            raise RuntimeError('AI response generation failed') from None
        if self.mock_ai:
            self._health('ai', 'Fake responder', SIMULATED)
        elif getattr(self.components.respond, 'last_source', None) == 'ollama':
            self._health('ai', 'Ollama responded')
        else:
            self._health('ai', 'Ollama unavailable or disabled; using existing fallback', UNAVAILABLE)
        return response

    def _speak(self, text, current, on_start=None):
        if self.components.output_problem:
            return
        with self._audio_lock:
            epoch = self._speech_epoch
        mode_context = self.controller.wake_context()

        def allowed():
            return (current() and epoch == self._speech_epoch
                    and self.controller.wake_context() == mode_context)

        if not allowed():
            return
        volume = self.controller.get_volume()['volume']
        try:
            if not volume:
                self._health('audio', 'Muted')
            else:
                self._health('wake', 'Suspended during speech', UNAVAILABLE)
                self._health('audio', 'Speech in progress')
                completed = self.components.speak(text, volume, allowed, on_start)
                self._health('audio', 'Speech stopped' if completed is False else 'Speech output completed')
        except PlaybackNotStopped as exc:
            self._audio_fault = exc
            self._health('audio', 'Playback exit unconfirmed; audio disabled', UNAVAILABLE)
            raise RuntimeError('Playback exit could not be confirmed') from None
        except Exception:
            detail = ('Speech output failed. Check the named ALSA device and run the standalone speaker test'
                      if self.components.audio_backend == 'pi' else 'Speech output failed')
            self._health('audio', detail, UNAVAILABLE)
            raise RuntimeError('Speech output failed') from None
        finally:
            if volume and on_start is None:
                self._stop.wait(0.3)

    def _voice_reply(self, generation, text, person_name, **kwargs):
        parsed = intent(text)
        if parsed is None:
            return self._respond(text, person_name, **kwargs)
        verb, *values = parsed
        if verb == 'clarify':
            return values[0]
        if verb == 'quiet':
            self.stop_speaking()
            return 'Speech stopped.'
        if verb == 'mode_query':
            return f"Current mode is {self.controller.get_mode()['mode']}."
        if verb == 'status':
            status = self.controller.status()
            return (f"Eli is in {status['mode']} mode. "
                    f"There are {len(status['active_utilities'])} active utilities.")
        if verb == 'remaining':
            timer = self.controller.utilities.snapshot()['timer']
            seconds = ceil(timer['remaining'])
            return f'{seconds} seconds remain.' if timer['state'] in ('running', 'paused') else 'No active timer.'
        if verb == 'utility':
            name, action, seconds = values
            result = self.controller.execute_voice(generation, Action.UTILITY, name=name,
                                                   action=action, duration=seconds)
            reply = f'{name.capitalize()} updated.'
        elif verb == 'mode':
            result = self.controller.execute_voice(generation, Action.MODE, mode=values[0])
            reply = f'{values[0].capitalize()} mode.'
        else:
            volume = values[0]
            if verb == 'relative_volume':
                volume = max(0, min(100, self.controller.get_volume()['volume'] + volume))
            result = self.controller.execute_voice(generation, Action.VOLUME, volume=volume)
            reply = f'Volume is {volume} percent.'
        return (reply if result.ok else result.message) if result else 'Request cancelled.'

    def _new_session(self, generation, current):
        visual = None
        if self.camera is not None:
            visual = self.camera.snapshot()['recognition']
            profile = {'person_name': visual['identity']}
        else:
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
        def respond(text, name, history=(), context=None):
            return self._voice_reply(generation, text, name, history=history, context=context)

        session = ConversationSession(
            fsm=fsm, record=lambda: self._record(current, timeout=max(0.1, self._listen_until - self.clock())),
            transcriber=lambda recording, timeout: self._transcribe(recording, timeout=timeout, current=current),
            responder=respond,
            speaker=lambda text, on_start=None: self._speak(text, current, on_start),
            sleep=lambda seconds: self._stop.wait(seconds),
            silence_rms=0 if self.components.capture or self.components.simulated else None,
            is_current=current, log_transcripts=False,
            **(profile or {}),
        )
        if visual is not None:
            session.context.update(face_present=visual['face_present'], identity_status=visual['identity_status'])
        elif session.person_name:
            session.say(greeting_for(session.person_name, session.visit_count))
        return session

    def _listen_for_wake(self):
        if self.components.input_problem or self.components.stt_problem:
            self._health('wake', 'Microphone or whisper.cpp unavailable', UNAVAILABLE)
            return
        context = self.controller.wake_context()
        mode = context[0]
        with self._audio_lock:
            settings = dict(self._wake)
            revision = self._wake_revision
        allowed_mode = mode in ('standby', 'companion') or mode == 'sleep' and settings['in_sleep']
        if (not settings['enabled'] or not allowed_mode or
                self.clock() - self._last_wake < self.wake_cooldown):
            self._health('wake', 'Disabled' if not settings['enabled'] else 'Suspended', UNAVAILABLE)
            return

        def current():
            return (not self._stop.is_set() and revision == self._wake_revision
                    and not self._speaker_test and self._mic_test is None and self.controller.interaction() is None
                    and self.controller.wake_context() == context)

        self._health('wake', 'Listening for wake phrase')
        try:
            recording = self._record(current, wake=True)
            if recording is None or not current():
                return
            detector = self.components.wake_detector
            matched = (detector(recording, settings['phrase']) if detector else
                       matches_wake(self._transcribe(recording, timeout=8, current=current, wake=True),
                                    settings['phrase']))
            if matched:
                with self._audio_lock:
                    if current() and self.controller.activate_voice(*context):
                        self._last_wake = self.clock()
                        self._health('wake', 'Wake phrase detected')
        except Exception:
            self._health('wake', 'Wake listening unavailable', UNAVAILABLE)
            self._stop.wait(0.8)
        finally:
            self._cleanup_recording()

    def _cleanup_recording(self):
        try:
            self.components.cleanup_recording()
        except Exception:
            self._health('microphone', 'Could not clean up recording', UNAVAILABLE)

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
            if current_generation is not None and (self.components.input_problem or self.components.stt_problem):
                self._health('conversation', self.components.input_problem or self.components.stt_problem, UNAVAILABLE)
                self.controller.publish_behavior(current_generation, State.IDLE, finished=True)
                current_generation = None
            if self._audio_fault:
                if current_generation is not None:
                    self.controller.publish_behavior(current_generation, State.IDLE, finished=True)
                self._stop.wait(POLL)
                continue
            if current_generation is not None:
                with self._audio_lock:
                    self._speaker_test = False
                    if self._mic_test is not None:
                        self._mic_test = None
                        self._mic_test_status = 'Cancelled'
                def current(token=current_generation):
                    return (not self._stop.is_set() and self.controller.interaction() == token
                            and self._turn_epoch == self._speech_epoch)

                try:
                    self._turn_epoch = self._speech_epoch
                    self._health('wake', 'Suspended during conversation', UNAVAILABLE)
                    if generation != current_generation:
                        generation = current_generation
                        self._listen_until = self.clock() + self.follow_up
                        self._health('conversation', 'Session running')
                        session = self._new_session(generation, current)
                        self._listen_until = self.clock() + self.follow_up
                    outcome = session.turn() if self.clock() < self._listen_until else 'expired'
                    if outcome == 'spoke':
                        self._listen_until = self.clock() + self.follow_up
                    if (outcome == 'expired' or self.clock() >= self._listen_until
                            or session.consecutive_errors >= MAX_CONSECUTIVE_ERRORS):
                        self.controller.publish_behavior(generation, State.IDLE, finished=True)
                except Exception:
                    self._health('conversation', 'Session failed', UNAVAILABLE)
                    self.controller.publish_behavior(generation, State.IDLE, finished=True)
                finally:
                    self._cleanup_recording()
            else:
                if generation is not None:
                    self._health('conversation', 'Ready on request')
                session = generation = None
                with self._audio_lock:
                    speaker_test, self._speaker_test = self._speaker_test, False
                    mic_test = self._mic_test
                    if mic_test is not None and speaker_test:
                        self._speaker_test = speaker_test
                if mic_test is not None:
                    self._run_microphone_test(mic_test)
                elif speaker_test:
                    context, epoch = speaker_test
                    try:
                        self._speak('Eli speaker test.', lambda: self.controller.can_announce()
                                    and self.controller.wake_context() == context
                                    and self._speech_epoch == epoch)
                    except Exception:
                        pass
                elif pending:
                    item = pending.popleft()
                    if self._announcement_allowed() and self.clock() - item['observed_at'] < ANNOUNCEMENT_TTL:
                        try:
                            self._speak(MESSAGES[item['kind']], self._announcement_allowed)
                        except Exception:
                            pass  # Audio health is recorded by the shared speech path.
                else:
                    self._listen_for_wake()
            self._stop.wait(POLL)

    def _announcement_allowed(self):
        return not self._stop.is_set() and self.controller.can_announce(self.announce_in_sleep)
