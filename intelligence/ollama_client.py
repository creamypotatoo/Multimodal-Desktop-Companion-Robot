"""Minimal, dependency-free client for a local-network Ollama server.

Only text crosses the network: the system prompt, recent conversation text and
the latest transcript. No audio, images or face data are ever sent.

Configuration (environment variables; Ollama is DISABLED unless ELI_OLLAMA_URL
is set, so the rule-based responder remains the default):
  ELI_OLLAMA_URL              e.g. http://<laptop-ip>:11434   (default: unset/disabled)
  ELI_OLLAMA_MODEL            model tag                       (default: qwen3:1.7b)
  ELI_OLLAMA_TIMEOUT          seconds per HTTP request        (default: 30)
  ELI_OLLAMA_NUM_PREDICT      max tokens per reply            (default: 96)
  ELI_OLLAMA_NUM_CTX          context window in tokens        (default: 2048)
  ELI_OLLAMA_KEEP_ALIVE       how long Ollama keeps the model (default: 30m)
  ELI_OLLAMA_TEMPERATURE      sampling temperature            (default: 0.7)
  ELI_OLLAMA_HISTORY_TURNS    past exchanges sent per request (default: 8)
  ELI_OLLAMA_RETRY_COOLDOWN   seconds to skip Ollama after a
                              connection failure              (default: 60)

Log lines never include conversation text, only timings and error categories.
"""
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_TIMEOUT = 30.0
DEFAULT_NUM_PREDICT = 96
DEFAULT_NUM_CTX = 2048
DEFAULT_KEEP_ALIVE = "30m"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_HISTORY_TURNS = 8
DEFAULT_RETRY_COOLDOWN = 60.0
MAX_RESPONSE_BYTES = 1_000_000  # a short spoken reply is a few hundred bytes


class OllamaError(Exception):
    """Base class; messages never contain conversation text."""


class OllamaUnavailable(OllamaError):
    """Server unreachable, timed out, or returned an HTTP error."""


class OllamaBadResponse(OllamaError):
    """Server answered, but the body was malformed, incomplete or empty."""


def _log(message):
    print(f"[Ollama] {message}", flush=True)


def _env_number(environ, name, default, cast=float, minimum=0):
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        _log(f"Ignoring invalid {name}; using {default}")
        return default
    if value < minimum:
        _log(f"Ignoring out-of-range {name}; using {default}")
        return default
    return value


