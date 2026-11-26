"""Audible + expressive alerts for utility completions (Eli OS Phase 2).

``AlertPlayer.handle(event)`` only queues the alert and returns, so the
display worker never blocks. One worker thread then:

  1. waits (bounded) while the conversation is LISTENING, so an alert is not
     recorded into the user's turn;
  2. briefly shows an expression on the eyes when the robot is idle;
  3. speaks through ``audio.text_to_speech.speak``, whose global speech lock
     already serialises it with conversation TTS (no overlapping audio);
  4. restores the expression that matches the conversation state, unless the
     conversation changed it in the meantime.
"""
import queue
import threading
import time

from behavior.state_machine import State
from display.expressions import Expression

MESSAGES = {
    'timer_done': ("Time's up! Your timer is done.", Expression.EXCITED),
    'focus_done': ('Great focus! Time for a short break.', Expression.HAPPY),
    'long_break': ('Four sessions done. Amazing work! Enjoy a long break.', Expression.HAPPY),
    'break_done': ("Break's over. Ready to focus again?", Expression.LISTENING),
}


def describe(event):
    """(spoken text, expression) for a UtilityEvent."""
    key = 'long_break' if event.kind == 'focus_done' and event.detail == 'long_break' else event.kind
    return MESSAGES[key]


class AlertPlayer:
    def __init__(self, eyes=None, fsm=None, speak=None, *, hold=2.5, max_wait=20.0,
                 log=print, clock=time.monotonic):
        self.eyes, self.fsm = eyes, fsm
        self._speak = speak
        self.hold, self.max_wait = hold, max_wait
        self._log, self._clock = log, clock
        self._queue = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._thread = None
        self._speak_failed = False
        self.played = []

    def start(self):
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='eli-alerts', daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def handle(self, event):
        """UIManager on_event callback: never blocks."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._log('[Alert] Alert queue full; dropping an alert')

    def _state(self):
        return self.fsm.current_state() if self.fsm is not None else State.IDLE

    def _run(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self.play(event)

    def play(self, event):
        text, expression = describe(event)
        deadline = self._clock() + self.max_wait
        while self._state() is State.LISTENING and self._clock() < deadline \
                and not self._stop.wait(0.1):
            pass
        shown = None
        if self.eyes is not None and self._state() is State.IDLE:
            try:
                self.eyes.set_expression(expression)
                shown = expression
            except Exception:
                pass
        started = self._clock()
        self._say(text)
        self.played.append(event)
        if shown is not None:
            self._stop.wait(max(0.0, self.hold - (self._clock() - started)))
            self._restore(shown)

    def _say(self, text):
        print(f'[Alert] {text}', flush=True)
        speak = self._speak
        if speak is None:
            from audio.text_to_speech import speak  # lazy: keeps the display package light
        try:
            speak(text)
        except Exception as exc:
            if not self._speak_failed:       # log once, keep alerting visually
                self._speak_failed = True
                self._log(f'[Alert] Speech unavailable: {exc}')

    def _restore(self, shown):
        from display.robot_eyes import STATE_EXPRESSIONS
        try:
            if self.eyes.expression is shown:   # the conversation did not take over
                self.eyes.set_expression(STATE_EXPRESSIONS.get(self._state(), Expression.IDLE))
        except Exception:
            pass
