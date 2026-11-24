"""Phase 2: robot state -> eye expression integration (hardware-free)."""
import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch

import main
from audio import text_to_speech as tts
from behavior.state_machine import RobotStateMachine, State
from display import robot_eyes
from display.eyes import EyeEngine
from display.expressions import Expression
from hardware.mock_hardware import MockDisplay
from intelligence import conversation as conv
from tests.test_text_to_speech import FakeRun, fake_which


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def idle_engine():
    """A real engine on a mock display, not started (state changes still apply)."""
    return EyeEngine(MockDisplay(), log=lambda *_: None)


class MappingTests(unittest.TestCase):
    def test_every_state_maps_to_its_expression(self):
        self.assertEqual(robot_eyes.STATE_EXPRESSIONS, {
            State.IDLE: Expression.IDLE, State.LISTENING: Expression.LISTENING,
            State.THINKING: Expression.THINKING, State.SPEAKING: Expression.SPEAKING})

    def test_attach_follows_transitions(self):
        fsm, eyes = RobotStateMachine(), idle_engine()
        eyes.set_expression('sad')
        robot_eyes.attach(fsm, eyes)
        self.assertIs(eyes.expression, Expression.IDLE)  # current state shown at once
        with quiet():
            for state in (State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE):
                fsm.transition_to(state)
                self.assertIs(eyes.expression, Expression[state.name])

    def test_attach_without_eyes_is_noop(self):
        fsm = RobotStateMachine()
        robot_eyes.attach(fsm, None)
        with quiet():
            fsm.transition_to(State.THINKING)
        self.assertIs(fsm.current_state(), State.THINKING)


