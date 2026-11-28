# ESP32 integration

## Architecture

The unified and standalone web launchers can select mock or ESP32 motors.
The controller owns mode, requested speed, safety latches, and control
leases. Only the ESP32 worker opens, reads, writes, and closes the serial
port. Flask queues requests through the controller. The USB bridge can be
enumerated without opening it. The application keeps existing eyes, video,
voice, audio, and productivity paths independent of motor selection.

ESP32 selection starts disconnected. Connect is an explicit dashboard
action in Standby. Choosing the mock or ESP32 backend requires Standby and a
disconnected link. This host expects protocol v1 as implemented by firmware
checkpoint 6f60076 in the separate firmware worktree. The approved firmware's
motor inversion is applied on the ESP32. Python sends direction names
unchanged. No flashing is required.

## Protocol and safety

The ESP32 uses LF-terminated ASCII JSON at 115200 baud, 8N1, without flow
control. Each host command gets a unique ID. The worker validates response
shapes, discards other IDs, and uses a bounded read and write timeout.
Connect allows a three-second stabilization interval after opening the port,
then clears serial input and the host parser before sending hello, heartbeat,
and status. Disconnect or runtime shutdown cancels the wait and closes the
port after a best-effort safety stop. Malformed replies after the flush
remain faults. Hello already removes drive.
Only move, heartbeat, and reset_safety carry the negotiated session token.

The host targets 200 ms heartbeats while the session is armed. Status polls
are independent. STOP, ESTOP, and reset invalidate the token. The next
explicit move or reset negotiates a new token as needed. Heartbeats never
extend a movement pulse. After disconnect, detected firmware reset, invalid
response, or failed acknowledgment, the worker discards queued movement
and attempts STOP. It closes the link and requires an explicit reconnect.
The independent firmware watchdog expires after 750 ms without a valid
heartbeat. That watchdog depends on firmware execution and cannot certify
physical motion.

Only an authenticated dashboard control session can request physical motion
in Movement mode with a healthy link and clear safety latches. Control leases
prevent competing sessions from driving. After a fault, reconnect and
explicitly reset safety. After an ordinary STOP, the next directional tap
establishes a fresh firmware session automatically. Servo commands are
unavailable with this backend. Voice, AI, and autonomous commands cannot drive
the ESP32.

**Obstacle and cliff sensors unavailable. Manual floor operation only.**

The speed slider selects a percentage from 0 to 100 and starts at 80% for
ESP32. Zero requests STOP. Each tap requests a 250 ms pulse by default.
The host only accepts 100 to 1,000 ms for configurable physical pulses and
can shorten a queued pulse to preserve its deadline. STOP and ESTOP discard
queued move commands and take precedence over unsent movement. An already
transmitted packet can still complete before the worker sends STOP. Host
mode exit and expired control leases also request STOP. Firmware pulse
expiration and watchdog form the independent fallback.

The UI reports requested speed, firmware commanded state, commanded PWM,
firmware latch, and last matching acknowledgment. It has no measured wheel
speed, position, or confirmation of physical stopping. An unplugged cable,
unacknowledged STOP, or failed UART must be treated as unknown movement.
Use an independent, reachable motor power disconnect. Verify motor rated
voltage, stall current, the battery's capacity under both startup loads,
and TB6612FNG carrier current and thermal limits before high-duty tests.

## Windows with COM9, Whisper Small, and Ollama

