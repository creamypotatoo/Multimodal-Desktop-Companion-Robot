#include <Arduino.h>
#include <esp_random.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "serial_protocol.h"

namespace {
enum Field : uint8_t { Id, Cmd, DirectionField, Speed, Duration, Session, FieldCount };
constexpr unsigned fieldBit(Field field) { return 1u << field; }
constexpr const char* keys[] = {"id", "cmd", "direction", "speed", "duration_ms", "session"};
constexpr uint32_t MAX_ID = 2147483647u;
constexpr uint32_t FRAME_MS = 100;

struct Message {
    unsigned fields = 0;
    uint32_t id = 0, speed = 0, duration = 0;
    char command[17]{}, direction[17]{}, session[17]{};
};

void whitespace(const char*& p) {
    while (*p == ' ' || *p == '\t' || *p == '\r') ++p;
}

bool consume(const char*& p, char token) {
    whitespace(p);
    if (*p != token) return false;
    ++p;
    return true;
}

bool word(const char*& p, char (&out)[17]) {
    if (!consume(p, '"')) return false;
    unsigned size = 0;
    while (*p && *p != '"') {
        const char c = *p++;
        if (size == 16 || !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) {
            return false;
        }
        out[size++] = c;
    }
    if (!size || *p != '"') return false;
    out[size] = '\0';
    ++p;
    return true;
}

bool integer(const char*& p, uint32_t& out) {
    whitespace(p);
    if (*p < '0' || *p > '9') return false;
    if (*p == '0' && p[1] >= '0' && p[1] <= '9') return false;
    uint32_t value = 0;
    while (*p >= '0' && *p <= '9') {
        const unsigned digit = *p++ - '0';
        if (value > (MAX_ID - digit) / 10u) return false;
        value = value * 10u + digit;
    }
    out = value;
    return true;
}

bool parse(const char* p, Message& msg) {
    if (!consume(p, '{')) return false;
    do {
        char key[17];
        if (!word(p, key) || !consume(p, ':')) return false;
        unsigned index = 0;
        while (index < FieldCount && strcmp(keys[index], key)) ++index;
        if (index == FieldCount || (msg.fields & (1u << index))) return false;
        msg.fields |= 1u << index;
        switch (index) {
            case Id: if (!integer(p, msg.id)) return false; break;
            case Speed: if (!integer(p, msg.speed)) return false; break;
            case Duration: if (!integer(p, msg.duration)) return false; break;
            case Cmd: if (!word(p, msg.command)) return false; break;
            case DirectionField: if (!word(p, msg.direction)) return false; break;
            case Session: if (!word(p, msg.session)) return false; break;
        }
    } while (consume(p, ','));
    if (!consume(p, '}')) return false;
    whitespace(p);
    return !*p && msg.id && (msg.fields & fieldBit(Cmd));
}

Direction decodeDirection(const char* name) {
    const Direction directions[] = {Direction::Forward, Direction::Backward,
                                    Direction::Left, Direction::Right};
    for (Direction direction : directions) {
        if (!strcmp(name, directionName(direction))) return direction;
    }
    return Direction::None;
}

bool hasSession(const Message& msg) {
    if (strlen(msg.session) != 16) return false;
    for (const char* c = msg.session; *c; ++c) {
        if (!((*c >= '0' && *c <= '9') || (*c >= 'a' && *c <= 'f'))) return false;
    }
    return true;
}
} // namespace

