"""Bench CLI speed policy with a simulated USB serial port."""
import contextlib
import io
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bench_serial


class BenchSpeedTests(unittest.TestCase):
    def run_helper(self, options, *, reject_move=False):
        requests = []

        def write(payload):
            request = json.loads(payload)
            requests.append(request)

        def read_until(*args):
            request = requests[-1]
            reply = dict(id=request["id"], ok=True)
            if request["cmd"] == "hello":
                reply.update(protocol_version=1, session="0123456789abcdef")
            if request["cmd"] == "move" and reject_move:
                reply.update(ok=False, error="simulated rejection")
            return (json.dumps(reply) + "\n").encode("ascii")

        port = SimpleNamespace(reset_input_buffer=lambda: None, write=write, read_until=read_until)
        serial = SimpleNamespace(Serial=lambda *args, **kwargs: contextlib.nullcontext(port))
        with patch.dict(sys.modules, {"serial": serial}), patch.object(
            sys, "argv", ["bench_serial.py", "FAKE", *options]
        ), patch.object(bench_serial.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            if reject_move:
                with self.assertRaisesRegex(RuntimeError, "simulated rejection"):
                    bench_serial.main()
            else:
                bench_serial.main()
        return requests

    def test_read_only_default(self):
        self.assertEqual([request["cmd"] for request in self.run_helper([])], ["status"])

    def test_movement_speed_selection(self):
        moving = ["--move", "forward", "--wheels-lifted"]
        cases = [([], 80), (["--speed", "1"], 1), (["--speed", "100"], 100)]
        for exercise in ("stop", "estop", "watchdog"):
            cases.extend([
                (["--verify", exercise], 25),
                (["--verify", exercise, "--speed", "100"], 100),
            ])
        for options, speed in cases:
            with self.subTest(options=options):
                requests = self.run_helper([*moving, *options], reject_move=True)
                self.assertEqual(requests[2]["cmd"], "move")
                self.assertEqual(requests[2]["speed"], speed)
                self.assertEqual(requests[-1]["cmd"], "stop")

    def test_invalid_speeds_and_missing_opt_in(self):
        for options in (["--speed", "0"], ["--speed", "101"], ["--speed", "1.5"],
                        ["--move", "forward"]):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.run_helper(options)
                self.assertEqual(error.exception.code, 2)