class FailureIsolationTests(unittest.TestCase):
    def test_backend_selection(self):
        for value in (None, '', 'off', 'none', 'disabled', '0'):
            env = {} if value is None else {'ELI_DISPLAY_BACKEND': value}
            self.assertIsNone(robot_eyes.selected_backend(env))
        self.assertEqual(robot_eyes.selected_backend({'ELI_DISPLAY_BACKEND': ' TFT '}), 'tft')
        with patch('display.robot_eyes.platform.system', return_value='Windows'):
            self.assertIsNone(robot_eyes.selected_backend({'ELI_DISPLAY_BACKEND': 'auto'}))
        with patch('display.robot_eyes.platform.system', return_value='Linux'):
            self.assertEqual(robot_eyes.selected_backend({'ELI_DISPLAY_BACKEND': 'auto'}), 'tft')

    def test_disabled_by_default(self):
        factory = Mock()
        self.assertIsNone(robot_eyes.start_robot_eyes({}, engine_factory=factory))
        factory.assert_not_called()

    def test_mock_backend_runs_at_configured_fps_and_stops_cleanly(self):
        env = {'ELI_DISPLAY_BACKEND': 'mock', 'ELI_DISPLAY_FPS': '20'}
        logs = []
        eyes = robot_eyes.start_robot_eyes(env, log=logs.append)
        try:
            self.assertTrue(eyes.running)
            self.assertEqual(eyes.animation.fps, 20.0)
            self.assertIn('mock at target 20 fps', logs[0])
        finally:
            robot_eyes.stop_robot_eyes(eyes)
        self.assertFalse(eyes.running)
        self.assertFalse(eyes.display.is_open)
        self.assertEqual(eyes.display.clears, 1)

    def test_tft_defaults_keep_verified_spi_clock_and_partial_updates(self):
        captured = {}

        def fake_create(name, config=None):
            captured['config'] = config
            return MockDisplay()
        with patch('display.eyes.create_display', fake_create):
            eyes = robot_eyes.start_robot_eyes(
                {'ELI_DISPLAY_BACKEND': 'tft', 'ELI_DISPLAY_FPS': '20'}, log=lambda *_: None)
        robot_eyes.stop_robot_eyes(eyes)
        self.assertEqual(captured['config'].baudrate, 16_000_000)
        self.assertTrue(captured['config'].partial_updates)

    def test_unavailable_tft_warns_once_and_returns_none(self):
        logs = []
        with patch.dict(sys.modules, {'board': None, 'digitalio': None}):
            eyes = robot_eyes.start_robot_eyes({'ELI_DISPLAY_BACKEND': 'tft'}, log=logs.append)
        self.assertIsNone(eyes)
        self.assertEqual(len(logs), 1)
        self.assertIn('Eyes disabled', logs[0])

    def test_bad_configuration_and_unsupported_backends_are_isolated(self):
        for env in ({'ELI_DISPLAY_BACKEND': 'preview'}, {'ELI_DISPLAY_BACKEND': 'hdmi'},
                    {'ELI_DISPLAY_BACKEND': 'mock', 'ELI_DISPLAY_FPS': 'fast'},
                    {'ELI_DISPLAY_BACKEND': 'tft', 'ELI_DISPLAY_CS': 'D8'}):
            logs = []
            with self.subTest(env=env):
                self.assertIsNone(robot_eyes.start_robot_eyes(env, log=logs.append))
                self.assertEqual(len(logs), 1)

    def test_start_failure_is_not_retried(self):
        engine = Mock()
        engine.start.side_effect = OSError('SPI busy')
        factory = Mock(return_value=engine)
        self.assertIsNone(robot_eyes.start_robot_eyes({'ELI_DISPLAY_BACKEND': 'mock'},
                                                      engine_factory=factory, log=lambda *_: None))
        factory.assert_called_once_with('mock')
        engine.start.assert_called_once()

    def test_failing_eyes_never_break_state_transitions(self):
        fsm, eyes, logs = RobotStateMachine(), Mock(), []
        eyes.set_expression.side_effect = RuntimeError('display gone')
        robot_eyes.attach(fsm, eyes, log=logs.append)
        with quiet():
            for state in (State.LISTENING, State.THINKING, State.SPEAKING):
                fsm.transition_to(state)
        self.assertIs(fsm.current_state(), State.SPEAKING)
        self.assertEqual(len(logs), 1)  # one concise warning, not one per transition

    def test_state_machine_ignores_listener_errors(self):
        fsm = RobotStateMachine()
        fsm.add_listener(Mock(side_effect=ValueError('boom')))
        seen = []
        fsm.add_listener(lambda old, new: seen.append((old, new)))
        with quiet():
            fsm.transition_to(State.LISTENING)
        self.assertEqual(seen, [(State.IDLE, State.LISTENING)])

    def test_render_failure_leaves_conversation_state_working(self):
        engine = EyeEngine(MockDisplay(fail_on_show=OSError('SPI write failed')), log=lambda *_: None)
        engine.start()
        try:
            for _ in range(300):
                if not engine.running:
                    break
                engine._thread.join(0.01)
            self.assertFalse(engine.running)
            fsm = RobotStateMachine()
            robot_eyes.attach(fsm, engine)
            with quiet():
                fsm.transition_to(State.THINKING)  # still accepted, no exception
            self.assertIs(engine.expression, Expression.THINKING)
        finally:
            robot_eyes.stop_robot_eyes(engine)
        self.assertFalse(engine.display.is_open)


