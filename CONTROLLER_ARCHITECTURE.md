# Eli controller architecture

## Ownership and lifecycle

Flask creates one RobotController per application process. The existing
RobotService facade translates dashboard requests and adds host metrics.
The controller owns mode, conversation state, eye expression and appearance,
volume, requested speed, movement, servo positions, safety, availability,
and recent results. It owns the same UtilityService used by the productivity
API. Snapshots are detached from controller state. The existing AI process
and its behavioral state machine run separately.

The launcher calls startup and shutdown explicitly. Startup issues a motor stop,
sets Standby, requests idle eyes and ends conversation. The controller starts
one safety/event worker and starts the utility scheduler once. Shutdown stops
movement, joins both workers and removes subscriptions. A failed stop leaves
movement unconfirmed and latches a safety fault. Mock tests can call tick()
with an injected clock to process deadlines without a worker.

## Modes

| Mode | Entry and allowed behavior |
|---|---|
| Standby | Default. Idle animated eyes, no conversation or movement. |
| Companion | Idle eyes on entry. Conversation may start, with simulated Listening state and response on the eye preview. A future camera adapter may permit recognition in this mode. |
| Movement | Authorized entry. End conversation, set idle eyes, and wait for a separate drive or servo request. |
| Sleep | Stop motors, end conversation, and set sleeping eyes. An authorized mode command wakes the controller. |

Leaving Movement requires a successful motor-stop request before the mode
changes. Leaving Companion ends conversation. Repeating the same mode is
idempotent. Emergency-stop and safety latches survive mode changes. Mode
transitions that encounter an adapter error return a failure and retain the
previous mode. Any stop or conversation end already applied remains in effect.
Productivity timers and their deadlines are independent of all four modes.

## Commands and adapters

Call RobotController.execute(Command(Action, Source, args), Authority).
The Action enum enumerates accepted operations. Each handler checks its
argument names, value bounds and state preconditions. CommandResult provides
success, status, response state, error code, field and message as applicable.
Source describes a dashboard, voice, system or autonomous command. Authority
is a separate, trusted application input. Flask creates it from an
authenticated session and checks CSRF before mutations. Request payloads
cannot supply a source or an authority. Future voice and behavior integrations
must supply their own authorization policy and use typed commands.

The adapter ports cover eye expression and appearance, audio volume,
conversation start/end, timed motor pulses and stop, servo positions and safety
sensors. An adapter returns promptly, raises on failure, and never calls back
into the controller. A sensor port reports unknown for stale or missing
telemetry. Mock ports record a bounded history and supply simulated safe
readings. Unavailable ports fail explicitly. Real adapters must advertise live
availability when connected. Requested runtime values and simulated states
are distinct from confirmed hardware telemetry, which this phase does not
provide. Camera, microphone, physical audio, TFT, AI and ESP32 stay unconnected.

## Movement and safety

A drive request requires Movement mode, authorized ownership, nonzero speed
and safe obstacle/cliff readings. A command accepts 100–5,000 ms and defaults
to 500 ms. The controller caps the effective pulse at the two-second lease.
A successful request acquires or renews the lease for its session. Other
sessions cannot drive, adjust nonzero speed, or set servos while that lease
belongs to someone else. A pulse can expire before the lease. Stop, safety
faults and mode exit release it. Any authenticated session can stop the robot.

The worker samples safety while moving and stops on either deadline. It
stops and latches a fault on cliff, obstacle, unknown required telemetry,
or failed motion. Emergency stop latches from any mode. An authorized reset
requires safe sensor readings and a successful stop. A fresh drive command
is required after reset. The controller reports an unconfirmed stop as
unknown, and keeps motion blocked.

A future physical adapter needs independent firmware safety enforcement:
a hardware-side watchdog, a bounded command lifetime, and interlocks that
stop motors even if the Pi, adapter, or transport fails. Coordinate ESP32
transport, message framing, acknowledgments, watchdog timing, telemetry
freshness and fault/reset semantics with the firmware owner. No protocol is
defined here.

## Events

EventStream provides named pull subscriptions, each with a cursor. Duplicate
names are rejected. A subscriber calls read(name) outside controller work;
consumer code never runs while a command holds the controller lock.
Events cover mode, conversation, expression, volume, movement, safety,
adapter availability and utility completions. The latest 128 events are
retained, with consecutive IDs so readers can detect gaps. Unsubscribe
explicitly or let shutdown clear all subscriptions. Future TTS, eye coordination,
WebSocket and voice integrations can consume the same stream.

## Tests

From the repository root with Python 3.12 and the web requirements installed:

```powershell
python -m unittest discover -s tests -p test_controller.py
python -m unittest discover -s tests -p test_web.py
python -m unittest discover -s tests -p test_productivity.py
python -m unittest discover -s tests
```

Controller tests use fake clocks and mock ports. The full suite's face test
requires the private data/trained_model.yml artifact.
