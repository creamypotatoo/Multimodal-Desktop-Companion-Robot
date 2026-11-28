# Raspberry Pi deployment

Run these steps locally on the Pi. Stage with mock motors first. The launcher
uses the existing virtual environment and starts one unified process in Standby.
Camera capture is off. Selecting the ESP32 backend leaves serial disconnected
until an authenticated operator connects it. The launcher does not operate the
touchscreen, change GPIO or I2S wiring, reflash firmware, or install a service.

## Deployment checkpoint

Preliminary checks on the target Pi passed with the motor battery disconnected:

- The ESP32 appeared as a Silicon Labs CP210x USB bridge (USB ID `10c4:ea60`)
  on `/dev/ttyUSB0`. Its persistent path was
  `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`.
- The deployment user already belonged to `dialout`, `audio`, `video`,
  `gpio`, `i2c` and `spi`. The existing virtual environment had PySerial 3.5.
- A Python JSON status request at 115200 baud returned protocol v1 and
  firmware v1.0.0, both commanded motor PWM values at zero,
  `driver_ready: true`, and no reported errors.
- The USB webcam was detected.

The project owner subsequently confirmed the integrated Pi 4 prototype with
physical TFT eyes, camera, face recognition, voice interaction, laptop Ollama,
ESP32 motor control, and private smartphone access through Tailscale HTTPS.
An initial `Malformed ESP32 response` cleared after a temporary three-second
wait and input-buffer flush after opening the serial port. The permanent
implementation makes the wait cancellable and flushes before negotiation.
Recheck it on the Pi with motor power disconnected before resuming drive.
Systemd startup remains a separate, optional check.

## 1. Inventory the existing installation

First run this read-only check on the Pi. Keep the output local.

```sh
cd /home/beshakhadeb/eli_robot
git status --short --branch
git rev-parse --short HEAD
.venv/bin/python --version
cat /etc/os-release
uname -m
```

The unified application requires Python 3.11 or newer. If the existing venv
has an older interpreter, leave it intact and create a separate venv with a
supported interpreter. Set `ELI_PI_PYTHON` to that venv's absolute Python path
before using the launcher. Keep the existing private LBPH model, matching
labels, configuration, backups, and local Python environment.

## 2. Sync after the stabilization commit is available on origin/main

Only fast-forward the existing checkout when it is clean, on main, and its
local files are backed up. A changed tracked labels file counts as local work.
So does the temporary serial initialization fix.

```sh
cd /home/beshakhadeb/eli_robot
git fetch origin main
git log --oneline HEAD..origin/main
if [ "$(git branch --show-current)" = main ] && [ -z "$(git status --porcelain)" ]; then
  git merge --ff-only --no-overwrite-ignore origin/main
fi
```

If the checkout has local edits or the histories diverge, leave it in place
and use a second checkout. Choose a new directory. The existing venv and
private assets can be referenced by absolute path without copying them.

```sh
git worktree add --detach "$HOME/eli_robot-phase8-stabilization" origin/main
cd "$HOME/eli_robot-phase8-stabilization"
export ELI_PI_PYTHON=/home/beshakhadeb/eli_robot/.venv/bin/python
```

If using the second checkout, use that directory for the remaining commands.
Configure `ELI_FACE_MODEL` and `ELI_FACE_LABELS` in the external environment
file to reference the original, matching pair. Never put the private copy of
a label file into a tracked path with different contents.
Keep the existing `~/.config/eli/pi.env` in place. Set
`ELI_FACE_THRESHOLD=70` there only if the file currently overrides the
threshold and you want the new default.

## 3. Check dependencies and hardware access

Select the existing venv Python, or keep the previous override.

```sh
export ELI_PI_PYTHON="${ELI_PI_PYTHON:-$PWD/.venv/bin/python}"
"$ELI_PI_PYTHON" -m pip freeze > "$(mktemp "$HOME/eli-pi-pip-before.XXXXXX")"
"$ELI_PI_PYTHON" -m pip install -r requirements-web.txt -r requirements-display.txt -r requirements-esp32.txt
"$ELI_PI_PYTHON" -m pip check
"$ELI_PI_PYTHON" -c 'import cv2; print(cv2.__version__, hasattr(cv2, "face"))'
"$ELI_PI_PYTHON" -c 'import sounddevice; print(sounddevice.query_devices())'
```

