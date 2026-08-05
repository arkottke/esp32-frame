#include "power_log.h"

#include <string.h>
#include <sys/time.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"

static const char *TAG = "power_log";

#define PWR_MAGIC  0x50575231u   /* "PWR1" — marks RTC memory as initialised */

/* -------------------------------------------------------------------------
 * RTC slow memory — survives deep sleep and software reset, cleared by a
 * power-on reset or brownout.
 * -----------------------------------------------------------------------*/
typedef struct {
    uint32_t magic;
    uint32_t boot_count;
    uint32_t wakes_timer;
    uint32_t wakes_button;
    uint32_t wakes_cold;
    uint32_t incomplete;
    uint32_t requested_ms;
    uint32_t prev_awake_ms;
    uint32_t prev_phase_ms[PWR_PHASE_MAX];
    uint64_t awake_ms_total;
    uint64_t sleep_ms_total;
    struct timeval sleep_enter_time;
    bool     cycle_in_progress;   /* set at boot, cleared on clean sleep entry */
} power_rtc_t;

static RTC_DATA_ATTR power_rtc_t s_rtc;

/* Current-cycle state — plain RAM, discarded at every sleep. */
static int64_t  s_phase_start_us[PWR_PHASE_MAX];
static uint32_t s_phase_ms[PWR_PHASE_MAX];
static uint32_t s_slept_ms;

static const char *reset_reason_str(void)
{
    switch (esp_reset_reason()) {
        case ESP_RST_POWERON:   return "poweron";
        case ESP_RST_SW:        return "sw";
        case ESP_RST_DEEPSLEEP: return "deepsleep";
        case ESP_RST_PANIC:     return "panic";
        case ESP_RST_INT_WDT:   return "int_wdt";
        case ESP_RST_TASK_WDT:  return "task_wdt";
        case ESP_RST_WDT:       return "wdt";
        case ESP_RST_BROWNOUT:  return "brownout";
        case ESP_RST_EXT:       return "ext";
        case ESP_RST_SDIO:      return "sdio";
        default:                return "unknown";
    }
}

void power_log_boot(uint32_t wakeup_causes)
{
    memset(s_phase_start_us, 0, sizeof(s_phase_start_us));
    memset(s_phase_ms, 0, sizeof(s_phase_ms));
    s_slept_ms = 0;

    bool rtc_valid = (s_rtc.magic == PWR_MAGIC);
    if (!rtc_valid) {
        /* First boot, or RTC memory lost to a power cut / brownout. */
        memset(&s_rtc, 0, sizeof(s_rtc));
        s_rtc.magic = PWR_MAGIC;
        ESP_LOGW(TAG, "RTC counters cleared (power-on or brownout) — "
                      "history restarts from zero");
    }

    /* Measure the sleep that just ended. gettimeofday() is backed by the RTC
       timer, which keeps counting through deep sleep. */
    if (rtc_valid && s_rtc.sleep_enter_time.tv_sec != 0) {
        struct timeval now;
        gettimeofday(&now, NULL);
        int64_t delta_ms = (int64_t)(now.tv_sec - s_rtc.sleep_enter_time.tv_sec) * 1000
                         + (now.tv_usec - s_rtc.sleep_enter_time.tv_usec) / 1000;
        if (delta_ms > 0) {
            s_slept_ms = (uint32_t)delta_ms;
            s_rtc.sleep_ms_total += (uint64_t)delta_ms;
        }
    }

    /* A cycle still flagged in-progress means the previous wake never reached
       esp_deep_sleep() — it hung, panicked, or was reset. That is the single
       most expensive failure mode, so count it separately. */
    if (rtc_valid && s_rtc.cycle_in_progress) {
        s_rtc.incomplete++;
        ESP_LOGW(TAG, "Previous cycle never reached deep sleep "
                      "(%lu total) — reset reason: %s",
                 (unsigned long)s_rtc.incomplete, reset_reason_str());
    }
    s_rtc.cycle_in_progress = true;

    s_rtc.boot_count++;
    if      (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_TIMER)) s_rtc.wakes_timer++;
    else if (wakeup_causes & BIT(ESP_SLEEP_WAKEUP_EXT1))  s_rtc.wakes_button++;
    else                                                   s_rtc.wakes_cold++;

    power_log_stats_t st;
    power_log_get(&st);
    ESP_LOGI(TAG, "WAKE  boot=%lu reset=%s slept=%lu ms (asked %lu ms) "
                  "wakes[timer=%lu btn=%lu cold=%lu] incomplete=%lu",
             (unsigned long)st.boot_count, st.reset_reason,
             (unsigned long)st.slept_ms, (unsigned long)st.requested_ms,
             (unsigned long)st.wakes_timer, (unsigned long)st.wakes_button,
             (unsigned long)st.wakes_cold, (unsigned long)st.incomplete);
    ESP_LOGI(TAG, "PREV  awake=%lu ms (wifi=%lu fetch=%lu display=%lu) "
                  "duty=%lu.%lu%% lifetime[awake=%llu s asleep=%llu s]",
             (unsigned long)st.prev_awake_ms, (unsigned long)st.prev_wifi_ms,
             (unsigned long)st.prev_fetch_ms, (unsigned long)st.prev_display_ms,
             (unsigned long)(st.duty_permille / 10),
             (unsigned long)(st.duty_permille % 10),
             (unsigned long long)(st.awake_ms_total / 1000),
             (unsigned long long)(st.sleep_ms_total / 1000));

    /* A sleep that ended much earlier than requested means something other
       than the timer woke the chip — most likely SW2 floating or bouncing. */
    if (s_slept_ms && s_rtc.requested_ms &&
        s_slept_ms + 5000 < s_rtc.requested_ms) {
        ESP_LOGW(TAG, "Sleep ended %lu ms early — check SW2 for spurious "
                      "ext1 wakeups", (unsigned long)(s_rtc.requested_ms - s_slept_ms));
    }
}

