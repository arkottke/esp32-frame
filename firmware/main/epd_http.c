#include "epd_http.h"

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "GDEP133C02.h"
#include "power_log.h"

static const char *TAG = "epd_http";

#define EPD_IMAGE_BYTES  960000UL   /* 1200 × 1600 × 4 bpp / 8 */
#define HTTP_TIMEOUT_MS  30000

typedef struct {
    uint8_t *buf;
    size_t   written;
    bool     overflow;
} recv_ctx_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    recv_ctx_t *ctx = (recv_ctx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        size_t remaining = EPD_IMAGE_BYTES - ctx->written;
        if ((size_t)evt->data_len > remaining) {
            ctx->overflow = true;
            ESP_LOGW(TAG, "Response larger than expected — ignoring excess bytes");
            evt->data_len = (int)remaining;
        }
        memcpy(ctx->buf + ctx->written, evt->data, evt->data_len);
        ctx->written += evt->data_len;
    }
    return ESP_OK;
}

esp_err_t epd_http_fetch_and_display(const char *server_url, const char *mac_str,
                                     const frame_soh_t *soh)
{
    /* Build URL: {server_url}/frame/{mac}/image?rssi=X&wakeup=Y&... */
    char url[768];
    int n = snprintf(url, sizeof(url),
        "%s/frame/%s/image?rssi=%d&wakeup=%s&fw_ver=%s&heap=%d",
        server_url, mac_str, soh->rssi, soh->wakeup, soh->fw_ver, soh->heap);
    if (soh->voltage_mv >= 0 && n > 0 && n < (int)sizeof(url)) {
        n += snprintf(url + n, sizeof(url) - n, "&voltage_mv=%d", soh->voltage_mv);
    }

    /* Deep-sleep cycle accounting. `slept_ms` is the sleep that just ended;
       the prev_* timings describe the last cycle that ran to completion,
       since this cycle's display phase hasn't happened yet. */
    power_log_stats_t pw;
    power_log_get(&pw);
    if (n > 0 && n < (int)sizeof(url)) {
        snprintf(url + n, sizeof(url) - n,
            "&boots=%lu&slept_ms=%lu&req_sleep_ms=%lu&prev_awake_ms=%lu"
            "&t_wifi=%lu&t_fetch=%lu&t_disp=%lu&duty=%lu"
            "&wakes_timer=%lu&wakes_btn=%lu&wakes_cold=%lu"
            "&incomplete=%lu&reset=%s",
            (unsigned long)pw.boot_count, (unsigned long)pw.slept_ms,
            (unsigned long)pw.requested_ms, (unsigned long)pw.prev_awake_ms,
            (unsigned long)pw.prev_wifi_ms, (unsigned long)pw.prev_fetch_ms,
            (unsigned long)pw.prev_display_ms, (unsigned long)pw.duty_permille,
            (unsigned long)pw.wakes_timer, (unsigned long)pw.wakes_button,
            (unsigned long)pw.wakes_cold, (unsigned long)pw.incomplete,
            pw.reset_reason);
    }
    ESP_LOGI(TAG, "Fetching image from %s", url);

    /* Allocate image buffer in PSRAM */
    uint8_t *buf = heap_caps_malloc(EPD_IMAGE_BYTES, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGE(TAG, "Failed to allocate %lu bytes in PSRAM", EPD_IMAGE_BYTES);
        return ESP_ERR_NO_MEM;
    }

    recv_ctx_t ctx = { .buf = buf, .written = 0, .overflow = false };

    esp_http_client_config_t http_cfg = {
        .url            = url,
        .timeout_ms     = HTTP_TIMEOUT_MS,
        .event_handler  = http_event_handler,
        .user_data      = &ctx,
        .buffer_size    = 8192,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (!client) {
        heap_caps_free(buf);
        return ESP_FAIL;
    }

    power_log_phase_begin(PWR_PHASE_FETCH);
    esp_err_t err = esp_http_client_perform(client);
    int http_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    power_log_phase_end(PWR_PHASE_FETCH);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "HTTP request failed: %s", esp_err_to_name(err));
        heap_caps_free(buf);
        return err;
    }
    if (http_status != 200) {
        ESP_LOGE(TAG, "HTTP status %d", http_status);
        heap_caps_free(buf);
        return ESP_FAIL;
    }
    if (ctx.written != EPD_IMAGE_BYTES) {
        ESP_LOGE(TAG, "Expected %lu bytes, received %zu", EPD_IMAGE_BYTES, ctx.written);
        heap_caps_free(buf);
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGI(TAG, "Image received (%zu bytes), displaying", ctx.written);

    power_log_phase_begin(PWR_PHASE_DISPLAY);
    initEPD();
    pic_display_test(buf);
    power_log_phase_end(PWR_PHASE_DISPLAY);

    heap_caps_free(buf);
    ESP_LOGI(TAG, "Display complete");
    return ESP_OK;
}
