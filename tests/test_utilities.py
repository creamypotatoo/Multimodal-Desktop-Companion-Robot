"""Phase 2 utilities: countdown, stopwatch, Pomodoro, live screens and alerts."""
import contextlib
import io
import threading
import time
import unittest
from unittest.mock import patch

from audio import text_to_speech as tts
from behavior.state_machine import RobotStateMachine, State
from display.config import AnimationConfig
from display.expressions import Expression
from display.eyes import EyeEngine
from display.ui import EYES, MENU, UIManager
from display.ui.alerts import AlertPlayer
from display.ui.apps import Countdown, Pomodoro, Stopwatch, UtilityEvent, format_time
from hardware.mock_hardware import MockDisplay

QUIET = AnimationConfig(blinking=False, auto_gaze=False)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class CountdownTests(unittest.TestCase):
    def test_accuracy_is_time_based_not_frame_based(self):
        t = Countdown()
        t.start(1000.0, 300)
        self.assertEqual(t.remaining(1000.0), 300)
        self.assertAlmostEqual(t.remaining(1123.4), 176.6)
        self.assertIsNone(t.poll(1299.999))
        self.assertEqual(t.poll(1300.0), UtilityEvent('timer_done'))   # exact, one poll is enough
        self.assertIsNone(t.poll(1301.0))                               # fires once
        self.assertEqual((t.state, t.remaining(1400)), (Countdown.DONE, 0.0))

    def test_late_poll_still_completes_once(self):
        t = Countdown()
        t.start(0.0, 60)
        self.assertEqual(t.poll(500.0).kind, 'timer_done')
        self.assertIsNone(t.poll(501.0))

    def test_pause_resume_excludes_paused_time(self):
        t = Countdown()
        t.start(0.0, 100)
        t.pause(30.0)
        self.assertEqual(t.remaining(500.0), 70)
        self.assertIsNone(t.poll(500.0))
        t.resume(500.0)
        self.assertEqual(t.remaining(560.0), 10)
        self.assertEqual(t.poll(570.0).kind, 'timer_done')

    def test_reset_cancel_and_restart(self):
        t = Countdown()
        t.start(0.0, 90)
        t.reset()
        self.assertEqual((t.state, t.remaining(50.0)), (Countdown.READY, 90))
        self.assertIsNone(t.poll(1000.0))
        t.toggle(10.0)
        self.assertEqual(t.remaining(40.0), 60)
        t.poll(100.0)
        t.toggle(200.0)                           # Restart after completion
        self.assertEqual((t.state, t.remaining(200.0)), (Countdown.RUNNING, 90))
        t.cancel()
        self.assertEqual((t.state, t.duration), (Countdown.SETUP, 90))

    def test_custom_duration_controls(self):
        t = Countdown()
        self.assertFalse(t.start(0.0))           # zero duration does nothing
        t.adjust('h', 1)
        t.adjust('m', -1)                         # wraps 0 -> 59
        t.adjust('s', 61)
        self.assertEqual(t.fields, (1, 59, 1))
        self.assertEqual(t.duration, 3600 + 59 * 60 + 1)
        t.adjust('h', -2)
        self.assertEqual(t.fields[0], 23)
        self.assertEqual(format_time(3661), '1:01:01')
        self.assertEqual(format_time(59.2), '00:59')


class StopwatchTests(unittest.TestCase):
    def test_elapsed_pause_resume_and_laps(self):
        s = Stopwatch()
        s.start(10.0)
        self.assertAlmostEqual(s.elapsed(22.5), 12.5)
        self.assertEqual(s.lap(22.5), (1, 12.5, 12.5))
        s.pause(30.0)
        self.assertIsNone(s.lap(31.0))            # no laps while paused
        self.assertEqual(s.elapsed(100.0), 20.0)
        s.resume(100.0)
        number, split, total = s.lap(105.0)
        self.assertEqual(number, 2)
        self.assertAlmostEqual(split, 12.5)
        self.assertAlmostEqual(total, 25.0)
        self.assertEqual(format_time(total, tenths=True), '00:25.0')
        s.reset()
        self.assertEqual((s.state, s.elapsed(200.0), s.laps), ('idle', 0.0, []))

    def test_lap_history_is_bounded(self):
        s = Stopwatch()
        s.start(0.0)
        for i in range(150):
            s.lap(float(i + 1))
        self.assertEqual(len(s.laps), 99)
        self.assertEqual(s.laps[-1][0], 150)


