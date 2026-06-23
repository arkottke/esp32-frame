#pragma once

#include "esp_err.h"

/**
 * State-of-health telemetry sent with each image request.
 * Fields with value -1 are omitted from the query string.
 */
typedef struct {
    int  rssi;          /* WiFi signal strength in dBm (typically negative) */
    int  voltage_mv;    /* Supply voltage in millivolts; -1 = not measured   */
    char wakeup[16];    /* "timer", "button", or "boot"                      */
    char fw_ver[16];    /* Firmware version string                           */
    int  heap;          /* Free heap in bytes                                */
} frame_soh_t;

/**
 * Fetch the pre-dithered image for this frame from the server and display it.
 * SoH fields are appended as query parameters to the image URL.
 *
 * @param server_url  Base URL, e.g. "http://192.168.1.10:8000"
 * @param mac_str     Frame MAC address string, e.g. "AA:BB:CC:DD:EE:FF"
 * @param soh         State-of-health data to send with the request
 *
 * @return ESP_OK on success, error code otherwise.
 *         On failure the EPD is not touched (keeps its previous image).
 */
esp_err_t epd_http_fetch_and_display(const char *server_url, const char *mac_str,
                                     const frame_soh_t *soh);
