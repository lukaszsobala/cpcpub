// The measurement itself; see measure.h.

#define _GNU_SOURCE 1
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "platform.h"
#include "kernels.h"
#include "measure.h"

// A clock the operator supplied with --mhz, in MHz, or 0. See the flag's own
// note in usage(): it exists for the machines that will not say.
double g_mhz_given = 0.0;

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------

#if defined(PLAT_NOW_SEC)
// The platform's own clock, and the only one there is: see its plat_now_sec().
// Whether it is an unadjusted one is a fact about the hardware, so --clock has
// nothing to choose between.
int g_clock_raw = PLAT_CLOCK_RAW;

void set_clock_mode(int use_raw) { (void)use_raw; }

static inline double now_sec(void) { return plat_now_sec(); }

#else

static clockid_t g_clock_id = CLOCK_MONOTONIC;
// What --clock actually got, which is not always what it asked for. Reported
// in place of the request: a result that says it used the raw clock on a system
// with no raw clock is describing a run that did not happen.
int g_clock_raw = 0;

void set_clock_mode(int use_raw) {
#ifdef CLOCK_MONOTONIC_RAW
    g_clock_raw = use_raw ? 1 : 0;
    g_clock_id = use_raw ? CLOCK_MONOTONIC_RAW : CLOCK_MONOTONIC;
#else
    // No unadjusted monotonic clock here -- mingw-w64's clock_gettime offers
    // CLOCK_MONOTONIC and nothing below it -- so --clock raw quietly gets the
    // NTP-disciplined one. Quietly in the sense that nothing fails: the result
    // document says "mono", because that is what was used.
    (void)use_raw;
    g_clock_raw = 0;
    g_clock_id = CLOCK_MONOTONIC;
#endif
}

