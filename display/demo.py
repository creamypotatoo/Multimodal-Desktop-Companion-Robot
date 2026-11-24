"""Standalone demo for Eli's animated eyes (main.py integration: display/robot_eyes.py).

  Windows preview:   python -m display.demo --backend preview
  Raspberry Pi TFT:  python -m display.demo --backend tft
  Hardware-free:     python -m display.demo --backend mock --cycles 1 --hold 1
  Measure speed:     python -m display.demo --backend tft --benchmark 100

Cycles through every expression, demonstrates blinking and gaze, and prints
measured FPS, render and transfer times. Ctrl+C (or closing the preview
window) stops it cleanly and blanks the display.

Preview keys: 1-9, 0 expressions | b blink | arrows look | c centre/release |
space pause/resume the cycle | Esc/q quit.
"""
import argparse
import random
import sys
import threading
import time

from display.config import AnimationConfig, EyeConfig
from display.expressions import Expression
from display.eyes import BACKENDS, EyeEngine, create_display, resolve_backend
from hardware.tft_display import DisplayUnavailableError, TFTConfig

KEY_EXPRESSIONS = dict(zip('1234567890', Expression))
GAZE_KEYS = {'Left': (-0.8, 0.0), 'Right': (0.8, 0.0), 'Up': (0.0, -0.8), 'Down': (0.0, 0.8)}


def _sleep(stop, seconds, paused=None):
    """Wait in short slices so Ctrl+C is honoured on every platform."""
    end = time.monotonic() + seconds
    while not stop.is_set():
        if paused is not None and paused.is_set():
            end += 0.1
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        stop.wait(min(0.1, remaining))
    return False


def _format_stats(s):
    return (f"[Display] sent {s['sent_fps']:.1f} fps (target {s['target_fps']:g}), "
            f"skipped {s['frames_skipped']}, render {s['render_ms']:.1f} ms "
            f"(max {s['render_ms_max']:.1f}), transfer {s['transfer_ms']:.1f} ms "
            f"(max {s['transfer_ms_max']:.1f}), {s['pixels_per_frame']:.0f} px/frame, "
            f"overruns {s['overruns']}")


def run_cycle(engine, stop, *, hold, cycles, expressions, stats_interval=0.0, paused=None,
              log=print):
    """Show each expression for ``hold`` seconds; ``cycles`` 0 = forever."""
    last_stats = time.monotonic()
    done = 0
    while not stop.is_set() and (cycles == 0 or done < cycles):
        for expression in expressions:
            if stop.is_set():
                return
            engine.release_gaze()
            engine.set_expression(expression)
            log(f'[Display] Expression: {expression.name}')
            script = []
            if expression is Expression.IDLE:
                script = [(0.3, lambda: engine.set_gaze(-0.8, 0.0)),
                          (0.5, lambda: engine.set_gaze(0.8, -0.3)),
                          (0.7, engine.release_gaze)]
            if expression is not Expression.SLEEPING:
                script.append((0.85, engine.blink))
            elapsed = 0.0
            for fraction, action in script + [(1.0, None)]:
                if not _sleep(stop, max(0.0, hold * fraction - elapsed), paused):
                    return
                if not engine.running:
                    log('[Display] Eye engine is no longer running; ending demo.')
                    return
                elapsed = hold * fraction
                if action is not None:
                    action()
                now = time.monotonic()
                if stats_interval and now - last_stats >= stats_interval:
                    log(_format_stats(engine.stats(reset=True)))
                    last_stats = now
        done += 1


