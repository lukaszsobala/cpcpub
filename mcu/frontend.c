// The benchmark on a microcontroller: the front end bench.c is on a desktop,
// for a machine with no operating system and no command line of its own.
//
// It waits on the serial console for a line of options -- the same ones the
// desktop binary takes, where they mean anything here -- runs the measurement
// in bench/src exactly as the desktop does, and prints the same tables. With
// --json (or --tsv) the result is the same cpu-bench/1 document, written
// between two marker lines so a program reading the console can pick it out
// of everything else that arrives there: mcu/cpcpub-serial.py does.
//
// Everything that differs between chips is in a port; see port.h.

#define _GNU_SOURCE 1
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "platform.h"
#include "kernels.h"
#include "measure.h"
#include "report.h"
#include "port.h"

#ifndef CPCPUB_VERSION
#define CPCPUB_VERSION "dev"
#endif
// The compiler and flags the measured code was built with, passed in by the
// build as bench/Makefile passes them to bench.c.
#ifndef BUILD_CC
#define BUILD_CC "cc"
#endif
#ifndef BUILD_FLAGS
#define BUILD_FLAGS ""
#endif

// What a reader of the console looks for. The BEGIN line names the format.
#define MARK_BEGIN "-----BEGIN CPCPUB %s-----\n"
#define MARK_END   "-----END CPCPUB %s-----\n"
#define PROMPT     "cpcpub> "

typedef enum { FMT_TEXT, FMT_JSON, FMT_TSV } out_fmt_t;

// The most cores a run here can use. Two on every chip this is for; the cap
// is for the test port, which runs on whatever desktop it is built on.
#define MAX_CORES 8

static int core_count(void) {
    const int n = plat_cpu_count();
    return n > MAX_CORES ? MAX_CORES : n;
}

// ---------------------------------------------------------------------------
// Build variants
// ---------------------------------------------------------------------------
//
// As in bench.c: four compilations of the kernels, of which the ones this
// target cannot tell apart are skipped by a plain --variants.

typedef struct {
    const kernel_set_t *k;
    int distinct;
} variant_t;

static const variant_t g_variants[] = {
    { &kernels_scalar_nofma, 1 },
    { &kernels_vector_nofma, TARGET_HAS_SIMD },
    { &kernels_scalar_fma,   TARGET_HAS_FMA },
    { &kernels_vector_fma,   TARGET_HAS_SIMD && TARGET_HAS_FMA },
};
#define N_VARIANTS ARRAY_LEN(g_variants)

static const variant_t *find_variant(const char *name) {
    for (int i = 0; i < N_VARIANTS; ++i) {
        if (!strcmp(name, g_variants[i].k->name)) return &g_variants[i];
    }
    return NULL;
}

static void list_variants(void) {
    printf("variants in this build (* = distinct on this target):\n");
    for (int i = 0; i < N_VARIANTS; ++i) {
        const kernel_set_t *k = g_variants[i].k;
        printf("  %c %-13s %s\n", g_variants[i].distinct ? '*' : ' ', k->name,
               k->flags);
    }
}

// NULL spec: the distinct ones. "all": all four. Otherwise a comma list.
static int select_variants(const char *spec, const variant_t **sel) {
    int n = 0;
    if (!spec || !strcmp(spec, "all")) {
        for (int i = 0; i < N_VARIANTS; ++i) {
            if (spec || g_variants[i].distinct) sel[n++] = &g_variants[i];
        }
        return n;
    }
    const char *p = spec;
    while (*p) {
        char name[32];
        const char *comma = strchr(p, ',');
        const size_t len = comma ? (size_t)(comma - p) : strlen(p);
        if (len == 0 || len >= sizeof(name)) return -1;
        memcpy(name, p, len);
        name[len] = '\0';
        const variant_t *v = find_variant(name);
        if (!v || n >= N_VARIANTS) return -1;
        sel[n++] = v;
        p = comma ? comma + 1 : p + len;
    }
    return n;
}

// ---------------------------------------------------------------------------
// The console
// ---------------------------------------------------------------------------

