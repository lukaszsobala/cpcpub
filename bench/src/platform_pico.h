// What platform.h means on a Raspberry Pi RP2040 or RP2350, under the pico-sdk.
//
// There is no operating system. Each of the two cores runs exactly one worker,
// put there by the front end (mcu/pico/port.c), so "pinning" describes where a
// worker was started rather than asking a scheduler for anything, and nothing
// else shares the core with it. Only what measure.c uses is here: a barrier
// across the two cores, the clock, where a worker is, and allocation.
//
// Included from platform.h when CPCPUB_PICO is defined; never on its own.

#ifndef CPCPUB_PLATFORM_PICO_H
#define CPCPUB_PLATFORM_PICO_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pico.h"
#include "hardware/sync.h"
#include "hardware/timer.h"

// A worker always runs on the core it was started on, and can say which.
#define PLAT_HAS_AFFINITY 1
#define PLAT_HAS_PROC     0

// ---------------------------------------------------------------------------
// Barriers
// ---------------------------------------------------------------------------
//
// The same generation-counted barrier as the pthread shim in platform.h, with
// a hardware spin lock where that has a mutex. The waiting side sleeps in WFE
// rather than spinning on the counter: the core that is still measuring shares
// the SRAM banks with this one, and a core polling memory in a tight loop
// would take bus cycles from the very measurement it is waiting for. The core
// that opens the barrier sends the event that wakes it.

typedef struct {
    spin_lock_t      *lock;
    unsigned          limit;
    volatile unsigned waiting;
    volatile unsigned generation;
} plat_barrier_t;

#define PLAT_BARRIER_SERIAL_THREAD 1

static inline int plat_barrier_init(plat_barrier_t *b, unsigned count) {
    if (count == 0) return -1;
    const int num = spin_lock_claim_unused(false);
    if (num < 0) return -1;
    b->lock = spin_lock_instance((uint)num);
    b->limit = count;
    b->waiting = 0;
    b->generation = 0;
    return 0;
}

static inline int plat_barrier_wait(plat_barrier_t *b) {
    const uint32_t irq = spin_lock_blocking(b->lock);
    const unsigned mine = b->generation;
    if (++b->waiting == b->limit) {
        b->waiting = 0;
        b->generation = mine + 1;
        spin_unlock(b->lock, irq);
        __sev();
        return PLAT_BARRIER_SERIAL_THREAD;
    }
    spin_unlock(b->lock, irq);
    // Looped: WFE also returns for events nobody here sent.
    while (b->generation == mine) __wfe();
    __mem_fence_acquire();
    return 0;
}

static inline int plat_barrier_destroy(plat_barrier_t *b) {
    // Claimed per run, so released per run: there are only 32 of them.
    spin_lock_unclaim(spin_lock_get_num(b->lock));
    return 0;
}

// ---------------------------------------------------------------------------
// Pinning
// ---------------------------------------------------------------------------

// Nothing can move a worker, so "pin to `cpu`" succeeds exactly when the front
// end already started it there.
static inline int plat_pin_self(int cpu) {
    return cpu >= 0 && (uint)cpu == get_core_num() ? 0 : -1;
}

static inline int plat_current_cpu(void) { return (int)get_core_num(); }

static inline int plat_cpu_count(void) { return NUM_CORES; }

// ---------------------------------------------------------------------------
// The clock
// ---------------------------------------------------------------------------
//
// The microsecond timer: a 64-bit count of ticks from the crystal, which
// nothing slews or steps -- an unadjusted monotonic clock, which is what --clock
// raw asks for on a host. A microsecond against batches of about 4 ms (see
// run_timed()) is a resolution of 0.025%.
#define PLAT_NOW_SEC   1
#define PLAT_CLOCK_RAW 1

static inline double plat_now_sec(void) { return (double)time_us_64() * 1e-6; }

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------
//
// SRAM with no cache in front of it, so a small buffer measures what a large
// one would and the floor measure.c applies on a host does not fit here. Large
// enough that each of the CHASE_WAYS chases is a cycle of at least 32 nodes.
#define PLAT_MEM_MIN_BYTES ((size_t)CHASE_WAYS * 32 * 64)

// No pages and no cache lines to align to: malloc's eight bytes are all a
// 64-bit access needs, and asking for 4 KiB here would lose up to that much of
// a 264 KiB machine to padding.
static inline void *plat_aligned_alloc(size_t align, size_t size) {
    (void)align;
    return malloc(size);
}

static inline void plat_aligned_free(void *p) { free(p); }

#endif  // CPCPUB_PLATFORM_PICO_H
