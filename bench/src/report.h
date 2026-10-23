// Writing result records out: the JSON sink, one record as JSON or a TSV row,
// and the tables a person reads. Shared by every front end, so a record means
// the same keys and the same columns whichever program wrote it -- that is
// what schema/cpu-bench-1.md and the contract test hold the two to.
//
// The document around the records (build, system, config) is each front
// end's own: it describes a host, and only the front end knows the host.

#ifndef CPCPUB_REPORT_H
#define CPCPUB_REPORT_H

#include <stddef.h>

#include "measure.h"

// ---------------------------------------------------------------------------
// The JSON sink
// ---------------------------------------------------------------------------
//
// The result document normally goes straight to stdout. --submit needs the
// same bytes in memory instead -- they are what gets POSTed, and a text-mode
// run has to produce them without printing them. So the JSON emitters write
// through jout(), which is stdout until a buffer is put in front of it.

typedef struct {
    char  *p;
    size_t len, cap;
    int    oom;             // an allocation failed; the document is incomplete
} sbuf_t;

extern sbuf_t *g_json_sink;
void sbuf_free(sbuf_t *b);
#if defined(__GNUC__)
__attribute__((format(printf, 1, 2)))
#endif
void jout(const char *fmt, ...);
void jputc(char c);
// `, "key": value`, with null for anything not measured.
void j_num(const char *key, double v, int ok);
// `, "key": "value"`, escaped.
void j_str(const char *key, const char *v);

// TSV: one header line, then one row per scope. Missing values are empty. The
// leading `variant` column is what keeps a --variants run's rows apart, and is
// present in a single-variant run too so the column set never depends on how
// the binary was invoked.
#define RESULT_COLUMNS \
    "variant\tscope\tcpu\tmhz\tmhz_src\tint_lat_mops\tint_thr_mops\tilp" \
    "\tmul_thr_mmul_s\tfp_lat_mflops\tfp_thr_mflops\tfilp\tmem_gbps" \
    "\tmem_lat_ns\tmem_lat8_ns\tmlp\tdisp_thr_mcall_s\tdisp_cap_calls" \
    "\tdisp_gain\tdisp_span\tscore\tfp32_lat_mflops\tfp32_thr_mflops\n"

void tsv_result(const char *variant, const char *scope, const result_t *r);
void json_result(const result_t *r, const char *scope, const char *indent);

// The threaded run's table, from its total row; the per-core sweep's table,
// which prints a row at a time as the sweep goes.
void text_threads(const result_t *t, int n_threads, size_t mem_per_thread);
void text_per_core_header(void);
void text_per_core_row(const result_t *r);

#endif  // CPCPUB_REPORT_H
