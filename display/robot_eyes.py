"""Connect the robot state machine to Eli's animated eyes (Phase 2).

``start_robot_eyes()`` creates and starts at most one ``EyeEngine`` from the
existing display environment variables, and returns ``None`` (after one
concise warning) when the display is disabled or unavailable. It never
retries. ``attach(fsm, eyes)`` mirrors every state transition onto the eyes;
updates only change animation state under a lock, so they never block
recording, transcription, Ollama requests or playback.

  ELI_DISPLAY_BACKEND  tft | mock | auto | off  (default: off for main.py)
                       auto = tft on Linux, off elsewhere
  ELI_DISPLAY_FPS      target FPS, e.g. 20 on the Pi or 10   (default: 10)
  plus the TFT/colour variables documented in DISPLAY_PI.md.

The desktop ``preview`` backend needs the main thread for Tk and is therefore
not available from main.py; use ``python -m display.demo`` for previews.
"""
import os
import platform

from behavior.state_machine import State
from display.expressions import Expression

DISABLED = ('', 'off', 'none', 'disabled', '0', 'false')
STATE_EXPRESSIONS = {
    State.IDLE: Expression.IDLE,
    State.LISTENING: Expression.LISTENING,
    State.THINKING: Expression.THINKING,
    State.SPEAKING: Expression.SPEAKING,
}


def _log(message):
    print(f'[Display] {message}', flush=True)


def selected_backend(environ=None):
    """The backend main.py should use, or None when the eyes are off."""
    environ = os.environ if environ is None else environ
    name = environ.get('ELI_DISPLAY_BACKEND', '').strip().lower()
    if name in DISABLED:
        return None
    if name == 'auto':
        return 'tft' if platform.system() == 'Linux' else None
    return name


def create_robot_eyes(backend, environ=None):
    """Build a configured eye engine. The caller owns startup and shutdown."""
    from display.config import AnimationConfig, EyeConfig
    from display.eyes import EyeEngine, create_display

    if backend == 'tft':
        from hardware.tft_display import TFTConfig
        display = create_display(backend, config=TFTConfig.from_env(environ))
    else:
        display = create_display(backend)
    return EyeEngine(display, EyeConfig.from_env(environ), AnimationConfig.from_env(environ))


def start_robot_eyes(environ=None, *, log=_log, engine_factory=None):
    """Start the eyes once; return the running EyeEngine or None.

    Any failure (bad configuration, missing libraries, absent or busy panel)
    is logged once and the robot continues without a display.
    """
    environ = os.environ if environ is None else environ
    try:
        backend = selected_backend(environ)
        if backend is None:
            return None
        if backend not in ('tft', 'mock'):
            raise ValueError(f'ELI_DISPLAY_BACKEND={backend!r} is not supported here; '
                             'use tft, mock or off')
        if engine_factory is None:
            engine = create_robot_eyes(backend, environ)
        else:
            engine = engine_factory(backend)
        engine.start()
    except Exception as exc:
        log(f'Eyes disabled: {exc}')
        return None
    fps = getattr(getattr(engine, 'animation', None), 'fps', None)
    log(f'Eyes running on {backend}' + (f' at target {fps:g} fps.' if fps else '.'))
    return engine


def attach(fsm, eyes, *, log=_log):
    """Show the current state now and follow every later transition."""
    if eyes is None:
        return
    warned = []

    def follow(_old, new):
        expression = STATE_EXPRESSIONS.get(new)
        if expression is None:
            return
        try:
            eyes.set_expression(expression)
        except Exception as exc:
            if not warned:  # one warning, then stay quiet
                warned.append(exc)
                log(f'Eye update failed: {exc}')

    follow(None, fsm.current_state())
    fsm.add_listener(follow)


def stop_robot_eyes(eyes, *, log=_log):
    """Stop the worker, blank the panel and release GPIO; never raises."""
    if eyes is None:
        return
    try:
        eyes.stop()
    except Exception as exc:
        log(f'Eye shutdown failed: {exc}')
