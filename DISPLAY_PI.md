# Animated eyes on the 2.4" ILI9341 TFT

Eli's eyes are two rounded cyan shapes on a black 320 × 240 landscape screen.
They blink naturally, glance around and move smoothly between ten expressions.
Phase 1 built the standalone engine. Phase 2 connects it to `main.py` and
the conversation pipeline (see [Running with the robot](#running-with-the-robot-phase-2)).

## Running with the robot (Phase 2)

The eyes are **off unless `ELI_DISPLAY_BACKEND` is set**, so existing launch
commands behave exactly as before. On the Pi, inside the project venv:

```sh
cd ~/eli_robot
. .venv/bin/activate
export ELI_DISPLAY_BACKEND=tft      # enable the physical TFT
export ELI_DISPLAY_FPS=20           # 20 for testing; use 10 to save CPU
export ELI_TTS_BACKEND=espeak       # unchanged speaker setup (SPEAKER_PI.md)
export ELI_OLLAMA_URL=http://localhost:11434   # unchanged (OLLAMA_SETUP.md)
python main.py --headless
```

`--headless` only disables desktop (OpenCV) windows; it does not disable the
TFT. The SPI clock stays at the verified 16 MHz and partial updates stay on.

| Robot state | When                                       | Eyes        |
|-------------|--------------------------------------------|-------------|
| IDLE        | Startup, face scanning, session end        | `idle`      |
| LISTENING   | Microphone recording / voice detection     | `listening` |
| THINKING    | Whisper transcription and Ollama reply, TTS synthesis | `thinking` |
| SPEAKING    | From the moment audio playback (`aplay`) starts | `speaking` |

- **Non-blocking:** state changes only update the eye state under a lock.
  Rendering and SPI run on the eyes' own worker thread.
- **Failure isolation:** if the display can't start (missing libraries, panel
  not connected, busy GPIO, bad setting), main.py prints one line,
  `[Display] Eyes disabled: ...`, and carries on without eyes. It doesn't
  retry. If rendering fails later, the worker logs once and stops while the
  conversation continues.
- **Shutdown:** Ctrl+C or the end of a session stops the worker, blanks the
  panel and releases the GPIO pins.

`ELI_DISPLAY_BACKEND` values for `main.py`:

| Value | Meaning |
|-------|---------|
| unset, `off`, `none`, `disabled` | No eyes (default) |
| `tft`  | Physical ILI9341 |
| `mock` | In-memory display; exercises the integration on Windows |
| `auto` | `tft` on Linux, off elsewhere |

`preview` is only for `python -m display.demo`, because Tk needs the main thread.

Windows integration check (no display hardware needed):

```powershell
$env:ELI_DISPLAY_BACKEND = "mock"; python main.py
```

## Architecture

| Responsibility                        | Module                         |
|---------------------------------------|--------------------------------|
| Expression definitions (pure data)    | `display/expressions.py`       |
| Animation state: easing, blinks, gaze | `display/animation.py`         |
| Pillow frame rendering                | `display/renderer.py`          |
| Lifecycle, worker thread, frame skip  | `display/eyes.py` (`EyeEngine`) |
| Style / timing configuration          | `display/config.py`            |
| Physical ILI9341 over SPI             | `hardware/tft_display.py`      |
| In-memory mock display                | `hardware/mock_hardware.py` (`MockDisplay`) |
| Desktop preview window (Tkinter)      | `display/preview.py`           |
| Standalone demo and benchmark         | `display/demo.py`              |
| Robot state → expression (main.py)    | `display/robot_eyes.py`        |

Data flow on each frame (worker thread, `fps` times per second):

```
EyeAnimator.update(now) -> FrameState -> EyeRenderer.layout() -> geometry
   geometry unchanged?  -> skip (no drawing, no SPI)
   otherwise            -> EyeRenderer.draw() -> display.show(image, changed_region)
```

- **Animation** works from monotonic time and doesn't count frames. Pose
  changes use frame-rate-independent exponential smoothing. A lower FPS keeps
  the same motion, just sampled less often.
- **Blinks** close, hold and reopen with eased curves, at a random interval
  set per expression. Some are doubled. Every blink is guaranteed one fully
  closed frame even at low FPS.
- **Gaze** fixates, then makes a quick smoothed saccade to the next point.
  Larger shifts sometimes trigger a blink. `set_gaze` overrides wandering
  until `release_gaze`.
- **Threading**: commands from any thread only update state under a lock and
  return immediately. There is no queue (latest value wins), and one renderer
  buffer is reused. On Linux the worker thread lowers its own priority
  (nice +5), so audio, STT and the camera stay ahead of it.

### Public interface

```python
from display.eyes import EyeEngine, create_display

eyes = EyeEngine(create_display('tft'))   # or 'preview', 'mock', 'auto'
eyes.start()                     # raises DisplayUnavailableError if no panel
eyes.set_expression('listening') # Expression, name, or State.LISTENING
eyes.set_gaze(0.5, -0.2)         # x, y in -1..1 (+x right, +y down); clamped
eyes.release_gaze()              # back to natural wandering
eyes.blink()
print(eyes.stats())              # measured fps, render/transfer ms
eyes.stop()                      # joins worker, blanks screen, frees GPIO
```

`set_expression` accepts any Enum with a matching name. That includes
`behavior.state_machine.State` (IDLE, LISTENING, THINKING, SPEAKING). Unknown expressions and non-finite gaze values
raise `ValueError` and leave the state unchanged.

### Expressions

| Expression | Look |
|------------|------|
| `idle`      | Relaxed; natural blinks; occasional glances |
| `listening` | Slightly enlarged, attentive; rarely wanders |
| `thinking`  | Slight squint; looks up-left / up-right / up |
| `speaking`  | Gentle smile cut; subtle "talking" pulse |
| `happy`     | Curved smiling arcs with a soft bob |
| `excited`   | Enlarged, with twinkling highlights and stars; lively bob |
| `sad`       | Lowered, outer corners droop, looks down; slow blinks |
| `confused`  | Asymmetric eyes; small gaze jitter; double blinks |
| `sleepy`    | Half-closed; lids slowly sag, then jerk back up; slow blinks |
| `sleeping`  | Closed (thin lines) with slow breathing motion |

## Verified wiring (Raspberry Pi 4, SPI0)

| TFT pin | Pi GPIO | Physical pin | Notes |
|---------|---------|--------------|-------|
| VCC     | 3.3 V   | 1 or 17      |       |
| GND     | GND     | 9            |       |
| CS      | GPIO22  | 15           | `board.D22`, software chip select |
| RESET   | GPIO25  | 22           | `board.D25` |
| DC      | GPIO24  | 18           | `board.D24` |
| MOSI    | GPIO10  | 19           | SPI0 MOSI |
| SCK     | GPIO11  | 23           | SPI0 SCLK |
| LED     | 3.3 V   | —            | Always on; brightness not software-controlled |
| MISO    | GPIO9   | 21           | Optional; unused for writes |

Driver `adafruit_rgb_display.ili9341.ILI9341`, `rotation=90`, 16 MHz, 320 × 240 frames.

**Do not use GPIO8 (CE0) as CS.** The Linux spidev driver already owns it, so
requesting it fails with "resource busy". `TFTConfig` rejects `D8`/`CE0`.
None of these pins clash with the MAX98357A I2S amplifier (GPIO18/19/21), and
its configuration is unchanged.

## Dependencies

| Where          | Packages |
|----------------|----------|
| Everywhere     | Pillow (≥ 8.2), numpy |
| Raspberry Pi   | `adafruit-blinka`, `adafruit-circuitpython-rgb-display` (already installed in the Pi venv) |
| Windows preview| Tkinter, which ships with the python.org installer |

`requirements-display.txt` installs the Adafruit packages only on Linux. All
hardware imports are lazy. On Windows, the `display` package and the tests
import and run without Blinka, GPIO or SPI.

## Windows preview

From the project root:

```powershell
python -m display.demo --backend preview
```

A 640 × 480 window (2× zoom) cycles through every expression, 5 s each.
Keys: `1`–`9`, `0` select an expression (pausing the cycle); `b` blinks; the
arrow keys move the gaze; `c` releases the gaze; `space` pauses or resumes the
cycle; `Esc`/`q` or closing the window quits. Other useful options:

```powershell
python -m display.demo --backend preview --expression excited
python -m display.demo --backend preview --fps 20 --hold 3 --scale 3
python -m display.demo --backend mock --cycles 1 --hold 1     # no window at all
```

## Raspberry Pi deployment

Copy the project as usual. Then, inside the existing project venv on the Pi:

```sh
cd ~/eli_robot
. .venv/bin/activate          # your existing venv that already has Blinka
python -c "import board, digitalio, PIL; from adafruit_rgb_display import ili9341; print('ok')"
# only if something is missing:
python -m pip install -r requirements-display.txt

# 1. Hardware-free tests (no GPIO used)
python -m unittest tests.test_display -v

# 2. Measure real render and SPI speed (about 10 s, then blanks the screen)
python -m display.demo --backend tft --benchmark 100

# 3. Standalone demonstration: cycles all expressions until Ctrl+C
python -m display.demo --backend tft

# Variants
python -m display.demo --backend tft --expression happy
python -m display.demo --backend tft --fps 12 --hold 3
python -m display.demo --backend tft --no-partial   # full-frame transfers only
```

The SSH user needs access to `/dev/spidev0.0` and `/dev/gpiochip*`. These are
normally granted through the `spi` and `gpio` groups. The demo prints measured
throughput every 5 s, for example:

```
[Display] sent 9.9 fps (target 10), skipped 3, render 2.1 ms (max 4.0), transfer 31.0 ms (max 48.2), 21000 px/frame, overruns 0
```

Ctrl+C stops the worker, blanks the screen and releases the GPIO pins. The
exit code is 0 after a normal stop or Ctrl+C. It is 1 if the display is
unavailable or failed mid-run, and 2 for an invalid option.

## Configuration

Explicit arguments and command-line flags override environment variables.

| Variable               | Default    | Meaning |
|------------------------|------------|---------|
| `ELI_DISPLAY_BACKEND`  | demo: auto; main.py: off | Demo: `tft`, `preview` or `mock` (auto = tft on Linux, preview elsewhere). main.py: see table above |
| `ELI_DISPLAY_FPS`      | `10`       | Target frame rate, 0 < fps ≤ 60 |
| `ELI_EYE_COLOR`        | `#00E5FF`  | Eye colour |
| `ELI_DISPLAY_SPI_HZ`   | `16000000` | SPI clock |
| `ELI_DISPLAY_CS`       | `D22`      | `board` pin name for CS (never `D8`) |
| `ELI_DISPLAY_DC`       | `D24`      | DC pin |
| `ELI_DISPLAY_RST`      | `D25`      | RESET pin |
| `ELI_DISPLAY_ROTATION` | `90`       | 0/90/180/270 |
| `ELI_DISPLAY_PARTIAL`  | `1`        | `0` always sends full frames |

In code, `display.config.EyeConfig` sets the look: `color`, `background`,
`sparkle_color`, `eye_width`, `eye_height`, `spacing` (gap between eyes),
`corner_radius`, `offset_x`/`offset_y` (position), `max_gaze_x`/`max_gaze_y`
(pixel travel), `closed_thickness`, `margin` and `supersample` (1–4).
`display.config.AnimationConfig` sets the timing: `fps`, `transition_time`,
`gaze_time`, `blink_close`/`blink_hold`/`blink_open`, `blinking`, `auto_gaze`
and `min_sleep`. The expressions themselves are data in `display/expressions.py`.

## Performance

- A full 320 × 240 RGB565 frame is 153,600 bytes. At 16 MHz that takes at least
  ~77 ms on the wire before Python overhead, so full frames top out around
  10–12 FPS.
- To beat that, the engine sends only the rectangle that changed: the union of
  the old and new eye bounds. On Windows the average was ~21,000 px versus
  76,800 for a full frame, about 3.5× less SPI data.
- If the integer geometry hasn't changed (eyes at rest), the frame is skipped
  completely, with no drawing and no SPI.
- Rendering is a few rounded rectangles, polygons and ellipses into one reused
  buffer: about 1 ms per frame on a desktop. Pi numbers still need measuring;
  run step 2 above.
- The worker sleeps on an `Event` between frames and never busy-waits. When a
  frame overruns its slot, it still yields at least `min_sleep` (2 ms) and
  drops behind rather than trying to catch up.
- `supersample=2` smooths edges but roughly quadruples drawing cost. Leave it
  at 1 on the Pi unless the benchmark shows headroom.
- If audio stutters, try `--fps 8` or `ELI_DISPLAY_FPS=8` first.

## Known limitations

- In `main.py`, the eyes show only the four robot states. Other expressions
  (happy, sad, ...) are available through `EyeEngine` but nothing drives them yet.
- **Partial updates need checking on the panel.** They write a rotated
  sub-rectangle at a computed native position. The maths is unit-tested
  against Pillow's own rotation, but not yet on the physical display. If you
  see misplaced fragments or trails, run with `--no-partial` or
  `ELI_DISPLAY_PARTIAL=0` and report it.
- Edges are not anti-aliased at `supersample=1`.
- The backlight is wired to 3.3 V, so brightness and sleep-dimming are not
  software-controlled.
- Only one process can drive the panel at a time. A second instance fails
  with a busy-GPIO `DisplayUnavailableError`.
- If the SPI transfer fails mid-run (for example, wiring is disturbed), the
  worker logs the error and stops. `stop()` still cleans up; restart with
  `start()`.