static inline double now_sec(void) {
    struct timespec ts;
    clock_gettime(g_clock_id, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

#endif

// Xorshift64* PRNG, used only for setup (never inside a timed loop).
static inline uint64_t xs64(uint64_t *s) {
    uint64_t x = *s;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *s = x;
    return x * UINT64_C(2685821657736338717);
}

// ---------------------------------------------------------------------------
// Indirect-dispatch ladders
// ---------------------------------------------------------------------------
//
// What the two are for, and the read-out that walks the second, is in
// measure.h. Sized there so other files can loop over them; checked here.

const size_t g_disp_periods[] = {
    1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 65536
};
_Static_assert(ARRAY_LEN(g_disp_periods) == DISP_SWEEP_N, "DISP_SWEEP_N");

const size_t g_disp_ladder[] = {
    1,                                                          // reference
    8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, // the span
    65536                                                       // floor
};
_Static_assert(ARRAY_LEN(g_disp_ladder) == DISP_LADDER_N, "DISP_LADDER_N");

// ---------------------------------------------------------------------------
// Timed driver
// ---------------------------------------------------------------------------
//
// Runs the kernel in batches sized to land near TARGET_BATCH_SEC, which keeps
// clock_gettime out of the measured work while bounding the overshoot past
// `seconds`. Iteration count and elapsed time both include the final batch, so
// the reported rate is exact regardless of overshoot.
#define TARGET_BATCH_SEC 0.004
#define MAX_BATCH_SEC    0.020

static uint64_t run_timed(kernel_fn fn, void *ctx, double seconds,
                          uint64_t batch_init, double *elapsed_out) {
    const double t0 = now_sec();
    const double t_end = t0 + seconds;
    uint64_t batch = batch_init ? batch_init : 1;
    uint64_t iters = 0;
    double t = t0;

    while (t < t_end) {
        fn(ctx, batch);
        iters += batch;
        const double t2 = now_sec();
        const double dt = t2 - t;
        if (dt < TARGET_BATCH_SEC && batch < (UINT64_C(1) << 30)) {
            batch *= 2;
        } else if (dt > MAX_BATCH_SEC && batch > 1) {
            batch /= 2;
        }
        t = t2;
    }

    double elapsed = t - t0;
    if (elapsed <= 0.0) elapsed = seconds;
    *elapsed_out = elapsed;
    return iters;
}

// Volatile so the compiler cannot delete a warm-up loop as dead code.
static volatile uint64_t g_warmup_sink;

// Wake the core from idle and ramp DVFS before measuring. Warms up with the
// variant about to be measured, so the caches hold the code that is next to run.

static void warmup_spin(const kernel_set_t *k, double sec) {
    if (sec <= 0.0) return;
    int_ctx_t c;
    for (int i = 0; i < LANES; ++i) c.l[i] = (uint64_t)i + 1u;
    c.k[0] = UINT64_C(0x9E3779B97F4A7C15); c.k[1] = UINT64_C(0xBF58476D1CE4E5B9);
    const double t_end = now_sec() + sec;
    while (now_sec() < t_end) {
        k->int_thr(&c, 20000);
    }
    for (int i = 0; i < LANES; ++i) g_warmup_sink ^= c.l[i];
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

static void wsync(worker_t *w) {
    if (w->bar) plat_barrier_wait(w->bar);
}

// Enter one repetition of a phase: line all threads up, warm up (first rep
// only), line up again so every thread starts measuring at the same instant.
// Every thread must call this the same number of times or the barrier deadlocks.
static void phase_begin(worker_t *w, int rep) {
    wsync(w);
    if (rep == 0) warmup_spin(w->ks, w->warmup);
    wsync(w);
}

// Warm-up for the memory phases. A pure-ALU spin does not ramp a DRAM
// controller whose governor scales the DDR clock, so the memory phases would
// start measuring at whatever idle clock it was parked at.
//
// `readonly` must be set once a pointer chase has been built into the buffer:
// the read+write sweep would overwrite the chase links and the next chase would
// dereference a wild pointer. A read-only sweep ramps the governor just as well.
NOINLINE static void mem_warmup(const kernel_set_t *k, void *buf, size_t nbytes,
                                double sec, int readonly) {
    if (!buf || nbytes < 4096 || sec <= 0.0) return;
    const double t_end = now_sec() + sec;
    if (readonly) {
        const uint64_t *p = (const uint64_t *)buf;
        const size_t nq = nbytes / sizeof(uint64_t);
        uint64_t a0 = 0, a1 = 0, a2 = 0, a3 = 0;
        while (now_sec() < t_end) {
            for (size_t i = 0; i + 3 < nq; i += 4) {
                a0 += p[i]; a1 += p[i + 1]; a2 += p[i + 2]; a3 += p[i + 3];
            }
        }
        g_warmup_sink ^= a0 ^ a1 ^ a2 ^ a3;
    } else {
        mem_ctx_t m = { .buf = (uint64_t *)buf,
                        .nqwords = nbytes / sizeof(uint64_t),
                        .acc = {0, 0, 0, 0} };
        while (now_sec() < t_end) {
            k->mem_bw(&m, 1);
        }
        for (int i = 0; i < 4; ++i) g_warmup_sink ^= m.acc[i];
    }
}

// Same contract as phase_begin, but ramps the memory subsystem.
static void phase_begin_mem(worker_t *w, int rep, void *buf, size_t nbytes,
                            int readonly) {
    wsync(w);
    if (rep == 0) mem_warmup(w->ks, buf, nbytes, w->warmup, readonly);
    wsync(w);
}

// Repeated measurements keep the *best* result, not the mean: interference can
// only ever make a run slower, so the fastest run is closest to the machine's
// true capability and averaging would fold the noise in.
static inline void keep_best_rate(double *best, double candidate) {
    if (candidate > *best) *best = candidate;
}
static inline void keep_best_lat(double *best, double candidate) {
    if (candidate > 0.0 && (*best <= 0.0 || candidate < *best)) *best = candidate;
}

// Build `ways` independent random pointer-chase cycles over `buf`, one node per
// `stride` bytes.
//
// Ways are *interleaved* (way w owns nodes w, w+ways, ...), not given contiguous
// slices, so the latency phase (ways=1) and the MLP phase (ways=CHASE_WAYS)
// present the same cache footprint; otherwise the one-chain measurement could
// fit in last-level cache and inflate the apparent MLP. Each way is one full
// cycle over its nodes, so a chase can never collapse into a short cached loop.
static int build_chases(void *buf, size_t nbytes, size_t stride, int ways,
                        uint64_t seed, void **heads_out) {
    const size_t nodes = nbytes / stride;
    const size_t per_way = nodes / (size_t)ways;
    if (per_way < 2) return -1;

    uint32_t *order = (uint32_t *)malloc(per_way * sizeof(uint32_t));
    if (!order) return -1;

    uint8_t *base = (uint8_t *)buf;
    uint64_t s = seed ? seed : UINT64_C(0x243F6A8885A308D3);
    for (int wi = 0; wi < ways; ++wi) {
        for (size_t i = 0; i < per_way; ++i) {
            order[i] = (uint32_t)(i * (size_t)ways + (size_t)wi);
        }
        for (size_t i = per_way - 1; i > 0; --i) {
            const size_t j = (size_t)(xs64(&s) % (uint64_t)(i + 1));
            const uint32_t tmp = order[i];
            order[i] = order[j];
            order[j] = tmp;
        }
        for (size_t i = 0; i < per_way; ++i) {
            void **node = (void **)(base + (size_t)order[i] * stride);
            *node = (void *)(base + (size_t)order[(i + 1) % per_way] * stride);
        }
        heads_out[wi] = (void *)(base + (size_t)order[0] * stride);
    }
    free(order);
    return 0;
}

void *worker(void *arg) {
    worker_t *w = (worker_t *)arg;
    const kernel_set_t *ks = w->ks;

    (void)plat_pin_self(w->cpu);
    w->cpu_observed = plat_current_cpu();

    uint8_t *buf = NULL;
    const size_t nbytes = w->mem_bytes;
    if (nbytes > 0) {
        buf = (uint8_t *)plat_aligned_alloc(4096, nbytes);
        if (!buf) {
            perror("aligned alloc");
        } else {
            memset(buf, 1, nbytes);   // allocate and fault in every page
        }
    }

    uint64_t seed = w->seed;
    double elapsed;

    const int reps = w->reps > 0 ? w->reps : 1;

    // ---- selector-period sweep (diagnostic mode, --disp-sweep) ----
    // Runs the dispatch kernel at every period in g_disp_periods and nothing
    // else: the raw curve the DISPcap number is derived from.
    if (w->disp_sweep) {
        // INT-lat is one dependent 1-cycle op per cycle, so it doubles as a
        // clock reading on targets with no cpufreq node -- see --mhz for what
        // to do where the core does not manage one op per cycle.
        for (int rep = 0; rep < reps; ++rep) {
            int_ctx_t c;
            for (int i = 0; i < LANES; ++i) c.l[i] = xs64(&seed) | 1u;
            for (int i = 0; i < 2; ++i) c.k[i] = xs64(&seed) | 1u;
            phase_begin(w, rep);
            const uint64_t it = run_timed(ks->int_lat, &c, w->seconds, 1024, &elapsed);
            keep_best_rate(&w->int_lat_mops,
                           (double)it * (double)(INT_OPS_PER_STEP * LANES)
                           / 1e6 / elapsed);
            for (int i = 0; i < LANES; ++i) w->checksum ^= c.l[i];
            if (w->cpu_observed >= 0) {
                const double mhz = cpu_cur_mhz(w->cpu_observed);
                if (mhz > w->mhz_observed) w->mhz_observed = mhz;
            }
        }

        uint8_t *idx = (uint8_t *)malloc(DISP_IDX_BYTES);
        if (idx) for (size_t i = 0; i < DISP_IDX_BYTES; ++i) idx[i] = (uint8_t)xs64(&seed);
        // The barrier count must not depend on the allocation succeeding.
        for (int k = 0; k < DISP_SWEEP_N; ++k) {
            for (int rep = 0; rep < reps; ++rep) {
                phase_begin(w, rep);
                if (!idx) continue;
                disp_ctx_t d = { .idx = idx, .mask = g_disp_periods[k] - 1,
                                 .pos = 0, .acc = 1 };
                const uint64_t calls = run_timed(ks->disp, &d, w->seconds, 1024, &elapsed);
                keep_best_rate(&w->sweep_serial[k], (double)calls / 1e6 / elapsed);
                w->checksum ^= d.acc;
            }
            for (int rep = 0; rep < reps; ++rep) {
                phase_begin(w, rep);
                if (!idx) continue;
                disp_ctx_t d = { .idx = idx, .mask = g_disp_periods[k] - 1,
                                 .pos = 0, .acc = 1 };
                const uint64_t calls = run_timed(ks->disp_free, &d, w->seconds, 1024, &elapsed);
                keep_best_rate(&w->sweep_free[k], (double)calls / 1e6 / elapsed);
                w->checksum ^= d.acc;
            }
        }
        free(idx);
        plat_aligned_free(buf);
        return NULL;
    }

    // ---- integer latency: one dependency chain ----
    for (int rep = 0; rep < reps; ++rep) {
        int_ctx_t c;
        for (int i = 0; i < LANES; ++i) c.l[i] = xs64(&seed) | 1u;
        for (int i = 0; i < 2; ++i) c.k[i] = xs64(&seed) | 1u;
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->int_lat, &c, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->int_lat_mops,
                       (double)it * (double)(INT_OPS_PER_STEP * LANES)
                       / 1e6 / elapsed);
        for (int i = 0; i < LANES; ++i) w->checksum ^= c.l[i];
    }

    // ---- integer throughput: LANES independent chains ----
    for (int rep = 0; rep < reps; ++rep) {
        int_ctx_t c;
        for (int i = 0; i < LANES; ++i) c.l[i] = xs64(&seed) | 1u;
        for (int i = 0; i < 2; ++i) c.k[i] = xs64(&seed) | 1u;
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->int_thr, &c, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->int_thr_mops,
                       (double)it * (double)(INT_OPS_PER_STEP * LANES) / 1e6 / elapsed);
        for (int i = 0; i < LANES; ++i) w->checksum ^= c.l[i];
        // Sample the clock while this core is still fully loaded.
        if (w->cpu_observed >= 0) {
            const double mhz = cpu_cur_mhz(w->cpu_observed);
            if (mhz > w->mhz_observed) w->mhz_observed = mhz;
        }
    }

    // ---- integer multiply throughput: LANES independent multiply chains ----
    for (int rep = 0; rep < reps; ++rep) {
        mul_ctx_t c;
        for (int i = 0; i < LANES; ++i) c.l[i] = xs64(&seed) | 1u;
        c.m = xs64(&seed) | 1u;
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->mul_thr, &c, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->mul_thr_mops,
                       (double)it * (double)(MUL_OPS_PER_STEP * LANES) / 1e6 / elapsed);
        for (int i = 0; i < LANES; ++i) w->checksum ^= c.l[i];
    }

    // Converges to the fixed point b/(1-c) and stays there: bounded forever.
    const double co = 0.5;

    // ---- FP latency ----
    for (int rep = 0; rep < reps; ++rep) {
        fp_ctx_t f;
        f.c = co;
        for (int i = 0; i < LANES; ++i) { f.x[i] = 1.0 + (double)i; f.b[i] = 1.0 + 0.25 * (double)i; }
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->fp_lat, &f, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->fp_lat_mflops,
                       (double)it * (double)FP_OPS_PER_STEP / 1e6 / elapsed);
        w->checksum ^= (uint64_t)(int64_t)(f.x[0] * 1e6);
    }

    // ---- FP throughput ----
    for (int rep = 0; rep < reps; ++rep) {
        fp_ctx_t f;
        f.c = co;
        for (int i = 0; i < LANES; ++i) { f.x[i] = 1.0 + (double)i; f.b[i] = 1.0 + 0.25 * (double)i; }
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->fp_thr, &f, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->fp_thr_mflops,
                       (double)it * (double)(FP_OPS_PER_STEP * LANES) / 1e6 / elapsed);
        for (int i = 0; i < LANES; ++i) {
            w->checksum ^= (uint64_t)(int64_t)(f.x[i] * 1e6);
        }
    }

    // ---- FP32 latency and throughput: the two above in single precision ----
    // Same coefficient, same starting values, same fixed point; reported
    // separately and kept out of the score.
    const float co32 = 0.5f;
    for (int rep = 0; rep < reps; ++rep) {
        fp32_ctx_t f;
        f.c = co32;
        for (int i = 0; i < LANES; ++i) { f.x[i] = 1.0f + (float)i; f.b[i] = 1.0f + 0.25f * (float)i; }
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->fp32_lat, &f, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->fp32_lat_mflops,
                       (double)it * (double)FP_OPS_PER_STEP / 1e6 / elapsed);
        w->checksum ^= (uint64_t)(int64_t)((double)f.x[0] * 1e6);
    }
    for (int rep = 0; rep < reps; ++rep) {
        fp32_ctx_t f;
        f.c = co32;
        for (int i = 0; i < LANES; ++i) { f.x[i] = 1.0f + (float)i; f.b[i] = 1.0f + 0.25f * (float)i; }
        phase_begin(w, rep);
        const uint64_t it = run_timed(ks->fp32_thr, &f, w->seconds, 1024, &elapsed);
        keep_best_rate(&w->fp32_thr_mflops,
                       (double)it * (double)(FP_OPS_PER_STEP * LANES) / 1e6 / elapsed);
        for (int i = 0; i < LANES; ++i) {
            w->checksum ^= (uint64_t)(int64_t)((double)f.x[i] * 1e6);
        }
    }

    // ---- indirect dispatch: one point per selector period ----
    // Each ladder point gets half a phase's time; less than that widens
    // run-to-run spread noticeably on small cores, more buys little.
    {
        uint8_t *idx = (uint8_t *)malloc(DISP_LADDER_BYTES);
        if (idx) {
            for (size_t i = 0; i < DISP_LADDER_BYTES; ++i) idx[i] = (uint8_t)xs64(&seed);
        }
        const double t_pt = w->seconds / 2.0;
        for (int k = 0; k < DISP_LADDER_N; ++k) {
            for (int rep = 0; rep < reps; ++rep) {
                // Only the first point pays for a warm-up; the barrier is still
                // entered once per point per rep by every thread.
                phase_begin(w, k == 0 ? rep : 1);
                if (!idx) continue;
                disp_ctx_t d = { .idx = idx, .mask = g_disp_ladder[k] - 1,
                                 .pos = 0, .acc = 1 };
                const uint64_t calls = run_timed(ks->disp, &d, t_pt, 1024, &elapsed);
                keep_best_rate(&w->disp_ladder[k], (double)calls / 1e6 / elapsed);
                w->checksum ^= d.acc;
            }
        }
        free(idx);
    }

    const int mem_ok = (buf != NULL && nbytes >= PLAT_MEM_MIN_BYTES);

    // ---- memory bandwidth: sequential read+write sweep ----
    for (int rep = 0; rep < reps; ++rep) {
        phase_begin_mem(w, rep, buf, nbytes, 0);
        if (!mem_ok) continue;
        mem_ctx_t m = { .buf = (uint64_t *)buf,
                        .nqwords = nbytes / sizeof(uint64_t),
                        .acc = {0, 0, 0, 0} };
        const uint64_t passes = run_timed(ks->mem_bw, &m, w->seconds, 1, &elapsed);
        const double bytes = (double)passes * (double)nbytes * 2.0; // read + write
        keep_best_rate(&w->mem_gbps, bytes / 1e9 / elapsed);
        const double dm = dram_mhz();
        if (dm > w->dram_mhz_observed) w->dram_mhz_observed = dm;
        for (int i = 0; i < 4; ++i) w->checksum ^= m.acc[i];
    }

    // ---- memory latency: one dependent chase over the whole buffer ----
    void *heads[CHASE_WAYS];
    int chase_ok = mem_ok && build_chases(buf, nbytes, 64, 1,
                                          w->seed ^ UINT64_C(0xA5A5A5A5), heads) == 0;
    for (int rep = 0; rep < reps; ++rep) {
        phase_begin_mem(w, rep, buf, nbytes, 1);
        if (!chase_ok) continue;
        chase_ctx_t ch;
        ch.cur[0] = (void **)heads[0];
        const uint64_t hops = run_timed(ks->mem_lat, &ch, w->seconds, 4096, &elapsed);
        keep_best_lat(&w->mem_lat_ns, elapsed / (double)hops * 1e9);
        w->checksum ^= (uint64_t)(uintptr_t)ch.cur[0];
    }

    // ---- memory-level parallelism: CHASE_WAYS chases over the same buffer ----
    // Interleaved, so the footprint matches the latency phase above.
    chase_ok = mem_ok && build_chases(buf, nbytes, 64, CHASE_WAYS,
                                      w->seed ^ UINT64_C(0xA5A5A5A5), heads) == 0;
    for (int rep = 0; rep < reps; ++rep) {
        phase_begin_mem(w, rep, buf, nbytes, 1);
        if (!chase_ok) continue;
        chase_ctx_t ch;
        for (int i = 0; i < CHASE_WAYS; ++i) ch.cur[i] = (void **)heads[i];
        const uint64_t hops = run_timed(ks->mem_mlp, &ch, w->seconds, 1024, &elapsed);
        keep_best_lat(&w->mem_mlp_ns,
                      elapsed / ((double)hops * (double)CHASE_WAYS) * 1e9);
        for (int i = 0; i < CHASE_WAYS; ++i) {
            w->checksum ^= (uint64_t)(uintptr_t)ch.cur[i];
        }
    }

    plat_aligned_free(buf);
    return NULL;
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------