If the existing audio tools are missing, install them with
`sudo apt install libportaudio2 alsa-utils espeak-ng ffmpeg`. Install
`sounddevice` in the selected venv only if missing. If OpenCV is missing, use
the known-working Pi OpenCV installation when possible. The OS
`python3-opencv` package is visible to a compatible system Python or a
separate venv created with `--system-site-packages`. Alternatively use a
compatible OpenCV contrib wheel in the selected venv when available for the
OS, architecture, and Python version. Keep one OpenCV build per environment.
Without the contrib `face` module, face detection and preview still work.
The launcher checks for the existing whisper.cpp CLI and local model; there
is no automatic model download.

```sh
id
ls -l /dev/spidev* /dev/gpiochip* /dev/video* /dev/serial/by-id/ 2>/dev/null
arecord -l
aplay -L
command -v espeak-ng ffmpeg aplay
"$ELI_PI_PYTHON" -m serial.tools.list_ports -v
```

Use the group ownership reported by the device nodes. Typical groups are
`spi` and `gpio` for the TFT, `video` for the webcam, `audio` for ALSA,
and `dialout` for serial. Add only missing groups that exist and own the
required devices, then log out and back in. For example,
`sudo usermod -aG dialout "$USER"` grants serial access after the next login.
Keep the normal user account and avoid root or blanket device permissions.

Keep the verified ILI9341 settings (D22 CS, D24 DC, D25 RESET, SPI0 at
16 MHz), I2S overlay and named ALSA device. Check the existing display and
speaker guides if a standalone test fails. [Display wiring](DISPLAY_PI.md)
[Speaker configuration](SPEAKER_PI.md)

## 4. Configure and launch in stages

```sh
install -d -m 700 "$HOME/.config/eli"
if [ ! -e "$HOME/.config/eli/pi.env" ]; then
  install -m 600 deploy/pi.env.example "$HOME/.config/eli/pi.env"
fi
sh deploy/launch-pi.sh mock --check
sh deploy/launch-pi.sh mock
```

Use another terminal to confirm `curl -I http://127.0.0.1:5000/login`.
For desktop access from the laptop during bench checks, run
`ssh -N -L 5000:127.0.0.1:5000 beshakhadeb@<pi-ssh-name>` and browse to
`http://127.0.0.1:5000/` on the laptop. Use the printed one-time dashboard
password or configured credentials.

Before moving to the next profile, exit the previous one with Ctrl+C. Stop the
legacy launcher, display demos and any tools holding the camera, microphone,
speaker or ESP32. Edit `~/.config/eli/pi.env` as needed. Use literal
`KEY=value` lines without quotes or expansion. For values containing spaces
or dollar signs, retain the characters literally. Existing exported
variables take precedence. Use `--env-file /path/to/file` if needed.

| Profile | Local command | Check |
|---|---|---|
| Display and audio | `sh deploy/launch-pi.sh audio --check`, then `sh deploy/launch-pi.sh audio` | TFT, microphone, whisper.cpp, wake, espeak output and cancellation |
| Camera | `sh deploy/launch-pi.sh camera --check`, then `sh deploy/launch-pi.sh camera` | Camera starts off. In Perception, choose a device and start it explicitly |
| Serial | `sh deploy/launch-pi.sh serial --check`, then `sh deploy/launch-pi.sh serial` | ESP32 starts disconnected. Connect in Standby with motor power off |
| Permanent manual launch | `sh deploy/launch-pi.sh run --check`, then `sh deploy/launch-pi.sh run` | Launch with the accepted settings in the external environment file |

