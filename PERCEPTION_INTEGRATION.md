# Perception integration

## Architecture

The unified runtime owns one OpenCV camera service. Starting capture on
Perception opens the selected device in a worker. The worker detects faces,
optionally predicts identity, and builds an annotated JPEG in memory. Signed-in
clients poll for the latest JPEG. Viewers and conversation workers share the
same capture and detection results. Stop invalidates the preview and removes
perception state immediately, then the worker releases the device when its
current driver call returns. The legacy launcher and standalone camera tools
still operate independently. Close them before using the dashboard camera.

The service confirms one overlapping face with the same LBPH result across
three analyzed frames. No face, multiple faces, or an expired sample clears
the selected identity and gaze. Each new conversation takes a snapshot of the
face-present and identity state. It keeps that context for the session and
does not greet or interrupt based on subsequent detections. The EyeEngine and
Perception eye preview use smoothed gaze in Idle and Listening. Thinking,
Speaking, and Sleeping keep their own gaze and expression behavior. The camera
has no motor-control path.

## Configuration and model files

Start the server with `--unified --local-camera`. Capture is off at launch.
Use **Find cameras** while capture is off, select an index on Windows or a
`/dev/videoN` device on Linux, then enter width, height, and FPS and start.
Windows discovery checks DirectShow indices 0–4. Enter a higher index manually
if needed. Linux discovery lists device nodes, some of which may not support
video capture. The worker verifies a real frame on start. Default request is
640 × 480 at 15 FPS. The dashboard reports actual frame dimensions and driver
reported FPS. The processing and preview rate is capped at 8 FPS and the JPEG
long edge at 960 pixels. Start with 640 × 480 and 5–8 FPS on a Pi if CPU use
affects audio. Held frames expire after two seconds if capture stalls.

Detection uses OpenCV's frontal-face Haar cascade. LBPH via OpenCV contrib uses
the existing `data/trained_model.yml` and matching `data/labels.txt` with one
label per line in training ID order. Set `--face-model` and `--face-labels` to
use another local pair. No assets are downloaded. Missing, incompatible or
unreadable files leave detection and preview available. The default
`--face-threshold 70` is an exclusive maximum LBPH distance. A higher
threshold tolerates more variation and may increase false-positive matches.
Distance is a raw comparison score and carries no probability
interpretation. Faces smaller than 80 pixels on either side are detected but
do not receive an LBPH prediction.

An existing local FER+ model can be enabled with
`--expression-model data/models/emotion-ferplus-8.onnx` if the environment
already has ONNX Runtime. Its output is an experimental facial-expression
estimate shown in Perception. It does not infer a person's internal state and
is excluded from the conversation prompt. Missing models or dependencies leave
the feature disabled.

## Windows launch with simulated motors

Run from the repository in PowerShell. Use a Python environment with the web
and display requirements, OpenCV contrib, sounddevice, faster-whisper and
pyttsx3. Use only one OpenCV wheel in a pip environment. Start local Ollama
with `ollama serve` if needed and use an already installed `qwen3:1.7b`.

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
& $py -m pip install -r requirements-web.txt -r requirements-display.txt
& $py -m pip install opencv-contrib-python sounddevice faster-whisper pyttsx3
$env:ELI_WHISPER_MODEL = 'D:\Models\faster-whisper-small'
$env:ELI_WHISPER_BEAM_SIZE = '5'
$env:ELI_WHISPER_PROMPT = 'Eli is a personal robot assistant.'
$env:ELI_AUDIO_INPUT = 'default'
$env:ELI_AUDIO_SAMPLE_RATE = '16000'
$env:ELI_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:ELI_OLLAMA_MODEL = 'qwen3:1.7b'
& $py -m web --unified --local-camera --motors mock --audio windows --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000/` and sign in. The browser shows animated eyes.
This launch uses simulated motors and does not open a serial connection.

## Raspberry Pi launch

Use the existing [Pi camera setup](CAMERA_PI.md) and
[unified runtime setup](UNIFIED_RUNTIME.md). Verify that the user can access
the chosen Video4Linux device, and that the existing Python environment has
OpenCV and optionally its contrib face module. For the configured TFT and
local Pi audio, run:

```bash
ELI_DISPLAY_BACKEND=tft python -m web --unified --local-io --local-camera --motors mock --audio pi --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

## Privacy and manual verification

Frames and recognition state stay in memory. The preview is authenticated and
served with `Cache-Control: no-store`. All signed-in dashboard users can view
the preview while capture is active. Closing a tab pauses that viewer; use
**Stop camera** to turn off capture for everyone. Session-start face presence,
identity status, and a confirmed name when available may be sent to the
configured Ollama endpoint as text. Frames and crops are never sent.

### Windows verification results

All eight manual checks passed on Windows:

1. Camera discovery and live preview.
2. Face detection.
3. Registered identity recognition using the restored original LBPH model
   and its matching labels.
4. Animated eye tracking.
5. Identity expiration when the face left the frame.
6. Wake-phrase activation and conversation while the camera was running.
7. Identity-aware conversation.
8. Repeated camera start and stop.

Keep private models, backups, and enrollment data local. For any private label
file that differs from the existing tracked label file, use `--face-labels`
with a local path outside the repository.

### Verification checklist

1. Sign in and confirm Perception starts with the camera off. Discover a
   device, select it, start the camera, and check the preview and negotiated
   format on desktop and mobile.
2. With the private model absent, check for face overlays and unavailable
   identity. With the authorized model and its matching label file, verify a
   registered face stabilizes, an unregistered face is shown as Unknown,
   and two people do not select an identity for a new conversation.
3. Check that eyes follow a lone face gently in Idle or Listening and release
   when the face leaves. Check that thinking, speaking and sleeping
   expressions retain their normal behavior.
4. With simulated motors, test dashboard-started and wake-initiated voice,
   Ollama replies and a timer command. Check that a second person entering
   cannot change the active session identity.
5. Stop capture and confirm the preview and recognition state clear. Restart,
   disconnect the camera, and verify the error state and explicit restart.
   Stop the server and check the camera is free for another application.

### Remaining limitations

The project owner confirmed camera, face recognition and TFT operation in the
integrated Raspberry Pi 4 prototype. Recognition error rates at the new
default threshold have not been measured. Optional FER+ expression
classification remains experimental and was outside the Windows checks.
Driver open and read calls can block. The dashboard
clears stale output while waiting for the driver to return. The camera
pipeline detects faces and can recognize enrolled identities. It has no
general visual scene understanding.
