#include <Arduino.h>
#include "config.h"
#include "motor_driver.h"
#include "safety_manager.h"
#include "serial_protocol.h"

MotorDriver motors;
SafetyManager safety(motors);
SerialProtocol protocol(safety);

void setup() {
    safety.begin(millis());
    Serial.begin(config::BAUD);
}

void loop() {
    protocol.poll();
}
