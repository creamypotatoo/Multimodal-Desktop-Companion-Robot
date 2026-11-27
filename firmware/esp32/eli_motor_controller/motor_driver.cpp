#include <Arduino.h>
#include "config.h"
#include "motor_driver.h"

using namespace config;

const char* directionName(Direction direction) {
    switch (direction) {
        case Direction::Forward: return "forward";
        case Direction::Backward: return "backward";
        case Direction::Left: return "left";
        case Direction::Right: return "right";
        default: return "none";
    }
}

bool MotorDriver::begin() {
    pinMode(STANDBY, OUTPUT);
    digitalWrite(STANDBY, LOW);
    const uint8_t outputs[] = {LEFT_IN1, LEFT_IN2, RIGHT_IN1, RIGHT_IN2, LEFT_PWM, RIGHT_PWM};
    for (uint8_t pin : outputs) {
        pinMode(pin, OUTPUT);
        digitalWrite(pin, LOW);
    }
    leftAttached_ = ledcAttach(LEFT_PWM, PWM_HZ, PWM_BITS);
    rightAttached_ = ledcAttach(RIGHT_PWM, PWM_HZ, PWM_BITS);
    ready_ = leftAttached_ && rightAttached_;
    stop(millis());
    return ready_;
}

void MotorDriver::stop(uint32_t now) {
    digitalWrite(STANDBY, LOW);
    if (driving_) disabledAt_ = now;
    driving_ = pending_ = false;
    const bool leftOk = !leftAttached_ || ledcWrite(LEFT_PWM, 0);
    const bool rightOk = !rightAttached_ || ledcWrite(RIGHT_PWM, 0);
    ready_ = ready_ && leftOk && rightOk;
    digitalWrite(LEFT_IN1, LOW);
    digitalWrite(LEFT_IN2, LOW);
    digitalWrite(RIGHT_IN1, LOW);
    digitalWrite(RIGHT_IN2, LOW);
}

void MotorDriver::request(Direction direction, uint8_t speed, uint32_t now) {
    stop(now);
    if (!ready_) return;
    left_ = (direction == Direction::Backward || direction == Direction::Left) ? -1 : 1;
    right_ = (direction == Direction::Backward || direction == Direction::Right) ? -1 : 1;
    if (LEFT_INVERT) left_ = -left_;
    if (RIGHT_INVERT) right_ = -right_;
    duty_ = (speed * ((1u << PWM_BITS) - 1) + 50) / 100;
    pending_ = true;
    update(now);
}

void MotorDriver::update(uint32_t now) {
    if (!pending_) return;
    const bool reversed = lastLeft_ && (lastLeft_ != left_ || lastRight_ != right_);
    if (reversed && uint32_t(now - disabledAt_) < REVERSE_MS) return;
    apply();
}

void MotorDriver::apply() {
    pending_ = false;
    digitalWrite(LEFT_IN1, left_ > 0 ? HIGH : LOW);
    digitalWrite(LEFT_IN2, left_ < 0 ? HIGH : LOW);
    digitalWrite(RIGHT_IN1, right_ > 0 ? HIGH : LOW);
    digitalWrite(RIGHT_IN2, right_ < 0 ? HIGH : LOW);
    const bool leftOk = ledcWrite(LEFT_PWM, duty_);
    const bool rightOk = ledcWrite(RIGHT_PWM, duty_);
    if (!leftOk || !rightOk) {
        ready_ = false;
        stop(millis());
        return;
    }
    lastLeft_ = left_;
    lastRight_ = right_;
    driving_ = true;
    digitalWrite(STANDBY, HIGH);
}