// One line, echoed as it is typed so a person at a terminal sees it. Returns
// its length; a line too long for the buffer is cut, not split.
static size_t read_line(char *buf, size_t cap) {
    size_t n = 0;
    for (;;) {
        const int c = port_getchar();
        if (c == '\r' || c == '\n') {
            if (n == 0) continue;        // the \n of a \r\n, or an empty line
            printf("\n");
            buf[n] = '\0';
            return n;
        }
        if (c == 0x08 || c == 0x7f) {    // backspace, either spelling
            if (n > 0) { --n; printf("\b \b"); fflush(stdout); }
            continue;
        }
        if (c < 0x20 || c > 0x7e || n + 1 >= cap) continue;
        buf[n++] = (char)c;
        putchar(c);
        fflush(stdout);
    }
}

// Split on spaces in place. No quoting: no option here takes text.
static int split_args(char *line, char **argv, int max) {
    int n = 0;
    for (char *tok = strtok(line, " \t"); tok && n < max; tok = strtok(NULL, " \t")) {
        argv[n++] = tok;
    }
    return n;
}

// ---------------------------------------------------------------------------
// Describing the build and the machine
// ---------------------------------------------------------------------------

static void compiler_string(char *out, size_t outsz) {
#if defined(__clang__)
    snprintf(out, outsz, "clang %d.%d.%d",
             __clang_major__, __clang_minor__, __clang_patchlevel__);
#elif defined(__GNUC__)
    snprintf(out, outsz, "gcc %d.%d.%d",
             __GNUC__, __GNUC_MINOR__, __GNUC_PATCHLEVEL__);
#else
    snprintf(out, outsz, "cc");
#endif
}

static const char *variant_flags(const kernel_set_t *k) {
    static char buf[1024];
    snprintf(buf, sizeof(buf), "%s%s%s", BUILD_FLAGS, BUILD_FLAGS[0] ? " " : "",
             k->flags);
    return buf;
}

static void print_platform(const port_info_t *pi) {
    char cc[64];
    compiler_string(cc, sizeof(cc));
    printf("build: %s", cc);
#ifdef __STDC_VERSION__
    printf(", C%ld", (long)__STDC_VERSION__);
#endif
    printf(", target=%s\n", pi->target);
    if (BUILD_FLAGS[0]) printf("flags: %s %s\n", BUILD_CC, BUILD_FLAGS);
    printf("system: %s %s, machine=%s, cpus=%d\n", pi->sdk, pi->sdk_ver,
           pi->chip, core_count());
    printf("cpu: %s (%s)\n", pi->core, pi->chip);
    const double mhz = cpu_cur_mhz(0);
    if (mhz > 0.0) printf("clock: %.1f MHz\n", mhz);
}

// ---------------------------------------------------------------------------
// One run
// ---------------------------------------------------------------------------

typedef struct {
    double seconds, warmup;
    int    reps;
    uint64_t seed;
    int    threads;
    int    cpus[MAX_CORES];
    int    n_cpus;
    int    want_threads, want_cores;
    const char *mode;
    size_t mem_mt, mem_pc;
    out_fmt_t format;
} cfg_t;

typedef struct {
    result_t threads[MAX_CORES];
    int      n_threads;
    result_t total;
    int      have_threads;
    result_t cores[MAX_CORES];
    int      n_cores;
    uint64_t checksum;
} suite_t;

// As run_workers() in bench.c, minus the threads: the port starts the cores.
static void run_workers(worker_t *w, const int *cores, int n, const cfg_t *c,
                        size_t mem, const kernel_set_t *ks) {
    plat_barrier_t bar;
    const int use_bar = n > 1 && plat_barrier_init(&bar, (unsigned)n) == 0;
    for (int i = 0; i < n; ++i) {
        w[i].id = i;
        w[i].cpu = cores[i];
        w[i].seconds = c->seconds;
        w[i].warmup = c->warmup;
        w[i].reps = c->reps;
        w[i].mem_bytes = mem;
        w[i].bar = use_bar ? &bar : NULL;
        w[i].ks = ks;
        w[i].seed = c->seed + (uint64_t)i * UINT64_C(0x9E3779B97F4A7C15) + 1u;
    }
    port_run(w, cores, n);
    if (use_bar) plat_barrier_destroy(&bar);
}

