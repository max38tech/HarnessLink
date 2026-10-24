#include <string.h>
#include "sdkconfig.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hardware.h"

static i2s_chan_handle_t rx, tx;

static void output_init(void) {
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    channel.dma_desc_num = 6;
    channel.dma_frame_num = 160;
    channel.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&channel, &tx, NULL));
    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HL_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = CONFIG_HL_AMP_BCLK,
            .ws = CONFIG_HL_AMP_WS, .dout = CONFIG_HL_AMP_DATA, .din = I2S_GPIO_UNUSED,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
}

static void input_init(void) {
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel.dma_desc_num = 4;
    channel.dma_frame_num = HL_CAPTURE_SAMPLES;
    ESP_ERROR_CHECK(i2s_new_channel(&channel, NULL, &rx));
    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(HL_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, .bclk = CONFIG_HL_MIC_BCLK,
            .ws = CONFIG_HL_MIC_WS, .dout = I2S_GPIO_UNUSED, .din = CONFIG_HL_MIC_DATA,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));
}

void hl_audio_init(void) {
    input_init();
    output_init();
}

void hl_audio_reset_input(void) {
    ESP_ERROR_CHECK(i2s_channel_disable(rx));
    ESP_ERROR_CHECK(i2s_del_channel(rx));
    input_init();
}

size_t hl_audio_read(int16_t *pcm) {
    int32_t raw[HL_CAPTURE_SAMPLES * 2];
    size_t bytes = 0;
    if (i2s_channel_read(rx, raw, sizeof(raw), &bytes, 30) != ESP_OK) return 0;
    size_t frames = bytes / (2 * sizeof(int32_t));
#ifdef CONFIG_HL_MIC_RIGHT
    const size_t slot = 1;
#else
    const size_t slot = 0;
#endif
    for (size_t i = 0; i < frames; i++) pcm[i] = hl_pcm16(raw[i * 2 + slot], CONFIG_HL_MIC_SHIFT);
    return frames;
}

esp_err_t hl_audio_play(const uint8_t *pcm, size_t bytes) {
    if (bytes > HL_PLAY_BYTES || bytes % 2) return ESP_ERR_INVALID_SIZE;
    int16_t stereo[HL_PLAY_BYTES];
    for (size_t i = 0; i < bytes / 2; i++) {
        // Explicit little-endian decoding avoids unaligned int16_t loads.
        int16_t sample = (int16_t)((uint16_t)pcm[i * 2] | ((uint16_t)pcm[i * 2 + 1] << 8));
        stereo[i * 2] = sample;
        stereo[i * 2 + 1] = sample;
    }
    size_t written = 0;
    esp_err_t err = i2s_channel_write(tx, stereo, bytes * 2, &written, 300);
    return err == ESP_OK && written == bytes * 2 ? ESP_OK : ESP_FAIL;
}

void hl_audio_reset_output(void) {
    // Recreate TX to discard queued speaker audio on interruption/disconnection.
    ESP_ERROR_CHECK(i2s_channel_disable(tx));
    ESP_ERROR_CHECK(i2s_del_channel(tx));
    output_init();
}

void hl_audio_drain(void) {
    // At most 60 ms of stereo samples can remain in the six DMA descriptors.
    vTaskDelay(pdMS_TO_TICKS(75));
    hl_audio_reset_output();
}