static double geomean(const double *v, int n) {
    double s = 0.0;
    int m = 0;
    for (int i = 0; i < n; ++i) {
        if (v[i] > 0.0) { s += log(v[i]); m++; }
    }
    return m > 0 ? exp(s / (double)m) : 0.0;
}

// Reduce the dispatch ladder to the numbers the tables report.
//
//   *thr  calls/s once the pattern has been learned -- the plateau, i.e.
//         front-end call throughput. Bigger is better.
//   *cap  longest repeating call sequence the core still predicts, in calls:
//         the floor-normalised curve integrated over log2(period).
//   *gain plateau / unpredictable floor: what prediction is worth on this core
//         at all. Used to reject a flat curve.
//   *span the same integral before exponentiation, i.e. capacity in ladder
//         steps. The score uses this because *cap spans orders of magnitude
//         between cores and would dominate a geomean of rates.
//
// Returns DISP_NA if the ladder was never measured, DISP_NONE if the core
// showed no prediction win at any period including a single repeated target,
// DISP_OK otherwise.
static int disp_summary(const worker_t *w, double *thr, double *cap,
                        double *gain, double *span) {
    *thr = 0.0; *cap = 0.0; *gain = 0.0; *span = 0.0;
    double plateau = 0.0;
    for (int i = 0; i < DISP_PLATEAU_PTS; ++i) {
        const double v = w->disp_ladder[DISP_SPAN_FIRST + i];
        if (v <= 0.0) return DISP_NA;
        if (v > plateau) plateau = v;
    }
    const double floor_rate = w->disp_ladder[DISP_LADDER_N - 1];
    const double ref_rate = w->disp_ladder[DISP_REF_IDX];
    if (plateau <= 0.0 || floor_rate <= 0.0 || ref_rate <= 0.0) return DISP_NA;

    *thr = plateau;
    *gain = plateau / floor_rate;

    if (*gain < DISP_MIN_GAIN) {
        // Curve is flat over the span. If the single-target reference is no
        // faster either, the core predicts no indirect targets at all; if it is
        // faster, capacity is below the first span point rather than unknown.
        if (ref_rate / floor_rate < DISP_MIN_GAIN) return DISP_NONE;
        *cap = (double)g_disp_ladder[DISP_SPAN_FIRST] / 2.0;
        return DISP_OK;
    }

    for (int i = DISP_SPAN_FIRST; i <= DISP_SPAN_LAST; ++i) {
        double q = (w->disp_ladder[i] - floor_rate) / (plateau - floor_rate);
        if (q > 1.0) q = 1.0;
        if (q < 0.0) q = 0.0;
        *span += q;
    }
    *cap = (double)g_disp_ladder[DISP_SPAN_FIRST] * pow(2.0, *span - 1.0);
    return DISP_OK;
}

