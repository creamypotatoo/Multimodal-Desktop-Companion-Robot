# Voice and audio

## Windows setup

Run from the repository root in PowerShell. Mock mode uses no microphone,
speaker, acoustic model or Ollama server.

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
& $py -m pip install -r requirements-web.txt -r requirements-display.txt
& $py -m web --unified --audio mock --ai mock --announce --host 127.0.0.1 --port 5000
```

For real Windows audio, install the optional CPU STT and playback packages.
Supply a local CTranslate2 Whisper model directory with model.bin, config.json
and tokenizer.json. The Windows bench checks found Whisper Small more accurate
than Tiny.en and Base.en for Eli's short English commands.
An explicit model download, if needed, is a separate action. Eli never
downloads models at startup or during inference.
[SYSTRAN faster-whisper](https://github.com/SYSTRAN/faster-whisper)

```powershell
& $py -m pip install sounddevice faster-whisper pyttsx3
$env:ELI_WHISPER_MODEL = 'D:\Models\faster-whisper-small'
$env:ELI_WHISPER_BEAM_SIZE = '5'
$env:ELI_WHISPER_PROMPT = 'Eli is a personal robot assistant.'
& $py -m web --unified --audio windows --ai mock --wake --announce --host 127.0.0.1 --port 5000
```

For local Ollama, start Ollama on loopback and choose an already installed tag
shown by `ollama list`. Set the URL and model before launching:

```powershell
$env:ELI_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:ELI_OLLAMA_MODEL = 'qwen3:1.7b'
& $py -m web --unified --audio windows --ai ollama --wake --announce --host 127.0.0.1 --port 5000
```

Ollama uses the existing timeout, model, context and fallback settings.
Real Windows audio can run independently of Ollama, the TFT, camera and motors.
Windows playback uses the OS default output device. Change it in Windows
Sound settings. The dashboard shows that policy, rather than claiming to
select a device through pyttsx3.

## Capture, wake and conversations

One audio worker owns wake capture, conversational capture/STT, local playback,
and spoken utility notifications. Windows and Pi use sounddevice with the
existing RMS voice activity detector. Input buffers are bounded and capture is
cancelled when its generation changes. Windows feeds mono 16 kHz float32 PCM
directly to Whisper with no WAV file. The Pi's whisper.cpp path uses a temporary
WAV deleted after each turn or wake sample. The media is processed locally. Local Ollama
receives transcript text only when explicitly configured. If configured
with a remote URL, the existing Ollama client sends the transcript there.

Wake listening is off at startup unless enabled with `--wake` or Settings.
Speak the phrase on its own. The default is **Hey Eli**. Speech is sampled in
short, bounded VAD windows and the normalized local transcript must match
the normalized phrase exactly. Case, punctuation and whitespace are normalized.
Eli, Ellie, and Ely are explicit aliases for an entire token. Extra words and
other names do not match. This is offline phrase detection, not an
acoustic keyword spotter. It requires local STT and may be slower on a Pi.
OpenWakeWord was evaluated as a future acoustic option. It has a model-based
interface, but the bundled models do not establish a validated custom
**Hey Eli** model for this project. Runtime Components accepts an optional
wake_detector(recording, phrase) -> bool adapter for a supplied model. Its recording
is PCM on Windows and a temporary path on Pi.
[OpenWakeWord models and integration](https://github.com/dscripka/openWakeWord#pre-trained-models)

Use `--wake-phrase`, `--wake-cooldown` (default three seconds) and
`--follow-up` (default 20 seconds) to tune startup behavior. The audio settings
form changes the phrase and listening policy for the running process.
VAD onset sensitivity is controlled by ELI_VAD_THRESHOLD (int16 RMS,
default 150) and ELI_VAD_NOISE_RATIO (default 3). The effective onset is the
larger of the configured RMS floor and the ambient RMS multiplied by that
ratio. Runtime calibration takes the lower quartile of the first quiet
samples and reuses the baseline for subsequent windows on the same device.
Wait for the first calibration to settle. Save sensitivity or run the
five-second microphone check from Settings. The test recalibrates and
reports whether speech crossed the gate without transcribing or saving it.
If needed, try an RMS floor of 60 and ratio of 2 in a quiet room, checking the
ambient value and false positives before leaving wake enabled.

Set ELI_AUDIO_INPUT to a device ID, a sounddevice name,
or default. The Settings device list applies to the next capture.
ELI_AUDIO_SAMPLE_RATE sets the input rate in the supported 8 to 48 kHz
range. Audio is normalized to 16 kHz for STT. RMS is shown relative to the
full int16 scale, with zero when no capture is active.
Peak is shown separately from RMS and describes raw input before gain.
ELI_AUDIO_GAIN_MAX is an optional Windows-only maximum boost from 1 to 8.
It defaults to 1. Selected speech with enough RMS can be boosted toward a
0.7 peak without attenuating stronger input. Gain runs after VAD and cannot
recover speech that VAD rejected. Prefer correcting the OS input level first.

Diagnostics show input device, raw level, VAD state and thresholds,
the configured or loaded model, and errors. The latest wake and command
transcriptions and their STT latency are available only to authenticated
dashboard sessions. These diagnostics keep up to 160 characters per channel
for two minutes in memory, clear on shutdown, and are not logged or cached.
Conversation history used for replies remains in memory for that session.

Standby waits for wake only if enabled. A valid wake event moves to Companion
and opens one session. Dashboard Start also opens a session in Companion.
Successful turns renew the follow-up window. Manual End ends the session.
A mode revision and the conversation generation reject late wake results
and prevent duplicate activation. Movement suppresses all conversational
capture. Sleep suppresses capture except when wake-only listening is
explicitly enabled with `--wake-in-sleep` or Settings. Dashboard mode
selection always remains available.

## Cancellation and commands

The real audio backends run the existing TTS path in an isolated playback
process. Stop speaking, mute, session cancellation, mode changes and shutdown
invalidate the active request. The worker terminates and reaps the process
before proceeding. On Pi it terminates the playback process group, including
the existing synthesis and player commands. If exit cannot be confirmed,
the runtime reports an audio fault and suspends capture and playback.
Already-running STT and AI calls may take time to return. Their stale
results cannot be spoken. OS and device buffering still require physical
verification.

Audio is half-duplex. Microphone capture stays closed during playback and
the short echo-settle period. You cannot use a voice interruption while
Eli is speaking. Use the dashboard Stop speaking button. When a listening
turn transcribes “stop speaking,” the router discards pending speech.

| Intent | Examples |
|---|---|
| Timer | “Set a timer for ten minutes,” “start a timer for one hour and two minutes,” “how much time is left on the timer,” “cancel timer” |
| Stopwatch | “Start a stopwatch,” “stop the stopwatch,” “resume stopwatch” |
| Pomodoro and utility controls | “Start a Pomodoro,” “pause Pomodoro,” “resume timer” |
| Volume | “Set volume to eighty percent,” “increase volume by ten percent,” “lower volume” |
| Mode and status | “Enter standby mode,” “go to sleep,” “switch to companion mode,” “what mode are you in,” “runtime status” |

The parser asks for clarification for incomplete or invalid recognized
requests. It routes only recognized typed actions before handing ordinary
conversation to the existing responder. Spoken mode changes can end a session
before a spoken confirmation. Voice does not arm motors or issue movement.
Timer and Pomodoro completions use the existing scheduler and event stream,
honor volume and mode, and defer only within the existing bounded queue.
Browser alerts are independent of audio.

## Pi compatibility and verification

On the Pi, `--audio pi` uses the existing whisper.cpp model and CLI and the
configured TTS backend. The previous `--local-io` shortcut still selects Pi
audio and configured eyes by default. The ALSA output can be selected at
startup via ELI_TTS_DEVICE when using espeak-ng/ffmpeg/aplay. Keep the
existing VAD and TTS environment settings. The dashboard preserves its
authentication, CSRF checks and LAN binding safeguards.

To verify on Windows, open http://127.0.0.1:5000, sign in, then open Settings.
Choose the microphone and check the level in Diagnostics. Click Test speaker
and verify audible output. Say the wake phrase separately, wait for Companion,
then ask for a short timer. During a longer response, click Stop speaking
and verify audible interruption. Verify Sleep with wake listening both off
and explicitly enabled. These steps require human observation.

Manual Windows verification confirmed microphone capture, dashboard-started
conversation, deterministic timer commands, and “Hey Eli” activation with
Whisper Small. Audible interruption on the Windows output device, end-to-end
conversation with real Ollama, and Pi audio still require physical verification.
For Ollama, confirm Diagnostics reports “Ollama responded” during the session
so a fallback response does not count as a successful live backend test.

If capture fails, check Windows microphone privacy permissions and the input
selection. Try the system default at 16 kHz. If STT fails, check the local model
files. If Ollama is unavailable, check its loopback URL and installed tag.
A long-running STT inference may delay the next request, but cannot revive
a cancelled interaction.

### Windows file decoder incompatibility

The reproduced setup used faster-whisper 1.2.1 with PyAV 19.0.2.
The older faster-whisper file decoder passes `metadata_errors` to
`av.open()`. PyAV 19 removed that argument. Both activation paths previously
used that decoder, so working capture could still fail before recognition.
Eli's Windows PCM path bypasses it. If a separate tool still needs file
decoding, use a faster-whisper release containing the upstream guard or
pin `av<19` in that tool's environment. Eli does not change package versions.
[Upstream report](https://github.com/SYSTRAN/faster-whisper/issues/1589) ·
[Upstream decoder guard](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/audio.py)

```powershell
& $py -m unittest discover -s tests -p test_voice_runtime.py
& $py -m unittest discover -s tests
```
