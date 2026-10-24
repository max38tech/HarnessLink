#pragma once
#include <stdbool.h>
#include <stdint.h>

#define HL_RATE 16000
#define HL_CAPTURE_SAMPLES 320
#define HL_MAX_SAMPLES (HL_RATE * 60)
#define HL_PLAY_BYTES 3200

// Portable signal/button primitives, also tested on the PC.
static inline int16_t hl_pcm16(int32_t sample, unsigned shift) {
    int32_t value = sample >> shift;
    return value > 32767 ? 32767 : value < -32768 ? -32768 : (int16_t)value;
}

typedef struct {
    bool raw, stable;
    int64_t changed_ms;
} hl_button_t;

// Returns 1 for press, -1 for release, 0 for no debounced edge.
static inline int hl_button_update(hl_button_t *b, bool pressed, int64_t now_ms) {
    if (pressed != b->raw) {
        b->raw = pressed;
        b->changed_ms = now_ms;
    }
    if (b->stable != b->raw && now_ms - b->changed_ms >= 35) {
        b->stable = b->raw;
        return b->stable ? 1 : -1;
    }
    return 0;
}