The audio configuration defaults to 16 kHz input, named MAX98357A output,
and 10% output gain as specified in the example. Enter the PortAudio device
name or index in `ELI_AUDIO_INPUT`, or use `default`. Set
`ELI_WHISPER_CPP_CLI` and `ELI_WHISPER_CPP_MODEL` for existing binaries and
models outside the default `~/whisper.cpp` tree. Set the initial gain with
`ELI_TTS_VOLUME` and adjust during operation from the dashboard. The camera
example requests 640 × 480 at 8 FPS, limits JPEG updates to 5 FPS and
face processing to 4 FPS. The last overlay is reused between analyzed frames,
so a change can take up to one analysis interval plus processing time to show.
The camera model and FER+ feature remain optional. [Perception setup](PERCEPTION_INTEGRATION.md)

For serial, select the ESP32's stable path from Settings or set
`ELI_SERIAL_PORT=/dev/serial/by-id/<your-ESP32>` in the local configuration.
The link validates the existing firmware protocol and preserves calibrated
directions. The dashboard still requires Movement mode and a valid control
lease and reports commanded state. STOP, emergency stop, explicit reset,
bounded pulses, heartbeat, firmware watchdog and communication failure
handling remain in place. Obstacle and cliff sensors are unavailable.
Manual floor operation only. [ESP32 operation](ESP32_INTEGRATION.md)

## 5. Connect laptop Ollama privately

Keep Ollama listening on laptop loopback. If previously configured to listen
on the LAN, return it to `127.0.0.1:11434`, restart it, and remove any inbound
firewall exception created for that old setting. Make sure the existing
`qwen3:1.7b` is available on the laptop. Start the following tunnel from
PowerShell, substituting the Pi's SSH hostname or private DNS name:

```powershell
curl.exe http://127.0.0.1:11434/api/version
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:11435:127.0.0.1:11434 beshakhadeb@<pi-ssh-name>
```

The Pi SSH server must permit remote forwarding and keep `GatewayPorts no`
(the default) so the forwarded endpoint remains Pi loopback only. Use a
trusted SSH connection with verified host keys. Keep this terminal open.
On the Pi run:

```sh
curl --max-time 5 http://127.0.0.1:11435/api/version
curl --max-time 5 http://127.0.0.1:11435/api/tags
```

