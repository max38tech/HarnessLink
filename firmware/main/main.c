#include <stdatomic.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sdkconfig.h"
#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "led_strip.h"
#include "nvs_flash.h"
#include "hardware.h"

typedef enum { OFFLINE, READY, WAIT_RECORD, RECORDING, WORKING, SPEAKING, APPROVAL } state_t;
typedef struct {
    int kind; // 1 connected, 2 JSON, 3 binary
    int len;
    uint8_t data[HL_PLAY_BYTES + 1];
} message_t;

static hl_config_t config;
static QueueHandle_t incoming;
static esp_websocket_client_handle_t ws;
static led_strip_handle_t led;
static atomic_bool transport_fault;
static bool online, capturing, waiting_record, playing;
static uint32_t record_id, play_id, play_seq, capture_samples, playback_bytes, expected_playback_bytes;
static state_t state = OFFLINE;
static int64_t record_ms, error_until;

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static void show_led(void) {
    static int previous = -1;
    int display = now_ms() < error_until ? 100 : state;
    if (display == previous) return;
    previous = display;
    uint32_t r = 0, g = 0, b = 0;
    switch (display) {
        case READY: g = 7; break;
        case WAIT_RECORD: r = 7; b = 7; break;
        case RECORDING: r = 12; break;
        case WORKING: r = 7; g = 4; break;
        case SPEAKING: b = 10; break;
        case APPROVAL: r = 7; g = 7; b = 7; break;
        case 100: r = 15; break;
        default: r = 2; b = 3; break;
    }
    ESP_ERROR_CHECK(led_strip_set_pixel(led, 0, r, g, b));
    ESP_ERROR_CHECK(led_strip_refresh(led));
}

static bool send_json(cJSON *obj) {
    char *text = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (!text) { atomic_store(&transport_fault, true); return false; }
    int len = (int)strlen(text);
    bool ok = esp_websocket_client_send_text(ws, text, len, pdMS_TO_TICKS(500)) == len;
    free(text);
    if (!ok) atomic_store(&transport_fault, true);
    return ok;
}

static void send_control(const char *type, uint32_t id, int seq) {
    cJSON *obj = cJSON_CreateObject();
    if (!obj) { atomic_store(&transport_fault, true); return; }
    cJSON_AddStringToObject(obj, "type", type);
    cJSON_AddNumberToObject(obj, "id", id);
    if (seq >= 0) cJSON_AddNumberToObject(obj, "seq", seq);
    send_json(obj);
}

static void hello(void) {
    cJSON *obj = cJSON_CreateObject();
    if (!obj) { atomic_store(&transport_fault, true); return; }
    cJSON_AddStringToObject(obj, "type", "hello");
    cJSON_AddNumberToObject(obj, "version", 1);
    cJSON_AddStringToObject(obj, "token", config.token);
    send_json(obj);
}

static void ws_event(void *arg, esp_event_base_t base, int32_t event, void *event_data) {
    (void)arg; (void)base;
    // Owned exclusively by the websocket task. Main receives copied, complete messages.
    static message_t assembly;
    if (event == WEBSOCKET_EVENT_CONNECTED) {
        message_t connected = {.kind = 1};
        if (xQueueSend(incoming, &connected, 0) != pdTRUE) atomic_store(&transport_fault, true);
    } else if (event == WEBSOCKET_EVENT_DISCONNECTED || event == WEBSOCKET_EVENT_ERROR
               || event == WEBSOCKET_EVENT_CLOSED) {
        atomic_store(&transport_fault, true);
    } else if (event == WEBSOCKET_EVENT_DATA) {
        esp_websocket_event_data_t *data = event_data;
        if (data->op_code != 1 && data->op_code != 2) {
            // Library handles ping/pong. Bridge always sends unfragmented data frames.
            if (data->op_code == 0) atomic_store(&transport_fault, true);
            return;
        }
        if (!data->fin || data->payload_len < 0 || data->payload_len > HL_PLAY_BYTES
                || data->payload_offset < 0 || data->data_len < 0
                || data->payload_offset + data->data_len > data->payload_len) {
            atomic_store(&transport_fault, true);
            return;
        }
        if (data->payload_offset == 0) {
            assembly.kind = data->op_code == 1 ? 2 : 3;
            assembly.len = 0;
        }
        if (data->payload_offset != assembly.len) {
            atomic_store(&transport_fault, true);
            return;
        }
        memcpy(assembly.data + assembly.len, data->data_ptr, data->data_len);
        assembly.len += data->data_len;
        if (assembly.len == data->payload_len) {
            assembly.data[assembly.len] = 0;
            if (xQueueSend(incoming, &assembly, 0) != pdTRUE) atomic_store(&transport_fault, true);
        }
    }
}

