# Eli web dashboard

A lightweight, mobile-first control dashboard for Eli, served by Flask.
Robot actions go through one **central controller with mock adapters**.
Productivity utilities use a shared service owned by the controller. The dashboard
never opens GPIO, SPI, the camera, the microphone, motors or the ESP32, and it
does not import or change the AI pipeline in `main.py`. The two run
independently.

Every control shows whether it is **Live** (real data from the host OS),
**Simulated** (accepted and remembered, but nothing physical happens) or
**Unavailable** (not linked yet, so no data is shown).

## Architecture

```
browser (vanilla HTML/CSS/JS, no CDN)
   │  session cookie + X-CSRF-Token
   ▼
web/__init__.py  create_app()  ── security headers, CSRF hook, error handlers
   ├── web/auth.py        /login, /logout, login_required, CSRF, login throttle
   ├── web/pages.py       server-rendered pages (templates/*.html)
   └── web/api.py         /api/v1/* JSON endpoints ──► RobotService facade
                                                        │
controller/robot.py       RobotController ◄─────────────┘
   ├── controller/adapters.py  simulated ports
   └── productivity/          shared utility service
web/services/host_metrics.py  read-only /proc metrics
web/config.py             WebConfig.from_env()
```

| Path | Responsibility |
|------|----------------|
| `web/__init__.py` | Application factory, security headers, JSON error handlers |
| `web/__main__.py` | `python -m web` launcher (debug off, threaded) |
| `web/config.py` | Environment configuration and safety checks |
| `web/auth.py` | Password login, sessions, CSRF, login throttling |
| `web/api.py` | REST routes. They only parse JSON and call the service |
| `web/pages.py` | Page routes and navigation definition |
| `web/responses.py` | `{"ok": ..., "data"/"error": ...}` envelopes |
| `web/services/` | Controller facade and host metrics |
| `controller/` | Typed commands, robot modes, adapters, safety worker and internal events |
| `productivity/` | Hardware-independent timing models, synchronized commands, scheduler and event feed |
| `web/hash_password.py` | Generates `ELI_WEB_PASSWORD_HASH` |
| `web/templates/` | Jinja templates (base layout, login, seven pages) |
| `web/static/css/dashboard.css` | Theme and responsive layout |
| `web/static/js/` | `api.js` client, `shell.js` status polling, `eyes.js` eye preview, `pages/*.js` |

See [CONTROLLER_ARCHITECTURE.md](CONTROLLER_ARCHITECTURE.md) for runtime
ownership, authorization, adapter contracts and future integration.

The eye preview (`static/js/eyes.js`) mirrors the geometry and expression poses
of `display/config.py`, `display/expressions.py` and `display/renderer.py`. The
mock service validates expressions and colours with those same modules.

## Installation

```sh
python -m pip install -r requirements-web.txt   # Flask only
```

No Raspberry Pi packages, OpenCV, audio or display libraries are needed to run
the dashboard.

## Windows: mock-mode launch

From the repository root in PowerShell:

```powershell
python -m pip install -r requirements-web.txt
python -m web --host 127.0.0.1 --port 5000
```

Open <http://localhost:5000/>. With no password configured, a **one-time
password** is printed in the terminal for that run. To use a fixed local
password instead:

```powershell
$env:ELI_WEB_PASSWORD = "choose-a-dev-password"
python -m web
```

Run the tests:

```powershell
python -m unittest discover -s tests -p test_web.py
python -m unittest discover -s tests -p test_productivity.py
python -m unittest discover -s tests -p test_controller.py
python -m unittest discover -s tests
```

## Raspberry Pi launch

```sh
cd ~/eli_robot
. .venv/bin/activate
python -m pip install -r requirements-web.txt
python -m web                 # localhost only: http://localhost:5000/
```

