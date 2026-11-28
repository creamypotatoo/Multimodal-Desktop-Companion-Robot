# Multimodal-Desktop-Companion-Robot

Eli can run the legacy robot launcher, a standalone dashboard, or an opt-in
unified dashboard and conversation runtime. See
[Unified runtime setup](UNIFIED_RUNTIME.md) for the Windows mock command and
future Raspberry Pi setup, [dashboard setup](WEB_DASHBOARD.md) for authentication
and networking, and [controller architecture](CONTROLLER_ARCHITECTURE.md) for
mode and movement policy.

Phase 5 was manually verified on Windows with Whisper Small and local Ollama
`qwen3:1.7b`. The checks covered microphone capture, dashboard-started
conversation, “Hey Eli” activation, voice productivity commands, speech
playback, and interruption.

## Resume on Windows

Run from this repository in PowerShell. If Ollama is stopped, run
`ollama serve` in a separate terminal. Use the default loopback address
and an already installed `qwen3:1.7b` model. Adjust the Whisper Small path
if your local model directory differs.

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
$env:ELI_WHISPER_MODEL = 'D:\Models\faster-whisper-small'
$env:ELI_WHISPER_BEAM_SIZE = '5'
$env:ELI_WHISPER_PROMPT = 'Eli is a personal robot assistant.'
$env:ELI_AUDIO_INPUT = 'default'
$env:ELI_AUDIO_SAMPLE_RATE = '16000'
$env:ELI_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:ELI_OLLAMA_MODEL = 'qwen3:1.7b'
& $py -m web --unified --audio windows --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

Sign in at `http://127.0.0.1:5000/`. See [Voice and audio](VOICE_AUDIO.md) for
dependency installation, mock launches, and the verification checklist.

For physical motor integration, see [ESP32 integration](ESP32_INTEGRATION.md).
Phase 6 manual hardware verification succeeded on Windows with the ESP32
DOIT DEVKIT V1 connected through the CP210x bridge on COM9.
Manual drive is available in Movement mode with a healthy ESP32 link and
clear safety latches. Obstacle and cliff sensors unavailable. Manual floor operation only.

For the authenticated camera preview, optional face recognition, eye tracking
and conversation context, see [Perception integration](PERCEPTION_INTEGRATION.md).
Launch with `--local-camera` and start capture from Perception.
Phase 7 passed Windows manual verification for discovery and live preview,
face detection, registered identity recognition and expiration, animated gaze,
wake and identity-aware conversation with capture active, and repeated start
and stop. Raspberry Pi camera and TFT operation still require verification.

## Raspberry Pi deployment

Use the [Raspberry Pi deployment guide](RASPBERRY_PI_DEPLOYMENT.md) for
non-destructive synchronization, staged hardware checks, private HTTPS
dashboard access, and optional systemd startup after manual acceptance.
