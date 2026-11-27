# Eli USB motor protocol v1

Firmware version is `1.0.0`. USB UART uses 115200 baud, 8 data bits, no
parity, one stop bit and no flow control. Send one ASCII JSON object per
line terminated by LF. CRLF also works. A request has at most 192 bytes
before LF, including a trailing CR. Start-to-end frame processing is bounded
to 100 ms from the first received payload byte. An overlong line, embedded
NUL or timed-out line is discarded through the next LF. Bootloader text can
appear before the sketch starts.

Requests use flat objects with unique known keys and no extra fields. Keys
and string values use literal lower-case ASCII letters, digits and underscores.
Numeric tokens are unsigned base-10 integers with no leading zero except
zero itself. Every request includes `id` (1–2,147,483,647) and `cmd`. Fields
can appear in any order. Match responses by ID because safety messages may
be processed before a deferred movement is applied.

## Commands

| Command | Additional fields | Behavior |
|---|---|---|
| `hello` | none | Stops, invalidates the old session, returns the protocol version and new session |
| `heartbeat` | `session` | Refreshes only the communication deadline |
| `move` | `session`, `direction`, `speed`, `duration_ms` | Starts or replaces one bounded pulse if armed and safe |
| `stop` | none | Disables STBY and invalidates the session in any state |
| `estop` | none | Same stop and session invalidation, plus a latch |
| `reset_safety` | `session` | Clears the emergency-stop latch after handshake and heartbeat; invalidates the session and leaves STBY low |
| `status` | none | Returns runtime and safety details in any state |

The session is a 16-character lower-case hexadecimal challenge. It prevents
accidental replay of buffered commands across sessions and does not
authenticate the serial host. A successful `hello` response looks like this:

```json
{"id":1,"ok":true,"state":"stopped","protocol_version":1,"firmware_version":"1.0.0","session":"0123456789abcdef"}
```

Use the actual returned session in subsequent requests:

```json
{"id":2,"cmd":"heartbeat","session":"0123456789abcdef"}
{"id":3,"cmd":"move","session":"0123456789abcdef","direction":"forward","speed":40,"duration_ms":500}
{"id":4,"cmd":"stop"}
```

The host must negotiate a fresh session and heartbeat after startup, STOP,
ESTOP, reset, malformed-frame shutdown or watchdog expiry. A handshake or
heartbeat never starts motion, extends a pulse or clears ESTOP. The watchdog
expires 750 ms after hello or the last valid heartbeat. Send heartbeats at
200 ms intervals, including when an armed connection is idle. Ignore late
replies from older sessions. Sequence IDs strictly increase within a session
for heartbeat, move and reset commands. Replayed or out-of-order IDs get
`duplicate_id` and never refresh a heartbeat or repeat a move. A correctly
addressed new ID is consumed even if safety or parameter checks fail. Status
does not affect sequencing. Repeating a hello ID while that hello's session
is valid returns `duplicate_id`; use a new ID for a deliberate re-handshake.
STOP and ESTOP are processed even if their IDs have previously appeared.

Speed accepts integer percentages from 1 to 100 with the default configuration.
`MAX_SPEED` can be lowered at build time. Use STOP for zero drive. Duration is
1 to 1,000 ms and includes any reversal dead time. Duty is rounded from
`speed * 255 / 100`, giving 102 at 40%, 153 at 60%, 204 at 80%, and 255 at 100%.
Before high-duty testing, verify motor voltage and stall current, the battery's
ability to supply both motors, and the TB6612FNG carrier's current and thermal
limits. See the [bench guide](README.md#bench-procedure).

Forward is (+,+), backward (−,−), left (−,+), right
(+,−) in left/right order. Reversal disables STBY before either direction
changes and keeps drive disabled for at least 20 ms from the last disable.
Duration expiry disables STBY and leaves the session usable while heartbeats
are fresh. Use one outstanding move at a time and rely on a fresh operator
command to move after any session break.

The loop defers a parsed move until pending RX data has been processed, so
a complete queued stop or emergency stop cancels it before drive is applied.
A safety fence invalidates the token for queued traffic. Pending movement
cannot wait indefinitely for an idle RX queue or a reply slot. The finite
TX queue may drop replies under backpressure without delaying safety
processing. After an acknowledgment timeout, treat movement outcome as
unknown, issue STOP and query status. Never replay a move automatically.

## Replies and status

A normal acknowledgment includes `id`, `ok` and `state`. State is
`stopped`, `moving`, `reversing`, `estopped` or `fault`. A rejected command
adds an `error` string and sets `ok:false`. If no usable ID was parsed, the
error reply uses `id:null`. Replies are solicited only. Poll status at a
host-selected rate and keep heartbeats independent of polling.

```json
{"id":6,"ok":true,"state":"stopped","protocol_version":1,"firmware_version":"1.0.0","uptime_ms":2398,"communication":"disconnected","direction":"none","left_pwm":0,"right_pwm":0,"remaining_ms":0,"estop":false,"watchdog":"disarmed","driver_ready":true,"last_error":null,"sensors":{"cliff":"unavailable","obstacle":"unavailable"}}
```

`communication` is `disconnected`, `awaiting_heartbeat`, or `ready`.
`watchdog` is `armed`, `disarmed`, or `tripped`. A fresh heartbeat clears
the tripped indicator. `direction` is the command target or `none`.
PWM values are the commanded duties currently applied by firmware, with zero
during stop and reversal. `remaining_ms` and `uptime_ms` use rollover-safe
millis accounting. `last_error` is a diagnostic code or null and persists
until a later error or successful safety reset. A PWM fault stays unsafe
until reboot. Both sensors are always unavailable. These fields provide no
measured speed, wheel movement, obstacle/cliff reading or position.

| Error | Meaning |
|---|---|
| `malformed`, `line_too_long`, `frame_timeout` | Invalid frame; stops and invalidates the session |
| `invalid_fields` | Field set or session format is wrong; malformed hello/status halt, and a recognizable stop still disables drive |
| `unsupported_command` | Unknown command name |
| `session_invalid` | Missing current session, wrong token or request cancelled by a safety fence |
| `heartbeat_required` | A fresh session heartbeat is required for move/reset |
| `duplicate_id` | Duplicate or out-of-order session command, or repeated active hello |
| `estop_latched` | Movement is disabled until an explicit reset |
| `invalid_direction`, `invalid_speed`, `invalid_duration` | A motion parameter exceeds the supported values |
| `rx_busy`, `rx_backlog` | Another move is queued, or the pending request has waited 100 ms; backlog halts and invalidates the session |
| `pwm_fault` | PWM failed to initialize or write; STBY is disabled |
| `watchdog_timeout` | Status diagnostic after the communication deadline expires |

The Pi integration should check the protocol version, retain only the current
session, keep its own independent control deadlines and use STOP on loss of
operator control. Firmware timing still depends on the processor running.
A physical motor power disconnect remains required.