The dashboard is a separate process. `python main.py --headless` keeps working
as before. The two processes currently run independently.

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `ELI_WEB_HOST` | `127.0.0.1` | Bind address. Anything other than `127.0.0.1`, `localhost` or `::1` is treated as LAN access |
| `ELI_WEB_PORT` | `5000` | TCP port (1–65535) |
| `ELI_WEB_PASSWORD_HASH` | — | Werkzeug password hash. **Required for LAN access.** Create one with `python -m web.hash_password` |
| `ELI_WEB_PASSWORD` | — | Plain-text password, **localhost only** (ignored if a hash is set, refused on the LAN) |
| `ELI_WEB_SECRET_KEY` | random per run | Session signing key, ≥ 32 characters. **Required for LAN access.** Without it, logins end when the server restarts |
| `ELI_WEB_COOKIE_SECURE` | off | `1` sends the cookie over HTTPS only. Enable it only behind an HTTPS reverse proxy |
| `ELI_WEB_SESSION_HOURS` | `12` | Session lifetime (1–168) |
| `ELI_WEB_BACKEND` | `mock` | The controller uses mock adapters |

`--host` and `--port` command-line options override the matching variables.
Never commit real values. Keep them in your shell profile or a `.env` file,
which is already gitignored.

## Local network access (Raspberry Pi)

1. Create a password hash and a secret key (the password is not echoed):

   ```sh
   python -m web.hash_password
   ```

2. Store the two printed values in a private file readable only by you:

   ```sh
   cat > ~/.eli_web.env <<'EOF'
   export ELI_WEB_PASSWORD_HASH='scrypt:32768:8:1$...'   # paste yours
   export ELI_WEB_SECRET_KEY='...'                        # paste yours
   export ELI_WEB_HOST=0.0.0.0
   EOF
   chmod 600 ~/.eli_web.env
   ```

   Use single quotes, because the hash contains `$`.

3. Launch, then find the Pi's address:

   ```sh
   . ~/.eli_web.env && python -m web
   hostname -I        # e.g. 192.168.1.42 -> http://192.168.1.42:5000/
   ```

4. If a firewall is enabled, allow the port from your LAN only, for example
   `sudo ufw allow from 192.168.1.0/24 to any port 5000 proto tcp`.

The server refuses to bind to a non-localhost address without
`ELI_WEB_PASSWORD_HASH` and `ELI_WEB_SECRET_KEY`. Traffic is plain HTTP, so use
it only on a trusted home network. **Never port-forward it to the internet.**

## Security summary

- Session login with a hashed password (constant-time check), session ID
  rotated at login, logout via POST.
- Cookies are signed, `HttpOnly` and `SameSite=Strict`, with an optional `Secure` flag.
- CSRF token required on every POST/PUT/PATCH/DELETE, including login and logout.
- Failed logins are throttled: 5 per address per 5 minutes, then HTTP 429.
- Strict input validation: exact JSON keys, typed values, bounded ranges,
  16 KB body limit.
- Headers: a Content-Security-Policy allowing only same-origin resources, no
  framing, `nosniff`, no referrer, and `Permissions-Policy` that denies camera,
  microphone and geolocation. API responses are `no-store`.
- Local redirects only after login (no open redirects).
- No camera frames, audio or face data are served.

## API (`/api/v1`)

All endpoints need a logged-in session. POST, PUT and PATCH need the
`X-CSRF-Token` header (the value of `<meta name="csrf-token">`) and a JSON
body (`Content-Type: application/json`) containing exactly the listed fields.

**Responses**

```json
{ "ok": true,  "data": { ... } }
{ "ok": false, "error": { "code": "validation_error", "message": "volume must be between 0 and 100", "field": "volume" } }
```

| Status | `code` | When |
|--------|--------|------|
| 400 | `bad_request`, `validation_error` | Malformed JSON, missing or unknown field, out-of-range value |
| 401 | `unauthorized` | Not logged in |
| 403 | `csrf_failed` | Missing or wrong CSRF token |
| 404 / 405 | `not_found`, `method_not_allowed` | Unknown endpoint or method |
| 409 | `conflict` | Valid request not allowed in the current mode |
| 413 | `payload_too_large` | Body over 16 KB |
| 415 | `unsupported_media_type` | Body is not JSON |

