#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "nvs_config.h"
#include "captive_portal.h"
#include "epd_http.h"
#include "GDEP133C02.h"
#include "comm.h"
#include "pindefine.h"
#include "status.h"

#if BATT_ADC_CHANNEL >= 0
#include "driver/adc.h"
#endif

static const char *TAG = "app_main";

/* -------------------------------------------------------------------------
 * SW4 factory-reset check
 * -----------------------------------------------------------------------*/
#define FACTORY_RESET_HOLD_MS  5000
#define FACTORY_RESET_POLL_MS  100

static void check_factory_reset(void)
{
    /* SW4 is active-HIGH: button connects IO14 to VCC; board has pull-down to GND */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << SW4,
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    vTaskDelay(pdMS_TO_TICKS(50));

    if (gpio_get_level(SW4) == 0) return;  /* not pressed at boot */

    ESP_LOGI(TAG, "SW4 held — hold for %d s to factory reset", FACTORY_RESET_HOLD_MS / 1000);
    int elapsed = 0;
    while (elapsed < FACTORY_RESET_HOLD_MS) {
        vTaskDelay(pdMS_TO_TICKS(FACTORY_RESET_POLL_MS));
        if (gpio_get_level(SW4) == 0) {
            ESP_LOGI(TAG, "SW4 released early, skipping factory reset");
            return;
        }
        elapsed += FACTORY_RESET_POLL_MS;
    }

    ESP_LOGW(TAG, "Factory reset triggered — erasing WiFi credentials");
    nvs_config_erase();
    esp_restart();
}

/* -------------------------------------------------------------------------
 * WiFi connection helpers
 * -----------------------------------------------------------------------*/
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_MAX_RETRIES   5

static EventGroupHandle_t s_wifi_event_group;
static int s_retry_count = 0;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                                int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_count < WIFI_MAX_RETRIES) {
            esp_wifi_connect();
            s_retry_count++;
            ESP_LOGW(TAG, "WiFi disconnected, retry %d/%d", s_retry_count, WIFI_MAX_RETRIES);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_count = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_connect(const char *ssid, const char *password)
{
    s_wifi_event_group = xEventGroupCreate();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t h_wifi, h_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, &h_wifi));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, &h_ip));

    wifi_config_t wifi_cfg = {0};
    strlcpy((char *)wifi_cfg.sta.ssid,     ssid,     sizeof(wifi_cfg.sta.ssid));
    strlcpy((char *)wifi_cfg.sta.password, password, sizeof(wifi_cfg.sta.password));
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(30000));

    esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, h_wifi);
    esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, h_ip);
    vEventGroupDelete(s_wifi_event_group);

    if (bits & WIFI_CONNECTED_BIT) return ESP_OK;
    ESP_LOGE(TAG, "Failed to connect to %s", ssid);
    return ESP_FAIL;
}

#if BATT_ADC_CHANNEL >= 0
static int read_battery_mv(void)
{
    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten(BATT_ADC_CHANNEL, ADC_ATTEN_DB_11);
    int raw = adc1_get_raw(BATT_ADC_CHANNEL);
    /* Linear approximation assuming 3.3 V Vref and a 1:1 divider.
       Adjust the multiplier to match your actual divider ratio. */
    return (raw * 3300) / 4095;
}
#endif

static void get_mac_string(char *out, size_t out_len)
{
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    snprintf(out, out_len, "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* -------------------------------------------------------------------------
 * app_main
 * -----------------------------------------------------------------------*/
void app_main(void)
{
    /* NVS — required by WiFi and our config */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    check_factory_reset();

    uint32_t wakeup_causes = esp_sleep_get_wakeup_causes();
    if      (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_TIMER)) ESP_LOGI(TAG, "Woke from timer");
    else if (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_EXT1))  ESP_LOGI(TAG, "Woke from SW2 (manual refresh)");
    else                                                   ESP_LOGI(TAG, "Cold boot");

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* EPD hardware init */
    initialGpio();
    initialSpi();
    setGpioLevel(LOAD_SW, GPIO_HIGH);
    epdHardwareReset();
    setPinCsAll(GPIO_HIGH);

    /* Load config from NVS */
    frame_config_t cfg;
    err = nvs_config_load(&cfg);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "No config found, starting captive portal");
        captive_portal_run();
        /* never returns */
    }

    ESP_LOGI(TAG, "Config loaded: ssid=%s server=%s poll=%lu s",
             cfg.ssid, cfg.server_url, (unsigned long)cfg.poll_seconds);

    /* Connect to WiFi */
    err = wifi_connect(cfg.ssid, cfg.password);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi connect failed, sleeping %lu s before retry",
                 (unsigned long)cfg.poll_seconds);
        esp_deep_sleep((uint64_t)cfg.poll_seconds * 1000000ULL);
    }

    /* Get MAC address for frame identity */
    char mac_str[18];
    get_mac_string(mac_str, sizeof(mac_str));
    ESP_LOGI(TAG, "Frame MAC: %s", mac_str);

    /* Build state-of-health telemetry */
    frame_soh_t soh = {0};

    wifi_ap_record_t ap_info;
    if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK)
        soh.rssi = ap_info.rssi;

#if BATT_ADC_CHANNEL >= 0
    soh.voltage_mv = read_battery_mv();
#else
    soh.voltage_mv = -1;
#endif

    if      (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_TIMER)) strlcpy(soh.wakeup, "timer",  sizeof(soh.wakeup));
    else if (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_EXT1))  strlcpy(soh.wakeup, "button", sizeof(soh.wakeup));
    else                                                   strlcpy(soh.wakeup, "boot",   sizeof(soh.wakeup));

    strlcpy(soh.fw_ver, FW_VERSION, sizeof(soh.fw_ver));
    soh.heap = (int)esp_get_free_heap_size();

    ESP_LOGI(TAG, "SoH: rssi=%d voltage_mv=%d wakeup=%s fw=%s heap=%d",
             soh.rssi, soh.voltage_mv, soh.wakeup, soh.fw_ver, soh.heap);

    /* Fetch image from server and display it */
    err = epd_http_fetch_and_display(cfg.server_url, mac_str, &soh);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Image fetch failed (%s), keeping current display",
                 esp_err_to_name(err));
    }

    /* Sleep until next poll; SW2 can also wake early for a manual refresh */
    esp_sleep_enable_ext1_wakeup(1ULL << SW2, ESP_EXT1_WAKEUP_ANY_HIGH);
    ESP_LOGI(TAG, "Entering deep sleep for %lu s", (unsigned long)cfg.poll_seconds);
    esp_deep_sleep((uint64_t)cfg.poll_seconds * 1000000ULL);
}
