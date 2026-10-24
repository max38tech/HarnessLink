#include <assert.h>
#include "core.h"

int main(void) {
    assert(hl_pcm16(0x12340000, 16) == 0x1234);
    assert(hl_pcm16(-65536, 16) == -1);
    assert(hl_pcm16(INT32_MAX, 8) == 32767);
    assert(hl_pcm16(INT32_MIN, 8) == -32768);
    hl_button_t b = {0};
    assert(hl_button_update(&b, true, 0) == 0);
    assert(hl_button_update(&b, false, 10) == 0);
    assert(hl_button_update(&b, true, 20) == 0);
    assert(hl_button_update(&b, true, 54) == 0);
    assert(hl_button_update(&b, true, 55) == 1);
    assert(hl_button_update(&b, true, 100) == 0);
    assert(hl_button_update(&b, false, 101) == 0);
    assert(hl_button_update(&b, false, 136) == -1);
    return 0;
}