**Endpoints**

| Method & path | Body | Notes |
|---------------|------|-------|
| `GET /status` | — | Mode, state, expression, volume, movement, capabilities, last 20 commands |
| `GET /mode` · `PUT /mode` | `{"mode": "standby"\|"companion"\|"movement"\|"sleep"}` | Returns canonical mode and, on write, `previous` |
| `GET /expression` · `PUT /expression` | `{"expression": "happy"}` | One of the 10 expressions in `display/expressions.py` |
| `GET /volume` · `PUT /volume` | `{"volume": 0–100}` | Whole number |
| `GET /settings` · `PATCH /settings` | `{"eye_color": "#RRGGBB", "animation_speed": 0.5–2.0}` | Any subset. All-or-nothing validation |
| `GET /movement` | — | Speed, servo angles, permission, safety latches and simulated sensor status |
| `POST /movement/move` | `{"direction": "forward"\|"backward"\|"left"\|"right", "duration_ms"?: 100–5000}` | Simulated pulse (default 500 ms, capped at the two-second lease) |
| `PUT /movement/speed` | `{"speed": 0–100}` | |
| `POST /movement/stop` | `{}` | Always accepted, in every mode |
| `POST /movement/emergency-stop` | `{}` | Stops and latches movement in any mode |
| `POST /movement/reset-safety` | `{}` | Clears latches after a safe sensor check and successful stop |
| `PUT /movement/servo` | `{"servo": "head_pan" (0–180) \| "head_tilt" (45–135), "angle": n}` | Needs Movement mode and a permitted control session |
| `GET /companion` | — | Conversation, face recognition, microphone and AI status |
| `POST /conversation` | `{"action": "start"\|"end"}` | Start needs companion mode |
| `GET /perception` | — | Camera and recognition status (no frames, ever) |
| `GET /diagnostics` | — | Host CPU %, RAM, temperature, uptime; ESP32; sensors |

**Mock mode rules:** `sleep` stops motion and shows `sleeping` eyes, and
expression changes are refused until Eli wakes. Leaving `sleep` returns to
`idle`. Leaving `companion` ends a conversation. Leaving `movement` stops motion.
The startup mode is `standby`. For older clients, mode writes also accept
`focus` for Standby and `manual` for Movement. Reads return canonical names.
Separate logins share state but cannot send competing drive commands during
the same control lease. A command can return 409 for a safety, mode, or lease
conflict and 503 when an adapter fails. `GET /status` includes subsystem
availability, active utilities, recent errors and the last command result.

The browser UI is the intended client, since calls need the session cookie and
CSRF token. `tests/test_web.py` shows every endpoint in use.

## Pages

| Page | Contents |
|------|----------|
| Home | Animated eye preview, active utilities, status, shortcuts, recent simulated commands |
| Companion | Start/end simulated conversation; face recognition, microphone and AI status |
| Movement | D-pad (also arrow keys/WASD, Space = stop), speed, head servos, emergency stop |
| Focus | Shared countdown, stopwatch with lap splits and extrema, configurable Pomodoro |
| Perception | Camera placeholder and recognition status |
| Diagnostics | Host CPU, RAM, temperature, uptime; ESP32 and peripheral status (refreshes every 3 s) |
| Settings | Expression, eye colour, animation speed, operating mode, volume |

Mobile uses a bottom bar (Home, Companion, Movement, Focus, Settings; the
other pages are reachable from Home). At 900 px and wider a sidebar is shown.
Animations respect `prefers-reduced-motion`.

## Productivity utilities

One `UtilityService` instance owns all three timers per server process.
It serializes reads, actions, settings and deadline processing under a
condition lock. Countdown and Pomodoro use monotonic deadlines. The stopwatch
adds time from running intervals and excludes paused time. The background
worker sleeps until the nearest deadline or until a command changes the
schedule. It emits each completion once and anchors automatic Pomodoro phases
to the previous deadline, so late wakeups do not shift the schedule.

