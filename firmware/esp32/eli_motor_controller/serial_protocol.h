#pragma once
#include "config.h"
#include "safety_manager.h"

class SerialProtocol {
public:
    explicit SerialProtocol(SafetyManager& safety) : safety_(safety) {}
    void poll();

private:
    struct PendingMove {
        uint32_t id = 0, duration = 0, receivedAt = 0;
        uint8_t speed = 0;
        Direction direction = Direction::None;
    };

    void handleLine(uint32_t now);
    void rejectFrame(uint32_t now, const char* error, uint32_t id = 0);
    void cancelMove(const char* error);
    void reply(uint32_t id, const char* error = nullptr, bool detailed = false,
               bool hello = false);
    void writeReplies();

    SafetyManager& safety_;
    char line_[config::MAX_LINE + 1]{};
    unsigned length_ = 0;
    bool discard_ = false;
    uint32_t lineStartedAt_ = 0;
    char session_[17]{};
    bool nonceInitialized_ = false;
    uint64_t nonce_ = 0;
    uint32_t helloId_ = 0, highId_ = 0;
    PendingMove pending_;

    // Fixed TX slots isolate safety and RX from slow readers.
    struct Reply { char bytes[512]; unsigned used = 0; };
    Reply replies_[4]{};
    unsigned txHead_ = 0, txCount_ = 0, txOffset_ = 0;
};