// Synthetic single-core score, spanning compute *and* the two things that
// separate a big core from a little one: indirect dispatch and overlapped cache
// misses. The per-component scale factors are constant factors on a geomean, so
// ratios between cores do not depend on them.
static double core_score(const worker_t *w) {
    double v[6];
    double disp_thr, disp_cap, disp_gain, disp_span;
    const int st = disp_summary(w, &disp_thr, &disp_cap, &disp_gain, &disp_span);
    v[0] = w->int_thr_mops;
    v[1] = w->fp_thr_mflops;
    v[4] = w->mul_thr_mops * 4.0;
    v[2] = disp_thr * 20.0;
    // Predictor capacity in ladder steps, +1 so a core with no indirect
    // prediction still scores poorly instead of dropping out of the geomean.
    v[5] = st == DISP_NA ? 0.0 : (disp_span + 1.0) * 2000.0;
    // Random-access rate with all chases in flight, in millions/s.
    v[3] = w->mem_mlp_ns > 0.0 ? (1000.0 / w->mem_mlp_ns) * 100.0 : 0.0;
    return geomean(v, ARRAY_LEN(v));
}

// The same six components as core_score(), rescaled to the whole machine: the
// throughputs are the ones aggregate() summed, and the random-access rate is
// multiplied by the thread count for the same reason -- n threads chase n sets
// of pointers at once, so the machine retires n times one thread's misses.
// DISPcap span is left as aggregate() averaged it: predictor size is a property
// of a core and does not add up across them.
//
// The result is therefore ~n times a single core's score and is not comparable
// with one. It answers "how much machine is there"; core_score() answers "how
// good is a core".
static double machine_score(const result_t *t, int n) {
    double v[6];
    v[0] = t->int_thr;
    v[1] = t->fp_thr;
    v[4] = t->mul_thr * 4.0;
    v[2] = t->disp_thr * 20.0;
    v[5] = t->disp_status == DISP_NA ? 0.0 : (t->disp_span + 1.0) * 2000.0;
    v[3] = t->mem_lat8_ns > 0.0
         ? (double)n * (1000.0 / t->mem_lat8_ns) * 100.0 : 0.0;
    return geomean(v, ARRAY_LEN(v));
}

