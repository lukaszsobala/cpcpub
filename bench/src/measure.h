// The measurement itself: the timed driver, the phases one worker runs, and the
// arithmetic that turns what it measured into a result record.
//
// Shared by every front end, so that a number means the same thing whichever
// program printed it. bench.c is the desktop one: it parses the command line,
// starts one thread per worker, probes the host and reports. What the code
// here needs from the host and cannot get portably is the three hooks at the
// bottom, which each front end supplies.

#ifndef CPCPUB_MEASURE_H
#define CPCPUB_MEASURE_H

#include <stddef.h>
#include <stdint.h>

#include "platform.h"
#include "kernels.h"

#ifndef ARRAY_LEN
#define ARRAY_LEN(x) ((int)(sizeof(x) / sizeof((x)[0])))
#endif

// The smallest buffer the memory phases will measure; below it they are
// skipped. Half a megabyte on a host, where anything less is a cache test and
// not much of one. A platform whose memory is all on-chip SRAM, with no cache
// in front of it, sets its own: there a small buffer measures the same thing a
// large one would.
#ifndef PLAT_MEM_MIN_BYTES
#define PLAT_MEM_MIN_BYTES ((size_t)CHASE_WAYS * 65536)
#endif

// A clock the operator supplied with --mhz, in MHz, or 0. summarize() prefers
// it to anything the machine says.
extern double g_mhz_given;

// What --clock actually got, which is not always what it asked for: see
// set_clock_mode(). The clock itself is private to measure.c, which is the only
// thing that reads it -- and static, so the compiler may keep it in a register
// across the clock_gettime calls of a warm-up loop.
extern int g_clock_raw;
void set_clock_mode(int use_raw);

// ---------------------------------------------------------------------------
// Indirect-dispatch ladders
// ---------------------------------------------------------------------------

// Selector-sequence periods swept by --disp-sweep, in calls.
//
// A uniformly random target sequence is unpredictable for every predictor ever
// built, so a "random" dispatch loop measures the *cost of a mispredict*, which
// is pipeline-depth-dominated and ranks cores by how shallow they are. What
// separates a big front end from a little one is predictor capacity: the length
// of deterministic pattern it can still learn. So the selector stream is a fixed
// random pattern of period L, repeated, and L is swept. Small L: everyone learns
// it. Huge L: nobody does. The knee in between reads out the predictor. The last
// entry is the unpredictable floor.
#define DISP_SWEEP_N   16
extern const size_t g_disp_periods[DISP_SWEEP_N];
// Backing array is sized for the largest period; smaller periods use a prefix.
#define DISP_IDX_BYTES ((size_t)65536)

// The ladder the suite itself walks: a single-target reference, a x2 geometric
// span, and the unpredictable floor.
//
// Period 1 calls the same function every time, which any core with a BTB
// predicts. It is not part of the span; it exists to tell "predicts nothing
// beyond a single target" apart from "predicts everything the ladder can throw
// at it", which look identical from the span alone. The floor must be the
// genuinely random point: a floor taken from the end of the ladder is still
// partly predicted on some cores, and a floor set too high compresses every q
// beneath it.
#define DISP_LADDER_N     14
extern const size_t g_disp_ladder[DISP_LADDER_N];
#define DISP_LADDER_BYTES ((size_t)65536)
#define DISP_REF_IDX      0
#define DISP_SPAN_FIRST   1
#define DISP_SPAN_LAST    (DISP_LADDER_N - 2)
// Span points establishing the "everyone predicts this" plateau, starting at
// DISP_SPAN_FIRST. The best is taken, not the mean: the plateau is the reference
// the whole curve is normalised against, so one point depressed by interference
// would inflate every q beneath it.
#define DISP_PLATEAU_PTS  3
// The capacity read-out integrates the curve rather than crossing a threshold
// (a threshold lands on the knee, where noise moves it between ladder steps). At
// each ladder point the rate is normalised to
//   q = (rate - floor) / (plateau - floor)      clipped to [0,1]
// i.e. how much of the available prediction win the core is still getting.
// Summing q gives the width of the predicted region in log2(period) units, so
//   capacity = first_period * 2^(sum(q) - 1)
// which returns exactly L for an ideal core that predicts everything up to L and
// nothing beyond. Dividing the floor out is what stops a shallow pipeline from
// scoring as a good predictor: the floor *is* the mispredict penalty.
//
// Below this plateau/floor ratio the curve is flat and there is no span to
// integrate -- the core has no usable indirect prediction.
#define DISP_MIN_GAIN     1.15