class ConversationStageTests(unittest.TestCase):
    """Expressions follow the real processing stages of one conversation turn."""

    def test_turn_stages(self):
        fsm, eyes = RobotStateMachine(), idle_engine()
        robot_eyes.attach(fsm, eyes)
        seen = {}

        def record():
            seen['record'] = eyes.expression
            return 'loud.wav'

        def transcribe(_file, timeout=None):
            seen['transcribe'] = eyes.expression
            return 'hello'

        def respond(text, person):
            seen['respond'] = eyes.expression
            return 'Hi there.'

        def speak(text, on_start=None):
            seen['synthesis'] = eyes.expression
            on_start()
            seen['playback'] = eyes.expression

        session = conv.ConversationSession(
            'Shanta', fsm, record=record, transcriber=transcribe, responder=respond,
            speaker=speak, sleep=Mock(), silence_rms=0)
        with quiet():
            self.assertEqual(session.turn(), 'spoke')
        self.assertEqual(seen, dict(
            record=Expression.LISTENING, transcribe=Expression.THINKING,
            respond=Expression.THINKING, synthesis=Expression.THINKING,
            playback=Expression.SPEAKING))
        self.assertEqual(session.history, [('hello', 'Hi there.')])

    def test_legacy_speaker_still_enters_speaking(self):
        fsm = RobotStateMachine()
        states = []
        session = conv.ConversationSession(
            'Shanta', fsm, speaker=lambda text: states.append(fsm.current_state()),
            responder=lambda text, person: 'ok', sleep=Mock())
        with quiet():
            self.assertTrue(session.say('hi'))
        self.assertEqual(states, [State.SPEAKING])

    def test_session_ends_idle(self):
        fsm, eyes = RobotStateMachine(), idle_engine()
        robot_eyes.attach(fsm, eyes)
        session = conv.ConversationSession(
            'Shanta', fsm, record=Mock(side_effect=KeyboardInterrupt),
            responder=lambda text, person: 'ok', speaker=lambda text, on_start=None: None,
            sleep=Mock())
        with quiet(), self.assertRaises(KeyboardInterrupt):
            session.run()
        self.assertIs(eyes.expression, Expression.IDLE)


class SpeakerHookTests(unittest.TestCase):
    def test_on_start_runs_after_synthesis_before_playback(self):
        run = FakeRun()
        order = []
        original = run.__call__

        def tracking(cmd, **kwargs):
            order.append(cmd[0].rsplit('/', 1)[-1])
            return original(cmd, **kwargs)
        with patch.object(tts.shutil, 'which', fake_which), \
                patch.object(tts.subprocess, 'run', tracking):
            tts.speak('hi', tts.TTSConfig(backend='espeak'), on_start=lambda: order.append('on_start'))
        self.assertEqual(order, ['espeak-ng', 'ffmpeg', 'on_start', 'aplay'])

    def test_failing_callback_does_not_stop_speech(self):
        run = FakeRun()
        with patch.object(tts.shutil, 'which', fake_which), patch.object(tts.subprocess, 'run', run):
            tts.speak('hi', tts.TTSConfig(backend='espeak'), on_start=Mock(side_effect=RuntimeError))
        self.assertEqual(len(run.calls), 3)


class MainIntegrationTests(unittest.TestCase):
    def run_main(self, env, argv=('--headless', '--vision-only')):
        camera = Mock()
        camera.read.side_effect = KeyboardInterrupt
        stopped = []
        real_stop = robot_eyes.stop_robot_eyes
        log = io.StringIO()
        with patch.dict('os.environ', env), \
                patch('main.Camera', return_value=camera), \
                patch('main.load_vision', return_value=(Mock(), Mock())), \
                patch('main.stop_robot_eyes', side_effect=lambda e: (stopped.append(e), real_stop(e))), \
                contextlib.redirect_stdout(log):
            status = main.main(list(argv))
        return status, stopped, log.getvalue()

    def test_headless_runs_mock_eyes_and_shuts_them_down(self):
        status, stopped, log = self.run_main({'ELI_DISPLAY_BACKEND': 'mock'})
        self.assertEqual(status, 0)
        self.assertEqual(len(stopped), 1)
        eyes = stopped[0]
        self.assertIsInstance(eyes, EyeEngine)
        self.assertFalse(eyes.running)
        self.assertFalse(eyes.display.is_open)
        self.assertIn('Eyes running on mock', log)

    def test_robot_starts_without_display_hardware(self):
        with patch.dict(sys.modules, {'board': None, 'digitalio': None}):
            status, stopped, log = self.run_main({'ELI_DISPLAY_BACKEND': 'tft'})
        self.assertEqual(status, 0)
        self.assertEqual(stopped, [None])
        self.assertEqual(log.count('Eyes disabled'), 1)
        self.assertIn('Eli is online', log)

    def test_display_off_by_default(self):
        status, stopped, log = self.run_main({'ELI_DISPLAY_BACKEND': ''})
        self.assertEqual((status, stopped), (0, [None]))
        self.assertNotIn('[Display]', log)


if __name__ == '__main__':
    unittest.main()