result_t summarize(const worker_t *w, int cpu) {
    result_t r;
    memset(&r, 0, sizeof(r));
    r.cpu = cpu;
    r.int_lat = w->int_lat_mops;
    r.int_thr = w->int_thr_mops;
    r.mul_thr = w->mul_thr_mops;
    r.fp_lat  = w->fp_lat_mflops;
    r.fp_thr  = w->fp_thr_mflops;
    r.fp32_lat = w->fp32_lat_mflops;
    r.fp32_thr = w->fp32_thr_mflops;
    r.mem_gbps    = w->mem_gbps;
    r.mem_lat_ns  = w->mem_lat_ns;
    r.mem_lat8_ns = w->mem_mlp_ns;
    r.ilp  = r.int_lat > 0.0 ? r.int_thr / r.int_lat : 0.0;
    r.filp = r.fp_lat  > 0.0 ? r.fp_thr  / r.fp_lat  : 0.0;
    r.mlp  = r.mem_lat8_ns > 0.0 ? r.mem_lat_ns / r.mem_lat8_ns : 0.0;
    r.disp_status = disp_summary(w, &r.disp_thr, &r.disp_cap, &r.disp_gain,
                                 &r.disp_span);
    r.score = core_score(w);
    r.shared = w->shared;

    // INT-lat is one dependent 1-cycle op per cycle by construction, so it
    // doubles as a clock estimate where nothing on the machine will say -- but
    // only by construction, so every source that knows is asked first: the
    // clock sampled under load, then cpufreq's ceiling, then whatever the
    // device tree declares for a board that has no cpufreq driver at all.
    if (g_mhz_given > 0.0) {
        r.mhz = g_mhz_given;
        r.mhz_src = MHZ_GIVEN;
    } else {
        r.mhz = w->mhz_observed;
        if (r.mhz <= 0.0) {
            // Nothing was sampled under load, so what is left is a rated
            // clock: cpufreq's ceiling (or the registry's on Windows, or
            // sysctl's on an Intel Mac), then the device tree's declaration --
            // unless INT-lat proves it a base clock that turbo outran.
            r.mhz = rated_mhz(cpu);
            if (!rated_fits(r.mhz, r.int_lat)) r.mhz = 0.0;
            if (r.mhz > 0.0)  r.mhz_src = MHZ_RATED;
        }
        // A CPU shared with another worker gave each a share of its time, and
        // INT-lat measured the share: no estimate, and the clock is unknown.
        if (r.mhz <= 0.0 && !w->shared) {
            r.mhz = r.int_lat;
            r.mhz_src = MHZ_ESTIMATED;
        }
    }
    return r;
}

