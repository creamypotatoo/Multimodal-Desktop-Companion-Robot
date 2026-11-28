# Unified runtime

## Launch on Windows

Run from the repository root in PowerShell.

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
& $py -m pip install -r requirements-web.txt -r requirements-display.txt
& $py -m web --unified --announce --host 127.0.0.1 --port 5000
```

Sign in at http://127.0.0.1:5000/ with the password printed at startup or your
configured dashboard credentials. The mock runtime uses the existing animated
EyeEngine with an off-screen MockDisplay and fake recording, transcription,
AI and speech. The dashboard has an eye preview. Mock speech does not play
through your speakers. `python -m web` still launches the standalone dashboard.
`python main.py --headless` and `--vision-only` retain the legacy behavior.

## Ownership and modes

The runtime injects exactly one controller into Flask. The controller owns
operating mode, behavior snapshots, volume, servo and motor requests, and
the existing productivity scheduler. Mock motors are the default. The ESP32
backend starts disconnected and requires an operator to connect. Authentication, CSRF, and LAN
binding requirements are shared with the standalone dashboard.
See [dashboard configuration](WEB_DASHBOARD.md).

The conversation worker serializes recording, transcription, AI, speech and
utility announcements. The shared camera has its own worker and capture starts
only when explicitly requested. The conversation worker snapshots perception
state at session start. An eye worker sends expressions and gaze to one
EyeEngine. The runtime acquires process-wide ownership on startup and releases
it after its workers and hardware resources stop. Stop the legacy hardware
application before launching local I/O.

| Mode | Unified behavior |
|---|---|
| Standby | Idle eyes, no conversation, dashboard and utilities available |
| Companion | Start and end conversations from the dashboard |
| Movement | Ends conversation and applies the controller's movement permissions, lease, timeout and safety rules |
| Sleep | Ends conversation, stops motion, displays sleeping eyes, and keeps utilities available |

## Interaction and audio

A conversation generation belongs to the controller. Its worker runs the
existing ConversationSession and behavioral state machine. Integrated state
reports Idle, Listening, Thinking and Speaking from that session. The worker
publishes behavioral updates only for the active generation. Ending a session
or changing mode invalidates the generation, so late recording, STT and AI
results cannot become a new reply. Playback checks again after synthesis.
The Phase 5 real-audio backends isolate playback in a child process and cancel
it on a mode change, interaction cancellation or Stop speaking. A new session
waits for prior I/O to drain. See [Voice and audio](VOICE_AUDIO.md) for backend
selection, wake controls and the half-duplex policy.

Expression priority is Sleep, latched movement safety, a three-second
operator expression, then the current behavioral expression. A mode change
clears the temporary expression. The dashboard's color and animation speed
settings still control the browser preview. Local display appearance uses
the existing display configuration.

Volume is a validated 0 to 100 setting. It applies to subsequent output in
the integrated backend, with zero muting playback. The local path preserves
the TTS backend and device environment variables and initializes the whole-
percent level from ELI_TTS_VOLUME. A change does not alter audio already
playing. Diagnostics report last observed availability and failures. An
unavailable Ollama server uses the existing fallback when configured.
No reply, face identity, or recording is exposed by the status API.

`--announce` enables timer and Pomodoro announcements. Each completion is
consumed at most once from the existing controller event stream. While a
session is active, events wait in a queue of at most 16 and expire after
20 seconds. The worker drops announcements in Movement, on safety faults,
or in Sleep unless `--announce-in-sleep` is set. Browser alerts keep working
independently. Muting audio also mutes voice announcements.

## Shutdown and local Raspberry Pi setup

Ctrl+C shuts down Flask, invalidates conversational work, stops movement,
and stops the scheduler. Runtime worker joins are bounded. An in-flight
local backend call may take longer to return. If so, cleanup retains resource
ownership until I/O drains. An incomplete display close retains ownership
and can be retried through the runtime lifecycle.

For future Pi validation, first set up the existing Whisper/VAD, TTS, Ollama,
and TFT dependencies and environment as documented for the legacy app.
Activate its Python environment and use this opt-in command from the repo:

```bash
ELI_DISPLAY_BACKEND=tft python -m web --unified --local-io --announce --host 127.0.0.1 --port 5000
```

Add `--local-camera` to enable the shared camera controls on Perception.
Capture starts when an operator explicitly turns it on. New conversations
can use the latest face-detection and identity state. See
[Perception integration](PERCEPTION_INTEGRATION.md) for camera and model setup
and the completed Windows manual checks.
Local I/O has yet to be verified on the Pi. Use the mock launch for review.

Phase 5 adds wake initiation and typed voice routing through the same
controller. Phase 6 can replace the mock motor port with a negotiated
transport and independently enforced hardware safety.

## Verify

1. On Settings, select Companion and adjust volume. Open Companion and start
   a session. Check conversation state and eye changes.
2. Start a short timer in Focus. Verify the browser completion alert.
3. Switch to Sleep during conversation. Check that the dashboard remains
   accessible and the eyes stay sleeping. Timers still progress.
4. On Diagnostics, check runtime health, fake AI status, and mock motor status.
5. Stop with Ctrl+C.

```powershell
& $py -m unittest discover -s tests -p test_runtime.py
& $py -m unittest discover -s tests
```

The full suite runs without private models. Runtime tests use injected fake
components and the LBPH test trains on a synthetic crop.
