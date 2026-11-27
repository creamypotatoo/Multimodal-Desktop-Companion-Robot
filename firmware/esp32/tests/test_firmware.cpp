#include "Arduino.h"
#include "config.h"
#include <assert.h>
#include <iostream>

void setup();
void loop();
FakeSerial Serial;
static uint32_t now = UINT32_MAX - 100;
static int pins[40]{};
static uint32_t pwm[40]{};
static bool failAttach = false, failWrite = false;
static unsigned enables = 0;

uint32_t millis() { return now; }
void pinMode(uint8_t, int) {}
void digitalWrite(uint8_t pin, int value) {
    if (pin == config::STANDBY && value == HIGH) {
        assert(pwm[config::LEFT_PWM] && pwm[config::RIGHT_PWM]);
        ++enables;
    }
    if (pin == config::LEFT_IN1 || pin == config::LEFT_IN2
        || pin == config::RIGHT_IN1 || pin == config::RIGHT_IN2) {
        assert(pins[config::STANDBY] == LOW);
    }
    pins[pin] = value;
}
bool ledcAttach(uint8_t pin, uint32_t frequency, uint8_t resolution) {
    assert(pin == config::LEFT_PWM || pin == config::RIGHT_PWM);
    assert(frequency == 20000 && resolution == 8);
    return !failAttach || pin != config::RIGHT_PWM;
}
bool ledcWrite(uint8_t pin, uint32_t duty) {
    assert(pins[config::STANDBY] == LOW);
    if (failWrite && duty) return false;
    pwm[pin] = duty;
    return true;
}
uint32_t esp_random() { static uint32_t nonce = 1; return nonce++; }

static void polls() {
    for (unsigned i = 0; i < 100; ++i) loop();
}
static std::string send(const std::string& input) {
    Serial.output.clear();
    Serial.input += input;
    polls();
    return Serial.output;
}
static void has(const std::string& text, const char* substring) {
    if (text.find(substring) == std::string::npos) {
        std::cerr << "Expected " << substring << " in " << text << '\n';
        assert(false);
    }
}
static std::string basic(unsigned id, const char* cmd) {
    return "{\"id\":" + std::to_string(id) + ",\"cmd\":\"" + cmd + "\"}\n";
}
static std::string link(unsigned id) {
    const auto reply = send(basic(id, "hello"));
    has(reply, "\"protocol_version\":1");
    const auto start = reply.find("\"session\":\"");
    assert(start != std::string::npos);
    return reply.substr(start + 11, 16);
}
static std::string sessionCommand(unsigned id, const char* cmd, const std::string& session) {
    auto message = basic(id, cmd);
    return message.substr(0, message.size() - 2) + ",\"session\":\"" + session + "\"}\n";
}
static std::string drive(unsigned id, const std::string& session, const char* direction,
                         unsigned speed = 40, unsigned duration = 500) {
    auto message = sessionCommand(id, "move", session);
    return message.substr(0, message.size() - 2) + ",\"direction\":\"" + direction +
        "\",\"speed\":" + std::to_string(speed) + ",\"duration_ms\":" + std::to_string(duration) + "}\n";
}
static void disabled() {
    assert(pins[config::STANDBY] == LOW);
    assert(!pwm[config::LEFT_PWM] && !pwm[config::RIGHT_PWM]);
}
static void direction(bool left, bool right, unsigned duty) {
    assert(pins[config::STANDBY] == HIGH);
    assert(pins[config::LEFT_IN1] == left && pins[config::LEFT_IN2] == !left);
    assert(pins[config::RIGHT_IN1] == right && pins[config::RIGHT_IN2] == !right);
    assert(pwm[config::LEFT_PWM] == duty && pwm[config::RIGHT_PWM] == duty);
}