void power_log_phase_begin(power_phase_t phase)
{
    if (phase < 0 || phase >= PWR_PHASE_MAX) return;
    s_phase_start_us[phase] = esp_timer_get_time();
}

void power_log_phase_end(power_phase_t phase)
{
    if (phase < 0 || phase >= PWR_PHASE_MAX) return;
    if (s_phase_start_us[phase] == 0) return;
    s_phase_ms[phase] = (uint32_t)((esp_timer_get_time() - s_phase_start_us[phase]) / 1000);
    s_phase_start_us[phase] = 0;
}

void power_log_before_sleep(uint32_t requested_seconds)
{
    /* esp_timer_get_time() is microseconds since this boot, and a deep-sleep
       wake is a fresh boot — so this is exactly the time spent awake. */
    uint32_t awake_ms = (uint32_t)(esp_timer_get_time() / 1000);

    s_rtc.prev_awake_ms   = awake_ms;
    s_rtc.awake_ms_total += awake_ms;
    for (int i = 0; i < PWR_PHASE_MAX; i++) {
        s_rtc.prev_phase_ms[i] = s_phase_ms[i];
    }

    s_rtc.requested_ms = requested_seconds * 1000u;
    gettimeofday(&s_rtc.sleep_enter_time, NULL);
    s_rtc.cycle_in_progress = false;

    ESP_LOGI(TAG, "SLEEP awake=%lu ms (wifi=%lu fetch=%lu display=%lu) "
                  "requesting %lu s",
             (unsigned long)awake_ms,
             (unsigned long)s_phase_ms[PWR_PHASE_WIFI],
             (unsigned long)s_phase_ms[PWR_PHASE_FETCH],
             (unsigned long)s_phase_ms[PWR_PHASE_DISPLAY],
             (unsigned long)requested_seconds);
}

void power_log_get(power_log_stats_t *out)
{
    if (!out) return;
    memset(out, 0, sizeof(*out));

    out->boot_count      = s_rtc.boot_count;
    out->wakes_timer     = s_rtc.wakes_timer;
    out->wakes_button    = s_rtc.wakes_button;
    out->wakes_cold      = s_rtc.wakes_cold;
    out->incomplete      = s_rtc.incomplete;
    out->slept_ms        = s_slept_ms;
    out->requested_ms    = s_rtc.requested_ms;
    out->prev_awake_ms   = s_rtc.prev_awake_ms;
    out->prev_wifi_ms    = s_rtc.prev_phase_ms[PWR_PHASE_WIFI];
    out->prev_fetch_ms   = s_rtc.prev_phase_ms[PWR_PHASE_FETCH];
    out->prev_display_ms = s_rtc.prev_phase_ms[PWR_PHASE_DISPLAY];
    out->awake_ms_total  = s_rtc.awake_ms_total;
    out->sleep_ms_total  = s_rtc.sleep_ms_total;
    out->reset_reason    = reset_reason_str();

    uint64_t total = s_rtc.awake_ms_total + s_rtc.sleep_ms_total;
    out->duty_permille = total ? (uint32_t)((s_rtc.awake_ms_total * 1000u) / total) : 0;
}