static double core_spread(const suite_t *s) {
    double best = 0.0, worst = 0.0;
    for (int i = 0; i < s->n_cores; ++i) {
        const double v = s->cores[i].score;
        if (v <= 0.0) continue;
        if (v > best) best = v;
        if (worst == 0.0 || v < worst) worst = v;
    }
    return worst > 0.0 ? best / worst : 0.0;
}

// The document, field for field what json_open() and emit_json() in bench.c
// write, with the machine described by the port rather than by /proc.
static void emit_json(const suite_t *s, const cfg_t *c, const kernel_set_t *ks,
                      const port_info_t *pi) {
    char cc[64], buf[160];
    compiler_string(cc, sizeof(cc));
    jout("{\n  \"schema\": \"cpu-bench/1\"");
    j_str("mode", c->mode);
    jout(",\n  \"build\": { \"compiler\": \"%s\"", cc);
#ifdef __VERSION__
    j_str("compiler_version", __VERSION__);
#else
    j_str("compiler_version", "");
#endif
    j_str("cc", BUILD_CC);
    j_str("flags", variant_flags(ks));
    j_str("target", pi->target);
    // No binary_sha256: there is no file to hash, only flash, and a digest of
    // that would match nothing a release publishes.
    jout(", \"vectorize\": %s, \"fma\": %s }",
         ks->vectorize ? "true" : "false", ks->fma ? "true" : "false");

    jout(",\n  \"system\": { \"cpus\": %d", core_count());
    j_str("sysname", pi->sdk);
    j_str("release", pi->sdk_ver);
    j_str("machine", pi->chip);
    snprintf(buf, sizeof(buf), "%s (%s)", pi->core, pi->chip);
    j_str("cpu_models", buf);
    snprintf(buf, sizeof(buf), "%dx %s", core_count(), pi->core);
    j_str("cpu_layout", buf);
    jout(" }");

    jout(",\n  \"config\": { \"threads\": %d, \"seconds_per_phase\": %g,"
         " \"reps\": %d, \"warmup_seconds\": %g, \"mem_bytes_per_thread\": %zu",
         c->threads, c->seconds, c->reps, c->warmup,
         c->want_threads ? c->mem_mt : c->mem_pc);
    if (c->want_cores) jout(", \"mem_bytes_per_core_sweep\": %zu", c->mem_pc);
    // Pinned by construction: a worker runs on the core it was started on.
    jout(", \"pin\": true, \"clock\": \"%s\", \"seed\": %llu }",
         g_clock_raw ? "raw" : "mono", (unsigned long long)c->seed);

    if (s->have_threads) {
        jout(",\n  \"threads\": [\n");
        for (int i = 0; i < s->n_threads; ++i) {
            json_result(&s->threads[i], "thread", "    ");
            jout("%s\n", i + 1 < s->n_threads ? "," : "");
        }
        jout("  ],\n  \"total\": ");
        json_result(&s->total, "total", "");
    }
    if (s->n_cores > 0) {
        jout(",\n  \"cores\": [\n");
        for (int i = 0; i < s->n_cores; ++i) {
            json_result(&s->cores[i], "cpu", "    ");
            jout("%s\n", i + 1 < s->n_cores ? "," : "");
        }
        jout("  ]");
        const double spread = core_spread(s);
        if (spread > 0.0) jout(",\n  \"core_spread\": %.6g", spread);
    }
    jout(",\n  \"checksum\": \"0x%016llx\"", (unsigned long long)s->checksum);
    jout("\n}\n");
}

static void emit_tsv(const suite_t *s, const char *variant, int first) {
    if (first) printf(RESULT_COLUMNS);
    for (int i = 0; i < s->n_threads; ++i) tsv_result(variant, "thread", &s->threads[i]);
    if (s->have_threads) tsv_result(variant, "total", &s->total);
    for (int i = 0; i < s->n_cores; ++i) tsv_result(variant, "cpu", &s->cores[i]);
}

