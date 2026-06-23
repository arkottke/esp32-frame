#include "captive_portal.h"

#include <string.h>
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_system.h"
#include "lwip/sockets.h"
#include "lwip/dns.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_config.h"

static const char *TAG = "captive_portal";

#define AP_IP       "192.168.4.1"
#define DNS_PORT    53
#define DNS_BUF_SZ  512

/* -------------------------------------------------------------------------
 * HTML for the config form
 * -----------------------------------------------------------------------*/
static const char *CONFIG_HTML =
    "<!DOCTYPE html><html><head><meta charset=UTF-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>ESP-Frame Setup</title>"
    "<style>body{font-family:sans-serif;max-width:480px;margin:40px auto;padding:16px;}"
    "input{display:block;width:100%;padding:8px;margin:6px 0 14px;box-sizing:border-box;}"
    "button{padding:10px 24px;background:#4f8ef7;color:#fff;border:none;border-radius:6px;cursor:pointer;}"
    "h1{color:#333;}label{font-size:.9rem;color:#555;}</style></head>"
    "<body><h1>ESP-Frame Setup</h1>"
    "<form method=POST action=/save>"
    "<label>WiFi SSID</label><input name=ssid required maxlength=63>"
    "<label>WiFi Password</label><input name=pass type=password maxlength=63>"
    "<label>Server URL (e.g. http://192.168.1.10:8000)</label>"
    "<input name=server required maxlength=127 placeholder='http://192.168.1.x:8000'>"
    "<label>Poll interval (minutes)</label>"
    "<input name=poll type=number value=10 min=1 max=1440>"
    "<button type=submit>Save &amp; Connect</button>"
    "</form></body></html>";

static const char *SAVED_HTML =
    "<!DOCTYPE html><html><head><meta charset=UTF-8>"
    "<meta http-equiv=refresh content='3;url=/'>"
    "<title>Saved</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding:40px'>"
    "<h2>Saved! Connecting&hellip;</h2>"
    "<p>The frame will restart and join your network.</p>"
    "</body></html>";

/* -------------------------------------------------------------------------
 * URL-decode helper
 * -----------------------------------------------------------------------*/
static void url_decode(const char *in, char *out, size_t out_len)
{
    size_t i = 0, j = 0;
    while (in[i] && j + 1 < out_len) {
        if (in[i] == '%' && in[i+1] && in[i+2]) {
            char hex[3] = { in[i+1], in[i+2], 0 };
            out[j++] = (char)strtol(hex, NULL, 16);
            i += 3;
        } else if (in[i] == '+') {
            out[j++] = ' ';
            i++;
        } else {
            out[j++] = in[i++];
        }
    }
    out[j] = '\0';
}

/* Extract a single field from an application/x-www-form-urlencoded body. */
static void parse_form_field(const char *body, const char *key,
                              char *out, size_t out_len)
{
    out[0] = '\0';
    size_t klen = strlen(key);
    const char *p = body;
    while (*p) {
        if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
            p += klen + 1;
            const char *end = strchr(p, '&');
            size_t vlen = end ? (size_t)(end - p) : strlen(p);
            char tmp[NVS_CONFIG_MAX_STR] = {0};
            if (vlen >= sizeof(tmp)) vlen = sizeof(tmp) - 1;
            memcpy(tmp, p, vlen);
            url_decode(tmp, out, out_len);
            return;
        }
        p = strchr(p, '&');
        if (!p) break;
        p++;
    }
}

/* -------------------------------------------------------------------------
 * HTTP handlers
 * -----------------------------------------------------------------------*/
static esp_err_t handler_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, CONFIG_HTML, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static esp_err_t handler_post_save(httpd_req_t *req)
{
    char body[512] = {0};
    int len = httpd_req_recv(req, body, sizeof(body) - 1);
    if (len <= 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty body");
        return ESP_FAIL;
    }
    body[len] = '\0';

    frame_config_t cfg = {0};
    parse_form_field(body, "ssid",   cfg.ssid,       sizeof(cfg.ssid));
    parse_form_field(body, "pass",   cfg.password,   sizeof(cfg.password));
    parse_form_field(body, "server", cfg.server_url, sizeof(cfg.server_url));

    char poll_str[16] = {0};
    parse_form_field(body, "poll", poll_str, sizeof(poll_str));
    cfg.poll_seconds = (uint32_t)(atoi(poll_str) * 60);
    if (cfg.poll_seconds < 60) cfg.poll_seconds = 600;

    if (strlen(cfg.ssid) == 0 || strlen(cfg.server_url) == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "SSID and server URL are required");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Saving config: ssid=%s server=%s poll=%lu s",
             cfg.ssid, cfg.server_url, (unsigned long)cfg.poll_seconds);

    nvs_config_save(&cfg);

    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, SAVED_HTML, HTTPD_RESP_USE_STRLEN);

    /* Restart after a short delay so the response is delivered. */
    vTaskDelay(pdMS_TO_TICKS(2000));
    esp_restart();
    return ESP_OK;
}

