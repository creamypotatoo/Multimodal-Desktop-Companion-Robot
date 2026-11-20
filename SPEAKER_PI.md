# Raspberry Pi speaker output (MAX98357A I2S)

Eli's `speak(text)` (in `audio/text_to_speech.py`) has two backends:

| Backend   | Used on            | How it works                                                        |
|-----------|--------------------|---------------------------------------------------------------------|
| `espeak`  | Raspberry Pi       | `espeak-ng` → WAV, `ffmpeg` applies `volume=0.10`, `aplay -D <device>` |
| `pyttsx3` | Windows dev        | Original pyttsx3 behaviour, unchanged                               |

With the default `auto`, Linux uses `espeak` when `espeak-ng`, `ffmpeg` and
`aplay` are all on `PATH`, and anything else uses `pyttsx3`. pyttsx3 does not
need to be installed on the Pi. Speech is serialised, so two calls never play
at the same time. Temporary WAV files are created in a private temp directory
and deleted after each utterance, including when an error occurs.

## Configuration

Environment variables (command-line flags on the test CLI override them):

| Variable          | Default                         | Notes                                  |
|-------------------|---------------------------------|----------------------------------------|
| `ELI_TTS_BACKEND` | `auto`                          | `auto`, `espeak` or `pyttsx3`          |
| `ELI_TTS_DEVICE`  | `plughw:CARD=MAX98357A,DEV=0`   | ALSA device **name**                   |
| `ELI_TTS_VOLUME`  | `0.10`                          | Digital gain, `0 < v <= 1.0`           |

Numeric card indices such as `hw:1,0`, `plughw:1,0` or `CARD=1` are rejected
because card numbers can change between boots. Use the name from `aplay -L`.

## Prerequisites on the Pi

The I2S overlay and ALSA output must already work (this project does not
change system audio config). Confirm:

```sh
command -v espeak-ng ffmpeg aplay
aplay -L | grep -A2 'CARD=MAX98357A'
```

If a tool is missing: `sudo apt install espeak-ng ffmpeg alsa-utils`.

## Testing on the Pi

From the project root, inside the project venv:

```sh
# 1. Hardware-free unit tests (no sound is produced)
python -m unittest tests.test_text_to_speech -v

# 2. Default phrase through the MAX98357A at volume 0.10
python -m audio.text_to_speech --backend espeak

# 3. Custom phrase, explicit device and volume
python -m audio.text_to_speech "Testing the speaker." --backend espeak \
    --device plughw:CARD=MAX98357A,DEV=0 --volume 0.10

# 4. Same thing configured through the environment
ELI_TTS_BACKEND=espeak ELI_TTS_VOLUME=0.10 python -m audio.text_to_speech

# 5. Should print a numeric-index error and exit 1 without playing anything
python -m audio.text_to_speech --backend espeak --device plughw:1,0; echo "exit=$?"
```

The CLI prints the chosen backend, device and volume first. On failure it prints
`[TTS] <stage> failed: <stderr>` (for example when the device is busy) and exits 1.

Full application (camera + conversation + speaker):

```sh
export ELI_TTS_BACKEND=espeak
python main.py --headless
```

`--vision-only` does not load any audio modules.

## Troubleshooting

- `aplay failed: ... Device or resource busy`: another process is using the
  card. Check with `fuser -v /dev/snd/*`.
- `aplay failed: ... No such file or directory`: the device name is wrong.
  Compare it with `aplay -L`.
- Too loud or too quiet: change `ELI_TTS_VOLUME`, e.g. `0.05` or `0.2`.
- No sound but no error: check the speaker wiring, then check that
  `speaker-test -D plughw:CARD=MAX98357A,DEV=0 -c 2 -t wav -l 1` is audible.
