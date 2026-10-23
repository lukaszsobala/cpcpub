// Writing result records out; see report.h.

// Before any header, as in bench.c: measure.h brings in platform.h, whose
// barrier and affinity calls -std=c2x hides without it.
#define _GNU_SOURCE 1
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "measure.h"
#include "report.h"

// ---------------------------------------------------------------------------
// The JSON sink
// ---------------------------------------------------------------------------

sbuf_t *g_json_sink = NULL;

void sbuf_free(sbuf_t *b) {
    free(b->p);
    b->p = NULL;
    b->len = b->cap = 0;
}

void jout(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    if (!g_json_sink) {
        vprintf(fmt, ap);
        va_end(ap);
        return;
    }
    sbuf_t *b = g_json_sink;
    for (int attempt = 0; attempt < 2 && !b->oom; ++attempt) {
        va_list copy;
        va_copy(copy, ap);
        const size_t room = b->cap - b->len;
        // NULL with a size of zero is what vsnprintf is defined to accept, and
        // is what the first pass on an empty buffer hands it -- b->p + 0 on a
        // null pointer would not be.
        char *at = b->p ? b->p + b->len : NULL;
        const int n = vsnprintf(at, room, fmt, copy);
        va_end(copy);
        if (n < 0) { b->oom = 1; break; }
        if ((size_t)n < room) { b->len += (size_t)n; break; }
        // It did not fit: grow to hold it and write it again. vsnprintf said
        // exactly how much it wanted, so the second pass always fits.
        size_t want = b->cap ? b->cap : 8192;
        while (want < b->len + (size_t)n + 1) want *= 2;
        char *bigger = (char *)realloc(b->p, want);
        if (!bigger) { b->oom = 1; break; }
        b->p = bigger;
        b->cap = want;
    }
    va_end(ap);
}

