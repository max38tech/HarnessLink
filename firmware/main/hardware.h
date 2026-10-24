#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "core.h"

typedef struct {
    uint32_t version;
    char ssid[33];
    char password[64];
    char uri[192];
    char token[129];
} hl_config_t;

bool hl_load_config(hl_config_t *config);
void hl_provision_task(void *arg);
void hl_audio_init(void);
void hl_audio_reset_input(void);
size_t hl_audio_read(int16_t *pcm);
esp_err_t hl_audio_play(const uint8_t *pcm, size_t bytes);
void hl_audio_reset_output(void);
void hl_audio_drain(void);
