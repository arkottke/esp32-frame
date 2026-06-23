#include "nvs_config.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "nvs_config";
#define NVS_NAMESPACE "frame_cfg"

esp_err_t nvs_config_load(frame_config_t *out)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) return err;

    size_t len;
    memset(out, 0, sizeof(*out));

#define LOAD_STR(key, field) \
    len = sizeof(out->field); \
    err = nvs_get_str(handle, key, out->field, &len); \
    if (err != ESP_OK) goto done;

    LOAD_STR("ssid",       ssid)
    LOAD_STR("password",   password)
    LOAD_STR("server_url", server_url)

    err = nvs_get_u32(handle, "poll_sec", &out->poll_seconds);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        out->poll_seconds = 600;
        err = ESP_OK;
    }

done:
    nvs_close(handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "nvs_config_load failed: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t nvs_config_save(const frame_config_t *cfg)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;

    err = nvs_set_str(handle, "ssid",       cfg->ssid);       if (err) goto done;
    err = nvs_set_str(handle, "password",   cfg->password);   if (err) goto done;
    err = nvs_set_str(handle, "server_url", cfg->server_url); if (err) goto done;
    err = nvs_set_u32(handle, "poll_sec",   cfg->poll_seconds); if (err) goto done;
    err = nvs_commit(handle);

done:
    nvs_close(handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_config_save failed: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t nvs_config_erase(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    err = nvs_erase_all(handle);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    return err;
}