// One variant, as run_variant() in bench.c. `headline` gets the row the
// variants table compares: the total, or else the best core.
static void run_variant(const cfg_t *c, const kernel_set_t *ks, int idx,
                        int n_sel, const port_info_t *pi, result_t *headline) {
    // Static, not on the stack: together they are several KiB, and a
    // microcontroller's main stack can be 2 KiB.
    static suite_t s;
    static worker_t w[MAX_CORES];
    memset(&s, 0, sizeof(s));

    if (n_sel > 1) printf("\n=== variant %d/%d: ", idx + 1, n_sel);
    else           printf("variant: ");
    printf("%s  vectorize=%s fma=%s  (%s)%s\n", ks->name,
           ks->vectorize ? "on" : "off", ks->fma ? "on" : "off", ks->flags,
           n_sel > 1 ? " ===" : "");

    if (c->want_threads) {
        printf("config: threads=%d time=%.2fs/phase x%d reps warmup=%.2fs "
               "mem=%zu KiB/thread pin=on\n\n", c->threads, c->seconds,
               c->reps, c->warmup, c->mem_mt >> 10);
        fflush(stdout);
        memset(w, 0, sizeof(w));
        run_workers(w, c->cpus, c->threads, c, c->mem_mt, ks);
        for (int i = 0; i < c->threads; ++i) {
            s.threads[i] = summarize(&w[i], w[i].cpu);
            s.checksum ^= w[i].checksum;
        }
        aggregate(s.threads, c->threads, &s.total);
        s.n_threads = c->threads;
        s.have_threads = 1;
        if (c->format == FMT_TEXT) text_threads(&s.total, s.n_threads, c->mem_mt);
    }

    if (c->want_cores) {
        printf("%smode: per-core sweep over %d CPUs, %.2fs/phase x %d reps "
               "(best kept),\n      warmup %.2fs, mem %zu KiB/thread\n\n",
               c->want_threads ? "\n" : "", c->n_cpus, c->seconds, c->reps,
               c->warmup, c->mem_pc >> 10);
        if (c->format == FMT_TEXT) text_per_core_header();
        fflush(stdout);
        for (int i = 0; i < c->n_cpus; ++i) {
            memset(w, 0, sizeof(w));
            run_workers(w, &c->cpus[i], 1, c, c->mem_pc, ks);
            s.cores[i] = summarize(&w[0], c->cpus[i]);
            s.checksum ^= w[0].checksum;
            if (c->format == FMT_TEXT) text_per_core_row(&s.cores[i]);
        }
        s.n_cores = c->n_cpus;
        const double spread = core_spread(&s);
        if (spread > 0.0 && c->format == FMT_TEXT) {
            printf("\nfastest/slowest core ratio: %.2fx\n", spread);
        }
    }

    if (c->format == FMT_TEXT && s.have_threads) {
        printf("\nchecksum: 0x%016llx\n", (unsigned long long)s.checksum);
    } else if (c->format == FMT_JSON) {
        printf("\n" MARK_BEGIN, "JSON");
        emit_json(&s, c, ks, pi);
        printf(MARK_END, "JSON");
    } else if (c->format == FMT_TSV) {
        printf("\n" MARK_BEGIN, "TSV");
        emit_tsv(&s, ks->name, 1);
        printf(MARK_END, "TSV");
    }
    fflush(stdout);

    *headline = s.have_threads ? s.total : s.cores[0];
    for (int i = 1; !s.have_threads && i < s.n_cores; ++i) {
        if (s.cores[i].score > headline->score) *headline = s.cores[i];
    }
}

// The variants side by side, as text_variant_table() in bench.c.
static void variant_table(const variant_t **sel, const result_t *rows, int n,
                          int whole_machine) {
    printf("\n%s across variants  (%s)\n",
           whole_machine ? "whole machine" : "fastest core",
           whole_machine ? "the total row" : "best row of the per-core sweep");
    printf("%-14s %9s %9s %9s %9s %8s %9s %7s | %9s %9s\n", "variant",
           "INT-thr", "MUL-thr", "FP-lat", "FP-thr", "MEM", "score", "vs base",
           "FP32-lat", "FP32-thr");
    printf("%-14s %9s %9s %9s %9s %8s %9s %7s | %9s %9s\n", "",
           "Mop/s", "Mmul/s", "Mflop/s", "Mflop/s", "GB/s", "geomean", "x",
           "Mflop/s", "Mflop/s");
    const double base = rows[0].score;
    for (int i = 0; i < n; ++i) {
        const result_t *r = &rows[i];
        printf("%-14s %9.1f %9.1f %9.1f %9.1f %8.2f %9.1f", sel[i]->k->name,
               r->int_thr, r->mul_thr, r->fp_lat, r->fp_thr, r->mem_gbps,
               r->score);
        if (base > 0.0 && r->score > 0.0) printf(" %7.3f", r->score / base);
        else                              printf(" %7s", "-");
        printf(" | %9.1f %9.1f\n", r->fp32_lat, r->fp32_thr);
    }
}