class PomodoroTests(unittest.TestCase):
    def test_defaults_and_full_cycle_with_long_break(self):
        p = Pomodoro()
        self.assertEqual(p.minutes, {'focus': 25, 'short': 5, 'long': 15})
        now = 0.0
        p.start(now)
        for session in range(1, 5):
            end_focus = now + 25 * 60
            self.assertIsNone(p.poll(end_focus - 0.01))
            event = p.poll(end_focus + 0.5)                 # polled late
            expected = Pomodoro.LONG if session == 4 else Pomodoro.SHORT
            self.assertEqual(event, UtilityEvent('focus_done', expected))
            self.assertEqual((p.phase, p.state, p.completed), (expected, 'running', session))
            # The break starts at the exact focus end, not at the late poll.
            break_len = (15 if session == 4 else 5) * 60
            self.assertAlmostEqual(p.remaining(end_focus + 0.5), break_len - 0.5)
            self.assertEqual(p.poll(end_focus + break_len), UtilityEvent('break_done'))
            self.assertEqual((p.phase, p.state), (Pomodoro.FOCUS, 'ready'))
            now = end_focus + break_len + 10
            p.start(now)
        self.assertEqual(p.cycle_position, 0)

    def test_pause_resume_skip_reset(self):
        p = Pomodoro()
        p.start(0.0)
        p.pause(60.0)
        self.assertEqual(p.remaining(9999.0), 24 * 60)
        self.assertIsNone(p.poll(9999.0))
        p.resume(10000.0)
        self.assertEqual(p.remaining(10000.0 + 60), 23 * 60)
        p.skip(10100.0)                                       # skipped focus is not counted
        self.assertEqual((p.phase, p.state, p.completed), (Pomodoro.SHORT, 'running', 0))
        self.assertEqual(p.remaining(10100.0), 5 * 60)
        p.skip(10110.0)
        self.assertEqual((p.phase, p.state), (Pomodoro.FOCUS, 'ready'))
        p.reset()
        self.assertEqual((p.phase, p.state, p.completed), (Pomodoro.FOCUS, 'ready', 0))

    def test_configurable_durations(self):
        p = Pomodoro()
        p.adjust('focus', -1)
        p.adjust('short', 3)
        p.adjust('long', 100)                               # clamped
        self.assertEqual(p.minutes, {'focus': 20, 'short': 8, 'long': 60})
        self.assertEqual(p.remaining(0.0), 20 * 60)          # ready phase shows the new length
        p.start(0.0)
        self.assertEqual(p.poll(20 * 60).detail, Pomodoro.SHORT)
        self.assertEqual(p.remaining(20 * 60), 8 * 60)
        custom = Pomodoro(focus=50, short=10, long=30, long_every=2)
        self.assertEqual(custom.phase_seconds(Pomodoro.LONG), 1800)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def tap(ui, button_id, clock):
    screen = ui.registry.get(ui.current)
    button = next(b for b in screen.buttons if b.id == button_id)
    x0, y0, x1, y1 = button.rect
    assert ui.tap((x0 + x1) // 2, (y0 + y1) // 2, now=clock.now) == button_id, button_id
    ui.render(clock.now)
    clock.now += 0.2
    return ui.render(clock.now)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(100.0)
        self.events = []
        self.ui = UIManager(clock=self.clock, on_event=self.events.append)
        self.ui.open(MENU)
        self.ui.render(self.clock.now)

    def test_timer_preset_runs_in_background_and_completes_on_eyes_screen(self):
        tap(self.ui, 'timer', self.clock)
        tap(self.ui, 'preset-1', self.clock)
        self.assertEqual(self.ui.apps.timer.state, Countdown.RUNNING)
        self.ui.home()
        self.assertIsNone(self.ui.render(self.clock.now + 30))
        self.assertEqual(self.events, [])
        self.assertIsNone(self.ui.render(self.clock.now + 61))     # still on the eyes
        self.assertEqual(self.events, [UtilityEvent('timer_done')])
        self.ui.open(MENU)
        self.ui.open('timer')
        self.ui.render(self.clock.now + 62)
        self.assertEqual([b.label for b in self.ui.registry.get('timer').buttons][2:], ['Restart', 'Reset', 'Cancel'])

    def test_only_changed_regions_are_redrawn(self):
        tap(self.ui, 'timer', self.clock)
        screen = self.ui.registry.get('timer')
        for _ in range(2):
            tap(self.ui, 'm+', self.clock)                          # value region + button only
        self.assertEqual(self.ui.apps.timer.fields, (0, 2, 0))
        tap(self.ui, 'start', self.clock)                          # new mode: full redraw
        t = self.clock.now
        self.assertEqual(self.ui.render(t + 0.3), (None, None))     # same second: nothing to send
        image, bbox = self.ui.render(t + 1.0)
        self.assertIsNotNone(image)
        self.assertTrue(bbox[1] >= screen.TIME[1] and bbox[3] <= screen.BAR[3])  # digits + bar only

    def test_timer_controls(self):
        tap(self.ui, 'timer', self.clock)
        tap(self.ui, 's+', self.clock)
        tap(self.ui, 'start', self.clock)
        timer = self.ui.apps.timer
        tap(self.ui, 'toggle', self.clock)
        self.assertEqual(timer.state, Countdown.PAUSED)
        tap(self.ui, 'toggle', self.clock)
        self.assertEqual(timer.state, Countdown.RUNNING)
        tap(self.ui, 'reset', self.clock)
        self.assertEqual(timer.state, Countdown.READY)
        tap(self.ui, 'cancel', self.clock)
        self.assertEqual(timer.state, Countdown.SETUP)

    def test_stopwatch_screen_laps(self):
        tap(self.ui, 'stopwatch', self.clock)
        tap(self.ui, 'toggle', self.clock)
        self.clock.now += 5
        tap(self.ui, 'lap', self.clock)
        self.clock.now += 3
        tap(self.ui, 'lap', self.clock)
        laps = self.ui.apps.stopwatch.laps
        self.assertEqual([n for n, *_ in laps], [1, 2])
        self.assertAlmostEqual(laps[1][1], 3.2, places=5)
        tap(self.ui, 'toggle', self.clock)
        tap(self.ui, 'reset', self.clock)
        self.assertEqual(self.ui.apps.stopwatch.laps, [])

    def test_focus_screen_and_durations(self):
        tap(self.ui, 'focus', self.clock)
        tap(self.ui, 'durations', self.clock)
        self.assertEqual(self.ui.current, 'focus_settings')
        tap(self.ui, 'focus-', self.clock)
        tap(self.ui, 'done', self.clock)
        self.assertEqual(self.ui.current, 'focus')
        tap(self.ui, 'toggle', self.clock)
        p = self.ui.apps.pomodoro
        self.assertEqual((p.state, p.remaining(self.clock.now)), ('running', 20 * 60))
        self.ui.home()
        self.ui.render(self.clock.now + 20 * 60)
        self.assertEqual(self.events, [UtilityEvent('focus_done', Pomodoro.SHORT)])
        tap(self.ui, 'open-menu', self.clock)
        tap(self.ui, 'focus', self.clock)
        tap(self.ui, 'skip', self.clock)
        self.assertEqual((p.phase, p.state), (Pomodoro.FOCUS, 'ready'))

    def test_timers_survive_navigation_and_conversation(self):
        display = MockDisplay()
        eyes = EyeEngine(display, animation=QUIET, log=lambda *_: None)
        events = []
        ui = UIManager(on_change=eyes.request_frame, on_event=events.append)
        eyes.attach_ui(ui)
        fsm = RobotStateMachine()
        from display.robot_eyes import attach
        attach(fsm, eyes)
        ui.apps.timer.start(0.0, 10)
        ui.apps.stopwatch.start(0.0)
        ui.open(MENU)
        ui.open('stopwatch')
        eyes.render_frame(1.0)
        ui.home()
        with quiet():
            for i, state in enumerate((State.LISTENING, State.THINKING, State.SPEAKING)):
                fsm.transition_to(state)
                eyes.render_frame(2.0 + i)
        self.assertEqual(events, [])
        eyes.render_frame(10.0)
        self.assertEqual(events, [UtilityEvent('timer_done')])
        self.assertAlmostEqual(ui.apps.stopwatch.elapsed(10.0), 10.0)
        self.assertIs(eyes.expression, Expression.SPEAKING)


class FakeEyes:
    def __init__(self):
        self.expression = Expression.IDLE
        self.history = []

    def set_expression(self, expression):
        self.expression = Expression.parse(expression)
        self.history.append(self.expression)


class AlertTests(unittest.TestCase):
    def test_flashes_expression_speaks_and_restores(self):
        eyes, spoken = FakeEyes(), []
        player = AlertPlayer(eyes, RobotStateMachine(), spoken.append, hold=0.0)
        with quiet():
            player.play(UtilityEvent('timer_done'))
            player.play(UtilityEvent('focus_done', Pomodoro.LONG))
        self.assertEqual(spoken, ["Time's up! Your timer is done.",
                                  'Four sessions done. Amazing work! Enjoy a long break.'])
        self.assertEqual(eyes.history, [Expression.EXCITED, Expression.IDLE,
                                        Expression.HAPPY, Expression.IDLE])

    def test_waits_while_listening_and_leaves_conversation_expression(self):
        eyes, spoken = FakeEyes(), []
        fsm = RobotStateMachine()
        with quiet():
            fsm.transition_to(State.LISTENING)
        player = AlertPlayer(eyes, fsm, spoken.append, hold=0.0)

        def finish_turn():
            time.sleep(0.2)
            with quiet():
                fsm.transition_to(State.THINKING)
        threading.Thread(target=finish_turn).start()
        t0 = time.monotonic()
        with quiet():
            player.play(UtilityEvent('break_done'))
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        self.assertEqual(len(spoken), 1)
        self.assertEqual(eyes.history, [])          # mid-conversation: no expression override

    def test_handle_never_blocks_and_speech_failure_is_logged_once(self):
        logs = []

        def broken(text):
            raise RuntimeError('no speaker')
        player = AlertPlayer(None, None, broken, log=logs.append).start()
        try:
            t0 = time.monotonic()
            with quiet():
                for _ in range(3):
                    player.handle(UtilityEvent('timer_done'))
                self.assertLess(time.monotonic() - t0, 0.05)
                deadline = time.monotonic() + 3
                while len(player.played) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
        finally:
            player.stop()
        self.assertEqual(len(player.played), 3)
        self.assertEqual(len(logs), 1)

    def test_alerts_never_overlap_conversation_speech(self):
        active, overlaps = [0], []
        guard = threading.Lock()

        def fake_backend(text, config, on_start=None):
            with guard:
                active[0] += 1
                if active[0] > 1:
                    overlaps.append(text)
            time.sleep(0.05)
            with guard:
                active[0] -= 1
        player = AlertPlayer(None, None, hold=0.0)      # default: the real speak()
        with patch.object(tts, 'resolve_backend', return_value='pyttsx3'), \
                patch.object(tts, '_speak_pyttsx3', fake_backend), quiet():
            talker = threading.Thread(target=lambda: [tts.speak('reply') for _ in range(4)])
            talker.start()
            for _ in range(4):
                player.play(UtilityEvent('timer_done'))
            talker.join()
        self.assertEqual(overlaps, [])


if __name__ == '__main__':
    unittest.main()
