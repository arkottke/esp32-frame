#pragma once

#include "esp_err.h"

#define NVS_CONFIG_MAX_STR 128

typedef struct {
    char ssid[NVS_CONFIG_MAX_STR];
    char password[NVS_CONFIG_MAX_STR];
    char server_url[NVS_CONFIG_MAX_STR];
    uint32_t poll_seconds;
} frame_config_t;

/**
 * Load config from NVS.
 * Returns ESP_ERR_NVS_NOT_FOUND if no credentials have been saved yet.
 */
esp_err_t nvs_config_load(frame_config_t *out);

/** Save config to NVS. */
esp_err_t nvs_config_save(const frame_config_t *cfg);

/** Erase all frame config from NVS (used to trigger re-provisioning). */
esp_err_t nvs_config_erase(void);