// ---------------------------------------------------------------------------
// Sizing the memory phases
// ---------------------------------------------------------------------------
//
// A host sizes them from its cache; here there is no cache in front of SRAM,
// so the only question is how much of it is free. Each worker holds its
// buffer for the whole run, and on top of it the dispatch phases' 64 KiB
// selector array, then the pointer chase's shuffle order (1/128 of the
// buffer, allocated after that array is freed). A margin stays back for the
// document and the console.
#define HEAP_MARGIN ((size_t)16 << 10)

// Whether `n` workers' buffers of `per` bytes fit at once, taken the way the
// workers take them: every buffer, then every selector array on top, with the
// margin held back too. A heap made of several regions -- an ESP32's is -- can
// have the bytes and not the blocks, and asking it is the only way to know.
static int fits(int n, size_t per) {
    void *margin = malloc(HEAP_MARGIN);
    void *buf[MAX_CORES] = { NULL }, *idx[MAX_CORES] = { NULL };
    int ok = margin != NULL;
    for (int i = 0; ok && i < n; ++i) ok = (buf[i] = plat_aligned_alloc(4096, per)) != NULL;
    for (int i = 0; ok && i < n; ++i) ok = (idx[i] = malloc(DISP_LADDER_BYTES)) != NULL;
    for (int i = 0; i < n; ++i) {
        free(idx[i]);
        plat_aligned_free(buf[i]);
    }
    free(margin);
    return ok;
}

static size_t size_buffers(int n) {
    const size_t heap = port_heap_bytes();
    const size_t fixed = HEAP_MARGIN + (size_t)n * DISP_LADDER_BYTES;
    if (n < 1 || heap <= fixed) return 0;
    size_t per = ((heap - fixed) / (size_t)n) & ~(size_t)4095;
    // An eighth smaller at a time until the blocks are there, not just the bytes.
    while (per >= PLAT_MEM_MIN_BYTES && !fits(n, per)) {
        per = (per - per / 8) & ~(size_t)4095;
    }
    return per;
}

// ---------------------------------------------------------------------------
// The command line
// ---------------------------------------------------------------------------

static void usage(void) {
    printf(
        "options, typed as one line:\n"
        "  --variant NAME     build variant to measure: scalar-nofma (default),\n"
        "                     vector-nofma, scalar-fma, vector-fma\n"
        "  --variants         every variant this target can tell apart, in turn;\n"
        "                     --variants=all for all four, --variants=A,B a list\n"
        "  --list-variants    show the variants this build carries\n"
        "  --threads N        cores to run on at once (default: all %d)\n"
        "  --cpus LIST        which cores, e.g. 1 or 0,1 (default: 0..threads-1)\n"
        "  --per-core         run the suite on each core alone, in turn\n"
        "  --full             both: all cores at once, then each alone\n"
        "  --time SEC         measured seconds per phase (default 0.5)\n"
        "  --reps N           repetitions per phase, best is kept (default 3)\n"
        "  --warmup SEC       warm-up seconds before each phase (default 0.15)\n"
        "  --mem-per-thread B buffer per core for the memory phases\n"
        "                     (default: what the free SRAM allows)\n"
        "  --mem BYTES        the same, as a total across the cores\n"
        "  --no-mem           skip the memory phases\n"
        "  --seed N           PRNG seed for setup (default 1)\n"
        "  --mhz N            the core clock, recorded as given\n"
        "  --json, --tsv      the result as a cpu-bench/1 document or TSV rows,\n"
        "                     between -----BEGIN CPCPUB ...----- marker lines\n"
        "  --version          the version, the build and this machine\n",
        core_count());
}

