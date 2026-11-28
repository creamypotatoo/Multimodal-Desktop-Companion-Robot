# Ollama language model for Eli

Eli's replies can come from a local `qwen3:1.7b` model that runs in Ollama on the
Windows laptop. Everything else stays on the Raspberry Pi: camera, face
recognition, microphone, whisper.cpp and the speaker. Only text crosses the
Wi-Fi: the system prompt, the last few exchanges and the latest transcript. No
cloud service or internet connection is needed once the model is downloaded.

```
Pi mic -> whisper.cpp -> conversation.py --SSH tunnel--> Ollama (laptop) --> reply text
                                        <-------------------------------
Pi espeak-ng -> aplay -> MAX98357A speaker
```

Ollama is **off unless `ELI_OLLAMA_URL` is set**. Without it, Eli uses the
original rule-based replies exactly as before. If the laptop cannot be reached,
Eli answers with the rule-based replies and tries Ollama again after a cooldown.

## 1. Windows laptop

### Install Ollama and the model

1. Download the Windows installer from <https://ollama.com/download> and run
   `OllamaSetup.exe`. It installs for your user account only and does not need
   administrator rights.
2. Open PowerShell and run:

   ```powershell
   ollama pull qwen3:1.7b
   ollama run qwen3:1.7b "Say hello in one sentence." --think=false
   ```

### Keep the Ollama API on loopback

Keep Ollama listening on `127.0.0.1:11434`. Verify it locally with
`curl.exe http://127.0.0.1:11434/api/version`. Use the SSH reverse tunnel in the
[Raspberry Pi deployment guide](RASPBERRY_PI_DEPLOYMENT.md#5-connect-laptop-ollama-privately)
to make Ollama available on the Pi's loopback interface.

## 2. Raspberry Pi

### Configuration

Ollama settings are environment variables. Nothing about them is hardcoded in
the source:

| Variable | Example / default | Meaning |
|---|---|---|
| `ELI_OLLAMA_URL` | `http://127.0.0.1:11435` (unset = rule-based) | Laptop's Ollama address |
| `ELI_OLLAMA_MODEL` | `qwen3:1.7b` | Model tag |
| `ELI_OLLAMA_TIMEOUT` | `30` | Seconds before one request is abandoned |
| `ELI_OLLAMA_RETRY_COOLDOWN` | `60` | Seconds of rule-based replies after a connection failure |
| `ELI_OLLAMA_NUM_PREDICT` | `96` | Max tokens per reply (keeps answers short) |
| `ELI_OLLAMA_HISTORY_TURNS` | `8` | Past exchanges sent with each request (session keeps 20) |
| `ELI_OLLAMA_NUM_CTX` | `2048` | Model context window |
| `ELI_OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded |
| `ELI_OLLAMA_TEMPERATURE` | `0.7` | Reply variety |
| `ELI_SYSTEM_PROMPT_FILE` | *(unset)* | Text file that replaces Eli's built-in personality prompt |

The loopback address and port in this example refer to the Pi end of the SSH tunnel.

Create `~/eli-ollama.env` on the Pi:

```bash
cat > ~/eli-ollama.env <<'EOF'
ELI_OLLAMA_URL=http://127.0.0.1:11435
ELI_OLLAMA_MODEL=qwen3:1.7b
ELI_OLLAMA_TIMEOUT=30
EOF
```

### Test connectivity

```bash
cd ~/Multimodal-Desktop-Companion-Robot
set -a; . ~/eli-ollama.env; set +a
curl -s "$ELI_OLLAMA_URL/api/version"
python -m intelligence.ollama_client "What is a Raspberry Pi?"
```

The second command prints the Ollama version, then Eli's reply and its timing.
The first request after the model was unloaded can take 15–25 s on the laptop's
CPU. Later replies take a few seconds.

### Start Eli

```bash
cd ~/Multimodal-Desktop-Companion-Robot
set -a; . ~/eli-ollama.env; set +a
export ELI_TTS_BACKEND=espeak
python main.py --headless
```

After the face lock, the log shows
`[Ollama] Using model qwen3:1.7b at 127.0.0.1:11435`. While Eli speaks the
greeting, the model is preloaded in the background (`[Ollama] Model ready.`).
Each reply logs its latency and token count. Conversation text is never written
to the `[Ollama]` log lines.

### Switch back to rule-based replies

```bash
unset ELI_OLLAMA_URL
python main.py --headless
```

You can also simply start a new shell without sourcing `~/eli-ollama.env`.

## 3. Troubleshooting

| Symptom | Fix |
|---|---|
| `connection failed (Connection refused)` | Check Ollama on laptop loopback and confirm the SSH tunnel is running. |
| `timed out after 30s` on every request | From the Pi, run `curl -m 5 http://127.0.0.1:11435/api/version`. Check the tunnel and private SSH connectivity. |
| Times out only on the first reply | Cold model load on CPU. Raise `ELI_OLLAMA_TIMEOUT` to `45`, or keep the model loaded with `ELI_OLLAMA_KEEP_ALIVE=2h`. |
| Worked yesterday, fails today | Check whether the SSH session dropped and reconnect using the Pi's private hostname. |
| `HTTP 404: model "…" not found` | Run `ollama pull qwen3:1.7b` on the laptop, or fix `ELI_OLLAMA_MODEL`. |
| Eli says "I heard you, but I am not sure how to respond" | That is the rule-based fallback. Look for an `[Ollama] Unavailable` line. Eli retries automatically after `ELI_OLLAMA_RETRY_COOLDOWN` seconds and logs `[Ollama] Reconnected.` |
| Replies cut short | Raise `ELI_OLLAMA_NUM_PREDICT`, for example to `150`. |

## 4. Tests

These tests need no Ollama server or audio hardware:

```bash
python -m unittest tests.test_ollama tests.test_conversation_session -v
```
