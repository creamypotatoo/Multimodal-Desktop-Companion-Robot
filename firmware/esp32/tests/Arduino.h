#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>

constexpr int LOW = 0, HIGH = 1, OUTPUT = 1;
uint32_t millis();
void pinMode(uint8_t pin, int mode);
void digitalWrite(uint8_t pin, int value);
bool ledcAttach(uint8_t pin, uint32_t frequency, uint8_t resolution);
bool ledcWrite(uint8_t pin, uint32_t duty);

struct FakeSerial {
    std::string input, output;
    size_t readAt = 0;
    int space = 17;
    unsigned baud = 0;
    void begin(unsigned value) { baud = value; }
    int available() const { return static_cast<int>(input.size() - readAt); }
    int read() { return static_cast<unsigned char>(input[readAt++]); }
    int availableForWrite() const { return space; }
    size_t write(const uint8_t* bytes, size_t size) {
        output.append(reinterpret_cast<const char*>(bytes), size);
        return size;
    }
};

extern FakeSerial Serial;
