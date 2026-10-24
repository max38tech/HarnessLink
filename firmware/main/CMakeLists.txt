idf_component_register(SRCS "main.c" "audio.c" "provision.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_i2s esp_driver_gpio esp_wifi esp_netif esp_event nvs_flash json esp_timer)