/* Redirect any unrecognised path to the root (captive-portal redirect). */
static esp_err_t handler_redirect(httpd_req_t *req)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://" AP_IP "/");
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

/* -------------------------------------------------------------------------
 * Minimal DNS server — answers every query with 192.168.4.1
 * -----------------------------------------------------------------------*/
static void dns_server_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "Failed to create DNS socket");
        vTaskDelete(NULL);
        return;
    }

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(DNS_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(sock, (struct sockaddr *)&addr, sizeof(addr));

    uint8_t buf[DNS_BUF_SZ];
    struct sockaddr_in client;
    socklen_t client_len = sizeof(client);

    while (1) {
        int n = recvfrom(sock, buf, sizeof(buf), 0,
                         (struct sockaddr *)&client, &client_len);
        if (n < 12) continue;

        /* Build minimal DNS response: flip QR bit, set answer count to 1,
         * copy question, append A record pointing to AP_IP. */
        uint8_t resp[DNS_BUF_SZ];
        memcpy(resp, buf, n);
        resp[2] = 0x81; /* QR=1, Opcode=0, AA=1 */
        resp[3] = 0x80; /* RA=1 */
        resp[6] = 0;
        resp[7] = 1;    /* ANCOUNT = 1 */

        /* Append answer: pointer to question name (0xC00C), type A, class IN,
         * TTL 60, rdlength 4, IP 192.168.4.1 */
        uint8_t answer[] = {
            0xC0, 0x0C,         /* name: pointer to question */
            0x00, 0x01,         /* type A */
            0x00, 0x01,         /* class IN */
            0x00, 0x00, 0x00, 60, /* TTL */
            0x00, 0x04,         /* rdlength */
            192, 168, 4, 1      /* 192.168.4.1 */
        };
        int resp_len = n;
        if (resp_len + (int)sizeof(answer) < DNS_BUF_SZ) {
            memcpy(resp + resp_len, answer, sizeof(answer));
            resp_len += sizeof(answer);
        }
        sendto(sock, resp, resp_len, 0,
               (struct sockaddr *)&client, client_len);
    }
    close(sock);
    vTaskDelete(NULL);
}

/* -------------------------------------------------------------------------
 * Public entry point
 * -----------------------------------------------------------------------*/
void captive_portal_run(void)
{
    /* --- WiFi SoftAP --- */
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));

    /* Derive SSID from last 3 bytes of MAC */
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_AP, mac);
    char ssid[32];
    snprintf(ssid, sizeof(ssid), "ESP-Frame-%02X%02X%02X",
             mac[3], mac[4], mac[5]);

    wifi_config_t ap_cfg = {
        .ap = {
            .max_connection = 4,
            .authmode       = WIFI_AUTH_OPEN,
        }
    };
    strlcpy((char *)ap_cfg.ap.ssid, ssid, sizeof(ap_cfg.ap.ssid));
    ap_cfg.ap.ssid_len = strlen(ssid);

    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "SoftAP started: SSID=%s", ssid);

    /* --- DNS task --- */
    xTaskCreate(dns_server_task, "dns_srv", 4096, NULL, 5, NULL);

    /* --- HTTP server --- */
    httpd_config_t hcfg = HTTPD_DEFAULT_CONFIG();
    hcfg.uri_match_fn = httpd_uri_match_wildcard;
    httpd_handle_t server = NULL;
    ESP_ERROR_CHECK(httpd_start(&server, &hcfg));

    httpd_uri_t uri_get = {
        .uri = "/", .method = HTTP_GET, .handler = handler_get
    };
    httpd_uri_t uri_post = {
        .uri = "/save", .method = HTTP_POST, .handler = handler_post_save
    };
    httpd_uri_t uri_redirect = {
        .uri = "/*", .method = HTTP_GET, .handler = handler_redirect
    };
    httpd_register_uri_handler(server, &uri_get);
    httpd_register_uri_handler(server, &uri_post);
    httpd_register_uri_handler(server, &uri_redirect);

    ESP_LOGI(TAG, "Captive portal running at http://%s/", AP_IP);

    /* Block forever — esp_restart() is called from handler_post_save */
    while (1) vTaskDelay(pdMS_TO_TICKS(10000));
}