void SerialProtocol::poll() {
    safety_.update(millis());
    if (!safety_.linked()) cancelMove("session_invalid");
    if (length_ && uint32_t(millis() - lineStartedAt_) >= FRAME_MS) {
        rejectFrame(millis(), "frame_timeout");
        length_ = 0;
        discard_ = true;
    }
    for (unsigned count = 0; count < config::RX_BUDGET && Serial.available(); ++count) {
        const uint32_t now = millis();
        safety_.update(now);
        const int byte = Serial.read();
        if (byte < 0) break;
        if (byte == '\n') {
            if (!discard_) {
                line_[length_] = '\0';
                handleLine(now);
            }
            length_ = 0;
            discard_ = false;
        } else if (!discard_) {
            if (!length_) lineStartedAt_ = now;
            if (length_ == config::MAX_LINE || byte == 0) {
                rejectFrame(now, byte == 0 ? "malformed" : "line_too_long");
                length_ = 0;
                discard_ = true;
            } else {
                line_[length_++] = static_cast<char>(byte);
            }
        }
    }
    const uint32_t now = millis();
    safety_.update(now);
    if (!safety_.linked()) cancelMove("session_invalid");
    if (pending_.id && uint32_t(now - pending_.receivedAt) >= FRAME_MS) {
        rejectFrame(now, "rx_backlog");
    }
    if (pending_.id && !Serial.available() && !length_ && !discard_ &&
        txCount_ < sizeof(replies_) / sizeof(replies_[0])) {
        const PendingMove request = pending_;
        pending_ = {};
        const char* error = safety_.move(request.direction, request.speed, request.duration, now);
        if (error) safety_.recordError(error);
        reply(request.id, error);
    }
    safety_.update(now, !Serial.available() && !length_ && !discard_);
    writeReplies();
}

void SerialProtocol::rejectFrame(uint32_t now, const char* error, uint32_t id) {
    safety_.malformed(now, error);
    cancelMove("session_invalid");
    reply(id, error);
}

void SerialProtocol::cancelMove(const char* error) {
    if (!pending_.id) return;
    reply(pending_.id, error);
    pending_ = {};
}

void SerialProtocol::handleLine(uint32_t now) {
    Message msg;
    if (!parse(line_, msg)) {
        rejectFrame(now, "malformed", msg.id);
        return;
    }
    const char* cmd = msg.command;
    constexpr unsigned basic = fieldBit(Id) | fieldBit(Cmd);
    constexpr unsigned sessionFields = basic | fieldBit(Session);

    if (!strcmp(cmd, "stop") || !strcmp(cmd, "estop")) {
        // Honor a recognizable stop even if it has extra recognized fields.
        if (!strcmp(cmd, "estop")) safety_.emergencyStop(now);
        else safety_.halt(now);
        const char* error = msg.fields == basic ? nullptr : "invalid_fields";
        if (error) safety_.recordError(error);
        reply(msg.id, error);
        cancelMove("session_invalid");
        return;
    }
    if (!strcmp(cmd, "status") || !strcmp(cmd, "hello")) {
        if (msg.fields != basic) {
            rejectFrame(now, "invalid_fields", msg.id);
        } else if (!strcmp(cmd, "status")) {
            reply(msg.id, nullptr, true);
        } else if (safety_.linked() && msg.id == helloId_) {
            safety_.recordError("duplicate_id");
            reply(msg.id, "duplicate_id");
        } else {
            safety_.handshake(now);
            cancelMove("session_invalid");
            if (!nonceInitialized_) {
                const uint32_t high = esp_random(), low = esp_random();
                nonce_ = (uint64_t(high) << 32) | low;
                nonceInitialized_ = true;
            }
            ++nonce_;
            snprintf(session_, sizeof(session_), "%08lx%08lx",
                     static_cast<unsigned long>(nonce_ >> 32),
                     static_cast<unsigned long>(uint32_t(nonce_)));
            highId_ = 0;
            helloId_ = msg.id;
            reply(msg.id, nullptr, false, true);
        }
        return;
    }

    const bool move = !strcmp(cmd, "move");
    const bool heartbeat = !strcmp(cmd, "heartbeat");
    const bool reset = !strcmp(cmd, "reset_safety");
    const unsigned expected = move ? sessionFields | fieldBit(DirectionField) | fieldBit(Speed) | fieldBit(Duration)
                                   : sessionFields;
    const char* error = nullptr;
    if (!move && !heartbeat && !reset) error = "unsupported_command";
    else if (msg.fields != expected || !hasSession(msg)) error = "invalid_fields";
    else if (!safety_.linked() || strcmp(msg.session, session_)) error = "session_invalid";
    else if (msg.id <= highId_) error = "duplicate_id";
    else {
        highId_ = msg.id;
        if (heartbeat) {
            safety_.heartbeat(now);
        } else if (!safety_.driverReady()) {
            error = "pwm_fault";
        } else if (!safety_.heartbeatFresh()) {
            error = "heartbeat_required";
        } else if (reset) {
            safety_.reset(now);
            cancelMove("session_invalid");
            if (!safety_.driverReady()) error = "pwm_fault";
        } else if (safety_.estopped()) {
            error = "estop_latched";
        } else {
            const Direction direction = decodeDirection(msg.direction);
            if (direction == Direction::None) error = "invalid_direction";
            else if (!msg.speed || msg.speed > config::MAX_SPEED) error = "invalid_speed";
            else if (!msg.duration || msg.duration > config::MAX_MOVE_MS) error = "invalid_duration";
            else if (pending_.id) error = "rx_busy";
            else {
                pending_ = {msg.id, msg.duration, now, static_cast<uint8_t>(msg.speed), direction};
                return;
            }
        }
    }
    if (error) safety_.recordError(error);
    reply(msg.id, error);
}

