"""Interactive Eli OS touchscreen preview: animated eyes + menu + placeholders.

  Windows preview:   python -m display.ui_demo
  Raspberry Pi:      python -m display.ui_demo --backend tft          (XPT2046 touch)
  Headless check:    python -m display.ui_demo --backend mock --tour

The full touch pipeline runs in every mode: a mouse click in the preview
window presses the mock touch panel at the inverse-calibrated raw position,
and the TouchController (80 ms settle, 15 samples, median, debounce) turns
it into one tap for the UIManager, exactly like the XPT2046 on the Pi.

Preview: click = tap. Keys: 1 idle, 2 listening, 3 thinking, 4 speaking
(simulated conversation state) | m menu | h home | Esc/q quit.
"""
import argparse
import sys
import threading
import time

from behavior.state_machine import RobotStateMachine, State
from display.config import AnimationConfig, EyeConfig
from display.eyes import EyeEngine, create_display
from display.robot_eyes import attach
from display.touch import TouchCalibration, TouchController
from display.ui import EYES, MENU, UTILITIES, UIManager
from hardware.tft_display import DisplayUnavailableError, TFTConfig

STATE_KEYS = {'1': State.IDLE, '2': State.LISTENING, '3': State.THINKING, '4': State.SPEAKING}


def _quiet_transition(fsm, state):
    """transition_to() prints every change; keep the demo console readable."""
    old, fsm.state = fsm.state, state
    for callback in list(fsm._listeners):
        try:
            callback(old, state)
        except Exception:
            pass