Then set `ELI_OLLAMA_URL=http://127.0.0.1:11435` and
`ELI_OLLAMA_MODEL=qwen3:1.7b` in the environment file and restart Eli.
If the connection fails, check the local Ollama endpoint, the SSH tunnel
terminal, and access to the Pi over the chosen private network. The runtime
uses rule-based responses during an Ollama outage. Privacy-sensitive camera
frames stay on the Pi. Conversation and identity context traverse the tunnel
as text. [OpenSSH client reference](https://man.openbsd.org/ssh)

## 6. Access from the phone over private HTTPS

Join the Pi and phone to the same restricted Tailscale network and allow
only the intended operator to reach the Pi's HTTPS endpoint. Configure
MagicDNS and HTTPS for the tailnet. Keep Flask on loopback and retain Eli's
own password login and CSRF protection.

```sh
"$ELI_PI_PYTHON" -m web.hash_password
```

Copy the generated `ELI_WEB_PASSWORD_HASH` and `ELI_WEB_SECRET_KEY` values
to the private environment file (omit the printed `Suggested ` prefix).
Set `ELI_PI_HTTPS=1`, keep the file mode at 600, and restart the selected
profile. This enables HTTPS-only cookies and requires configured credentials.
From another Pi terminal:

```sh
tailscale status
tailscale serve 5000
```

Open the printed HTTPS URL from the connected phone and sign in to Eli.
For a dashboard port other than 5000, pass the actual port to Serve. Use
private Serve only. If HTTPS works but the login does not persist, confirm
the URL starts with HTTPS and both credentials are set on the Pi. If the
phone cannot connect, check that its tailnet VPN is connected, access policy
allows it, the Serve process is running, and loopback `/login` responds.
Use the protected HTTPS URL even for camera viewing. [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve)

## 7. Hardware acceptance and monitoring

1. Run the mock profile. Verify login, productivity tools, simulated motion,
   and dashboard loading. Start the audio profile and verify that the ILI9341
   shows Idle, Listening, Thinking, Speaking and Sleeping. Verify the USB
   microphone and wake phrase, audible output, interruption and quiet mode.
2. Establish the Ollama tunnel. Verify a real response. Stop the tunnel and
   verify that the fallback replies and controls remain available.
3. Start the camera profile. Verify off-by-default, device discovery, preview
   and authentication. Check face overlays, optional private recognition and
   gaze tracking on the physical TFT. Leave the frame and verify gaze release.
   Stop capture and verify the device is released. Check combined camera,
   wake and speech performance.
4. Start the serial profile with motor power disconnected. Locate the
   ESP32, connect in Standby, and verify protocol v1, heartbeat, zero commanded
   PWM and unavailable sensors. No drive controls should operate in Standby.
5. For local physical motor acceptance, secure the chassis with wheels lifted
   and power disconnect within reach. Follow the staged checks in the ESP32
   guide for manual directional pulses, STOP, emergency stop, reset, mode
   exit and disconnect/reconnection. Never infer physical stopping from
   commanded PWM or an acknowledgment alone.
6. On the phone, verify camera and all readouts after login. Verify that API
   reads reject an unauthenticated session and state changes require CSRF.
   Stop the runtime and verify the camera, microphone, TFT and serial device
   can be opened by their standalone diagnostics after shutdown.

Diagnostics shows Linux CPU use, RAM and temperature. Perception shows
measured JPEG production FPS, last face-processing time and capture-to-JPEG
time. Audio diagnostics shows the latest wake and command recognition latency.
For OS-level checks, use `top`, `free -h` and, when installed,
`vcgencmd measure_temp`. If speech stutters, reduce camera analysis FPS and
TFT FPS before increasing camera resolution.

## 8. Optional automatic startup after acceptance

Install the user unit only after successful foreground acceptance. If using
another checkout or Python environment, edit the installed unit's
`WorkingDirectory`, `ExecStart`, and if needed `Environment=ELI_PI_PYTHON=...`.
Use absolute paths for the Python override in the unit. Keep credentials in
the external environment file.

```sh
install -d -m 700 "$HOME/.config/systemd/user"
install -m 644 deploy/eli.service "$HOME/.config/systemd/user/eli.service"
systemctl --user daemon-reload
systemctl --user start eli.service
systemctl --user status eli.service
journalctl --user -u eli.service -f
```

After verifying systemd startup and clean shutdown, use
`systemctl --user enable eli.service` to start with the user manager. If
startup before login is required and approved on the Pi, enable lingering
for the same account using `sudo loginctl enable-linger "$USER"`.
The unit restarts on process failures with a delay and rate limit. User
configuration errors exit with status 2 and are excluded from automatic
restart. SIGTERM requests runtime shutdown and the unit bounds the wait for
stuck I/O. Check the journal for a shutdown timeout. Keep the serial firmware
watchdog and independent motor power cutoff available.

## Rollback

Use `systemctl --user disable --now eli.service` if enabled. Stop foreground
Serve and the SSH tunnel with Ctrl+C. Preserve the external configuration
and private data. If deployed in the separate checkout, relaunch the prior
application from the original checkout. If the original checkout was
fast-forwarded, create a separate rollback checkout at the recorded prior
commit and use the original venv. For the Phase 7 source, for example:

```sh
git worktree add --detach "$HOME/eli_robot-phase7" 38af0c02951cc8810282a56256eb723d7f5dafc2
cd "$HOME/eli_robot-phase7"
ELI_DISPLAY_BACKEND=tft /home/beshakhadeb/eli_robot/.venv/bin/python -m web --unified --local-io --motors mock --host 127.0.0.1 --port 5000
```

Restore only verified dependency changes if necessary, using the saved freeze
as a reference. Do not remove the original venv or private data. Pi hardware
acceptance of the permanent serial patch, sustained CPU and thermal
performance, physical stopping distances and watchdog timing, false-positive
rates at the new LBPH threshold, and boot-time systemd behavior remain to be
measured or verified locally.
