#pragma once
#include <stdint.h>

enum class Direction : uint8_t { None, Forward, Backward, Left, Right };
const char* directionName(Direction direction);

class MotorDriver {
public:
    bool begin();
    void request(Direction direction, uint8_t speed, uint32_t now);
    void stop(uint32_t now);
    void update(uint32_t now);
    bool ready() const { return ready_; }
    bool driving() const { return driving_; }
    uint8_t duty() const { return driving_ ? duty_ : 0; }

private:
    void apply();
    bool leftAttached_ = false, rightAttached_ = false;
    bool ready_ = false, driving_ = false, pending_ = false;
    int8_t lastLeft_ = 0, lastRight_ = 0, left_ = 0, right_ = 0;
    uint8_t duty_ = 0;
    uint32_t disabledAt_ = 0;
};
