"""Opt-in single-pulse bench test. Default command only queries status."""
import argparse
import json
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port")
    parser.add_argument("--move", choices=["forward", "backward", "left", "right"])
    parser.add_argument("--wheels-lifted", action="store_true")
    parser.add_argument("--verify", choices=["stop", "estop", "watchdog"],
                        help="run one supervised 1000 ms safety exercise")
    parser.add_argument("--speed", type=int, metavar="1-40", default=25)
    parser.add_argument("--duration-ms", type=int, metavar="1-1000", default=300)
    args = parser.parse_args()
    if not 1 <= args.speed <= 40 or not 1 <= args.duration_ms <= 1000:
        parser.error("--speed must be 1-40 and --duration-ms must be 1-1000")
    if args.verify and not args.move:
        parser.error("--verify requires a direction via --move")
    if args.move and not args.wheels_lifted:
        parser.error("--move requires --wheels-lifted and a reachable motor power disconnect")

    import serial

    with serial.Serial(args.port, 115200, timeout=0.05, write_timeout=0.25) as port:
        time.sleep(2)  # Opening some USB bridges resets the board.
        port.reset_input_buffer()
        sequence = 0

        def send(command, *, expect_error=None, **fields):
            nonlocal sequence
            sequence += 1
            payload = dict(id=sequence, cmd=command, **fields)
            port.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii"))
            deadline = time.monotonic() + 0.18
            buffer = bytearray()
            while time.monotonic() < deadline:
                chunk = port.read_until(b"\n", 512 - len(buffer))
                buffer.extend(chunk)
                if buffer.endswith(b"\n") or len(buffer) == 512:
                    try:
                        response = json.loads(buffer)
                    except (ValueError, UnicodeError):
                        buffer.clear()
                        continue
                    buffer.clear()
                    if not isinstance(response, dict) or response.get("id") != sequence:
                        continue
                    print(json.dumps(response))
                    if expect_error:
                        if response.get("ok") is not False or response.get("error") != expect_error:
                            raise RuntimeError(f"Expected {expect_error} from {command}")
                    elif response.get("ok") is not True:
                        raise RuntimeError(response.get("error", "Unknown firmware rejection"))
                    return response
            raise TimeoutError(f"No acknowledgment for {command}; do not replay movement")

        if not args.move:
            send("status")
            return
        def arm():
            hello = send("hello")
            if hello.get("protocol_version") != 1:
                raise RuntimeError("Unexpected protocol version")
            session = hello["session"]
            send("heartbeat", session=session)
            return session

        def expect_stopped(**fields):
            status = send("status")
            expected = dict(state="stopped", left_pwm=0, right_pwm=0, remaining_ms=0)
            expected.update(fields)
            if any(status.get(field) != value for field, value in expected.items()):
                raise RuntimeError(f"Unexpected stop status, expected {expected}")

        try:
            session = arm()
            duration = 1000 if args.verify else args.duration_ms
            movement = dict(direction=args.move, speed=args.speed, duration_ms=duration)
            send("move", session=session, **movement)

            if args.verify == "stop":
                send("stop")
                expect_stopped()
                send("move", session=session, expect_error="session_invalid", **movement)
                return
            if args.verify == "estop":
                send("estop")
                expect_stopped(state="estopped", estop=True)
                session = arm()
                send("move", session=session, expect_error="estop_latched", **movement)
                send("reset_safety", session=session)
                expect_stopped(estop=False)
                arm()
                expect_stopped(estop=False)
                return
            if args.verify == "watchdog":
                time.sleep(0.85)
                expect_stopped(watchdog="tripped", communication="disconnected")
                send("move", session=session, expect_error="session_invalid", **movement)
                arm()
                expect_stopped(communication="ready")
                return

            end = time.monotonic() + duration / 1000
            while time.monotonic() < end:
                time.sleep(min(0.15, max(0, end - time.monotonic())))
                send("heartbeat", session=session)
            send("status")
        finally:
            # Unconditional STOP is valid even if hello or an earlier reply was lost.
            send("stop")


if __name__ == "__main__":
    main()