// INT-lat is a floor under the clock: a chain of dependent 1-cycle adds cannot
// finish more than one per cycle on any core. A rated clock far below it is
// not the clock the core ran at. Windows' `~MHz` and an Intel Mac's
// `hw.cpufrequency` are the *base* clock, which turbo leaves behind: an Intel
// N100 is rated 806 MHz there and runs its INT-lat at 3370. The slack is for
// clocks that are named a little under what they run -- an RK3566 whose
// cpufreq says 1800 runs INT-lat at 1907 -- and is far short of what turbo
// does.
#define RATED_SLACK 1.1

int rated_fits(double rated, double int_lat) {
    return rated > 0.0 && int_lat <= rated * RATED_SLACK;
}

int settle_rated(result_t *rows, int n, const result_t *also, int n_also) {
    int dropped = 0;
    for (int i = 0; i < n; ++i) {
        result_t *r = &rows[i];
        if (r->mhz_src != MHZ_RATED || r->mhz <= 0.0 || r->cpu < 0) continue;
        double floor = r->int_lat;
        for (int j = 0; j < n; ++j) {
            if (rows[j].cpu == r->cpu && rows[j].int_lat > floor) floor = rows[j].int_lat;
        }
        for (int j = 0; j < n_also; ++j) {
            if (also[j].cpu == r->cpu && also[j].int_lat > floor) floor = also[j].int_lat;
        }
        if (rated_fits(r->mhz, floor)) continue;
        // What summarize() would have said had it known: the estimate, or
        // unknown on a shared CPU.
        r->mhz     = r->shared ? 0.0 : r->int_lat;
        r->mhz_src = r->shared ? MHZ_READ : MHZ_ESTIMATED;
        dropped++;
    }
    return dropped;
}

