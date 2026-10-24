#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "hardware.h"

bool hl_load_config(hl_config_t *config) {
    nvs_handle_t handle;
    if (nvs_open("harnesslink", NVS_READONLY, &handle) != ESP_OK) return false;
    size_t bytes = sizeof(*config);
    esp_err_t err = nvs_get_blob(handle, "config", config, &bytes);
    nvs_close(handle);
    return err == ESP_OK && bytes == sizeof(*config) && config->version == 1;
}

static bool field(cJSON *obj, const char *name, char *dest, size_t capacity, bool allow_empty) {
    cJSON *value = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!cJSON_IsString(value)) return false;
    size_t len = strlen(value->valuestring);
    if (len >= capacity || (!allow_empty && len == 0)) return false;
    memcpy(dest, value->valuestring, len + 1);
    return true;
}

static bool configure(const char *line) {
    cJSON *obj = cJSON_Parse(line);
    if (!obj) return false;
    cJSON *op = cJSON_GetObjectItemCaseSensitive(obj, "op");
    hl_config_t config = {.version = 1};
    bool valid = cJSON_IsString(op) && strcmp(op->valuestring, "configure") == 0
        && field(obj, "ssid", config.ssid, sizeof(config.ssid), false)
        && field(obj, "password", config.password, sizeof(config.password), true)
        && field(obj, "uri", config.uri, sizeof(config.uri), false)
        && field(obj, "token", config.token, sizeof(config.token), false)
        && strncmp(config.uri, "ws://", 5) == 0 && strlen(config.uri) > 5
        && strlen(config.token) >= 32;
    for (size_t i = 0; valid && config.token[i]; i++) {
        if ((unsigned char)config.token[i] < 33 || (unsigned char)config.token[i] > 126) valid = false;
    }
    for (size_t i = 0; valid && config.uri[i]; i++) {
        if ((unsigned char)config.uri[i] <= 32 || (unsigned char)config.uri[i] > 126) valid = false;
    }
    cJSON_Delete(obj);
    if (!valid) return false;
    nvs_handle_t handle;
    if (nvs_open("harnesslink", NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(handle, "config", &config, sizeof(config));
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    memset(&config, 0, sizeof(config));
    return err == ESP_OK;
}

void hl_provision_task(void *arg) {
    (void)arg;
    char line[1024];
    size_t pos = 0;
    bool overflow = false;
    for (;;) {
        int ch = getchar();
        if (ch == EOF) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (ch == '\r') continue;
        if (ch == '\n') {
            line[pos] = 0;
            if (!overflow && pos && configure(line)) {
                puts("HARNESSLINK_CONFIGURED");
                fflush(stdout);
                memset(line, 0, sizeof(line));
                vTaskDelay(pdMS_TO_TICKS(300));
                esp_restart();
            } else if (pos || overflow) {
                puts("HARNESSLINK_CONFIG_REJECTED");
            }
            memset(line, 0, sizeof(line));
            pos = 0;
            overflow = false;
        } else if (pos < sizeof(line) - 1 && !overflow) {
            line[pos++] = (char)ch;
        } else {
            overflow = true;
        }
    }
}
