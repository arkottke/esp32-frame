#pragma once

#include <stdint.h>
#include <stdbool.h>

/**
 * Deep-sleep cycle accounting.
 *
 * Counters live in RTC slow memory, so they survive deep sleep and software
 * resets but are cleared by a power-on reset or brownout. A wiped set of
 * counters is itself a diagnostic: it means the board lost power rather than
 * completing a sleep cycle.
 *
 * Two independent clocks are used, because each measures a different thing:
 *   - esp_timer_get_time() counts microseconds since *this* boot. A deep-sleep
 *     wake is a fresh boot, so this measures time spent awake.
 *   - gettimeofday() is backed by the RTC timer, which keeps running through
 *     deep sleep, so a delta across the sleep measures time spent asleep.
 */

/* Phases of a wake cycle, timed individually so the awake budget can be
   attributed to the subsystem responsible for it. */
typedef enum {
    PWR_PHASE_WIFI = 0,   /* association + DHCP                  */
    PWR_PHASE_FETCH,      /* HTTP GET of the 960 KB image        */
    PWR_PHASE_DISPLAY,    /* initEPD() + panel refresh           */
    PWR_PHASE_MAX,
} power_phase_t;

/**
 * Snapshot of the counters, for logging and telemetry.
 *
 * Fields prefixed `prev_` describe the last cycle that ran to completion;
 * the current cycle's own totals aren't known until it enters sleep.
 * `slept_ms` describes the sleep that just ended, so it is current.
 */
typedef struct {
    uint32_t boot_count;
    uint32_t wakes_timer;
    uint32_t wakes_button;
    uint32_t wakes_cold;
    uint32_t incomplete;        /* cycles that never reached deep sleep      */
    uint32_t slept_ms;          /* actual duration of the sleep just ended   */
    uint32_t requested_ms;      /* what that sleep was asked to be           */
    uint32_t prev_awake_ms;
    uint32_t prev_wifi_ms;
    uint32_t prev_fetch_ms;
    uint32_t prev_display_ms;
    uint32_t duty_permille;     /* awake / (awake + asleep), lifetime, x1000 */
    uint64_t awake_ms_total;
    uint64_t sleep_ms_total;
    const char *reset_reason;
} power_log_stats_t;

/**
 * Call once at the top of app_main, before any work is done.
 *
 * Attributes the wake to its cause, measures how long the preceding sleep
 * actually lasted, and detects whether the previous cycle ever reached
 * deep sleep. Logs a summary line at INFO level.
 *
 * @param wakeup_causes  Bitmask from esp_sleep_get_wakeup_causes().
 */
void power_log_boot(uint32_t wakeup_causes);

/** Start timing a phase of the current wake cycle. */
void power_log_phase_begin(power_phase_t phase);

/** Stop timing a phase; safe to call without a matching begin (records 0). */
void power_log_phase_end(power_phase_t phase);

/**
 * Call immediately before esp_deep_sleep().
 *
 * Rolls the current cycle's timings into the `prev_` fields, records the
 * sleep entry timestamp, marks the cycle as cleanly completed, and logs a
 * summary line.
 *
 * @param requested_seconds  Sleep duration about to be requested.
 */
void power_log_before_sleep(uint32_t requested_seconds);

/** Copy the current counters out for telemetry. */
void power_log_get(power_log_stats_t *out);
