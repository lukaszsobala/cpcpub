// The Raspberry Pi RP2040 and RP2350 under the pico-sdk: see mcu/port.h.
//
// Core 0 runs the console and one worker; core 1, when a run uses it, is
// started fresh for each run with one worker and a stack of its own, and
// reports back through the inter-core FIFO when it is done. A core that is
// waiting -- core 0 for core 1, or either at a barrier -- sleeps in WFE rather
// than polling, so it takes no bus cycles from the core still measuring.

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "pico/mutex.h"
#include "pico/version.h"
#include "hardware/clocks.h"

#include "port.h"

void port_init(void) {
    stdio_init_all();
#if LIB_PICO_STDIO_USB
    // Nothing printed before a terminal opens the port reaches anyone, the
    // banner included, so wait for one.
    while (!stdio_usb_connected()) sleep_ms(50);
#endif
}

int port_getchar(void) {
    int c;
    while ((c = getchar_timeout_us(1000000)) == PICO_ERROR_TIMEOUT) {}
    return c;
}

// ---------------------------------------------------------------------------
// The second core
// ---------------------------------------------------------------------------

static void core1_main(void) {
    worker_t *w = (worker_t *)(uintptr_t)multicore_fifo_pop_blocking();
    worker(w);
    multicore_fifo_push_blocking(1);
}

void port_run(worker_t *w, const int *cores, int n) {
    worker_t *on0 = NULL, *on1 = NULL;
    for (int i = 0; i < n; ++i) {
        if (cores[i] == 0) on0 = &w[i];
        else               on1 = &w[i];
    }
    if (on1) {
        multicore_reset_core1();
        multicore_launch_core1(core1_main);
        multicore_fifo_push_blocking((uint32_t)(uintptr_t)on1);
    }
    if (on0) worker(on0);
    if (on1) (void)multicore_fifo_pop_blocking();
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

// What malloc can give, found by asking it: the largest block it will hand
// out, then the largest after that, and so on, all freed again. Needs
// PICO_MALLOC_PANIC=0, which the build sets -- otherwise the first refusal is
// a panic rather than a NULL.
size_t port_heap_bytes(void) {
    void *held[16];
    int n = 0;
    size_t total = 0;
    while (n < (int)(sizeof(held) / sizeof(held[0]))) {
        size_t lo = 0, hi = (size_t)1 << 20;   // more than either chip has
        while (hi - lo > 256) {
            const size_t mid = lo + (hi - lo) / 2;
            void *p = malloc(mid);
            if (p) { free(p); lo = mid; }
            else   hi = mid;
        }
        if (lo < 1024) break;
        held[n] = malloc(lo);
        if (!held[n]) break;
        total += lo;
        ++n;
    }
    while (n > 0) free(held[--n]);
    return total;
}

// ---------------------------------------------------------------------------
// Clocks
// ---------------------------------------------------------------------------

// The frequency counter is one block of hardware, and the two workers sample
// the clock at about the same moment, so it is taken in turn.
auto_init_mutex(g_fc_lock);

// Measured, not configured: the clock as the chip's own frequency counter
// counts it against the crystal, which is what "sampled under load" means on
// a host. clock_get_hz() would say what the SDK set, which is not the same
// claim.
double cpu_cur_mhz(int cpu) {
    (void)cpu;   // one clock drives both cores
    mutex_enter_blocking(&g_fc_lock);
    const uint32_t khz = frequency_count_khz(CLOCKS_FC0_SRC_VALUE_CLK_SYS);
    mutex_exit(&g_fc_lock);
    return (double)khz / 1000.0;
}

double rated_mhz(int cpu) {
    (void)cpu;
    return (double)clock_get_hz(clk_sys) / 1e6;
}

// No DRAM: the memory phases measure on-chip SRAM, which has no clock of its
// own to report.
double dram_mhz(void) { return 0.0; }

// ---------------------------------------------------------------------------
// The machine
// ---------------------------------------------------------------------------

void port_info(port_info_t *out) {
#if PICO_RP2040
    out->chip = "RP2040";
    out->core = "Cortex-M0+";
    out->target = "armv6m";
#elif defined(__riscv)
    out->chip = "RP2350";
    out->core = "Hazard3";
    out->target = "riscv32";
#else
    out->chip = "RP2350";
    out->core = "Cortex-M33";
    out->target = "armv8m";
#endif
    out->sdk = "pico-sdk";
    out->sdk_ver = PICO_SDK_VERSION_STRING;
}
