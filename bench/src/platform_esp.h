// What platform.h means on an Espressif ESP32-family chip, under ESP-IDF.
//
// FreeRTOS underneath, but used the way the Pico port uses bare metal: one
// worker task per core, pinned there when it is created (mcu/esp32/main/port.c),
// so nothing moves a worker and nothing else of note shares its core. Only what
// measure.c uses is here: a barrier across the cores, the clock, where a worker
// is, and allocation.
//
// Included from platform.h when CPCPUB_ESP is defined; never on its own.

#ifndef CPCPUB_PLATFORM_ESP_H
#define CPCPUB_PLATFORM_ESP_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// ESP-IDF's headers are not written for -Wconversion, which this repository
// builds with (the RISC-V parts' CSR macros narrow an unsigned long to int),
// so the conversion warnings are off for them and back on for everything after.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wsign-conversion"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#pragma GCC diagnostic pop

// A worker always runs on the core its task was pinned to, and can say which.
#define PLAT_HAS_AFFINITY 1
#define PLAT_HAS_PROC     0

// ---------------------------------------------------------------------------
// Barriers
// ---------------------------------------------------------------------------
//
// FreeRTOS's event-group rendezvous, which is a reusable barrier already: each
// worker sets its bit and blocks until every bit is set, and the bits clear as
// they all release, ready for the next one. A worker's bit is its core's
// number, which no other worker shares; a barrier for `count` workers is used
// with them on cores 0..count-1, which is every core of a chip that has two.
//
// The waiting worker blocks rather than spins, so its core drops to the idle
// task -- which sleeps in WAITI -- and takes no bus cycles from the core still
// measuring.

typedef struct {
    EventGroupHandle_t group;
    StaticEventGroup_t storage;
    EventBits_t        all;
} plat_barrier_t;

#define PLAT_BARRIER_SERIAL_THREAD 1

static inline int plat_barrier_init(plat_barrier_t *b, unsigned count) {
    if (count == 0 || count > (unsigned)portNUM_PROCESSORS) return -1;
    b->group = xEventGroupCreateStatic(&b->storage);
    b->all = (EventBits_t)((1u << count) - 1u);
    return b->group ? 0 : -1;
}

static inline int plat_barrier_wait(plat_barrier_t *b) {
    const EventBits_t mine = (EventBits_t)(1u << esp_cpu_get_core_id());
    (void)xEventGroupSync(b->group, mine, b->all, portMAX_DELAY);
    return 0;
}

static inline int plat_barrier_destroy(plat_barrier_t *b) {
    vEventGroupDelete(b->group);
    return 0;
}

// ---------------------------------------------------------------------------
// Pinning
// ---------------------------------------------------------------------------

// The task is pinned when it is created, so "pin to `cpu`" succeeds exactly
// when it was created there.
static inline int plat_pin_self(int cpu) {
    return cpu >= 0 && cpu == esp_cpu_get_core_id() ? 0 : -1;
}

static inline int plat_current_cpu(void) { return esp_cpu_get_core_id(); }

static inline int plat_cpu_count(void) { return portNUM_PROCESSORS; }

// ---------------------------------------------------------------------------
// The clock
// ---------------------------------------------------------------------------
//
// esp_timer: a 64-bit microsecond count from a hardware timer clocked off the
// crystal, which nothing slews -- the unadjusted monotonic clock --clock raw
// asks for on a host. A microsecond against batches of about 4 ms is 0.025%.
#define PLAT_NOW_SEC   1
#define PLAT_CLOCK_RAW 1

static inline double plat_now_sec(void) { return (double)esp_timer_get_time() * 1e-6; }

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------
//
// Internal SRAM, reached directly rather than through a cache, so as on the
// Pico a small buffer measures what a large one would.
#define PLAT_MEM_MIN_BYTES ((size_t)CHASE_WAYS * 32 * 64)

// Asked of the internal heap by name: a board with PSRAM can be set to serve
// large mallocs from it, and the memory phases would then be measuring an
// external chip through a cache while saying nothing of it. No alignment
// beyond the heap's own -- there are no pages to align to.
static inline void *plat_aligned_alloc(size_t align, size_t size) {
    (void)align;
    return heap_caps_malloc(size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

static inline void plat_aligned_free(void *p) { heap_caps_free(p); }

#endif  // CPCPUB_PLATFORM_ESP_H
