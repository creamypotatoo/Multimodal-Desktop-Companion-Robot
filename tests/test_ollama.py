"""Hardware- and network-free tests for the Ollama integration.

Every HTTP call goes through a fake ``opener``; no Ollama server, microphone,
speaker, webcam or Raspberry Pi is needed.
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from intelligence import ollama_client as oc
from intelligence import personality

# Import conversation's real dependencies BEFORE patching sys.modules.
# patch.dict restores sys.modules on exit and drops every module first imported
# inside the block; if behavior.state_machine were first imported there, later
# imports would create a second State enum class and identity checks such as
# fsm.transition_to(State.IDLE) in other test modules would fail.
import audio.text_to_speech  # noqa: F401,E402
import behavior.state_machine  # noqa: F401,E402
import intelligence  # noqa: E402

# Same audio stub as test_conversation_session: no sounddevice/whisper needed.
_stt = types.ModuleType("audio.speech_to_text")
_stt.record_audio = Mock(name="record_audio")
_stt.transcribe = Mock(name="transcribe")
with patch.dict(sys.modules, {"audio.speech_to_text": _stt}):
    sys.modules.pop("intelligence.conversation", None)
    from intelligence import conversation as conv
sys.modules.pop("intelligence.conversation", None)
# Also drop the package attribute so other test modules re-import their own
# copy instead of silently receiving this one via `from intelligence import ...`.
if getattr(intelligence, "conversation", None) is conv:
    del intelligence.conversation

PRIVATE = "my secret diary entry"
URL = "http://192.0.2.10:11434"  # TEST-NET address, never routable


class FakeResponse:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stand-in for urllib.request.urlopen; each item is a body or an exception."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({"url": request.full_url, "method": request.get_method(),
                              "timeout": timeout,
                              "payload": json.loads(request.data) if request.data else None})
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            item = json.dumps(item)
        return FakeResponse(item)

    @property
    def payload(self):
        return self.requests[-1]["payload"]


def reply(content="Sure thing.", **extra):
    body = {"model": "qwen3:1.7b", "message": {"role": "assistant", "content": content},
            "done": True, "done_reason": "stop", "eval_count": 12,
            "eval_duration": 900_000_000, "load_duration": 1_000_000}
    body.update(extra)
    return body


REFUSED = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
TIMEOUT = urllib.error.URLError(socket.timeout("timed out"))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_client(*outcomes, clock=None, **config):
    opener = FakeOpener(*outcomes)
    cfg = oc.OllamaConfig(url=URL, **config)
    return oc.OllamaClient(cfg, opener=opener, clock=clock or Clock()), opener


def make_responder(*outcomes, clock=None, **config):
    clock = clock or Clock()
    client, opener = make_client(*outcomes, clock=clock, **config)
    return conv.OllamaResponder(client, clock=clock), opener, clock


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ConfigTests(unittest.TestCase):
    def test_disabled_without_url(self):
        self.assertIsNone(oc.OllamaConfig.from_env({}))
        self.assertIsNone(oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": "  "}))
        self.assertIs(conv.default_responder({}), conv.generate_response)

    def test_env_values_and_defaults(self):
        cfg = oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": URL + "/",
                                        "ELI_OLLAMA_TIMEOUT": "12"})
        self.assertEqual(cfg.url, URL)
        self.assertEqual(cfg.model, "qwen3:1.7b")
        self.assertEqual(cfg.timeout, 12.0)
        self.assertEqual(cfg.host, "192.0.2.10:11434")
        cfg = oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": URL, "ELI_OLLAMA_MODEL": "qwen3:4b"})
        self.assertEqual(cfg.model, "qwen3:4b")

    def test_invalid_values_fall_back_to_defaults(self):
        with quiet():
            cfg = oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": URL, "ELI_OLLAMA_TIMEOUT": "soon",
                                            "ELI_OLLAMA_NUM_PREDICT": "-5"})
            self.assertIsNone(oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": "192.0.2.10:11434"}))
            self.assertIsNone(oc.OllamaConfig.from_env({"ELI_OLLAMA_URL": "ftp://x"}))
        self.assertEqual(cfg.timeout, oc.DEFAULT_TIMEOUT)
        self.assertEqual(cfg.num_predict, oc.DEFAULT_NUM_PREDICT)

    def test_default_responder_uses_ollama_when_configured(self):
        with quiet():
            responder = conv.default_responder({"ELI_OLLAMA_URL": URL})
        self.assertIsInstance(responder, conv.OllamaResponder)
        self.assertEqual(responder.client.config.url, URL)


class ClientTests(unittest.TestCase):
    def test_successful_response(self):
        client, opener = make_client(reply("Hi there, nice to hear from you."))
        with quiet():
            text = client.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(text, "Hi there, nice to hear from you.")
        self.assertEqual(opener.requests[0]["url"], URL + "/api/chat")
        self.assertEqual(opener.requests[0]["method"], "POST")

    def test_request_payload(self):
        client, opener = make_client(reply(), model="qwen3:1.7b", timeout=7,
                                     num_predict=64, keep_alive="15m")
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        with quiet():
            client.chat(messages)
        payload = opener.payload
        self.assertEqual(payload["model"], "qwen3:1.7b")
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], "15m")
        self.assertEqual(payload["options"]["num_predict"], 64)
        self.assertIn("num_ctx", payload["options"])
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(opener.requests[0]["timeout"], 7)  # never unbounded

    def test_connection_refused(self):
        client, _ = make_client(REFUSED)
        with self.assertRaises(oc.OllamaUnavailable) as ctx:
            client.chat([])
        self.assertIn("Connection refused", str(ctx.exception))

    def test_timeouts(self):
        for error in (TIMEOUT, socket.timeout("timed out"), TimeoutError()):
            client, _ = make_client(error, timeout=5)
            with self.assertRaises(oc.OllamaUnavailable) as ctx:
                client.chat([])
            self.assertIn("timed out after 5s", str(ctx.exception))

    def test_http_error_reports_ollama_error_field(self):
        err = urllib.error.HTTPError(URL, 404, "Not Found", {},
                                     io.BytesIO(b'{"error":"model \\"x\\" not found"}'))
        client, _ = make_client(err)
        with self.assertRaises(oc.OllamaUnavailable) as ctx:
            client.chat([])
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json(self):
        for body in ("not json", b"\xff\xfe", "[1, 2]", ""):
            client, _ = make_client(body)
            with self.assertRaises(oc.OllamaBadResponse):
                client.chat([])

    def test_empty_response(self):
        for content in ("", "   ", "<think>\n</think>\n", "**"):
            client, _ = make_client(reply(content))
            with self.assertRaises(oc.OllamaBadResponse) as ctx:
                client.chat([])
            self.assertIn("empty", str(ctx.exception))

    def test_missing_fields(self):
        for body in ({"done": True}, {"message": "text"}, {"message": {"role": "assistant"}},
                     {"message": {"content": None}}, {"error": "model failed"}):
            client, _ = make_client(body)
            with self.assertRaises(oc.OllamaBadResponse):
                client.chat([])

    def test_reply_is_cleaned_for_speech(self):
        content = "<think>hmm</think>**Sure!** Here's how:\n- step one\n- step two \U0001F600"
        client, _ = make_client(reply(content))
        with quiet():
            self.assertEqual(client.chat([]), "Sure! Here's how: step one step two")

    def test_token_limit_trims_cut_off_sentence(self):
        client, _ = make_client(reply("Robots are fun. They can also be very", done_reason="length"))
        with quiet():
            self.assertEqual(client.chat([]), "Robots are fun.")

    def test_latency_logged_without_conversation_text(self):
        clock = Clock()
        client, opener = make_client(reply("Your diary sounds fun."), clock=clock)
        opener.outcomes[0]["load_duration"] = 5_600_000_000
        original = opener.__call__

        def slow(request, timeout=None):
            clock.now += 2.5
            return original(request, timeout)

        client._open = slow
        with quiet() as log:
            client.chat([{"role": "user", "content": PRIVATE}])
        out = log.getvalue()
        self.assertIn("Reply in 2.5s", out)
        self.assertIn("model load 5.6s", out)
        self.assertIn("12 tokens", out)
        self.assertNotIn(PRIVATE, out)
        self.assertNotIn("diary", out)

    def test_warm_up_sends_empty_chat(self):
        client, opener = make_client({"done": True}, keep_alive="30m")
        client.warm_up()
        self.assertEqual(opener.payload, {"model": "qwen3:1.7b", "messages": [],
                                          "keep_alive": "30m"})

    def test_warm_up_primes_system_prompt(self):
        client, opener = make_client(reply("x"), num_predict=96)
        client.warm_up("SYSTEM")
        payload = opener.payload
        self.assertEqual(payload["messages"], [{"role": "system", "content": "SYSTEM"}])
        self.assertEqual(payload["options"]["num_predict"], 1)
        self.assertIs(payload["think"], False)
        self.assertEqual(client.config.num_predict, 96)  # config untouched

    def test_version(self):
        client, opener = make_client({"version": "0.40.17"})
        self.assertEqual(client.version(), "0.40.17")
        client, _ = make_client(REFUSED)
        with self.assertRaises(oc.OllamaUnavailable):
            client.version()


class MessageTests(unittest.TestCase):
    def test_history_order_and_latest_message_once(self):
        history = [("hi", "Hello!"), ("what's two plus two", "Four."), ("thanks", "Any time.")]
        msgs = oc.build_messages("SYS", history, "tell me a joke")
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "assistant", "user", "assistant",
                          "user", "assistant", "user"])
        self.assertEqual([m["content"] for m in msgs[1:]],
                         ["hi", "Hello!", "what's two plus two", "Four.", "thanks",
                          "Any time.", "tell me a joke"])
        self.assertEqual(sum(m["content"] == "tell me a joke" for m in msgs), 1)

    def test_history_is_limited_to_recent_turns(self):
        history = [(f"q{i}", f"a{i}") for i in range(20)]
        msgs = oc.build_messages("SYS", history, "latest", history_turns=3)
        self.assertEqual([m["content"] for m in msgs[1:]],
                         ["q17", "a17", "q18", "a18", "q19", "a19", "latest"])
        self.assertEqual(len(oc.build_messages("SYS", history, "x", history_turns=0)), 2)


class PersonalityTests(unittest.TestCase):
    def test_system_prompt_content(self):
        prompt = personality.build_system_prompt("Shanta", {"visit_count": 4, "emotion": "happy"})
        self.assertIn("You are Eli", prompt)
        self.assertIn("no markdown", prompt)
        self.assertIn("not a human", prompt)
        self.assertIn("talking with Shanta", prompt)
        self.assertIn("visited you 4 times", prompt)
        self.assertIn("'happy'", prompt)
        self.assertIn("do not know the name", personality.build_system_prompt(None))
        self.assertIn("do not know the name", personality.build_system_prompt("Unknown"))

    def test_prompt_file_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            path.write_text("You are a pirate robot.", encoding="utf-8")
            base = personality.load_base_prompt({"ELI_SYSTEM_PROMPT_FILE": str(path)})
            self.assertEqual(base, "You are a pirate robot.")
            with quiet():
                missing = personality.load_base_prompt(
                    {"ELI_SYSTEM_PROMPT_FILE": str(Path(tmp) / "nope.txt")})
            self.assertEqual(missing, personality.BASE_PROMPT)
        self.assertEqual(personality.load_base_prompt({}), personality.BASE_PROMPT)


class ResponderTests(unittest.TestCase):
    def test_request_contains_system_prompt_identity_and_history(self):
        responder, opener, _ = make_responder(reply("Nice to see you again."))
        with quiet():
            out = responder("how are you", "Shanta", history=(("hi", "Hey!"),),
                            context={"visit_count": 3})
        self.assertEqual(out, "Nice to see you again.")
        self.assertEqual(responder.last_source, "ollama")
        msgs = opener.payload["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("You are Eli", msgs[0]["content"])
        self.assertIn("talking with Shanta", msgs[0]["content"])
        self.assertEqual(msgs[1:], [{"role": "user", "content": "hi"},
                                    {"role": "assistant", "content": "Hey!"},
                                    {"role": "user", "content": "how are you"}])

    def test_fallback_on_every_failure_kind(self):
        failures = [REFUSED, TIMEOUT, "garbage", reply(""), {"done": True},
                    RuntimeError("client bug")]
        for failure in failures:
            responder, _, _ = make_responder(failure)
            with quiet() as log:
                out = responder("hello", "Shanta", history=(), context={})
            self.assertEqual(out, conv.generate_response("hello", "Shanta"), failure)
            self.assertEqual(responder.last_source, "fallback")
            self.assertNotIn("hello", log.getvalue().lower().replace("[ollama]", ""))

    def test_failure_log_does_not_leak_conversation(self):
        responder, _, _ = make_responder(REFUSED)
        with quiet() as log:
            responder(PRIVATE, "Shanta", history=((PRIVATE, "ok"),))
        self.assertIn("Unavailable", log.getvalue())
        self.assertNotIn(PRIVATE, log.getvalue())
        self.assertNotIn("Shanta", log.getvalue())

    def test_retry_cooldown_skips_network(self):
        responder, opener, clock = make_responder(REFUSED, retry_cooldown=60)
        with quiet():
            responder("hello", "Shanta")
            for _ in range(5):
                clock.now += 10
                self.assertEqual(responder("hello", "Shanta"), conv.generate_response("hello", "Shanta"))
        self.assertEqual(len(opener.requests), 1)  # only the first call touched the network
        self.assertTrue(responder.cooling_down)

    def test_recovery_after_reconnection(self):
        responder, opener, clock = make_responder(REFUSED, TIMEOUT, reply("Back online!"),
                                                  reply("Still here."), retry_cooldown=30)
        with quiet() as log:
            self.assertEqual(responder.last_source, None)
            responder("a", "S")
            clock.now += 31
            responder("b", "S")           # retry fails -> new cooldown
            self.assertEqual(responder.last_source, "fallback")
            clock.now += 5
            responder("c", "S")           # still cooling down, no request
            self.assertEqual(len(opener.requests), 2)
            clock.now += 30
            self.assertEqual(responder("d", "S"), "Back online!")
            self.assertEqual(responder("e", "S"), "Still here.")
        self.assertEqual(responder.last_source, "ollama")
        self.assertFalse(responder.cooling_down)
        self.assertEqual(log.getvalue().count("Reconnected"), 1)

    def test_bad_response_does_not_start_cooldown(self):
        responder, opener, _ = make_responder("garbage", reply("Okay!"))
        with quiet():
            responder("a", "S")
            self.assertEqual(responder("b", "S"), "Okay!")
        self.assertEqual(len(opener.requests), 2)

    def test_warm_up_runs_in_background_and_tolerates_failure(self):
        responder, opener, _ = make_responder(REFUSED)
        with quiet() as log:
            responder.warm_up("Shanta", {}).join(timeout=5)
        self.assertIn("Warm-up skipped", log.getvalue())
        self.assertFalse(responder.cooling_down)  # warm-up never changes cooldown state
        self.assertIn("talking with Shanta", opener.payload["messages"][0]["content"])

    def test_warm_up_prompt_matches_first_request_prefix(self):
        responder, opener, _ = make_responder(reply("x"), reply("Hi!"))
        ctx = {"visit_count": 2}
        with quiet():
            responder.warm_up("Shanta", ctx).join(timeout=5)
            responder("hello", "Shanta", history=(), context=ctx)
        warm, first = (r["payload"]["messages"][0] for r in opener.requests)
        self.assertEqual(warm, first)  # identical system prompt -> Ollama prefix cache hit


class Script:
    """Scripted audio stack recording event order (record/speak never overlap)."""

    def __init__(self, transcripts):
        self.events = []
        self.transcripts = list(transcripts)
        self.speaking = False

    def record(self):
        assert not self.speaking, "recording while speaking"
        self.events.append("record")
        if not self.transcripts:
            raise KeyboardInterrupt
        return "loud.wav"

    def transcribe(self, audio_file, timeout=None):
        self.events.append("transcribe")
        return self.transcripts.pop(0)

    def speak(self, text):
        self.speaking = True
        self.events.append(f"speak:{text}")
        self.speaking = False


def run_session(script, responder, **kwargs):
    session = conv.ConversationSession("Shanta", Mock(), record=script.record,
                                       transcriber=script.transcribe, speaker=script.speak,
                                       responder=responder, sleep=Mock(), silence_rms=0,
                                       emotion="happy", visit_count=2, **kwargs)
    with quiet(), patch.object(conv, "_stop_recording"):
        try:
            session.run(greeting="Hello, Shanta!")
        except KeyboardInterrupt:
            pass
    return session


class SessionIntegrationTests(unittest.TestCase):
    def test_full_session_with_ollama_passes_history_and_context(self):
        responder, opener, _ = make_responder(reply("Hey Shanta."), reply("I'm a robot, so great."),
                                              reply("Bye for now."))
        responder.warm_up = Mock()
        script = Script(["hello", "how are you", "goodbye"])
        session = run_session(script, responder)
        responder.warm_up.assert_called_once_with("Shanta", {"emotion": "happy",
                                                             "visit_count": 2})
        self.assertEqual(session.history, [("hello", "Hey Shanta."),
                                           ("how are you", "I'm a robot, so great."),
                                           ("goodbye", "Bye for now.")])
        third = opener.requests[2]["payload"]["messages"]
        self.assertEqual([m["content"] for m in third[1:]],
                         ["hello", "Hey Shanta.", "how are you", "I'm a robot, so great.",
                          "goodbye"])
        self.assertIn("visited you 2 times", third[0]["content"])
        # Strict alternation: every playback is followed by a recording.
        for i, event in enumerate(script.events):
            if event.startswith("speak"):
                self.assertEqual(script.events[i + 1], "record")

    def test_session_survives_ollama_outage_and_recovers(self):
        clock = Clock()
        responder, opener, _ = make_responder(REFUSED, reply("I'm back."), clock=clock,
                                              retry_cooldown=60)
        responder.warm_up = Mock()

        def transcribe(audio_file, timeout=None):
            clock.now += 25  # each turn takes some time
            return script.transcripts.pop(0)

        script = Script(["hello", "hi", "hi there", "are you there"])
        script.transcribe = transcribe
        session = run_session(script, responder)
        spoken = [e for e in script.events if e.startswith("speak")]
        self.assertEqual(spoken, ["speak:Hello, Shanta!",
                                  "speak:Hello Shanta! Nice to hear you.",
                                  "speak:Hello Shanta! Nice to hear you.",
                                  "speak:Hello Shanta! Nice to hear you.",
                                  "speak:I'm back."])
        self.assertEqual(len(opener.requests), 2)  # turns 2 and 3 skipped the network
        self.assertEqual(session.consecutive_errors, 0)  # outages are not audio errors
        self.assertEqual(len(session.history), 4)

    def test_legacy_two_argument_responders_still_work(self):
        calls = []

        def legacy(user_text, person_name):
            calls.append((user_text, person_name))
            return "ok"

        for responder in (legacy, lambda t, n: "ok", Mock(return_value="ok"),
                          conv.generate_response):
            session = run_session(Script(["hello"]), responder)
            self.assertEqual(len(session.history), 1)
        self.assertEqual(calls, [("hello", "Shanta")])

    def test_history_aware_custom_responder_receives_completed_turns_only(self):
        seen = []

        def responder(user_text, person_name, history=()):
            seen.append(history)
            return f"re:{user_text}"

        run_session(Script(["one", "two"]), responder)
        self.assertEqual(seen, [(), (("one", "re:one"),)])
        self.assertEqual(conv._optional_kwargs(responder), {"history"})
        self.assertEqual(conv._optional_kwargs(lambda t, n, **kw: ""), set())

    def test_history_limited_to_max_exchanges(self):
        script = Script([f"message {i}" for i in range(25)])
        session = run_session(script, lambda t, n: "ok")
        self.assertEqual(len(session.history), conv.MAX_HISTORY)
        self.assertEqual(session.history[0][0], "message 5")
        self.assertEqual(session.history[-1][0], "message 24")

    def test_failed_or_unspoken_responses_not_in_history(self):
        script = Script(["one", "two", "three"])
        script.speak = Mock(side_effect=[None, RuntimeError("aplay busy"), None, None])
        responses = iter(["", "unheard", "heard"])
        session = run_session(script, lambda t, n: next(responses))
        self.assertEqual(session.history, [("three", "heard")])
        self.assertEqual(session.consecutive_errors, 0)

    def test_responder_exception_still_counts_as_error(self):
        script = Script(["a"] * conv.MAX_CONSECUTIVE_ERRORS)
        session = conv.ConversationSession(
            "Shanta", Mock(), record=script.record, transcriber=script.transcribe,
            speaker=script.speak, responder=Mock(side_effect=RuntimeError("boom")),
            sleep=Mock(), silence_rms=0)
        with quiet(), patch.object(conv, "_stop_recording"):
            self.assertEqual(session.run(), 1)

    def test_warm_up_only_for_ollama_responder(self):
        legacy = Mock(return_value="ok")
        run_session(Script(["hello"]), legacy)
        legacy.warm_up.assert_not_called()

    def test_default_session_is_rule_based_without_env(self):
        with patch.dict("os.environ", {}, clear=True):
            session = conv.ConversationSession("Shanta", Mock())
        self.assertIs(session._respond, conv.generate_response)


if __name__ == "__main__":
    unittest.main()