def _wait(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _center(rect):
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def tour(touch, ui, fsm, processor, log=print):
    """Tap through every menu entry with the mock touch panel. True if all worked."""
    def tap(x, y, expect):
        touch.press_screen(x, y)
        touch.release()
        ok = _wait(lambda: ui.current == expect)
        _wait(lambda: processor.state == 'idle')   # press fully ended before the next tap
        log(f'[Tour] tap ({x:3d},{y:3d}) -> {ui.current:<9} {"ok" if ok else "FAILED (expected " + expect + ")"}')
        return ok

    menu = ui.registry.get(MENU)
    results = [tap(160, 120, MENU)]
    for button in menu.buttons:
        if button.id == 'companion':
            continue
        results.append(tap(*_center(button.rect), button.id))
        screen = ui.registry.get(button.id)
        back = next(b for b in screen.buttons if b.action == 'back')
        results.append(tap(*_center(back.rect), MENU))
    # Conversation keeps changing while a utility screen is open; the screen stays.
    results.append(tap(*_center(menu.buttons[0].rect), UTILITIES[0].id))
    _quiet_transition(fsm, State.THINKING)
    results.append(_wait(lambda: ui.current == UTILITIES[0].id, 0.3))
    home = next(b for b in ui.registry.get(UTILITIES[0].id).buttons if b.action == 'home')
    results.append(tap(*_center(home.rect), EYES))
    results.append(tap(160, 120, MENU))
    companion = next(b for b in menu.buttons if b.id == 'companion')
    results.append(tap(*_center(companion.rect), EYES))
    return all(results)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', choices=('preview', 'mock', 'tft'), default='preview')
    parser.add_argument('--touch', choices=('mock', 'xpt2046'), default=None,
                        help='default: xpt2046 with --backend tft, else mock')
    parser.add_argument('--fps', type=float, default=None, help='target FPS (default ELI_DISPLAY_FPS or 10)')
    parser.add_argument('--scale', type=int, default=2, help='preview window zoom')
    parser.add_argument('--simulate-conversation', action='store_true',
                        help='cycle idle/listening/thinking/speaking every 3 s')
    parser.add_argument('--mute', action='store_true', help='print utility alerts instead of speaking')
    parser.add_argument('--tour', action='store_true',
                        help='tap through every screen automatically and exit (mock touch only)')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    touch_name = args.touch or ('xpt2046' if args.backend == 'tft' else 'mock')
    try:
        if args.tour and touch_name != 'mock':
            raise ValueError('--tour needs the mock touch backend')
        animation = AnimationConfig.from_env(**({} if args.fps is None else {'fps': args.fps}))
        kwargs = {'scale': args.scale} if args.backend == 'preview' else \
            {'config': TFTConfig.from_env()} if args.backend == 'tft' else {}
        display = create_display(args.backend, **kwargs)
        calibration = TouchCalibration.from_env(width=display.width, height=display.height)
    except ValueError as exc:
        print(f'[UI] Invalid option: {exc}', file=sys.stderr)
        return 2

    engine = EyeEngine(display, EyeConfig.from_env(), animation)
    try:
        engine.start()
    except DisplayUnavailableError as exc:
        print(f'[Display unavailable] {exc}', file=sys.stderr)
        return 1
    fsm = RobotStateMachine()
    attach(fsm, engine, log=print)
    from display.ui.alerts import AlertPlayer
    alerts = AlertPlayer(engine, fsm, (lambda text: None) if args.mute else None).start()
    ui = UIManager(size=(display.width, display.height), on_change=engine.request_frame,
                   on_event=alerts.handle)
    if touch_name == 'mock':
        from hardware.mock_hardware import MockTouch
        touch = MockTouch(calibration=calibration)
    else:
        from hardware.touch_xpt2046 import XPT2046Config, XPT2046Touch
        touch = XPT2046Touch(XPT2046Config.from_env(), bus=getattr(display, 'spi_bus', None))

    def on_tap(event):
        hit = ui.handle_touch(event)
        print(f'[Touch] tap ({event.x},{event.y}) raw ({event.raw_x:.0f},{event.raw_y:.0f}) '
              f'-> {hit or "nothing"}; screen {ui.current}', flush=True)

    controller = TouchController(touch, on_tap, calibration)
    status = 0
    stop = threading.Event()
    try:
        try:
            controller.start()
        except Exception as exc:
            print(f'[Touch] Touch disabled: {exc}', file=sys.stderr)
            return 1
        engine.attach_ui(ui)
        print(f'[UI] {type(display).__name__} with {touch_name} touch. Tap the eyes to open the menu.'
              + (' Keys 1-4 set the conversation state; Esc quits.' if args.backend == 'preview' else
                 ' Ctrl+C to stop.'), flush=True)

        def conversation():
            states = list(STATE_KEYS.values())
            i = 0
            while not stop.wait(3.0):
                i = (i + 1) % len(states)
                _quiet_transition(fsm, states[i])
                print(f'[UI] Conversation state: {states[i].name}', flush=True)

        if args.simulate_conversation:
            threading.Thread(target=conversation, name='eli-ui-demo', daemon=True).start()

        if args.tour:
            ok = tour(touch, ui, fsm, controller.processor)
            print(f'[Tour] {"all screens ok" if ok else "FAILED"}', flush=True)
            status = 0 if ok else 1
        elif hasattr(display, 'run'):
            def on_key(key):
                if key in STATE_KEYS:
                    _quiet_transition(fsm, STATE_KEYS[key])
                    print(f'[UI] Conversation state: {STATE_KEYS[key].name}', flush=True)
                elif key == 'm':
                    ui.open(MENU)
                elif key == 'h':
                    ui.home()

            def on_mouse(kind, x, y):
                if kind == 'press':
                    touch.press_screen(x, y)
                else:
                    touch.release()

            display.run(on_key=on_key, on_mouse=on_mouse)
        else:
            while engine.running and not stop.wait(0.5):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        controller.stop()        # before the display: touch may share its SPI bus
        alerts.stop()
        engine.stop()
        print('[UI] Stopped.', flush=True)
    if engine.last_error is not None:
        status = 1
    return status


if __name__ == '__main__':
    raise SystemExit(main())