`python -m web` starts and stops the controller, which manages both workers.
Embedders using `create_app()` directly should call `startup()` and
`shutdown()` on `app.extensions['eli_controller']` from their server
lifecycle. Use **one process**
with threads. Multiple workers would have independent utility state and event
streams. The browser can refresh, close, or disconnect while the server
continues running. Restarting the process resets all utilities, settings,
lap history and events.

### Utility API

These paths are relative to `/api/v1`. All require login. PUT and POST also
require `X-CSRF-Token` and JSON. Invalid fields return 400, unsupported
utility resources return 404, and invalid state transitions return 409.

| Method and path | Request or response |
|---|---|
| GET `/utilities` | Current snapshots, timer presets, and recent completion events |
| PUT `/utilities/timer/settings` | `{"duration":300}` in seconds, from 1 to 86,399 |
| PUT `/utilities/pomodoro/settings` | Partial settings: `focus` 1–90 min, `short_break` 1–30 min, `long_break` 1–60 min, `long_every` 1–12 sessions, `automatic` boolean |
| POST `/utilities/timer/actions` | `{"action":"start","duration":600}`. Duration is optional on start. Also: pause, resume, reset, cancel |
| POST `/utilities/stopwatch/actions` | start, pause, resume, reset, lap, clear_laps |
| POST `/utilities/pomodoro/actions` | start, pause, resume, reset, skip |
| GET `/utilities/events?after=0` | Stream identifier, latest cursor and retained events with a greater ID |

Settings can be edited while the corresponding utility is idle or completed.
Timer reset keeps its duration. Cancel clears it. Stopwatch retains the
latest 99 lap rows and tracks extrema across all laps since reset or clear.
Pomodoro defaults to 25/5/15 minutes, with a long break after four completed
focus sessions. By default, each new phase waits for Start. Auto-start applies
to both focus and breaks. Skip advances without crediting the phase and reset
clears session totals while keeping settings.

### Notifications and future voice integration

Home and Focus poll every two seconds while visible and refresh on
reconnection and visibility changes. Local interpolation only paints displays.
Completion events (`timer_done`, `focus_done`, `break_done`) carry stream-local
IDs and Pomodoro phase details. The service retains the latest 128 events.
A tab starts at the current cursor on its first sync and remembers its
cursor across dashboard navigation and refreshes. Each tab shows in-page
alerts for newly observed events.

Completion sound is optional and enabled by a click for the current Focus
page. Browser notifications require a secure context (such as localhost),
browser support and permission explicitly requested from Focus. Some mobile
browsers require a service worker for system notifications. Background tabs
can be suspended and deliver alerts on return. Closing the browser stops
browser alerts even though the server timers keep running.

A future controller can use the same `UtilityService.act()`,
`configure()` and `snapshot()` methods directly when hosted with the
service. Its notification consumer can implement against the
`NotificationSource` protocol and track a stream and cursor. Voice parsing,
speaker playback and TTS are future integrations.

## Known limitations

- **Robot commands are simulated.** Mode, expression, volume, eye colour, movement,
  servos and conversation state live only in the dashboard process and reset
  when it restarts. They don't reach the TFT, speaker, motors or `main.py`.
- Face recognition, microphone, AI (Ollama) status, camera, ESP32 and sensors
  are reported as **unavailable**. No data is shown for them.
- Host metrics come from Linux `/proc` and the thermal zone. On Windows they
  show as unavailable, because no extra dependency such as `psutil` is used. The
  first CPU reading after startup is unavailable until a second sample exists.
- Login throttling and sessions are per process. With no
  `ELI_WEB_SECRET_KEY`, sessions end on restart.
- Flask's built-in server is used. It's fine for one operator on a home LAN, but
  it isn't a hardened internet-facing server and has no HTTPS.
- Single shared password. There are no user accounts or roles.
- The eye preview approximates the TFT renderer in a browser canvas. It doesn't
  show frames from the physical display.