void aggregate(const result_t *rows, int n, result_t *tot) {
    memset(tot, 0, sizeof(*tot));
    tot->cpu = -1;
    double lat_n = 0, mlp_n = 0, cap_n = 0, mhz_n = 0;
    int cap_none = 0;
    for (int i = 0; i < n; ++i) {
        const result_t *r = &rows[i];
        tot->int_lat   += r->int_lat;
        tot->int_thr   += r->int_thr;
        tot->mul_thr   += r->mul_thr;
        tot->fp_lat    += r->fp_lat;
        tot->fp_thr    += r->fp_thr;
        tot->fp32_lat  += r->fp32_lat;
        tot->fp32_thr  += r->fp32_thr;
        tot->mem_gbps  += r->mem_gbps;
        tot->disp_thr  += r->disp_thr;
        tot->disp_gain += r->disp_gain;
        tot->disp_span += r->disp_span;
        if (r->mhz > 0.0)         { tot->mhz += r->mhz; mhz_n++; }
        if (r->mhz > 0.0 && r->mhz_src > tot->mhz_src) tot->mhz_src = r->mhz_src;
        if (r->mem_lat_ns > 0.0)  { tot->mem_lat_ns += r->mem_lat_ns; lat_n++; }
        if (r->mem_lat8_ns > 0.0) { tot->mem_lat8_ns += r->mem_lat8_ns; mlp_n++; }
        if (r->disp_status == DISP_OK && r->disp_cap > 0.0) {
            tot->disp_cap += r->disp_cap;
            cap_n++;
        } else if (r->disp_status == DISP_NONE) {
            cap_none++;
        }
    }
    const double d = n > 0 ? (double)n : 1.0;
    if (mhz_n > 0) tot->mhz /= mhz_n;
    if (lat_n > 0) tot->mem_lat_ns /= lat_n;
    if (mlp_n > 0) tot->mem_lat8_ns /= mlp_n;
    if (cap_n > 0) tot->disp_cap /= cap_n;
    tot->disp_gain /= d;
    tot->disp_span /= d;
    tot->disp_status = cap_n > 0 ? DISP_OK : (cap_none > 0 ? DISP_NONE : DISP_NA);
    tot->ilp  = tot->int_lat > 0.0 ? tot->int_thr / tot->int_lat : 0.0;
    tot->filp = tot->fp_lat  > 0.0 ? tot->fp_thr  / tot->fp_lat  : 0.0;
    tot->mlp  = tot->mem_lat8_ns > 0.0 ? tot->mem_lat_ns / tot->mem_lat8_ns : 0.0;
    tot->score = machine_score(tot, n);   // last: reads the fields set above
}
