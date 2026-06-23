#include "epd_http.h"

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "GDEP133C02.h"

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

esp_err_t epd_http_fetch_and_display(const char *server_url, const char *mac_str)
{
    /* Build URL: {server_url}/frame/{mac}/image */
    char url[256];
    snprintf(url, sizeof(url), "%s/frame/%s/image", server_url, mac_str);
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

    esp_err_t err = esp_http_client_perform(client);
    int http_status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

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

    initEPD();
    pic_display_test(buf);

    heap_caps_free(buf);
    ESP_LOGI(TAG, "Display complete");
    return ESP_OK;
}