void SerialProtocol::reply(uint32_t id, const char* error, bool detailed, bool hello) {
    if (txCount_ == sizeof(replies_) / sizeof(replies_[0])) return;
    Reply& slot = replies_[(txHead_ + txCount_) % (sizeof(replies_) / sizeof(replies_[0]))];
    char jsonId[12] = "null";
    if (id) snprintf(jsonId, sizeof(jsonId), "%lu", static_cast<unsigned long>(id));
    const char* state = safety_.state();
    int size;
    if (error) {
        size = snprintf(slot.bytes, sizeof(slot.bytes),
            "{\"id\":%s,\"ok\":false,\"state\":\"%s\",\"error\":\"%s\"}\n", jsonId, state, error);
    } else if (hello) {
        size = snprintf(slot.bytes, sizeof(slot.bytes),
            "{\"id\":%s,\"ok\":true,\"state\":\"%s\",\"protocol_version\":%u,"
            "\"firmware_version\":\"%s\",\"session\":\"%s\"}\n",
            jsonId, state, config::PROTOCOL_VERSION, config::FIRMWARE_VERSION, session_);
    } else if (detailed) {
        const char* communication = !safety_.linked() ? "disconnected" :
            safety_.heartbeatFresh() ? "ready" : "awaiting_heartbeat";
        const char* watchdog = safety_.watchdogTripped() ? "tripped" :
            safety_.linked() ? "armed" : "disarmed";
        const char* last = safety_.lastError();
        size = snprintf(slot.bytes, sizeof(slot.bytes),
            "{\"id\":%s,\"ok\":true,\"state\":\"%s\",\"protocol_version\":%u,"
            "\"firmware_version\":\"%s\",\"uptime_ms\":%" PRIu64 ",\"communication\":\"%s\","
            "\"direction\":\"%s\",\"left_pwm\":%u,\"right_pwm\":%u,\"remaining_ms\":%lu,"
            "\"estop\":%s,\"watchdog\":\"%s\",\"driver_ready\":%s,\"last_error\":%s%s%s,"
            "\"sensors\":{\"cliff\":\"unavailable\",\"obstacle\":\"unavailable\"}}\n",
            jsonId, state, config::PROTOCOL_VERSION, config::FIRMWARE_VERSION,
            safety_.uptime(),
            communication, directionName(safety_.direction()), unsigned(safety_.duty()),
            unsigned(safety_.duty()), static_cast<unsigned long>(safety_.remaining(millis())),
            safety_.estopped() ? "true" : "false",
            watchdog, safety_.driverReady() ? "true" : "false", last ? "\"" : "",
            last ? last : "null", last ? "\"" : "");
    } else {
        size = snprintf(slot.bytes, sizeof(slot.bytes),
            "{\"id\":%s,\"ok\":true,\"state\":\"%s\"}\n", jsonId, state);
    }
    if (size > 0 && unsigned(size) < sizeof(slot.bytes)) {
        slot.used = static_cast<unsigned>(size);
        ++txCount_;
    }
}

void SerialProtocol::writeReplies() {
    if (!txCount_) return;
    Reply& slot = replies_[txHead_];
    const int space = Serial.availableForWrite();
    if (space <= 0) return;
    const unsigned remaining = slot.used - txOffset_;
    const unsigned chunk = remaining < unsigned(space) ? remaining : unsigned(space);
    txOffset_ += Serial.write(reinterpret_cast<const uint8_t*>(slot.bytes + txOffset_), chunk);
    if (txOffset_ == slot.used) {
        txOffset_ = 0;
        txHead_ = (txHead_ + 1) % (sizeof(replies_) / sizeof(replies_[0]));
        --txCount_;
    }
}