// What a result's disp_status says about the dispatch ladder: never measured,
// measured, or no prediction win at any period at all. disp_summary() in
// measure.c is the read-out that decides.
#define DISP_NA   0
#define DISP_OK   1
#define DISP_NONE 2

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

// One thread's share of a run: what to measure going in, what it measured
// coming out. The caller fills in the first block and starts worker() on it.
typedef struct {
    int    id;
    int    cpu;            // CPU to pin to, or -1 for unpinned
    size_t mem_bytes;      // per-thread buffer for the memory phases (0 = skip)
    double seconds;        // measured time per phase
    double warmup;         // warm-up seconds before each phase
    int    reps;           // repetitions per phase; the best one is kept
    plat_barrier_t *bar;
    uint64_t seed;
    const kernel_set_t *ks;   // the build variant this run measures

    volatile uint64_t checksum;

    // results
    double int_lat_mops;
    double int_thr_mops;
    double mul_thr_mops;   // integer 64-bit multiplies per second
    double fp_lat_mflops;
    double fp_thr_mflops;
    double fp32_lat_mflops;   // single precision; not part of the score
    double fp32_thr_mflops;
    double mem_gbps;
    double mem_lat_ns;     // one dependent chain
    double mem_mlp_ns;     // CHASE_WAYS chains in flight, per access
    double disp_ladder[DISP_LADDER_N];  // Mcall/s at each selector period
    int    disp_sweep;     // run only the selector-period sweep, not the suite
    double sweep_serial[DISP_SWEEP_N];  // Mcall/s per period, dependent chain
    double sweep_free[DISP_SWEEP_N];    // Mcall/s per period, independent calls
    double mhz_observed;   // sampled while the core was under load
    double dram_mhz_observed; // DRAM controller clock during the memory phases
    int    cpu_observed;   // where it actually ran, -1 where unknowable
} worker_t;

// Runs the whole suite (or, with disp_sweep, only the selector-period sweep)
// on the calling thread. A pthread start routine, hence the signature.
void *worker(void *arg);

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

// One row of results: a single core in the per-core sweep, one thread of a
// multi-threaded run, or the aggregate over all of them.
typedef struct {
    int    cpu;              // pinned CPU, or -1
    double mhz;              // 0 if unknown
    // Where `mhz` came from, because the four are worth different amounts to
    // a reader: sampled under load, asserted by the operator, a ceiling the
    // machine declares (cpufreq max, the device tree, the Windows registry --
    // rated, not observed), or inferred from INT-lat under an assumption the
    // core may not honour. Ordered by trust, weakest last: aggregate() keeps
    // the weakest source of any row it folds in.
    enum { MHZ_READ, MHZ_GIVEN, MHZ_RATED, MHZ_ESTIMATED } mhz_src;
    double int_lat, int_thr, mul_thr, fp_lat, fp_thr;
    double fp32_lat, fp32_thr;   // single precision, outside the score
    double mem_gbps, mem_lat_ns, mem_lat8_ns;
    double ilp, filp, mlp;
    double disp_thr, disp_cap, disp_gain, disp_span;
    int    disp_status;
    double score;
} result_t;

// One worker's measurements as a result row, with `cpu` as its CPU and its
// clock resolved from the best source there is: see mhz_src above.
result_t summarize(const worker_t *w, int cpu);
// The whole-machine row over `n` per-thread rows.
void aggregate(const result_t *rows, int n, result_t *tot);

// ---------------------------------------------------------------------------
// Supplied by the front end
// ---------------------------------------------------------------------------
//
// Each returns 0 where the host has no such number, and the result then says
// so: a clock nobody could read is reported as unknown or estimated, never
// made up.

// The clock `cpu` is running at now, in MHz. Sampled under load.
double cpu_cur_mhz(int cpu);
// The DRAM controller's clock now, in MHz.
double dram_mhz(void);
// The clock `cpu` is rated for, in MHz: a ceiling the machine declares, used
// only when nothing was sampled under load.
double rated_mhz(int cpu);

#endif  // CPCPUB_MEASURE_H
