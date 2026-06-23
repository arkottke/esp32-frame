#pragma once

#include "esp_err.h"

/**
 * Fetch the pre-dithered image for this frame from the server and display it.
 *
 * @param server_url  Base URL, e.g. "http://192.168.1.10:8000"
 * @param mac_str     Frame MAC address string, e.g. "AA:BB:CC:DD:EE:FF"
 *
 * @return ESP_OK on success, error code otherwise.
 *         On failure the EPD is not touched (keeps its previous image).
 */
esp_err_t epd_http_fetch_and_display(const char *server_url, const char *mac_str);