void jputc(char c) {
    if (g_json_sink) jout("%c", c);
    else             putchar(c);
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

// The `mhz_src` value in JSON and TSV. "given" is deliberately not "measured":
// a reader can then tell a clock the machine reported from one its owner did.
// And "rated" is neither: it is the ceiling the machine declares, which a
// throttling core never reached.
static const char *mhz_src_name(int src) {
    switch (src) {
        case MHZ_GIVEN:     return "given";
        case MHZ_RATED:     return "rated";
        case MHZ_ESTIMATED: return "estimated";
        default:            return "measured";
    }
}

static void tsv_num(double v, int ok) {
    if (ok && v > 0.0) printf("\t%.6g", v);
    else               printf("\t");
}

void tsv_result(const char *variant, const char *scope, const result_t *r) {
    printf("%s\t%s", variant, scope);
    if (r->cpu >= 0) printf("\t%d", r->cpu); else printf("\t");
    tsv_num(r->mhz, 1);
    printf("\t%s", r->mhz > 0.0 ? mhz_src_name((int)r->mhz_src)
                                : "unknown");
    tsv_num(r->int_lat, 1);
    tsv_num(r->int_thr, 1);
    tsv_num(r->ilp, 1);
    tsv_num(r->mul_thr, 1);
    tsv_num(r->fp_lat, 1);
    tsv_num(r->fp_thr, 1);
    tsv_num(r->filp, 1);
    tsv_num(r->mem_gbps, 1);
    tsv_num(r->mem_lat_ns, 1);
    tsv_num(r->mem_lat8_ns, 1);
    tsv_num(r->mlp, 1);
    tsv_num(r->disp_thr, 1);
    tsv_num(r->disp_cap, r->disp_status == DISP_OK);
    tsv_num(r->disp_gain, 1);
    tsv_num(r->disp_span, r->disp_status != DISP_NA);
    tsv_num(r->score, 1);
    tsv_num(r->fp32_lat, 1);
    tsv_num(r->fp32_thr, 1);
    printf("\n");
}

// JSON: `,"key": value`, with null for anything not measured.
void j_num(const char *key, double v, int ok) {
    if (ok && v > 0.0) jout(", \"%s\": %.6g", key, v);
    else               jout(", \"%s\": null", key);
}

void j_str(const char *key, const char *v) {
    jout(", \"%s\": \"", key);
    for (const char *p = v; *p; ++p) {
        if (*p == '"' || *p == '\\') jputc('\\');
        if ((unsigned char)*p < 0x20) continue;
        jputc(*p);
    }
    jout("\"");
}

void json_result(const result_t *r, const char *scope, const char *indent) {
    jout("%s{ \"scope\": \"%s\"", indent, scope);
    if (r->cpu >= 0) jout(", \"cpu\": %d", r->cpu); else jout(", \"cpu\": null");
    j_num("mhz", r->mhz, 1);
    j_str("mhz_src", r->mhz > 0.0 ? mhz_src_name((int)r->mhz_src)
                                  : "unknown");
    j_num("int_lat_mops", r->int_lat, 1);
    j_num("int_thr_mops", r->int_thr, 1);
    j_num("ilp", r->ilp, 1);
    j_num("mul_thr_mmul_s", r->mul_thr, 1);
    j_num("fp_lat_mflops", r->fp_lat, 1);
    j_num("fp_thr_mflops", r->fp_thr, 1);
    j_num("filp", r->filp, 1);
    j_num("mem_gbps", r->mem_gbps, 1);
    j_num("mem_lat_ns", r->mem_lat_ns, 1);
    j_num("mem_lat8_ns", r->mem_lat8_ns, 1);
    j_num("mlp", r->mlp, 1);
    j_num("disp_thr_mcall_s", r->disp_thr, 1);
    j_num("disp_cap_calls", r->disp_cap, r->disp_status == DISP_OK);
    j_str("disp_prediction", r->disp_status == DISP_OK   ? "measured"
                          : r->disp_status == DISP_NONE ? "none" : "unknown");
    j_num("disp_gain", r->disp_gain, 1);
    j_num("disp_span", r->disp_span, r->disp_status != DISP_NA);
    j_num("score", r->score, 1);
    j_num("fp32_lat_mflops", r->fp32_lat, 1);
    j_num("fp32_thr_mflops", r->fp32_thr, 1);
    jout(" }");
}

// Render DISPcap for a table cell: below the ladder, past the ladder, or no
// prediction at all.
static void disp_cap_str(int st, double cap, char *buf, size_t n) {
    if (st == DISP_NONE)                             snprintf(buf, n, "none");
    else if (st != DISP_OK || cap <= 0.0)            snprintf(buf, n, "-");
    else if (cap < (double)g_disp_ladder[DISP_SPAN_FIRST])
        snprintf(buf, n, "<%zu", g_disp_ladder[DISP_SPAN_FIRST]);
    else if (cap >= (double)g_disp_ladder[DISP_SPAN_LAST])
        snprintf(buf, n, ">=%zu", g_disp_ladder[DISP_SPAN_LAST]);
    else                                             snprintf(buf, n, "%.0f", cap);
}

// ---------------------------------------------------------------------------
// Emitters
// ---------------------------------------------------------------------------

void text_threads(const result_t *t, int n_threads, size_t mem_per_thread) {
    const double n = n_threads > 0 ? (double)n_threads : 1.0;
    printf("%-9s %-23s %8s %11s %11s\n",
           "metric", "what it measures", "unit", "total", "per-thread");
    // The latency rows keep their total cell blank: n dependency chains on n
    // threads do not make any one chain shorter, so a summed latency rate is
    // not a number the machine ever produces. Per-thread is the mean.
    printf("%-9s %-23s %8s %11s %11.1f\n",
           "INT-lat", "integer latency", "Mop/s", "-", t->int_lat / n);
    printf("%-9s %-23s %8s %11.1f %11.1f\n",
           "INT-thr", "integer throughput", "Mop/s", t->int_thr, t->int_thr / n);
    printf("%-9s %-23s %8s %11.1f %11.1f\n",
           "MUL-thr", "multiply throughput", "Mmul/s", t->mul_thr, t->mul_thr / n);
    printf("%-9s %-23s %8s %11s %11.1f\n",
           "FP-lat", "FP latency", "Mflop/s", "-", t->fp_lat / n);
    printf("%-9s %-23s %8s %11.1f %11.1f\n",
           "FP-thr", "FP throughput", "Mflop/s", t->fp_thr, t->fp_thr / n);
    printf("%-9s %-23s %8s %11.1f %11.1f\n",
           "DISP-thr", "indirect dispatch rate", "Mcall/s", t->disp_thr, t->disp_thr / n);
    if (t->disp_status == DISP_OK) {
        printf("%-9s %-23s %8s %11s %11.0f\n",
               "DISPcap", "indirect predictor size", "calls", "-", t->disp_cap);
    } else if (t->disp_status == DISP_NONE) {
        printf("%-9s %-23s %8s %11s %11s\n",
               "DISPcap", "indirect predictor size", "calls", "-", "none");
    }
    if (mem_per_thread > 0) {
        printf("%-9s %-23s %8s %11.2f %11.2f\n",
               "MEM", "memory bandwidth", "GB/s", t->mem_gbps, t->mem_gbps / n);
        if (t->mem_lat_ns > 0.0) {
            printf("%-9s %-23s %8s %11s %11.1f\n",
                   "MEMlat", "memory latency", "ns", "-", t->mem_lat_ns);
        }
        if (t->mem_lat8_ns > 0.0) {
            printf("%-9s %-23s %8s %11s %11.1f\n",
                   "MEMlat/8", "latency, 8 chases", "ns", "-", t->mem_lat8_ns);
        }
    }
    // Derived ratios: per-thread only, so the "total" column stays blank.
    printf("%-9s %-23s %8s %11s %11.2f\n", "ILP", "instruction parallelism",
           "x", "-", t->ilp);
    printf("%-9s %-23s %8s %11s %11.2f\n", "fILP", "FP ops in flight",
           "x", "-", t->filp);
    if (t->mlp > 0.0) {
        printf("%-9s %-23s %8s %11s %11.2f\n", "MLP", "memory parallelism",
               "x", "-", t->mlp);
    }
    // Whole-machine only: it is a geomean of the totals, not of the per-thread
    // column, and is ~n times what one core of this machine scores.
    if (t->score > 0.0) {
        printf("%-9s %-23s %8s %11.1f %11s\n", "score", "composite",
               "geomean", t->score, "-");
    }
    // Below the score, because they are not in it: the same FP kernels in
    // single precision, for cores whose FPU has no double-precision unit.
    if (t->fp32_thr > 0.0) {
        printf("single precision, not in the score\n");
        printf("%-9s %-23s %8s %11s %11.1f\n",
               "FP32-lat", "FP32 latency", "Mflop/s", "-", t->fp32_lat / n);
        printf("%-9s %-23s %8s %11.1f %11.1f\n",
               "FP32-thr", "FP32 throughput", "Mflop/s", t->fp32_thr, t->fp32_thr / n);
    }
}

#define PER_CORE_FMT "%4s %6s  %8s %8s %5s %8s  %8s %8s %5s  %7s %7s %5s  %8s %7s  %8s | %8s %8s\n"

void text_per_core_header(void) {
    printf(PER_CORE_FMT, "CPU", "MHz", "INT-lat", "INT-thr", "ILP", "MUL-thr",
           "FP-lat", "FP-thr", "fILP", "MEM", "MEMlat", "MLP",
           "DISP-thr", "DISPcap", "score", "FP32-lat", "FP32-thr");
    printf(PER_CORE_FMT, "", "", "Mop/s", "Mop/s", "x", "Mmul/s",
           "Mflop/s", "Mflop/s", "x", "GB/s", "ns", "x",
           "Mcall/s", "calls", "geomean", "Mflop/s", "Mflop/s");
}

void text_per_core_row(const result_t *r) {
    char mhzbuf[16], capbuf[16];
    if (r->mhz <= 0.0)         snprintf(mhzbuf, sizeof(mhzbuf), "?");
    else if (r->mhz_src == MHZ_ESTIMATED)
        snprintf(mhzbuf, sizeof(mhzbuf), "~%.0f", r->mhz);
    else                       snprintf(mhzbuf, sizeof(mhzbuf), "%.0f", r->mhz);
    disp_cap_str(r->disp_status, r->disp_cap, capbuf, sizeof(capbuf));
    printf("%4d %6s  %8.1f %8.1f %5.2f %8.1f  %8.1f %8.1f %5.2f  %7.2f %7.1f %5.2f  %8.1f %7s  %8.1f | %8.1f %8.1f\n",
           r->cpu, mhzbuf,
           r->int_lat, r->int_thr, r->ilp, r->mul_thr,
           r->fp_lat, r->fp_thr, r->filp,
           r->mem_gbps, r->mem_lat_ns, r->mlp,
           r->disp_thr, capbuf, r->score, r->fp32_lat, r->fp32_thr);
    fflush(stdout);
}
