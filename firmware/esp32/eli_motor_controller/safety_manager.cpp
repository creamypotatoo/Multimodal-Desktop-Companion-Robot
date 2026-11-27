#include "config.h"
#include "safety_manager.h"

void SafetyManager::begin(uint32_t now) {
    lastTick_ = now;
    if (!motors_.begin()) error_ = "pwm_fault";
}

void SafetyManager::update(uint32_t now, bool allowDrive) {
    uptime_ += uint32_t(now - lastTick_);
    lastTick_ = now;
    if (linked_ && uint32_t(now - seenAt_) >= config::WATCHDOG_MS) {
        halt(now);
        watchdogTripped_ = true;
        if (motors_.ready()) error_ = "watchdog_timeout";
    }
    if (duration_ && uint32_t(now - moveAt_) >= duration_) {
        motors_.stop(now);
        duration_ = 0;
        direction_ = Direction::None;
    }
    if (allowDrive) motors_.update(now);
    if (!motors_.ready() && duration_) halt(now);
    if (!motors_.ready()) error_ = "pwm_fault";
}

void SafetyManager::halt(uint32_t now) {
    motors_.stop(now);
    linked_ = fresh_ = false;
    duration_ = 0;
    direction_ = Direction::None;
    if (!motors_.ready()) error_ = "pwm_fault";
}

void SafetyManager::handshake(uint32_t now) {
    halt(now);
    linked_ = true;
    seenAt_ = now;
}

void SafetyManager::heartbeat(uint32_t now) {
    fresh_ = true;
    seenAt_ = now;
    watchdogTripped_ = false;
}

void SafetyManager::emergencyStop(uint32_t now) {
    halt(now);
    estop_ = true;
}

void SafetyManager::reset(uint32_t now) {
    halt(now);
    if (motors_.ready()) {
        estop_ = false;
        watchdogTripped_ = false;
        error_ = nullptr;
    }
}

void SafetyManager::malformed(uint32_t now, const char* error) {
    halt(now);
    if (motors_.ready()) error_ = error;
}

const char* SafetyManager::move(Direction direction, uint8_t speed, uint32_t duration, uint32_t now) {
    if (!motors_.ready()) return "pwm_fault";
    if (estop_) return "estop_latched";
    if (!heartbeatFresh()) return "heartbeat_required";
    motors_.request(direction, speed, now);
    if (!motors_.ready()) {
        halt(now);
        return "pwm_fault";
    }
    moveAt_ = now;
    duration_ = duration;
    direction_ = direction;
    return nullptr;
}

const char* SafetyManager::state() const {
    if (!motors_.ready()) return "fault";
    if (estop_) return "estopped";
    if (!duration_) return "stopped";
    return motors_.driving() ? "moving" : "reversing";
}

uint32_t SafetyManager::remaining(uint32_t now) const {
    const uint32_t elapsed = now - moveAt_;
    return elapsed < duration_ ? duration_ - elapsed : 0;
}