// Parse one line's options into `c` and the variant selection. Returns 0 to
// run, 1 when the line asked only for information, -1 on a bad option.
static int parse(int argc, char **argv, cfg_t *c, const variant_t **sel,
                 int *n_sel, const port_info_t *pi) {
    const int cores = core_count();
    int per_core = 0, full = 0;
    size_t mem_per_thread = 0, mem_total = 0;
    int mem_explicit = 0, threads_explicit = 0;

    memset(c, 0, sizeof(*c));
    c->seconds = 0.5;
    c->warmup = 0.15;
    c->reps = 3;
    c->seed = 1;
    c->threads = cores;
    c->format = FMT_TEXT;
    sel[0] = &g_variants[0];
    *n_sel = 1;
    g_mhz_given = 0.0;

    // Typing the program's name first is a habit worth allowing.
    int i0 = argc > 0 && !strcmp(argv[0], "cpcpub") ? 1 : 0;
    for (int i = i0; i < argc; ++i) {
        const char *a = argv[i];
        const int more = i + 1 < argc;
        if (!strcmp(a, "--variant") && more) {
            const variant_t *v = find_variant(argv[++i]);
            if (!v) { printf("unknown --variant '%s'\n", argv[i]); list_variants(); return -1; }
            sel[0] = v;
            *n_sel = 1;
        } else if (!strcmp(a, "--variants")) {
            *n_sel = select_variants(NULL, sel);
        } else if (!strncmp(a, "--variants=", 11)) {
            *n_sel = select_variants(a + 11, sel);
            if (*n_sel <= 0) { printf("bad --variants list '%s'\n", a + 11); list_variants(); return -1; }
        } else if (!strcmp(a, "--list-variants")) {
            list_variants();
            return 1;
        } else if (!strcmp(a, "--threads") && more) {
            c->threads = atoi(argv[++i]);
            threads_explicit = 1;
        } else if (!strcmp(a, "--cpus") && more) {
            // Single digits, comma-separated or as a range: there are two.
            c->n_cpus = 0;
            for (const char *p = argv[++i]; *p; ++p) {
                if (*p >= '0' && *p <= '9') {
                    int lo = *p - '0', hi = lo;
                    if (p[1] == '-' && p[2] >= '0' && p[2] <= '9') { hi = p[2] - '0'; p += 2; }
                    for (int k = lo; k <= hi && c->n_cpus < ARRAY_LEN(c->cpus); ++k) {
                        c->cpus[c->n_cpus++] = k;
                    }
                } else if (*p != ',') {
                    c->n_cpus = -1;
                    break;
                }
            }
            for (int k = 0; k < c->n_cpus; ++k) {
                for (int j = 0; j < k; ++j) {
                    if (c->cpus[j] == c->cpus[k]) c->n_cpus = -1;
                }
                if (c->n_cpus > 0 && c->cpus[k] >= cores) c->n_cpus = -1;
            }
            if (c->n_cpus <= 0) {
                printf("bad --cpus list: distinct cores from 0 to %d\n", cores - 1);
                return -1;
            }
        } else if (!strcmp(a, "--per-core")) {
            per_core = 1;
        } else if (!strcmp(a, "--full")) {
            full = 1;
        } else if (!strcmp(a, "--time") && more) {
            c->seconds = atof(argv[++i]);
        } else if (!strcmp(a, "--reps") && more) {
            c->reps = atoi(argv[++i]);
        } else if (!strcmp(a, "--warmup") && more) {
            c->warmup = atof(argv[++i]);
        } else if (!strcmp(a, "--mem-per-thread") && more) {
            mem_per_thread = (size_t)strtoull(argv[++i], NULL, 0);
            mem_explicit = 1;
        } else if (!strcmp(a, "--mem") && more) {
            mem_total = (size_t)strtoull(argv[++i], NULL, 0);
            mem_explicit = 1;
        } else if (!strcmp(a, "--no-mem")) {
            mem_per_thread = 0;
            mem_total = 0;
            mem_explicit = 1;
        } else if (!strcmp(a, "--seed") && more) {
            c->seed = strtoull(argv[++i], NULL, 0);
        } else if (!strcmp(a, "--mhz") && more) {
            g_mhz_given = atof(argv[++i]);
            if (g_mhz_given < 0.0) g_mhz_given = 0.0;
        } else if (!strcmp(a, "--json")) {
            c->format = FMT_JSON;
        } else if (!strcmp(a, "--tsv")) {
            c->format = FMT_TSV;
        } else if (!strcmp(a, "--version")) {
            printf("cpcpub %s, schema cpu-bench/1\n", CPCPUB_VERSION);
            print_platform(pi);
            return 1;
        } else if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage();
            return 1;
        } else if (!strcmp(a, "--disp-sweep") || !strcmp(a, "--submit") ||
                   !strncmp(a, "--submit=", 9) || !strcmp(a, "--cooldown")) {
            printf("%s is not available on a microcontroller%s\n", a,
                   strncmp(a, "--submit", 8) ? ""
                       : ": save the --json result with mcu/cpcpub-serial.py "
                         "and upload it from there");
            return -1;
        } else {
            printf("unknown option: %s (--help lists them)\n", a);
            return -1;
        }
    }

    if (c->seconds < 0.05) c->seconds = 0.05;
    if (c->warmup < 0.0) c->warmup = 0.0;
    if (c->reps < 1) c->reps = 1;
    // One worker per core, so a --cpus list without --threads is the cores
    // to run on together, not a list to share out more workers than it has.
    if (!threads_explicit && c->n_cpus > 0) c->threads = c->n_cpus;
    if (c->threads < 1) c->threads = 1;
    if (c->threads > cores) c->threads = cores;

    c->want_threads = !per_core;
    c->want_cores = per_core || full;
    c->mode = full ? "full" : (per_core ? "per-core" : "threads");

    if (c->n_cpus == 0) {
        const int want = c->want_cores ? cores : c->threads;
        for (int k = 0; k < want; ++k) c->cpus[k] = k;
        c->n_cpus = want;
    }
    if (c->want_threads && c->threads > c->n_cpus) {
        printf("--threads %d needs as many cores in --cpus: one worker per core\n",
               c->threads);
        return -1;
    }

    if (mem_explicit) {
        c->mem_mt = mem_total > 0 ? mem_total / (size_t)c->threads : mem_per_thread;
        c->mem_pc = mem_total > 0 ? mem_total : mem_per_thread;
    } else {
        c->mem_mt = size_buffers(c->threads);
        c->mem_pc = size_buffers(1);
    }
    c->mem_mt &= ~(size_t)7;
    c->mem_pc &= ~(size_t)7;
    // Below the floor measure.c skips the memory phases anyway; saying so here,
    // and recording no buffer, keeps the tables and the document from showing
    // a size nothing was measured with.
    size_t *sizes[2] = { &c->mem_mt, &c->mem_pc };
    for (int k = 0; k < 2; ++k) {
        const size_t want = *sizes[k];
        if (want == 0 || want >= PLAT_MEM_MIN_BYTES) continue;
        if ((k == 0 && c->want_threads) || (k == 1 && c->want_cores)) {
            printf("NOTE: %zu bytes per core is under the %zu the memory phases "
                   "need, so the %s skips them\n", want, (size_t)PLAT_MEM_MIN_BYTES,
                   k == 0 ? "multi-core run" : "per-core sweep");
        }
        *sizes[k] = 0;
    }
    return 0;
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