def _normalise_url(raw):
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        _log("Ignoring ELI_OLLAMA_URL: expected http://host:port")
        return None
    return url


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    num_predict: int = DEFAULT_NUM_PREDICT
    num_ctx: int = DEFAULT_NUM_CTX
    keep_alive: str = DEFAULT_KEEP_ALIVE
    temperature: float = DEFAULT_TEMPERATURE
    history_turns: int = DEFAULT_HISTORY_TURNS
    retry_cooldown: float = DEFAULT_RETRY_COOLDOWN

    @classmethod
    def from_env(cls, environ=None):
        """Config from the environment, or None when Ollama is not configured."""
        environ = os.environ if environ is None else environ
        url = _normalise_url(environ.get("ELI_OLLAMA_URL"))
        if url is None:
            return None
        return cls(
            url=url,
            model=(environ.get("ELI_OLLAMA_MODEL") or "").strip() or DEFAULT_MODEL,
            timeout=_env_number(environ, "ELI_OLLAMA_TIMEOUT", DEFAULT_TIMEOUT, minimum=1),
            num_predict=_env_number(environ, "ELI_OLLAMA_NUM_PREDICT", DEFAULT_NUM_PREDICT,
                                    cast=int, minimum=8),
            num_ctx=_env_number(environ, "ELI_OLLAMA_NUM_CTX", DEFAULT_NUM_CTX,
                                cast=int, minimum=512),
            keep_alive=(environ.get("ELI_OLLAMA_KEEP_ALIVE") or "").strip() or DEFAULT_KEEP_ALIVE,
            temperature=_env_number(environ, "ELI_OLLAMA_TEMPERATURE", DEFAULT_TEMPERATURE),
            history_turns=_env_number(environ, "ELI_OLLAMA_HISTORY_TURNS",
                                      DEFAULT_HISTORY_TURNS, cast=int),
            retry_cooldown=_env_number(environ, "ELI_OLLAMA_RETRY_COOLDOWN",
                                       DEFAULT_RETRY_COOLDOWN),
        )

    @property
    def host(self):
        """host:port for logs (no path, no credentials)."""
        parsed = urllib.parse.urlparse(self.url)
        return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def build_messages(system_prompt, history, user_text, history_turns=DEFAULT_HISTORY_TURNS):
    """Ollama chat messages: system, then past (user, assistant) pairs oldest
    first, then the latest user text exactly once.

    ``history`` holds completed ``(user_text, eli_response)`` pairs and must not
    already contain ``user_text`` for the current turn.
    """
    messages = [{"role": "system", "content": system_prompt}]
    recent = list(history or [])
    recent = recent[-history_turns:] if history_turns > 0 else []
    for past_user, past_reply in recent:
        if past_user and past_reply:
            messages.append({"role": "user", "content": str(past_user)})
            messages.append({"role": "assistant", "content": str(past_reply)})
    messages.append({"role": "user", "content": user_text})
    return messages


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MARKDOWN = re.compile(r"(\*\*|__|`+|^#+\s*|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.MULTILINE)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def clean_for_speech(text, truncated=False):
    """Remove reasoning tags, markdown and emoji; trim a cut-off final sentence."""
    text = _THINK_BLOCK.sub(" ", text or "")
    text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
    text = _MARKDOWN.sub("", text)
    text = _EMOJI.sub("", text).replace("*", "")
    text = " ".join(text.split())
    if truncated:
        ends = [m.end() for m in _SENTENCE_END.finditer(text)]
        if ends:
            text = text[:ends[-1]]
    return text


class OllamaClient:
    """Blocking /api/chat calls with a hard per-request timeout."""

    def __init__(self, config, *, opener=None, clock=time.monotonic):
        self.config = config
        self._open = opener or urllib.request.urlopen
        self._clock = clock

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.config.url + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._open(request, timeout=self.config.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise OllamaUnavailable(f"HTTP {exc.code}{_http_error_detail(exc)}") from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise OllamaUnavailable(f"timed out after {self.config.timeout:g}s") from None
            raise OllamaUnavailable(f"connection failed ({_reason_text(reason)})") from None
        except (socket.timeout, TimeoutError):
            raise OllamaUnavailable(f"timed out after {self.config.timeout:g}s") from None
        except (ConnectionError, OSError) as exc:
            raise OllamaUnavailable(f"connection failed ({_reason_text(exc)})") from None
        except http.client.HTTPException as exc:
            raise OllamaUnavailable(f"connection failed ({type(exc).__name__})") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise OllamaBadResponse("response too large")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OllamaBadResponse("invalid JSON") from None
        if not isinstance(parsed, dict):
            raise OllamaBadResponse("unexpected JSON structure")
        if parsed.get("error"):
            raise OllamaBadResponse(f"server error: {str(parsed['error'])[:120]}")
        return parsed

    def chat_payload(self, messages):
        cfg = self.config
        return {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": cfg.keep_alive,
            "options": {
                "num_predict": cfg.num_predict,
                "num_ctx": cfg.num_ctx,
                "temperature": cfg.temperature,
            },
        }

    def chat(self, messages):
        """Return the cleaned reply text. Raises OllamaUnavailable/OllamaBadResponse."""
        started = self._clock()
        data = self._post("/api/chat", self.chat_payload(messages))
        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaBadResponse("missing 'message' field")
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaBadResponse("missing 'message.content' field")
        reply = clean_for_speech(content, truncated=data.get("done_reason") == "length")
        if not reply:
            raise OllamaBadResponse("empty reply")
        _log(_timing_summary(self._clock() - started, data))
        return reply

    def version(self):
        """GET /api/version -> version string (connectivity check)."""
        request = urllib.request.Request(self.config.url + "/api/version", method="GET")
        try:
            with self._open(request, timeout=self.config.timeout) as response:
                data = json.loads(response.read(65536).decode("utf-8"))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise OllamaUnavailable(f"connection failed ({_reason_text(getattr(exc, 'reason', exc))})") from None
        except (UnicodeDecodeError, ValueError):
            raise OllamaBadResponse("invalid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            raise OllamaBadResponse("missing 'version' field")
        return data["version"]

    def warm_up(self, system_prompt=None):
        """Load the model now so the first spoken reply is faster.

        With ``system_prompt``, Ollama also processes and caches that prompt
        prefix (one token is generated and discarded); without it, the model
        is only loaded (empty chat).
        """
        if not system_prompt:
            self._post("/api/chat", {"model": self.config.model, "messages": [],
                                     "keep_alive": self.config.keep_alive})
            return
        payload = self.chat_payload([{"role": "system", "content": system_prompt}])
        payload["options"]["num_predict"] = 1
        self._post("/api/chat", payload)


def _reason_text(reason):
    if isinstance(reason, OSError) and reason.strerror:
        return reason.strerror
    return type(reason).__name__ if isinstance(reason, BaseException) else str(reason)[:80]


def _http_error_detail(exc):
    """Ollama's JSON 'error' field (e.g. model not found), never the request."""
    try:
        body = json.loads(exc.read(4096).decode("utf-8"))
        return f": {str(body.get('error'))[:120]}" if body.get("error") else ""
    except Exception:
        return ""


def _timing_summary(elapsed, data):
    parts = [f"Reply in {elapsed:.1f}s"]
    load = data.get("load_duration")
    if isinstance(load, (int, float)) and load > 5e8:  # only mention loads > 0.5 s
        parts.append(f"model load {load / 1e9:.1f}s")
    tokens, eval_ns = data.get("eval_count"), data.get("eval_duration")
    if isinstance(tokens, int):
        rate = ""
        if isinstance(eval_ns, (int, float)) and eval_ns > 0:
            rate = f", {tokens / (eval_ns / 1e9):.1f} tok/s"
        parts.append(f"{tokens} tokens{rate}")
    return ", ".join(parts)


def main(argv=None):
    """Connectivity check: python -m intelligence.ollama_client ["test message"]"""
    import sys
    from intelligence.personality import build_system_prompt, load_base_prompt
    args = sys.argv[1:] if argv is None else argv
    config = OllamaConfig.from_env()
    if config is None:
        print("ELI_OLLAMA_URL is not set; Eli will use rule-based replies.")
        return 2
    client = OllamaClient(config)
    try:
        print(f"Ollama {client.version()} reachable at {config.host}; asking {config.model}...")
        system = build_system_prompt(None, None, load_base_prompt())
        text = args[0] if args else "Say hello in one short sentence."
        print("Reply:", client.chat(build_messages(system, (), text)))
    except OllamaError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
