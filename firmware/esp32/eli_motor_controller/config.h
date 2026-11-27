#pragma once
#include <stdint.h>

namespace config {
constexpr uint8_t LEFT_PWM = 25, LEFT_IN1 = 26, LEFT_IN2 = 27;
constexpr uint8_t RIGHT_PWM = 23, RIGHT_IN1 = 19, RIGHT_IN2 = 18;
constexpr uint8_t STANDBY = 13;
constexpr bool LEFT_INVERT = false, RIGHT_INVERT = false;
constexpr uint32_t PWM_HZ = 20000;
constexpr uint8_t PWM_BITS = 8, MAX_SPEED = 100;
constexpr uint32_t MAX_MOVE_MS = 1000, REVERSE_MS = 20, WATCHDOG_MS = 750;
constexpr uint32_t BAUD = 115200;
constexpr unsigned PROTOCOL_VERSION = 1;
constexpr char FIRMWARE_VERSION[] = "1.0.0";
constexpr unsigned MAX_LINE = 192, RX_BUDGET = 128;
static_assert(MAX_SPEED > 0 && MAX_SPEED <= 100, "Invalid speed limit");
}