Run from the repository root in PowerShell. Start local Ollama in a separate
terminal with `ollama serve` if it is stopped. The `qwen3:1.7b` model and
the local Whisper Small model must already be present. Close Serial Monitor
and other COM9 applications first.

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
& $py -m pip install -r requirements-web.txt -r requirements-display.txt -r requirements-esp32.txt
& $py -m pip install sounddevice faster-whisper pyttsx3
$env:ELI_WHISPER_MODEL = 'D:\Models\faster-whisper-small'
$env:ELI_WHISPER_BEAM_SIZE = '5'
$env:ELI_WHISPER_PROMPT = 'Eli is a personal robot assistant.'
$env:ELI_AUDIO_INPUT = 'default'
$env:ELI_AUDIO_SAMPLE_RATE = '16000'
$env:ELI_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:ELI_OLLAMA_MODEL = 'qwen3:1.7b'
& $py -m web --unified --motors esp32 --serial-port COM9 --serial-baud 115200 --audio windows --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000/` and authenticate. The selected port is filled
in on Settings and Movement. Find serial ports displays the device
descriptions. Explicitly select Connect ESP32 in Standby.
`ELI_SERIAL_PORT` and `ELI_SERIAL_BAUD` can also prefill the port and rate.
`python -m web` still starts with mock motors by default.

## Raspberry Pi

For staged hardware checks and the permanent launcher, see the
[Raspberry Pi deployment guide](RASPBERRY_PI_DEPLOYMENT.md).

Install `requirements-esp32.txt` in the existing Pi environment. Set up the
existing whisper.cpp, TTS, Ollama, and display dependencies as needed. Replace
`/dev/ttyUSB0` with the USB bridge's enumerated device or use its stable
`/dev/serial/by-id/` path. Give the OS user access to the serial device and
close other programs using it.

```bash
python -m pip install -r requirements-web.txt -r requirements-esp32.txt
python -m serial.tools.list_ports
ELI_DISPLAY_BACKEND=tft python -m web --unified --local-io --motors esp32 --serial-port /dev/ttyUSB0 --serial-baud 115200 --audio pi --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

Use your normal local Ollama environment settings. Keep the dashboard on
loopback during bench checks. [Existing dashboard authentication and LAN configuration](WEB_DASHBOARD.md)

## Staged physical verification

Phase 6 manual hardware verification of the simplified motor workflow
was completed successfully on Windows using the ESP32 DOIT DEVKIT V1,
CP210x bridge on COM9 at 115200 baud, firmware 1.0.0 (protocol v1), and
the TB6612FNG with two geared DC motors. The checklist below is for
repeating verification after hardware or configuration changes.

1. With motor power disconnected, plug in USB. Launch the dashboard, sign
   in, locate the CP210x COM9 port, and connect in Standby. In Diagnostics
   verify firmware 1.0.0, protocol v1, heartbeat acknowledgments, zero
   commanded PWM, and unavailable obstacle and cliff sensing.
2. Verify direction controls remain disabled in Standby. Enter Movement and
   verify they enable when the link is healthy and safety latches are clear.
   Set a low speed before connecting motor power.
3. Lift both wheels and secure the chassis. Keep the battery disconnect
   within reach. Connect motor power, then tap each direction once. Compare
   forward, backward and both turns with the calibrated firmware behavior.
   Confirm the pulse ends without holding or releasing a browser control.
4. Request STOP during a pulse. Compare the UI acknowledgment with the wheels
   and commanded PWM. Tap another direction once STOP is acknowledged and
   repeat the cycle. Then test ESTOP and verify it blocks movement until an
   explicit safety reset.
5. With wheels lifted, confirm leaving Movement requests STOP. Disconnect
   USB during a pulse while keeping the ESP32 powered if your setup permits.
   Check the independent watchdog and the dashboard fault indication.
   Reconnect explicitly in Standby and confirm it never resumes motion.
6. Verify voice mode, wake, productivity, and dashboard behavior with motors
   still disarmed. Keep floor movement supervised after successful bench tests.

If no port appears, check the USB data cable, bridge driver and OS device
manager. If the port is busy, close Arduino Serial Monitor or the bench tool.
If negotiation fails, confirm 115200 baud and firmware protocol v1 against
the firmware bench tool with motor power disconnected. For a latched or
unknown state, check Diagnostics and use the physical disconnect as needed.

Automated tests use fake firmware and serial transport. The project owner
confirmed Raspberry Pi 4 integration and manual ESP32 motor control after a
temporary three-second post-open wait and input-buffer flush resolved
`Malformed ESP32 response`. Startup bytes or a partial frame from USB serial
initialization are the likely cause. The original bytes were not captured.
The permanent cancellation and cleanup paths are covered by automated tests.
Confirm the permanent patch on the Pi with motor power disconnected first.