void frontend_run(void) {
    port_init();
    port_info_t pi;
    port_info(&pi);
    set_clock_mode(1);

    printf("\ncpcpub %s on %s: type options and press Enter "
           "(--help lists them)\n", CPCPUB_VERSION, pi.chip);
    for (;;) {
        static char line[256];
        char *argv[32];
        printf(PROMPT);
        fflush(stdout);
        read_line(line, sizeof(line));
        const int argc = split_args(line, argv, ARRAY_LEN(argv));

        cfg_t c;
        const variant_t *sel[N_VARIANTS];
        int n_sel = 0;
        if (parse(argc, argv, &c, sel, &n_sel, &pi) != 0) continue;

        print_platform(&pi);
        if (n_sel > 1) {
            printf("variants: %d in turn, so the run takes %dx as long:", n_sel, n_sel);
            for (int i = 0; i < n_sel; ++i) printf("%s %s", i ? "," : "", sel[i]->k->name);
            printf("\n");
        }
        result_t headline[N_VARIANTS];
        for (int i = 0; i < n_sel; ++i) {
            run_variant(&c, sel[i]->k, i, n_sel, &pi, &headline[i]);
        }
        if (n_sel > 1 && c.format == FMT_TEXT) {
            variant_table(sel, headline, n_sel, c.want_threads);
        }
        printf("\ndone\n");
    }
}
