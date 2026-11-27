#pragma once
#include "motor_driver.h"

class SafetyManager {
public:
    explicit SafetyManager(MotorDriver& motors) : motors_(motors) {}
    void begin(uint32_t now);
    void update(uint32_t now, bool allowDrive = false);
    void handshake(uint32_t now);
    void heartbeat(uint32_t now);
    void halt(uint32_t now);
    void emergencyStop(uint32_t now);
    void reset(uint32_t now);
    void malformed(uint32_t now, const char* error);
    const char* move(Direction direction, uint8_t speed, uint32_t duration, uint32_t now);
    bool linked() const { return linked_; }
    bool heartbeatFresh() const { return linked_ && fresh_; }
    bool estopped() const { return estop_; }
    bool watchdogTripped() const { return watchdogTripped_; }
    bool driverReady() const { return motors_.ready(); }
    const char* state() const;
    Direction direction() const { return direction_; }
    uint8_t duty() const { return motors_.duty(); }
    uint32_t remaining(uint32_t now) const;
    uint64_t uptime() const { return uptime_; }
    const char* lastError() const { return error_; }
    void recordError(const char* error) { error_ = error; }

private:
    MotorDriver& motors_;
    bool linked_ = false, fresh_ = false, estop_ = false, watchdogTripped_ = false;
    uint32_t seenAt_ = 0, moveAt_ = 0, duration_ = 0, lastTick_ = 0;
    uint64_t uptime_ = 0;
    Direction direction_ = Direction::None;
    const char* error_ = nullptr;
};
