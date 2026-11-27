# Eli web dashboard (Phase 1: mock mode)

A lightweight, mobile-first control dashboard for Eli, served by Flask. In
Phase 1 every robot action goes to an **in-memory mock robot**. The dashboard
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
   └── web/api.py         /api/v1/* JSON endpoints ──► RobotService
                                                         │
web/services/base.py      RobotService (abstract contract)
web/services/mock_robot.py  MockRobotService (Phase 1)  ──► display.expressions,
web/services/host_metrics.py  read-only /proc metrics      display.config (pure data)
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
| `web/services/` | `RobotService` contract, mock implementation and host metrics |
| `web/hash_password.py` | Generates `ELI_WEB_PASSWORD_HASH` |
| `web/templates/` | Jinja templates (base layout, login, seven pages) |
| `web/static/css/dashboard.css` | Theme and responsive layout |
| `web/static/js/` | `api.js` client, `shell.js` status polling, `eyes.js` eye preview, `pages/*.js` |

**Connecting the real robot later:** implement `RobotService`
(`web/services/base.py`) with the same method names and return shapes, add it
to `create_service()`, and allow its name in `web/config.py:BACKENDS`. Routes
and the frontend don't change. Raise `ValidationError` (400) for bad input and
`ConflictError` (409) for valid requests the robot can't perform right now.

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
python -m web
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
python -m unittest tests.test_web -v
```

## Raspberry Pi launch

```sh
cd ~/eli_robot
. .venv/bin/activate
python -m pip install -r requirements-web.txt
python -m web                 # localhost only: http://localhost:5000/
```

The dashboard is a separate process. `python main.py --headless` keeps working
as before and isn't affected. In Phase 1 the two processes don't communicate.

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
| `ELI_WEB_BACKEND` | `mock` | Robot service. Only `mock` exists in Phase 1 |

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
| `GET /mode` · `PUT /mode` | `{"mode": "companion"\|"focus"\|"manual"\|"sleep"}` | Returns `previous` |
| `GET /expression` · `PUT /expression` | `{"expression": "happy"}` | One of the 10 expressions in `display/expressions.py` |
| `GET /volume` · `PUT /volume` | `{"volume": 0–100}` | Whole number |
| `GET /settings` · `PATCH /settings` | `{"eye_color": "#RRGGBB", "animation_speed": 0.5–2.0}` | Any subset. All-or-nothing validation |
| `GET /movement` | — | Speed, servo angles, `moving`, `enabled` (true in manual mode) |
| `POST /movement/move` | `{"direction": "forward"\|"backward"\|"left"\|"right", "duration_ms"?: 100–5000}` | Simulated pulse (default 500 ms). Needs manual mode and speed > 0 |
| `PUT /movement/speed` | `{"speed": 0–100}` | |
| `POST /movement/stop` | `{}` | Always accepted, in every mode |
| `PUT /movement/servo` | `{"servo": "head_pan" (0–180) \| "head_tilt" (45–135), "angle": n}` | Needs manual mode |
| `GET /companion` | — | Conversation, face recognition, microphone and AI status |
| `POST /conversation` | `{"action": "start"\|"end"}` | Start needs companion mode |
| `GET /perception` | — | Camera and recognition status (no frames, ever) |
| `GET /diagnostics` | — | Host CPU %, RAM, temperature, uptime; ESP32; sensors |

**Mock mode rules:** `sleep` stops motion and shows `sleeping` eyes, and
expression changes are refused until Eli wakes. Leaving `sleep` returns to
`idle`. Leaving `companion` ends a conversation. Leaving `manual` stops motion.

The browser UI is the intended client, since calls need the session cookie and
CSRF token. `tests/test_web.py` shows every endpoint in use.

## Pages

| Page | Contents |
|------|----------|
| Home | Animated eye preview, status, shortcuts, recent simulated commands |
| Companion | Start/end simulated conversation; face recognition, microphone and AI status |
| Movement | D-pad (also arrow keys/WASD, Space = stop), speed, head servos, emergency stop |
| Focus | Countdown timer, stopwatch with laps, Pomodoro (25/5/15). Runs in the browser |
| Perception | Camera placeholder and recognition status |
| Diagnostics | Host CPU, RAM, temperature, uptime; ESP32 and peripheral status (refreshes every 3 s) |
| Settings | Expression, eye colour, animation speed, operating mode, volume |

Mobile uses a bottom bar (Home, Companion, Movement, Focus, Settings; the
other pages are reachable from Home). At 900 px and wider a sidebar is shown.
Animations respect `prefers-reduced-motion`.

## Known limitations

- **Everything is simulated.** Mode, expression, volume, eye colour, movement,
  servos and conversation state live only in the dashboard process and reset
  when it restarts. They don't reach the TFT, speaker, motors or `main.py`.
- Face recognition, microphone, AI (Ollama) status, camera, ESP32 and sensors
  are reported as **unavailable**. No data is shown for them.
- Host metrics come from Linux `/proc` and the thermal zone. On Windows they
  show as unavailable, because no extra dependency such as `psutil` is used. The
  first CPU reading after startup is unavailable until a second sample exists.
- Focus timers run in the browser tab and don't sync between devices.
- Login throttling and sessions are per process. With no
  `ELI_WEB_SECRET_KEY`, sessions end on restart.
- Flask's built-in server is used. It's fine for one operator on a home LAN, but
  it isn't a hardened internet-facing server and has no HTTPS.
- Single shared password. There are no user accounts or roles.
- The eye preview approximates the TFT renderer in a browser canvas. It doesn't
  show frames from the physical display.
