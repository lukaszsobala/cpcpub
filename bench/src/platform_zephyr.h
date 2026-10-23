// What platform.h means under the Zephyr RTOS, on whichever board it runs.
//
// One worker thread per CPU, pinned there before it starts on an SMP build
// (mcu/zephyr/src/port.c); a single-CPU board runs its one worker on its one
// core. Only what measure.c uses is here: a barrier across the CPUs, the
// clock, where a worker is, and allocation.
//
// Included from platform.h when CPCPUB_ZEPHYR is defined; never on its own.

#ifndef CPCPUB_PLATFORM_ZEPHYR_H
#define CPCPUB_PLATFORM_ZEPHYR_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Zephyr's headers are not written for -Wconversion, which this repository
// builds with, so the conversion warnings are off for them and back on for
// everything after.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wsign-conversion"
#include <zephyr/kernel.h>
#pragma GCC diagnostic pop

// A worker always runs on the CPU it was started on, and can say which.
#define PLAT_HAS_AFFINITY 1
#define PLAT_HAS_PROC     0

// ---------------------------------------------------------------------------
// Barriers
// ---------------------------------------------------------------------------
//
// The generation-counted barrier of platform.h's pthread shim, on Zephyr's own
// mutex and condition variable. A waiting worker blocks, and its CPU drops to
// the idle thread -- which sleeps until the next interrupt -- rather than
// taking bus cycles from the CPU still measuring.

typedef struct {
    struct k_mutex   mu;
    struct k_condvar cv;
    unsigned         limit;
    unsigned         waiting;
    unsigned         generation;
} plat_barrier_t;

#define PLAT_BARRIER_SERIAL_THREAD 1

static inline int plat_barrier_init(plat_barrier_t *b, unsigned count) {
    if (count == 0) return -1;
    b->limit = count;
    b->waiting = 0;
    b->generation = 0;
    k_mutex_init(&b->mu);
    k_condvar_init(&b->cv);
    return 0;
}

static inline int plat_barrier_wait(plat_barrier_t *b) {
    k_mutex_lock(&b->mu, K_FOREVER);
    const unsigned mine = b->generation;
    if (++b->waiting == b->limit) {
        b->waiting = 0;
        b->generation++;
        k_condvar_broadcast(&b->cv);
        k_mutex_unlock(&b->mu);
        return PLAT_BARRIER_SERIAL_THREAD;
    }
    // Looped: the generation says whether this barrier is the one that opened.
    while (mine == b->generation) k_condvar_wait(&b->cv, &b->mu, K_FOREVER);
    k_mutex_unlock(&b->mu);
    return 0;
}

static inline int plat_barrier_destroy(plat_barrier_t *b) {
    (void)b;
    return 0;
}

// ---------------------------------------------------------------------------
// Pinning
// ---------------------------------------------------------------------------

static inline int plat_current_cpu(void) {
#if defined(CONFIG_SMP)
    // A pinned thread cannot move, but Zephyr asserts on reading the CPU of
    // one that could, so the read is done with interrupts held off.
    const unsigned int key = irq_lock();
    const int cpu = (int)arch_curr_cpu()->id;
    irq_unlock(key);
    return cpu;
#else
    return 0;
#endif
}

// The thread is pinned before it starts, so "pin to `cpu`" succeeds exactly
// when it was started there.
static inline int plat_pin_self(int cpu) {
    return cpu >= 0 && cpu == plat_current_cpu() ? 0 : -1;
}

static inline int plat_cpu_count(void) { return (int)arch_num_cpus(); }

// ---------------------------------------------------------------------------
// The clock
// ---------------------------------------------------------------------------
//
// The system timer's hardware cycle count where the timer driver keeps it as
// 64 bits; elsewhere the kernel's 64-bit tick count, which has a coarser grain
// but the same source. Either is a hardware counter nothing slews -- the
// unadjusted clock --clock raw asks for on a host -- and a phase's time is the
// difference of two readings half a second apart, so even a 30 us tick is
// 0.006% of it.
#define PLAT_NOW_SEC   1
#define PLAT_CLOCK_RAW 1

static inline double plat_now_sec(void) {
#if defined(CONFIG_TIMER_HAS_64BIT_CYCLE_COUNTER)
    return (double)k_cycle_get_64() / (double)sys_clock_hw_cycles_per_sec();
#else
    return (double)k_uptime_ticks() / (double)CONFIG_SYS_CLOCK_TICKS_PER_SEC;
#endif
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------
//
// On-chip SRAM on most boards Zephyr runs on, so as on the Pico a small buffer
// still measures something; frontend.c sizes the buffer from what is free.
#define PLAT_MEM_MIN_BYTES ((size_t)CHASE_WAYS * 32 * 64)

// The C library's heap. No alignment beyond its own: nothing here has pages.
static inline void *plat_aligned_alloc(size_t align, size_t size) {
    (void)align;
    return malloc(size);
}

static inline void plat_aligned_free(void *p) { free(p); }

#endif  // CPCPUB_PLATFORM_ZEPHYR_H