static bool number(cJSON *obj, const char *name, uint32_t *result) {
    cJSON *value = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!cJSON_IsNumber(value) || value->valuedouble < 0 || value->valuedouble > UINT32_MAX) return false;
    *result = (uint32_t)value->valuedouble;
    return value->valuedouble == *result;
}

static void end_recording(void) {
    capturing = false;
    waiting_record = false;
    state = WORKING;
    send_control("record_end", record_id, -1);
}

static void control(const char *text, bool held) {
    cJSON *obj = cJSON_Parse(text);
    if (!obj) { atomic_store(&transport_fault, true); return; }
    cJSON *type = cJSON_GetObjectItemCaseSensitive(obj, "type");
    if (!cJSON_IsString(type)) { cJSON_Delete(obj); atomic_store(&transport_fault, true); return; }
    const char *kind = type->valuestring;
    uint32_t id = 0;
    bool has_id = number(obj, "id", &id);
    if (!strcmp(kind, "ready")) {
        online = true;
        if (!capturing && !waiting_record && !playing) state = READY;
    } else if (!strcmp(kind, "recording") && has_id && id == record_id && waiting_record) {
        // TX can delay idle RX draining; discard any pre-press or speaker-era samples.
        hl_audio_reset_input();
        waiting_record = false;
        capturing = true;
        capture_samples = 0;
        record_ms = now_ms();
        state = RECORDING;
        if (!held) end_recording();
    } else if (!strcmp(kind, "status")) {
        cJSON *s = cJSON_GetObjectItemCaseSensitive(obj, "state");
        if (!capturing && !waiting_record && cJSON_IsString(s))
            state = !strcmp(s->valuestring, "approval") ? APPROVAL : WORKING;
    } else if (!strcmp(kind, "error")) {
        capturing = waiting_record = playing = false;
        hl_audio_reset_output();
        state = online ? READY : OFFLINE;
        error_until = now_ms() + 1500;
        puts("HARNESSLINK_BRIDGE_ERROR: inspect PC terminal");
    } else if (!strcmp(kind, "play_start") && !capturing && !waiting_record) {
        uint32_t rate, bytes;
        if (!has_id || !number(obj, "sample_rate", &rate) || rate != HL_RATE
                || !number(obj, "bytes", &bytes) || bytes % 2 || bytes > HL_RATE * 2 * 180) {
            atomic_store(&transport_fault, true);
        } else {
            hl_audio_reset_output();
            playing = true;
            play_id = id;
            play_seq = playback_bytes = 0;
            expected_playback_bytes = bytes;
            state = SPEAKING;
        }
    } else if (!strcmp(kind, "play_cancel")) {
        playing = false;
        hl_audio_reset_output();
        if (!capturing && !waiting_record) state = READY;
    } else if (!strcmp(kind, "play_end") && playing && has_id && id == play_id) {
        if (playback_bytes != expected_playback_bytes) {
            atomic_store(&transport_fault, true);
        } else {
            hl_audio_drain();
            playing = false;
            send_control("play_done", play_id, -1);
            state = READY;
        }
    }
    cJSON_Delete(obj);
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t event, void *data) {
    (void)arg; (void)base; (void)data;
    if (event == WIFI_EVENT_STA_START || event == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    incoming = xQueueCreate(6, sizeof(message_t));
    assert(incoming);
    led_strip_config_t led_cfg = {.strip_gpio_num = CONFIG_HL_LED_GPIO, .max_leds = 1};
    led_strip_rmt_config_t rmt_cfg = {.resolution_hz = 10000000};
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&led_cfg, &rmt_cfg, &led));
    show_led();
    BaseType_t created = xTaskCreate(hl_provision_task, "provision", 8192, NULL, 4, NULL);
    assert(created == pdPASS);
    puts("HARNESSLINK_BOOT: USB configuration console ready");
    if (!hl_load_config(&config)) {
        puts("HARNESSLINK_UNCONFIGURED: run harnesslink provision");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    gpio_config_t button_cfg = {.pin_bit_mask = 1ULL << CONFIG_HL_BOOT_GPIO,
        .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    ESP_ERROR_CHECK(gpio_config(&button_cfg));
    hl_audio_init();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    assert(esp_netif_create_default_wifi_sta());
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    wifi_config_t wifi = {0};
    memcpy(wifi.sta.ssid, config.ssid, strlen(config.ssid));
    memcpy(wifi.sta.password, config.password, strlen(config.password));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    esp_websocket_client_config_t ws_cfg = {
        .uri = config.uri, .buffer_size = 4096, .task_stack = 6144,
        .reconnect_timeout_ms = 2000, .network_timeout_ms = 2000,
        .enable_close_reconnect = true,
    };
    ws = esp_websocket_client_init(&ws_cfg);
    assert(ws);
    ESP_ERROR_CHECK(esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, ws_event, NULL));
    ESP_ERROR_CHECK(esp_websocket_client_start(ws));
    hl_button_t button = {0};
    static message_t msg;
    int16_t pcm[HL_CAPTURE_SAMPLES];
    for (;;) {
        if (atomic_exchange(&transport_fault, false)) {
            online = capturing = waiting_record = playing = false;
            state = OFFLINE;
            hl_audio_reset_output();
            esp_websocket_client_stop(ws);
            xQueueReset(incoming);
            show_led();
            vTaskDelay(pdMS_TO_TICKS(1000));
            atomic_store(&transport_fault, false);
            ESP_ERROR_CHECK(esp_websocket_client_start(ws));
        }
        bool held = gpio_get_level(CONFIG_HL_BOOT_GPIO) == 0;
        int edge = hl_button_update(&button, held, now_ms());
        if (edge == 1 && online) {
            capturing = false;
            playing = false;
            waiting_record = true;
            hl_audio_reset_output();
            state = WAIT_RECORD;
            if (++record_id == 0) { atomic_store(&transport_fault, true); continue; }
            send_control("record_start", record_id, -1);
            record_ms = now_ms();
        } else if (edge == -1 && capturing) {
            end_recording();
        }
        if (xQueueReceive(incoming, &msg, 0) == pdTRUE) {
            if (msg.kind == 1) {
                online = capturing = waiting_record = playing = false;
                record_id = 0;
                state = OFFLINE;
                hello();
            } else if (msg.kind == 2) {
                control((const char *)msg.data, held);
            } else if (msg.kind == 3 && playing) {
                if (playback_bytes + msg.len > expected_playback_bytes
                        || hl_audio_play(msg.data, msg.len) != ESP_OK) {
                    atomic_store(&transport_fault, true);
                } else {
                    playback_bytes += msg.len;
                    send_control("play_ack", play_id, (int)play_seq++);
                }
            }
        }
        // Drain RX continuously, including idle, to avoid sending stale DMA audio on press.
        size_t samples = hl_audio_read(pcm);
        if (capturing && samples) {
            size_t remaining = HL_MAX_SAMPLES - capture_samples;
            if (samples > remaining) samples = remaining;
            int bytes = (int)(samples * sizeof(int16_t));
            if (bytes && esp_websocket_client_send_bin(ws, (char *)pcm, bytes, pdMS_TO_TICKS(200)) != bytes) {
                atomic_store(&transport_fault, true);
            }
            capture_samples += samples;
            if (capture_samples >= HL_MAX_SAMPLES || now_ms() - record_ms >= 60000) {
                end_recording();
                error_until = now_ms() + 1000;
            }
        }
        if (waiting_record && now_ms() - record_ms > 40000) atomic_store(&transport_fault, true);
        show_led();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}
