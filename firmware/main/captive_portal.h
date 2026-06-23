#pragma once

#include "nvs_config.h"

/**
 * Start the captive-portal provisioning flow.
 *
 * Creates a SoftAP named "ESP-Frame-XXXXXX", starts a DNS server that
 * redirects everything to 192.168.4.1, and serves a one-page config form.
 *
 * Blocks until the user submits valid credentials, then saves them to NVS
 * and calls esp_restart().  Never returns normally.
 */
void captive_portal_run(void);
