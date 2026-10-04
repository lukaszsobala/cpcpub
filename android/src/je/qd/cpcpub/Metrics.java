package je.qd.cpcpub;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * What a result document's columns are and what they mean: the desktop
 * window's tables (gui/cpcpub-gui.py), in Java. The two should say the same
 * thing about the same number.
 */
final class Metrics {
    private Metrics() {}

    /** JSON key, printed name, unit: the clock, the score, then bench/README.md's order. */
    static final String[][] COLUMNS = {
        {"mhz", "clock", "MHz"},
        {"score", "score", "geomean"},
        {"int_lat_mops", "INT-lat", "Mop/s"},
        {"int_thr_mops", "INT-thr", "Mop/s"},
        {"ilp", "ILP", "x"},
        {"mul_thr_mmul_s", "MUL-thr", "Mmul/s"},
        {"fp_lat_mflops", "FP-lat", "Mflop/s"},
        {"fp_thr_mflops", "FP-thr", "Mflop/s"},
        {"filp", "fILP", "x"},
        {"mem_gbps", "MEM", "GB/s"},
        {"mem_lat_ns", "MEMlat", "ns"},
        {"mem_lat8_ns", "MEMlat/8", "ns"},
        {"mlp", "MLP", "x"},
        {"disp_thr_mcall_s", "DISP-thr", "Mcall/s"},
        {"disp_cap_calls", "DISPcap", "calls"},
    };

    /** What the score is a geometric mean of: core_score() in bench/src/bench.c. */
    static final Set<String> SCORE_PARTS = Set.of(
        "score", "int_thr_mops", "mul_thr_mmul_s", "fp_thr_mflops", "mem_lat8_ns",
        "disp_thr_mcall_s", "disp_cap_calls");

    static final Map<String, String> EXPLAIN = new HashMap<>();
    static {
        EXPLAIN.put("mhz", "Core clock during the run, in MHz. Tap a value to see how "
            + "it was obtained.");
        EXPLAIN.put("score", "Overall score: a geometric mean of the columns in bold -- "
            + "integer, multiply, floating-point and indirect-call throughput, "
            + "call-pattern capacity and parallel memory access. Higher is better.\n\n"
            + "It compares cores within one run: the total grows with the number of "
            + "cores, so compare totals only with totals.");
        EXPLAIN.put("int_lat_mops", "Integer latency: millions of simple integer "
            + "operations per second when each must wait for the one before. Higher is "
            + "better.");
        EXPLAIN.put("int_thr_mops", "Integer throughput: the same operations as eight "
            + "independent chains the core can overlap. Higher is better.");
        EXPLAIN.put("ilp", "Integer parallelism: throughput over latency -- roughly how "
            + "many integer operations the core issues at once.");
        EXPLAIN.put("mul_thr_mmul_s", "Millions of 64-bit integer multiplies per second. "
            + "Higher is better.");
        EXPLAIN.put("fp_lat_mflops", "Floating-point latency: millions of multiply-adds "
            + "per second when each must wait for the one before. Higher is better.");
        EXPLAIN.put("fp_thr_mflops", "Floating-point throughput: eight independent "
            + "multiply-add chains. Higher is better.");
        EXPLAIN.put("filp", "Floating-point parallelism: throughput over latency -- how "
            + "many operations are in flight.\n\nNot \"higher is better\": a core with "
            + "slow floating point needs more in flight. FP-thr says what it can do.");
        EXPLAIN.put("mem_gbps", "Memory bandwidth: gigabytes per second read and written "
            + "in order. Higher is better.");
        EXPLAIN.put("mem_lat_ns", "Memory latency: nanoseconds per access, following "
            + "random pointers one at a time. Lower is better.\n\nVaries by about 20% "
            + "between runs.");
        EXPLAIN.put("mem_lat8_ns", "The same random accesses with eight in flight at "
            + "once, nanoseconds per access. Lower is better. The score counts it as "
            + "accesses per second.");
        EXPLAIN.put("mlp", "Memory parallelism: single-chase latency over eight-chase "
            + "latency -- how much waiting on memory the core overlaps.");
        EXPLAIN.put("disp_thr_mcall_s", "Indirect calls per second, in millions, once "
            + "their pattern is learned: what virtual calls, function pointers and "
            + "interpreters run at. Higher is better.");
        EXPLAIN.put("disp_cap_calls", "The longest repeating pattern of indirect calls "
            + "the core still predicts. Higher is better.\n\nBackground load inflates "
            + "it, so it needs a quiet machine.");
    }

    static final Map<String, String> CLOCK_SOURCES = Map.of(
        "measured", "Measured: sampled while the core was under load.",
        "given", "Given: stated by whoever ran it, not observed.",
        "rated", "Rated: the most the machine declares, not observed -- a throttling "
            + "core ran below it.",
        "estimated", "Estimated: inferred from integer latency, and low on a core that "
            + "does not finish one dependent operation per cycle.");

    /** A value as the tables print it: thousands grouped, fewer decimals as it grows. */
    static String format(Object value) {
        if (value == null || value == JSONObject.NULL) return "--";
        if (value instanceof Boolean) return ((Boolean) value) ? "yes" : "no";
        if (value instanceof Number) {
            double v = ((Number) value).doubleValue();
            if (v >= 1000) return String.format(Locale.getDefault(), "%,.0f", v);
            if (v >= 10) return String.format(Locale.getDefault(), "%.1f", v);
            return String.format(Locale.getDefault(), "%.2f", v);
        }
        return String.valueOf(value);
    }

    /** The variant a document came from, spelled the way --variant takes it. */
    static String variantOf(JSONObject doc) {
        JSONObject build = doc.optJSONObject("build");
        if (build == null) return "?";
        return (build.optBoolean("vectorize") ? "vector" : "scalar") + "-"
            + (build.optBoolean("fma") ? "fma" : "nofma");
    }

    /** (name, record) for each measured scope, in the document's order. */
    static List<Object[]> scopes(JSONObject doc) {
        List<Object[]> out = new ArrayList<>();
        JSONObject total = doc.optJSONObject("total");
        if (total != null) out.add(new Object[] {"total", total});
        JSONArray cores = doc.optJSONArray("cores");
        for (int i = 0; cores != null && i < cores.length(); i++) {
            JSONObject rec = cores.optJSONObject(i);
            if (rec != null) out.add(new Object[] {"cpu" + rec.opt("cpu"), rec});
        }
        if (out.isEmpty()) {
            JSONArray threads = doc.optJSONArray("threads");
            for (int i = 0; threads != null && i < threads.length(); i++) {
                JSONObject rec = threads.optJSONObject(i);
                if (rec != null) out.add(new Object[] {"thr" + rec.opt("cpu"), rec});
            }
        }
        return out;
    }

    static String explainScope(String name) {
        if (name.equals("total")) {
            return "Every thread at once: what the whole machine does. Its score grows "
                + "with the number of cores.";
        }
        if (name.startsWith("cpu")) {
            return "CPU " + name.substring(3) + " on its own, single-threaded: what that "
                + "kind of core does.";
        }
        return "One thread of the multi-threaded run.";
    }
}