int main(int argc, char**) {
    failAttach = argc > 1;
    setup();
    assert(Serial.baud == 115200);
    disabled();
    auto status = send(basic(1, "status"));
    has(status, "\"cliff\":\"unavailable\"");
    has(status, "\"obstacle\":\"unavailable\"");
    if (failAttach) {
        has(status, "\"last_error\":\"pwm_fault\"");
        const auto session = link(2);
        send(sessionCommand(1, "heartbeat", session));
        has(send(drive(2, session, "forward")), "pwm_fault");
        disabled();
        std::cout << "PWM attach failure passed\n";
        return 0;
    }

    const auto old = link(2);
    has(send(drive(1, old, "forward")), "heartbeat_required");
    has(send(sessionCommand(2, "heartbeat", old)), "\"ok\":true");
    disabled();
    has(send(drive(3, old, "forward", 20, 100)), "\"state\":\"moving\"");
    direction(true, true, 51);
    now += 80;
    has(send(drive(3, old, "forward", 40, 999)), "duplicate_id");
    now += 20;  // rollover
    polls();
    disabled();
    has(send(basic(30, "status")), "\"uptime_ms\":100");
    has(send(drive(4, old, "backward")), "\"state\":\"reversing\"");
    disabled();
    now += 19;
    polls();
    disabled();
    now += 1;
    polls();
    direction(false, false, 102);
    has(send(drive(5, old, "left")), "\"state\":\"reversing\"");
    now += 20;
    polls();
    direction(false, true, 102);
    has(send(drive(6, old, "right")), "\"state\":\"reversing\"");
    now += 20;
    polls();
    direction(true, false, 102);
    has(send(drive(7, old, "right", 41)), "invalid_speed");
    has(send(drive(8, old, "right", 0)), "invalid_speed");
    has(send(drive(9, old, "right", 40, 0)), "invalid_duration");
    has(send(drive(10, old, "right", 40, 1001)), "invalid_duration");

    const unsigned beforeStop = enables;
    has(send(drive(11, old, "forward") + basic(12, "stop") + drive(13, old, "forward")),
        "\"id\":12,\"ok\":true");
    disabled();
    assert(enables == beforeStop);
    has(send(sessionCommand(14, "heartbeat", old)), "session_invalid");

    auto current = link(20);
    assert(current != old);
    has(send(sessionCommand(14, "heartbeat", old)), "session_invalid");
    has(send(drive(15, old, "forward")), "session_invalid");
    disabled();
    send(sessionCommand(1, "heartbeat", current));
    send(drive(2, current, "right"));
    direction(true, false, 102);
    has(send(basic(1, "estop")), "\"state\":\"estopped\"");
    disabled();
    current = link(21);
    send(sessionCommand(1, "heartbeat", current));
    has(send(drive(2, current, "forward")), "estop_latched");
    has(send(sessionCommand(3, "reset_safety", current) + drive(4, current, "forward")),
        "\"id\":3,\"ok\":true,\"state\":\"stopped\"");
    disabled();

    const auto timed = link(22);
    send(sessionCommand(1, "heartbeat", timed));
    send(drive(2, timed, "right", 40, 1000));
    now += 749;
    has(send(sessionCommand(1, "heartbeat", timed)), "duplicate_id");
    direction(true, false, 102);
    now += 1;
    has(send(basic(1, "status")), "\"watchdog\":\"tripped\"");
    disabled();
    has(send(sessionCommand(3, "heartbeat", timed)), "session_invalid");
    has(send(drive(4, timed, "forward")), "session_invalid");

    current = link(23);
    send(sessionCommand(1, "heartbeat", current));
    send(drive(2, current, "right"));
    has(send("{\"id\":31,\"cmd\":\n"), "\"id\":31,\"ok\":false");
    disabled();
    current = link(24);
    send(sessionCommand(1, "heartbeat", current));
    send(drive(2, current, "right"));
    has(send(std::string(193, 'x') + "\n"), "line_too_long");
    disabled();
    current = link(25);
    send(sessionCommand(1, "heartbeat", current));
    send(drive(2, current, "right"));
    send("{");
    now += 100;
    polls();
    has(Serial.output, "frame_timeout");
    disabled();

    send("\n");
    current = link(26);
    send(sessionCommand(1, "heartbeat", current));
    send(drive(2, current, "right", 40, 500));
    now += 400;
    send(sessionCommand(3, "heartbeat", current));
    now += 100;
    polls();
    disabled();  // Heartbeat never extended the move.

    send(drive(4, current, "right"));
    Serial.space = 0;
    send(basic(11, "status") + basic(12, "status") + basic(13, "status")
         + basic(14, "status") + basic(15, "estop"));
    disabled();
    Serial.space = 17;
    polls();
    has(send(basic(16, "status")), "\"estop\":true");

    current = link(27);
    send(sessionCommand(1, "heartbeat", current));
    send(sessionCommand(2, "reset_safety", current));
    now += 20;
    current = link(28);
    send(sessionCommand(1, "heartbeat", current));
    failWrite = true;
    has(send(drive(2, current, "right")), "pwm_fault");
    disabled();
    std::cout << "Serial safety, timing, rollover, directions, backpressure and PWM tests passed\n";
}