def benchmark(display, frames, config, partial=True, log=print):
    """Measure render and transfer separately, without the frame scheduler."""
    from display.animation import EyeAnimator
    from display.renderer import EyeRenderer, union_bounds
    renderer = EyeRenderer(config)
    animator = EyeAnimator(AnimationConfig(), rng=random.Random(1), now=0.0)
    display.open()
    render, full, part, areas = [], [], [], []
    previous = None
    try:
        expressions = list(Expression)
        for i in range(frames):
            if i % 10 == 0:
                animator.set_expression(expressions[(i // 10) % len(expressions)], i * 0.1)
                animator.set_gaze(random.Random(i).uniform(-1, 1), 0.0)
            t0 = time.perf_counter()
            layout = renderer.layout(animator.update(i * 0.1))
            image = renderer.draw(layout)
            t1 = time.perf_counter()
            display.show(image, None)
            t2 = time.perf_counter()
            bounds = renderer.bounds(layout)
            if partial and previous is not None:
                dirty = union_bounds(bounds, previous)
                display.show(image, dirty)
                part.append(time.perf_counter() - t2)
                areas.append((dirty[2] - dirty[0]) * (dirty[3] - dirty[1]))
            previous = bounds
            render.append(t1 - t0)
            full.append(t2 - t1)
    finally:
        display.clear()
        display.close()

    def line(name, values):
        avg = sum(values) / len(values)
        return f'  {name:<16} avg {avg * 1000:6.1f} ms  min {min(values) * 1000:6.1f}  ' \
               f'max {max(values) * 1000:6.1f}'
    log(f'[Benchmark] {frames} frames, {config.frame_width}x{config.frame_height}, '
        f'supersample {config.supersample}')
    log(line('render', render))
    log(line('full transfer', full))
    best = sum(render) / len(render) + sum(full) / len(full)
    log(f'  => full-frame ceiling ~{1 / best:.1f} fps')
    if part:
        log(line('partial transfer', part) + f'  ({sum(areas) / len(areas):.0f} px avg)')
        log(f'  => partial-frame ceiling ~{1 / (sum(render) / len(render) + sum(part) / len(part)):.1f} fps')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', choices=BACKENDS, default='auto',
                        help='auto = ELI_DISPLAY_BACKEND, else tft on Linux, preview elsewhere')
    parser.add_argument('--fps', type=float, default=None, help='target FPS (default ELI_DISPLAY_FPS or 10)')
    parser.add_argument('--hold', type=float, default=5.0, help='seconds per expression')
    parser.add_argument('--cycles', type=int, default=0, help='full cycles to run; 0 = until Ctrl+C')
    parser.add_argument('--expression', help='show only this expression')
    parser.add_argument('--stats', type=float, default=5.0, help='stats interval seconds; 0 = off')
    parser.add_argument('--color', help='eye colour #RRGGBB (default ELI_EYE_COLOR or #00E5FF)')
    parser.add_argument('--supersample', type=int, default=1, help='1-4; smoother edges, more CPU')
    parser.add_argument('--scale', type=int, default=2, help='preview window zoom')
    parser.add_argument('--no-partial', action='store_true', help='TFT: always send full frames')
    parser.add_argument('--spi-hz', type=int, default=None, help='TFT SPI clock (default 16000000)')
    parser.add_argument('--benchmark', type=int, default=0, metavar='FRAMES',
                        help='measure render/transfer for FRAMES frames and exit')
    parser.add_argument('--seed', type=int, default=None, help='random seed for repeatable motion')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        expressions = [Expression.parse(args.expression)] if args.expression else list(Expression)
        if args.hold <= 0 or args.cycles < 0:
            raise ValueError('--hold must be positive and --cycles non-negative')
        overrides = {} if args.color is None else {'color': args.color}
        config = EyeConfig.from_env(supersample=args.supersample, **overrides)
        anim_overrides = {} if args.fps is None else {'fps': args.fps}
        animation = AnimationConfig.from_env(**anim_overrides)
        backend = resolve_backend(args.backend)
        backend_kwargs = {}
        if backend == 'preview':
            backend_kwargs['scale'] = args.scale
        if backend == 'tft':
            tft = {} if args.spi_hz is None else {'baudrate': args.spi_hz}
            if args.no_partial:
                tft['partial_updates'] = False
            backend_kwargs = {'config': TFTConfig.from_env(**tft)}
        display = create_display(backend, **backend_kwargs)
    except ValueError as exc:
        print(f'[Display] Invalid option: {exc}', file=sys.stderr)
        return 2

    name = type(display).__name__
    if args.benchmark:
        try:
            benchmark(display, args.benchmark, config, partial=not args.no_partial)
        except DisplayUnavailableError as exc:
            print(f'[Display unavailable] {exc}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print('[Display] Benchmark interrupted.')
        return 0

    engine = EyeEngine(display, config, animation,
                       rng=random.Random(args.seed) if args.seed is not None else None)
    try:
        engine.start()
    except DisplayUnavailableError as exc:
        print(f'[Display unavailable] {exc}', file=sys.stderr)
        return 1
    partial = getattr(display, 'partial_updates', None)
    print(f'[Display] {name} {config.frame_width}x{config.frame_height} at target '
          f'{animation.fps:g} fps' + ('' if partial is None else f', partial updates {"on" if partial else "off"}')
          + '. Press Ctrl+C to stop.', flush=True)
    stop = threading.Event()
    paused = threading.Event()
    cycle = dict(hold=args.hold, cycles=args.cycles, expressions=expressions,
                 stats_interval=args.stats, paused=paused)
    status = 0
    try:
        if hasattr(display, 'run'):  # desktop preview: Tk owns the main thread
            def director():
                try:
                    run_cycle(engine, stop, **cycle)
                finally:
                    if not stop.is_set():
                        display.request_close()

            def on_key(key):
                if key in KEY_EXPRESSIONS:
                    paused.set()
                    engine.set_expression(KEY_EXPRESSIONS[key])
                    print(f'[Display] Expression: {KEY_EXPRESSIONS[key].name} (cycle paused)')
                elif key == 'b':
                    engine.blink()
                elif key in GAZE_KEYS:
                    engine.set_gaze(*GAZE_KEYS[key])
                elif key == 'c':
                    engine.release_gaze()
                elif key == 'space':
                    paused.clear() if paused.is_set() else paused.set()
                    print('[Display] Cycle ' + ('paused' if paused.is_set() else 'resumed'))

            worker = threading.Thread(target=director, name='eli-eyes-demo', daemon=True)
            worker.start()
            display.run(on_key=on_key)
            stop.set()
            worker.join(2.0)
        else:
            run_cycle(engine, stop, **cycle)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if engine.last_error is not None:
            status = 1
        print(_format_stats(engine.stats()), flush=True)
        engine.stop()
        print('[Display] Eyes stopped.', flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
