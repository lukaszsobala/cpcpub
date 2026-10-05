#!/usr/bin/env python3
"""A small GTK4 front end for the cpcpub benchmark.

It builds a `cpcpub` command line from the form, runs it with `--json`,
streams the binary's prose to the log pane, saves the result document to the
chosen output directory, and renders the numbers. Everything the benchmark
does -- including the upload -- is still done by the benchmark itself; this
process only assembles argv and reads the two streams back.

Needs PyGObject and GTK 4.10 or newer (python3-gi / gir1.2-gtk-4.0 on Debian
and Ubuntu, python3-gobject / gtk4 on Fedora and Arch). No other dependency, and
none for the benchmark. The Windows installer carries its own copy of both.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
# gi.repository is filled in at run time from the GObject introspection data,
# so pyright without pygobject-stubs sees no names in it (an error), and with
# the stubs finds no source behind them (a warning). Neither is a fault here.
from gi.repository import Gio, GLib, GObject, Gtk, Pango  # noqa: E402 # pyright: ignore

APP_ID = "je.qd.cpcpub.Gui"

# A frozen copy is the Windows build: PyInstaller's executable, with GTK and
# the icon unpacked beside it in sys._MEIPASS.
FROZEN = getattr(sys, "frozen", False)
EXE = ".exe" if os.name == "nt" else ""

# A console program started from a windowed one gets a console window of its
# own on Windows unless told otherwise, and the benchmark has nothing to show
# in one: its output comes here. Zero, and so no flag, everywhere else.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Set by the packaging tests to a file path: the window then makes one short
# run by itself, writes what happened to that file as JSON, and quits. It is
# how a machine with no one at it checks that an installed copy can find the
# benchmark, start it, read both of its streams and show a result.
SMOKE = os.environ.get("CPCPUB_GUI_SMOKE", "")

# Where the window keeps the upload fields between runs. The token in it is a
# credential, so the file is the user's alone; see save_settings.
SETTINGS = os.path.join(GLib.get_user_config_dir(), "cpcpub", "gui.json")

# What is kept there besides the notes, the upload tick and where results go:
# the window's text fields, each with the variable that wins over it. Those
# win for one session and are not written back in what was kept.
KEPT_TEXT = (("hub", "CPCPUB_HUB"), ("token", "CPCPUB_TOKEN"), ("run_label", None))


def cpuinfo(key):
    """The first `key` line of /proc/cpuinfo, or "" where there is none."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as info:
            for line in info:
                name, _, value = line.partition(":")
                if name.strip() == key:
                    return value.strip()
    except OSError:
        pass
    return ""


# What x86-64-v3 adds to the baseline, by the names Linux gives them (abm is
# how it lists LZCNT).
X86_64_V3 = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}


def has_x86_64_v3():
    if os.name == "nt":
        # Python has no CPUID. Windows answers for AVX2 (feature 40,
        # PF_AVX2_INSTRUCTIONS_AVAILABLE), and the processors that have AVX2
        # have the rest of x86-64-v3 too; a Windows too old to know the
        # question answers no, which only keeps the plain build.
        try:
            import ctypes
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        except (AttributeError, OSError):
            return False
    return X86_64_V3 <= set(cpuinfo("flags").split())


# The RVA23 extensions a compiler emits code for, beyond the vector unit. A
# kernel too old to name them answers no, which only keeps the plain build.
RVA23 = {"zba", "zbb", "zbs", "zicond", "zfa", "zcb", "zvbb"}


def has_rva23():
    base, *extensions = cpuinfo("isa").lower().split("_")  # rv64imafdcv_zba_...
    return base.startswith("rv64") and "v" in base[4:] and RVA23 <= set(extensions)


# The other builds a package installs beside the plain one, by the suffix
# their file names carry: the same benchmark for a newer instruction set, and
# how to tell whether this processor has it. The builds do not check for
# themselves: one started on a processor without those instructions dies in
# the middle of a run.
BUILDS = {
    "-v3": ("x86-64-v3 (AVX2 and FMA)", has_x86_64_v3),
    "-rva23": ("the RISC-V RVA23 profile", has_rva23),
}

# The four variants the binary carries, in the order --list-variants prints
# them. Used as the fallback when the binary cannot be asked -- the real list,
# and which of them are distinct on this target, comes from the binary itself.
FALLBACK_VARIANTS = [
    ("scalar-nofma", "-fno-tree-vectorize -ffp-contract=off", True),
    ("vector-nofma", "-ftree-vectorize -ffp-contract=off", True),
    ("scalar-fma", "-fno-tree-vectorize -ffp-contract=fast", True),
    ("vector-fma", "-ftree-vectorize -ffp-contract=fast", True),
]

# (json key, printed name, unit) in the order bench/README.md's table lists
# them, so a result read here and a result read there say the same thing --
# except the score, which that table ends on and this one leads with: it is
# the number a row is read for.
METRICS = [
    ("score", "score", "geomean"),
    ("int_lat_mops", "INT-lat", "Mop/s"),
    ("int_thr_mops", "INT-thr", "Mop/s"),
    ("ilp", "ILP", "x"),
    ("mul_thr_mmul_s", "MUL-thr", "Mmul/s"),
    ("fp_lat_mflops", "FP-lat", "Mflop/s"),
    ("fp_thr_mflops", "FP-thr", "Mflop/s"),
    ("filp", "fILP", "x"),
    ("mem_gbps", "MEM", "GB/s"),
    ("mem_lat_ns", "MEMlat", "ns"),
    ("mem_lat8_ns", "MEMlat/8", "ns"),
    ("mlp", "MLP", "x"),
    ("disp_thr_mcall_s", "DISP-thr", "Mcall/s"),
    ("disp_cap_calls", "DISPcap", "calls"),
]

# What a results table shows: the clock first, as the context the rest of a row
# is read in, then the metrics.
COLUMNS = [("mhz", "clock", "MHz")] + METRICS

# What each column means, for whoever hovers over it: in words, not in the
# benchmark's flags. Condensed from bench/README.md's "What it measures".
EXPLAIN = {
    "mhz": "Core clock during the run, in MHz. Hover over a value to see how it "
           "was obtained.",
    "score": "Overall score: a geometric mean of the columns in bold -- integer, "
             "multiply, floating-point and indirect-call throughput, call-pattern "
             "capacity and parallel memory access. Higher is better.\nIt compares cores within one run: "
             "the total grows with the number of cores, so compare totals only "
             "with totals.",
    "int_lat_mops": "Integer latency: millions of simple integer operations per "
                    "second when each must wait for the one before. Higher is better.",
    "int_thr_mops": "Integer throughput: the same operations as eight independent "
                    "chains the core can overlap. Higher is better.",
    "ilp": "Integer parallelism: throughput over latency -- roughly how many integer "
           "operations the core issues at once.",
    "mul_thr_mmul_s": "Millions of 64-bit integer multiplies per second. "
                      "Higher is better.",
    "fp_lat_mflops": "Floating-point latency: millions of multiply-adds per second "
                     "when each must wait for the one before. Higher is better.",
    "fp_thr_mflops": "Floating-point throughput: eight independent multiply-add "
                     "chains. Higher is better.",
    "filp": "Floating-point parallelism: throughput over latency -- how many "
            "operations are in flight.\nNot \"higher is better\": a core with slow "
            "floating point needs more in flight. FP-thr says what it can do.",
    "mem_gbps": "Memory bandwidth: gigabytes per second read and written in order. "
                "Higher is better.",
    "mem_lat_ns": "Memory latency: nanoseconds per access, following random pointers "
                  "one at a time. Lower is better.\nVaries by about 20% between runs.",
    "mem_lat8_ns": "The same random accesses with eight in flight at once, "
                   "nanoseconds per access. Lower is better. The score counts it "
                   "as accesses per second.",
    "mlp": "Memory parallelism: single-chase latency over eight-chase latency -- "
           "how much waiting on memory the core overlaps.",
    "disp_thr_mcall_s": "Indirect calls per second, in millions, once their pattern "
                        "is learned: what virtual calls, function pointers and "
                        "interpreters run at. Higher is better.",
    "disp_cap_calls": "The longest repeating pattern of indirect calls the core "
                      "still predicts. Higher is better.\nBackground load inflates "
                      "it, so it needs a quiet machine.",
}

# What the score is a geometric mean of -- core_score() and machine_score() in
# bench/src/bench.c. The memory part is a rate there, 1000 / MEMlat/8, so its
# column is the one that carries it.
SCORE_PARTS = {
    "int_thr_mops", "mul_thr_mmul_s", "fp_thr_mflops", "mem_lat8_ns",
    "disp_thr_mcall_s", "disp_cap_calls",
}

# The line of run details under a result's CPU name -- variant, mode, timing,
# pinning, clock source, core spread. Built but hidden: the table says what
# matters, and the hover text on its values covers the rest.
SHOW_RUN_DETAILS = False

MODES = {
    "threads": "multi-threaded",
    "per-core": "per core",
    "full": "multi-threaded and per core",
}

CLOCK_SOURCES = {
    "measured": "measured: sampled while the core was under load",
    "given": "given: stated by whoever ran it, not observed",
    "rated": "rated: the most the machine declares, not observed -- a throttling "
             "core ran below it",
    "estimated": "estimated: inferred from integer latency, and low on a core that "
                 "does not finish one dependent operation per cycle",
}


def explain_scope(name):
    """What a row's name means."""
    if name == "total":
        return "Every thread at once: what the whole machine does."
    if name.startswith("cpu"):
        return f"CPU {name[3:]} on its own, with nothing else running."
    return f"The thread on CPU {name[3:]}, during the run with every thread at once."


def explain_variant(name, distinct=True):
    """A variant's name in words: what the compiler was allowed to do."""
    vector = name.startswith("vector")
    fma = name.endswith("-fma")
    if not vector and not fma:
        text = ("Plain code: no automatic vector (SIMD) instructions and no fused "
                "multiply-add. The baseline, and the one to compare different CPU "
                "families with.")
    elif vector and fma:
        text = ("The compiler may use vector (SIMD) instructions and fuse multiplies "
                "with adds. Not comparable with the baseline.")
    elif vector:
        text = "The compiler may use vector (SIMD) instructions."
    else:
        text = "The compiler may fuse multiplies with adds into one instruction."
    if not distinct:
        # Decided by the -march the binary was built for, not by the CPU: the
        # plain x86-64 build has no FMA variant even on a CPU with FMA.
        text += ("\nIn this build it is the same code as the baseline: the "
                 "instruction set it was built for lacks what it would use.")
    return text

# What a run costs, in the benchmark's own units. One pass of the suite is:
#
#   8 phases that each warm up once and then measure `reps` x `time`
#     (INT-lat, INT-thr, MUL-thr, FP-lat, FP-thr, MEM, MEMlat, MEMlat/8)
#   + the indirect-dispatch ladder: 14 selector periods, one warm-up between
#     them all, and half a phase's time at each -- so 7 phases' worth
#
# giving 9 warm-ups and 15 measured phases, plus about a tenth of a second of
# setup per pass (allocating the buffer and building the pointer chases).
# Checked against this tree: predicted 2.82 s for a 3-CPU sweep that took 2.83,
# and 6.55 s for a threaded run that took 6.63.
WARMUPS_PER_PASS = 9
PHASES_PER_PASS = 15
SETUP_SECONDS_PER_PASS = 0.1


def count_cpus(cpu_list):
    """How many CPUs a --cpus list like `0-3,6` names. 0 if it says nothing."""
    total = 0
    for part in cpu_list.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            lo, _, hi = part.partition("-")
            try:
                total += max(0, int(hi) - int(lo) + 1)
            except ValueError:
                return 0
        else:
            try:
                int(part)
            except ValueError:
                return 0
            total += 1
    return total


def human_duration(seconds):
    if seconds < 90:
        return f"about {round(seconds)} s"
    if seconds < 3600:
        return f"{seconds / 60.0:.1f} min"
    return f"{seconds / 3600.0:.1f} h ({seconds / 60.0:.0f} min)"


def find_binary():
    """The benchmark: the one this tree builds, else the one installed beside
    this program, else whatever PATH has.

    Installed, this program is /usr/bin/cpcpub-gui beside /usr/bin/cpcpub, or
    on Windows cpcpub-gui.exe in a folder of its own inside the one holding
    cpcpub.exe.
    """
    # Resolved, so a link to this program finds what is beside the program
    # rather than beside the link -- and a merged /usr/sbin, which some PATHs
    # list first, still finds /usr/bin/cpcpub under its usual name.
    if FROZEN:
        here = os.path.dirname(os.path.realpath(sys.executable))
        cands = [os.path.join(here, "cpcpub.exe"),
                 os.path.join(os.path.dirname(here), "cpcpub.exe")]
    else:
        here = os.path.dirname(os.path.realpath(__file__))
        cands = [os.path.join(os.path.dirname(here), "bench", "cpcpub" + EXE),
                 os.path.join(here, "cpcpub" + EXE)]
    for cand in cands:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("cpcpub") or ""


def other_builds(binary):
    """The builds installed beside the plain `binary`, as (file name, target,
    whether this processor runs it)."""
    folder, name = os.path.split(binary)
    if name != "cpcpub" + EXE:
        return []
    return [("cpcpub" + suffix + EXE, target, runs) for suffix, (target, runs) in BUILDS.items()
            if os.path.isfile(os.path.join(folder, "cpcpub" + suffix + EXE))]


def best_build(binary):
    """The build beside the plain `binary` for the newest instruction set this
    processor has -- the fastest one that runs here -- else `binary`."""
    for name, _target, runs in other_builds(binary):
        if runs():
            return os.path.join(os.path.dirname(binary), name)
    return binary


def load_settings():
    if SMOKE:
        return {}  # the packaging tests' run is the same on every machine
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save_settings(settings):
    """Write `settings` readable by its owner only: it holds the token. The
    reason it could not be written, or "" when it was."""
    if SMOKE:
        return ""
    tmp = SETTINGS + ".tmp"
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
        os.replace(tmp, SETTINGS)
    except OSError as exc:
        return f"could not keep the upload settings in {SETTINGS}: {exc.strerror or exc}"
    return ""


def quote_command(argv):
    """argv as the shell this platform has would want it typed."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def default_hub(binary):
    """The hub a build uploads to when told none: the address its help text
    gives as the default, which a release bakes in and a tree you built has
    not."""
    try:
        done = subprocess.run(
            [binary, "--help"], capture_output=True, timeout=10, check=False,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    # The usage text goes to stderr.
    found = re.search(r" \(default: (https?://[^)\s]+)\)",
                      (done.stdout + done.stderr).decode("utf-8", "replace"))
    return found.group(1) if found else ""


def curl_hint():
    """How to install curl here, for when an upload needs it and it is missing."""
    if os.name == "nt":
        return ("Windows 10 1803 and later ship curl.exe in System32; on an older "
                "Windows, install it with: winget install cURL.cURL")
    try:
        with open("/etc/os-release", encoding="utf-8") as release:
            fields = dict(line.rstrip("\n").split("=", 1) for line in release if "=" in line)
    except OSError:
        fields = {}
    family = " ".join(fields.get(k, "").strip('"') for k in ("ID", "ID_LIKE")).split()
    for ids, command in ((("debian", "ubuntu"), "sudo apt install curl"),
                         (("fedora", "rhel", "centos"), "sudo dnf install curl"),
                         (("suse", "opensuse"), "sudo zypper install curl"),
                         (("arch",), "sudo pacman -S curl")):
        # Prefixes, for IDs such as opensuse-tumbleweed.
        if any(f.startswith(i) for f in family for i in ids):
            return f"Install it with: {command}"
    return "Install it with the system's package manager."


def list_variants(binary):
    """Ask the binary which variants it carries. Falls back to the four names."""
    if not binary or not os.access(binary, os.X_OK):
        return FALLBACK_VARIANTS
    try:
        done = subprocess.run(
            [binary, "--list-variants"], capture_output=True, timeout=10, check=False,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_VARIANTS
    if done.returncode != 0:
        return FALLBACK_VARIANTS
    found = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        # The listing indents each variant by two spaces; the trailing notes
        # about a target without SIMD or FMA are indented the same and
        # parenthesised, which is what tells them apart.
        if not line.startswith("  ") or line.lstrip().startswith("("):
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(("scalar", "vector")):
            continue
        flags = " ".join(p for p in parts[1:] if p.startswith("-"))
        found.append((parts[0], flags, "distinct" in line))
    return found or FALLBACK_VARIANTS


def fmt(value):
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "yes" if value else "no"
    # int as well as float: JSON writes a round number without a decimal point,
    # and 23854 in a column of 23,048s reads as a different quantity.
    if isinstance(value, (int, float)):
        if value >= 1000:
            return f"{value:,.0f}"
        if value >= 10:
            return f"{value:.1f}"
        return f"{value:.2f}"
    return str(value)


def label_of(doc):
    """The variant a document came from, spelled the way --variant takes it."""
    b = doc.get("build", {})
    vec = "vector" if b.get("vectorize") else "scalar"
    fma = "fma" if b.get("fma") else "nofma"
    return f"{vec}-{fma}"


def scopes_of(doc):
    """(name, record) for each measured scope in a document, in its order.

    `total` and `cores` are both present in a --full document, and one of the
    two in the narrower modes; `threads` is the per-thread detail behind a
    total, worth a row only when there is no total to show.
    """
    scopes = []
    if doc.get("total"):
        scopes.append(("total", doc["total"]))
    for rec in doc.get("cores") or []:
        scopes.append((f"cpu{rec.get('cpu')}", rec))
    if not scopes:
        for rec in doc.get("threads") or []:
            scopes.append((f"thr{rec.get('cpu')}", rec))
    return scopes


def clock_sources(scopes):
    """How much a document's clocks are worth -- measured, rated, given,
    estimated -- which schema/cpu-bench-1.md warns are not the same claim."""
    return sorted({rec.get("mhz_src") for _, rec in scopes if rec.get("mhz_src")})


def render(docs):
    """One monospace report for the whole run: a row per measured scope and a
    column per kernel, so a many-CPU sweep grows down the page, not across it."""
    out = []
    for doc in docs:
        sysinfo = doc.get("system", {})
        cfg = doc.get("config", {})
        out.append("=" * 72)
        out.append(
            f"{label_of(doc)}   mode={doc.get('mode', '?')}   "
            f"{doc.get('build', {}).get('target', '')}"
        )
        out.append(
            f"{sysinfo.get('cpu_models', '(cpu not named)')} -- "
            f"{sysinfo.get('sysname', '?')}, {sysinfo.get('cpus', '?')} cpus"
        )
        out.append(
            f"threads={cfg.get('threads')} "
            f"time={cfg.get('seconds_per_phase')}s/phase "
            f"x{cfg.get('reps')} reps warmup={cfg.get('warmup_seconds')}s "
            f"pin={fmt(cfg.get('pin'))} clock={cfg.get('clock')}"
        )
        out.append("")

        scopes = scopes_of(doc)
        if not scopes:
            out.append("(no records)")
            out.append("")
            continue

        # Each column as wide as its widest cell, heading and unit included:
        # fourteen kernels at a fixed width would not fit a normal window.
        cells = [[fmt(rec.get(key)) for key, _, _ in COLUMNS] for _, rec in scopes]
        name_w = max(len(name) for name, _ in scopes)
        widths = [
            max(len(name), len(unit), *(len(r[i]) for r in cells))
            for i, (_, name, unit) in enumerate(COLUMNS)
        ]

        rows = [
            ("", [name for _, name, _ in COLUMNS]),
            ("", [unit for _, _, unit in COLUMNS]),
        ] + [(name, values) for (name, _), values in zip(scopes, cells)]
        lines = [
            f"{first:<{name_w}}" + "".join(f"  {v:>{w}}" for v, w in zip(values, widths))
            for first, values in rows
        ]
        out += lines[:2]
        out.append("-" * len(lines[0]))
        out += lines[2:]
        srcs = clock_sources(scopes)
        if srcs:
            out.append("")
            out.append(f"clock: {', '.join(srcs)}")
        if doc.get("core_spread") is not None:
            out.append("")
            out.append(f"core spread: {fmt(doc['core_spread'])} x")
        out.append("")
    return "\n".join(out)


def compare(a, b):
    """Order two cells for a sorted column, a missing value always last."""
    if a is None or b is None:
        return (a is None) - (b is None)
    return (a > b) - (a < b)


def scope_order(scope):
    """The document's own order: the total, then the CPUs by number."""
    return -1 if scope.name == "total" else scope.rec.get("cpu", 0)


def cell_tip(key):
    """The hover text for a cell in the `key` column: what the column means,
    and for a clock, how that one was obtained."""
    if key != "mhz":
        return lambda scope: EXPLAIN[key]

    def clock_tip(scope):
        src = scope.rec.get("mhz_src")
        if not src:
            return EXPLAIN["mhz"]
        return f"Core clock during the run, {CLOCK_SOURCES.get(src, src)}."
    return clock_tip


class Scope(GObject.Object):
    """One row of a results table: a measured scope and its record."""

    def __init__(self, name, rec):
        super().__init__()
        self.name = name
        self.rec = rec


class ResultsView(Gtk.Box):
    """The run's numbers as tables: a page per variant, a row per measured
    scope, and a column per kernel that sorts on a click of its heading."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.stack = Gtk.Stack(vexpand=True, vhomogeneous=False)
        # Only a --variants run has more than one page to switch between.
        self.switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER)
        self.switcher.set_margin_top(6)
        self.append(self.switcher)
        self.append(self.stack)
        self.set_placeholder("No results yet.")

    def clear(self):
        child = self.stack.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt

    def set_placeholder(self, text):
        self.clear()
        lab = Gtk.Label(label=text, wrap=True)
        lab.add_css_class("dim-label")
        self.stack.add_child(lab)
        self.switcher.set_visible(False)

    def set_docs(self, docs):
        self.clear()
        for i, doc in enumerate(docs):
            self.stack.add_titled(self.page(doc), f"doc{i}", label_of(doc))
        # The switcher makes a button per page, in order, with no say over
        # their tooltips.
        button = self.switcher.get_first_child()
        for doc in docs:
            if button is None:
                break
            button.set_tooltip_text(explain_variant(label_of(doc)))
            button = button.get_next_sibling()
        self.switcher.set_visible(len(docs) > 1)

    def page(self, doc):
        sysinfo = doc.get("system", {})
        cfg = doc.get("config", {})
        scopes = scopes_of(doc)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        title = Gtk.Label(
            label=sysinfo.get("cpu_models", "(cpu not named)"), xalign=0, wrap=True,
        )
        title.add_css_class("heading")
        box.append(title)

        # The line under the CPU's name, and what its terms mean on hover.
        variant = label_of(doc)
        facts = [
            variant,
            MODES.get(doc.get("mode", ""), doc.get("mode", "")),
            doc.get("build", {}).get("target", ""),
            f"{sysinfo.get('sysname', '?')}, {sysinfo.get('cpus', '?')} CPUs",
            f"{cfg.get('threads')} threads",
            (
                f"{cfg.get('seconds_per_phase')} s per measurement, best of "
                f"{cfg.get('reps')}, {cfg.get('warmup_seconds')} s warm-up"
            ),
            "threads pinned to CPUs" if cfg.get("pin") else "threads not pinned",
        ]
        tips = [f"{variant}: {explain_variant(variant)}"]
        srcs = clock_sources(scopes)
        if srcs:
            facts.append(f"clock {', '.join(srcs)}")
            tips += [f"Clock {CLOCK_SOURCES.get(src, src)}." for src in srcs]
        if doc.get("core_spread") is not None:
            facts.append(f"fastest core {fmt(doc['core_spread'])}× the slowest")
            tips.append(
                "Fastest core against slowest, by score: on a CPU with big and "
                "little cores it is their ratio, on one with identical cores a "
                "measure of noise."
            )
        sub = Gtk.Label(label="  ·  ".join(f for f in facts if f), xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.set_tooltip_text("\n\n".join(tips))
        sub.set_visible(SHOW_RUN_DETAILS)
        box.append(sub)

        if not scopes:
            empty = Gtk.Label(label="(no records)", vexpand=True)
            empty.add_css_class("dim-label")
            box.append(empty)
            return box

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_margin_top(6)
        scroll.set_child(self.table(scopes))
        scroll.add_css_class("frame")
        box.append(scroll)
        return box

    def table(self, scopes):
        store = Gio.ListStore(item_type=Scope)
        for name, rec in scopes:
            store.append(Scope(name, rec))
        view = Gtk.ColumnView(show_column_separators=True)
        view.add_css_class("data-table")  # compact rows: a table, not a list
        # The view's sorter follows whichever heading was clicked last.
        model = Gtk.SortListModel(model=store, sorter=view.get_sorter())
        # The model after the flags: given at construction it can arrive first,
        # and an autoselecting model highlights the top row on its own.
        selection = Gtk.SingleSelection(autoselect=False, can_unselect=True)
        selection.set_model(model)
        view.set_model(selection)

        view.append_column(self.column(
            "", lambda s: s.name, lambda s: explain_scope(s.name),
            lambda a, b: compare(scope_order(a), scope_order(b)),
        ))
        for key, name, unit in COLUMNS:
            view.append_column(self.column(
                f"{name}\n{unit}",
                lambda s, key=key: fmt(s.rec.get(key)),
                cell_tip(key),
                lambda a, b, key=key: compare(a.rec.get(key), b.rec.get(key)),
                numeric=True,
                bold=key in SCORE_PARTS or key == "score",
            ))
        self.dress_headings(
            view,
            [("Which part of the machine the row measured.", False)]
            + [(EXPLAIN[key], key in SCORE_PARTS or key == "score")
               for key, _, _ in COLUMNS],
        )
        return view

    @staticmethod
    def dress_headings(view, looks):
        """Hover text, and bold for the score's parts, on the column headings,
        which GTK has no properties for: its header row holds a title widget
        per column, in column order, each with a label inside."""
        header = view.get_first_child()
        while header is not None and header.get_css_name() != "header":
            header = header.get_next_sibling()
        title = header.get_first_child() if header is not None else None
        for tip, bold in looks:
            if title is None:
                break
            title.set_tooltip_text(tip)
            if bold:
                box = title.get_first_child()
                lab = box.get_first_child() if box is not None else None
                if lab is not None:
                    lab.add_css_class("heading")
            title = title.get_next_sibling()

    def column(self, title, text_of, tip_of, cmp, numeric=False, bold=False):
        """A column whose cells are `text_of(scope)`, explained on hover by
        `tip_of(scope)` and sorted by `cmp`. `bold` marks the score and the
        parts it is made of."""
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item):
            lab = Gtk.Label(xalign=1 if numeric else 0)
            if numeric:
                lab.add_css_class("numeric")  # tabular figures: digits line up
            if bold:
                lab.add_css_class("heading")
            item.set_child(lab)

        def bind(_factory, item):
            scope, lab = item.get_item(), item.get_child()
            lab.set_text(text_of(scope))
            lab.set_tooltip_text(tip_of(scope))
            # Bold in the numbers means "part of the score", so the total is
            # picked out by its name alone. Labels are recycled between rows:
            # the emphasis is set or cleared on every bind.
            if not numeric:
                if scope.name == "total":
                    lab.add_css_class("heading")
                else:
                    lab.remove_css_class("heading")

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _data: cmp(a, b), None))
        return col


class CheckGroup:
    """A box that sums up the boxes under it: ticked when every one is, a dash
    when some are. Ticking the last one ticks it too; unticking any one is the
    way back out with the rest still ticked, and unticking it goes back to the
    first box alone -- the default."""

    def __init__(self, master, on_change, keep_one=False):
        self.master = master
        self.on_change = on_change
        self.keep_one = keep_one  # never leave every box unticked
        self.checks = []
        self.syncing = False  # set while the code, not the user, ticks boxes
        master.connect("toggled", self.on_master)

    def set_checks(self, checks, every):
        """Take over `checks`, ticking all of them or only the first."""
        self.checks = checks
        for check in checks:
            check.connect("toggled", self.on_check)
        self.tick(every)

    def tick(self, every):
        self.syncing = True
        for i, check in enumerate(self.checks):
            check.set_active(every or i == 0)
        self.syncing = False
        self.sync()

    def on_master(self, *_):
        if not self.syncing:
            self.tick(self.master.get_active())

    def on_check(self, check):
        if self.syncing:
            return
        if self.keep_one and not any(c.get_active() for c in self.checks):
            # Unticking the last ticked box moves the tick to the next one,
            # rather than refusing: a click should always change something.
            others = [c for c in self.checks if c is not check]
            self.syncing = True
            (others[0] if others else check).set_active(True)
            self.syncing = False
        self.sync()

    def sync(self):
        ticked = sum(c.get_active() for c in self.checks)
        total = len(self.checks)
        self.syncing = True
        self.master.set_active(total > 0 and ticked == total)
        self.master.set_inconsistent(0 < ticked < total)
        self.syncing = False
        self.on_change()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="cpcpub")
        self.set_default_size(800, 660)

        self.proc = None
        self.started = 0.0
        self.estimated = 1.0
        self.tick_id = 0
        self.stdout_buf = []
        self.pending_streams = 0
        self.exit_status = -1
        self.submitting = False
        self.upload_hub = ""
        self.uploads = []    # the hub's replies, one per uploaded document
        self.stopped = False
        self.command_line = ""
        self.token_in_env = False

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        # The text behind the table -- the benchmark's own log, the document
        # and a plain report -- out of the way until someone asks for it.
        self.output_btn = Gtk.ToggleButton(label="Output")
        self.output_btn.set_tooltip_text(
            "Show the benchmark's log, the result document, and the numbers as text."
        )
        self.output_btn.connect("toggled", self.on_output_toggled)
        header.pack_start(self.output_btn)
        self.run_btn = Gtk.Button(label="Run")
        self.run_btn.add_css_class("suggested-action")
        self.run_btn.connect("clicked", self.on_run)
        header.pack_end(self.run_btn)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_visible(False)
        self.stop_btn.connect("clicked", self.on_stop)
        header.pack_end(self.stop_btn)

        paned = self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_position(300)
        self.set_child(paned)

        form_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.form_box = self.build_form()
        form_scroll.set_child(self.form_box)
        paned.set_start_child(form_scroll)
        paned.set_resize_start_child(False)
        paned.set_end_child(self.build_output())
        # The form scrolls and the output does not: squeezed below its height,
        # the status line and the link to an upload fall off the window.
        paned.set_shrink_end_child(False)

        # Both groups tick their boxes only now: each tick rewrites the
        # command line, which is in the footer built after the form.
        self.reload_variants()
        self.mode_group.set_checks([self.mode_threads, self.mode_percore], every=True)
        self.advanced.connect("notify::expanded", lambda *_: self.fit_form())
        self.restore_settings()
        self.connect("close-request", self.on_close)
        # Once, after the first layout pass: until then there is nothing to
        # measure.
        self.connect("map", lambda *_: GLib.idle_add(self.fit_form))

    def do_measure(self, orientation, for_size):
        """Keep the window from widening itself when a long line arrives.

        GTK takes a window's minimum width as the width its content needs at
        the content's minimum height -- and squeezed that flat, every wrapping
        label wants its whole text on one line. The details under a result's
        CPU name alone came to 1357 px. Widths are asked for as if no height
        were given; heights still follow the width, so the labels wrap.
        """
        if orientation == Gtk.Orientation.VERTICAL:
            return Gtk.ApplicationWindow.do_measure(self, orientation, for_size)
        minimum, natural, _, _ = Gtk.ApplicationWindow.do_measure(self, orientation, -1)
        return minimum, natural, -1, -1  # baselines are a vertical thing

    # -- the form ----------------------------------------------------------

    def build_form(self):
        """A label column and a control column, densely: the whole form is
        meant to sit above the output without pushing it off the window."""
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        row = 0

        def field(text, widget, span=1, top=False):
            nonlocal row
            if text:
                valign = Gtk.Align.START if top else Gtk.Align.BASELINE_CENTER
                # A check's text sits a little below its top edge.
                lab = Gtk.Label(label=text, xalign=0, valign=valign, margin_top=4 if top else 0)
                lab.add_css_class("dim-label")
                grid.attach(lab, 0, row, 1, 1)
                grid.attach(widget, 1, row, span, 1)
            else:
                grid.attach(widget, 0, row, span + 1, 1)
            row += 1
            return widget

        # Variants and what to run, each a column of checks and the two side
        # by side: a column reads as a list to pick from, and side by side they
        # cost the height of the longer one rather than of both.
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.all_variants = Gtk.CheckButton(label="All")
        self.all_variants.set_tooltip_text(
            "Run every variant that differs from the baseline in this build, one "
            "after another, and compare them.\n"
            "Ticks itself when every variant is ticked; untick it to go back to "
            "the baseline alone."
        )
        self.variant_group = CheckGroup(self.all_variants, self.update_command)
        vbox.append(self.all_variants)
        self.variant_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=16,
        )
        vbox.append(self.variant_list)
        self.variant_checks = []

        # The same shape as the variants: Both sums up the two under it, and
        # a run needs at least one of them.
        modes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.mode_full = Gtk.CheckButton(label="Both")
        self.mode_threads = Gtk.CheckButton(label="Multi-threaded")
        self.mode_percore = Gtk.CheckButton(label="Per-core")
        self.mode_full.set_tooltip_text(
            "Every thread at once, then each CPU on its own. The one to upload: "
            "neither half means much without the other.\n"
            "Ticks itself when both are ticked; untick it to go back to the "
            "multi-threaded run alone."
        )
        self.mode_threads.set_tooltip_text(
            "Every thread at once: what the whole machine does."
        )
        self.mode_percore.set_tooltip_text(
            "Each CPU on its own, one after another: what each kind of core does."
        )
        modes.append(self.mode_full)
        mode_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=16)
        mode_list.append(self.mode_threads)
        mode_list.append(self.mode_percore)
        modes.append(mode_list)
        self.mode_group = CheckGroup(self.mode_full, self.update_command, keep_one=True)

        picks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        picks.append(vbox)
        run_lab = Gtk.Label(label="Run", xalign=0, valign=Gtk.Align.START,
                            margin_start=32, margin_top=4)
        run_lab.add_css_class("dim-label")
        picks.append(run_lab)
        picks.append(modes)
        field("Variants", picks, top=True)

        # Output directory.
        outrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.outdir = Gtk.Entry(hexpand=True)
        self.outdir.set_text(
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            or GLib.get_home_dir()
        )
        self.outdir.set_tooltip_text("The result lands here as cpcpub-<date>.json.")
        outrow.append(self.outdir)
        choose = Gtk.Button(label="Choose…")
        choose.connect("clicked", self.on_pick_outdir)
        outrow.append(choose)
        field("Save to", outrow)

        # Submit to the hub. The fields appear only when there is an upload:
        # they are the tallest thing here and most runs do not want them.
        self.do_submit = Gtk.CheckButton(label="Upload the result to a hub")
        self.do_submit.connect("toggled", self.on_submit_toggled)
        field("", self.do_submit)

        self.submit_grid = Gtk.Grid(column_spacing=10, row_spacing=4, margin_start=16)
        self.submit_grid.set_visible(False)
        field("", self.submit_grid)

        # The placeholder names the binary's own default; see reload_variants.
        self.hub = Gtk.Entry(hexpand=True)
        self.token = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
        self.token.set_tooltip_text(
            "From the hub's Account tab; it ties the upload to your account. "
            "Kept for next time.\n"
            "Leave empty to upload anonymously -- every hub accepts that."
        )
        self.run_label = Gtk.Entry(hexpand=True, placeholder_text="short name for this machine")
        self.run_label.set_tooltip_text("What the hub shows this run as.")
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
        self.notes.set_tooltip_text(
            "Anything worth knowing about the run: cooling, power settings, what "
            "else was running."
        )
        notes_scroll = Gtk.ScrolledWindow(hexpand=True, min_content_height=44)
        notes_scroll.set_child(self.notes)
        notes_scroll.add_css_class("frame")
        for i, (text, widget) in enumerate(
            (("Hub URL", self.hub), ("Token", self.token),
             ("Label", self.run_label), ("Notes", notes_scroll))
        ):
            lab = Gtk.Label(label=text, xalign=0, valign=Gtk.Align.START)
            lab.add_css_class("dim-label")
            self.submit_grid.attach(lab, 0, i, 1, 1)
            self.submit_grid.attach(widget, 1, i, 1, 1)
        for w in (self.hub, self.run_label):
            w.connect("changed", lambda *_: self.update_command())

        # Advanced.
        exp = self.advanced = Gtk.Expander(label="Advanced")
        adv = Gtk.Grid(column_spacing=10, row_spacing=4, margin_start=16, margin_top=4)
        exp.set_child(adv)

        self.threads = Gtk.Entry(placeholder_text="auto (online CPUs)", width_chars=16)
        self.cpus = Gtk.Entry(placeholder_text="e.g. 0-3,6", width_chars=16)
        self.seconds = Gtk.SpinButton.new_with_range(0.05, 60.0, 0.05)
        self.seconds.set_digits(2)
        self.seconds.set_value(0.5)
        self.reps = Gtk.SpinButton.new_with_range(1, 99, 1)
        self.reps.set_value(3)
        self.warmup = Gtk.SpinButton.new_with_range(0.0, 10.0, 0.05)
        self.warmup.set_digits(2)
        self.warmup.set_value(0.15)

        fields = (
            ("Threads", self.threads,
             "How many threads to run at once. Blank: one per CPU."),
            ("CPUs", self.cpus,
             "Use only these CPUs: 0-3,6 means CPUs 0 to 3 and CPU 6."),
            ("Seconds/phase", self.seconds,
             "How long each measurement lasts. Longer is steadier, and slower."),
            ("Repetitions", self.reps,
             ("How often each measurement is taken. The best is kept: "
              "interference only ever slows a run down.")),
            ("Warm-up", self.warmup,
             "Unmeasured time before each measurement, for the clock to ramp up."),
        )
        for i, (text, widget, tip) in enumerate(fields):
            widget.set_tooltip_text(tip)
            widget.set_halign(Gtk.Align.START)
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class("dim-label")
            adv.attach(lab, 0, i, 1, 1)
            adv.attach(widget, 1, i, 1, 1)
            signal = "changed" if isinstance(widget, Gtk.Entry) else "value-changed"
            widget.connect(signal, lambda *_: self.update_command())

        # The benchmark itself, last: the window finds it, and picks the
        # fastest build of it this processor runs, so there is seldom a reason
        # to look.
        binrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
        self.binary = Gtk.Entry(hexpand=True, placeholder_text="path to cpcpub")
        self.binary.set_text(best_build(find_binary()))
        self.binary.connect(
            "changed",
            lambda *_: (self.reload_variants(), self.update_command(),
                        self.update_binary_tip()),
        )
        self.update_binary_tip()
        binrow.append(self.binary)
        pick = Gtk.Button(label="Browse…")
        pick.connect("clicked", self.on_pick_binary)
        binrow.append(pick)
        lab = Gtk.Label(label="Benchmark", xalign=0)
        lab.add_css_class("dim-label")
        adv.attach(lab, 0, len(fields), 1, 1)
        adv.attach(binrow, 1, len(fields), 1, 1)
        field("", exp)
        return grid

    def build_output(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results = ResultsView()
        self.notebook = Gtk.Notebook()
        self.page_of = {}  # text view -> its page number in the notebook
        self.log_view = self.text_page("Log")
        self.report_view = self.text_page("Report")
        self.json_view = self.text_page("JSON")
        # One or the other: the half of the window under the form is too short
        # to split between them.
        self.out_stack = Gtk.Stack(vexpand=True)
        self.out_stack.add_named(self.results, "table")
        self.out_stack.add_named(self.notebook, "text")
        box.append(self.out_stack)

        # What the form adds up to, kept out of the scrolling half so it is
        # always in view: how long it should take, and the parameters that do
        # it -- one small line cut to the width there is, with a button that
        # copies the whole command. It is echoed into the log when a run starts.
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        foot.set_margin_start(12)
        foot.set_margin_end(12)
        foot.set_margin_top(6)
        self.estimate = Gtk.Label(xalign=0, wrap=True)
        self.estimate.add_css_class("heading")
        foot.append(self.estimate)
        cmdrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.command = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
        )
        self.command.add_css_class("dim-label")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new("monospace"))
        attrs.insert(Pango.attr_scale_new(0.85))
        self.command.set_attributes(attrs)
        cmdrow.append(self.command)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", has_frame=False)
        copy.set_tooltip_text("Copy the command, to run it in a terminal.")
        copy.connect("clicked", self.on_copy_command)
        cmdrow.append(copy)
        foot.append(cmdrow)
        box.append(foot)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status.set_margin_start(12)
        status.set_margin_end(12)
        status.set_margin_bottom(8)
        status.set_margin_top(2)
        self.progress = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        self.progress.set_visible(False)
        status.append(self.progress)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.status.set_text("Ready.")
        status.append(self.status)
        # After an upload: the result's page on the hub.
        self.hub_page = ""
        self.hub_link = Gtk.Button(label="See how it compares", valign=Gtk.Align.CENTER)
        self.hub_link.add_css_class("suggested-action")
        self.hub_link.set_visible(False)
        self.hub_link.connect(
            "clicked", lambda *_: Gtk.UriLauncher(uri=self.hub_page).launch(self, None, None),
        )
        status.append(self.hub_link)
        box.append(status)
        return box

    def text_page(self, title):
        view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        view.set_left_margin(8)
        view.set_top_margin(6)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(view)
        self.page_of[view] = self.notebook.append_page(scroll, Gtk.Label(label=title))
        return view

    def on_output_toggled(self, *_):
        self.out_stack.set_visible_child_name(
            "text" if self.output_btn.get_active() else "table"
        )

    def show_output(self, view):
        """Open the text pane at the page holding `view`."""
        self.notebook.set_current_page(self.page_of[view])
        self.output_btn.set_active(True)

    # -- form behaviour ----------------------------------------------------

    def update_binary_tip(self):
        tip = ("The benchmark this window runs. It starts as the fastest build "
               "installed that this processor runs.")
        folder = os.path.dirname(self.binary.get_text().strip())
        others = other_builds(os.path.join(folder, "cpcpub" + EXE))
        if others:
            # A package installs the newer-ISA builds beside the plain one.
            tip += "\nInstalled:\n  cpcpub" + EXE + ", for any processor of its kind" + "".join(
                f"\n  {other}, built for {target}"
                + ("" if runs() else " -- which this processor lacks")
                for other, target, runs in others
            )
        self.binary.set_tooltip_text(tip)

    def reload_variants(self):
        child = self.variant_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.variant_list.remove(child)
            child = nxt
        self.variant_checks = []
        for name, _flags, distinct in list_variants(self.binary.get_text().strip()):
            check = Gtk.CheckButton(label=name)
            # Which variants the toggles actually change on this target is the
            # binary's answer, not ours; a target with no vector unit carries
            # four names that are one piece of code.
            check.set_tooltip_text(explain_variant(name, distinct))
            self.variant_list.append(check)
            if distinct:
                self.variant_checks.append((name, check))
            else:
                # Running it would only measure the baseline again, under
                # another name and for as long again.
                check.set_sensitive(False)
        # A new binary keeps "all" if that is what was asked for; anything
        # narrower starts again from the baseline, which it always carries.
        self.variant_group.set_checks(
            [check for _, check in self.variant_checks], self.all_variants.get_active(),
        )
        hub = default_hub(self.binary.get_text().strip())
        self.hub.set_placeholder_text(f"{hub} (this build's default)" if hub
                                      else "required: this build has no default hub")

    def fit_form(self):
        """Give the form the height it now wants, up to most of the window.

        Opening the hub fields or Advanced roughly doubles it, and a fixed
        split would leave half the form behind a scrollbar for no reason.
        """
        natural = self.form_box.measure(Gtk.Orientation.VERTICAL, -1)[1]
        height = self.paned.get_height()
        cap = int(height * 0.62) if height > 0 else 380
        if height > 0:
            # Never into the room the output needs to show all of itself.
            below = self.paned.get_end_child().measure(
                Gtk.Orientation.VERTICAL, self.paned.get_width())[0]
            cap = min(cap, height - below - 1)  # and the handle's pixel
        self.paned.set_position(max(min(natural, cap), 0))

    def on_submit_toggled(self, *_):
        self.submit_grid.set_visible(self.do_submit.get_active())
        self.fit_form()
        self.run_btn.set_label("Run and upload" if self.do_submit.get_active() else "Run")
        self.update_command()

    def restore_settings(self):
        self.kept = load_settings()
        for name, env in KEPT_TEXT:
            text = os.environ.get(env, "") if env else ""
            getattr(self, name).set_text(text or str(self.kept.get(name) or ""))
        self.notes.get_buffer().set_text(str(self.kept.get("notes") or ""))
        outdir = str(self.kept.get("outdir") or "")
        if os.path.isdir(outdir):
            self.outdir.set_text(outdir)
        self.do_submit.set_active(bool(self.kept.get("upload")))

    def keep_settings(self):
        settings = dict(self.kept)
        for name, env in KEPT_TEXT:
            text = getattr(self, name).get_text().strip()
            if not (env and text and text == os.environ.get(env)):
                settings[name] = text
        buf = self.notes.get_buffer()
        settings["notes"] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        settings["outdir"] = self.outdir.get_text().strip()
        settings["upload"] = self.do_submit.get_active()
        if settings != self.kept:
            problem = save_settings(settings)
            if problem:
                self.log(problem + "\n")
            else:
                self.kept = settings

    def on_close(self, *_):
        self.keep_settings()
        return False  # and close

    def selected_variants(self):
        return [name for name, check in self.variant_checks if check.get_active()]

    def build_argv(self):
        """argv for the run, plus the environment the token travels in."""
        binary = self.binary.get_text().strip()
        argv = [binary or "cpcpub"]

        if self.all_variants.get_active():
            # Every distinct one: the ones that are the baseline's code again
            # are greyed out in the form, and would only repeat it.
            argv.append("--variants")
        else:
            chosen = self.selected_variants()
            if len(chosen) == 1:
                argv += ["--variant", chosen[0]]
            elif len(chosen) > 1:
                argv.append("--variants=" + ",".join(chosen))

        if self.mode_full.get_active():
            argv.append("--full")
        elif self.mode_percore.get_active():
            argv.append("--per-core")

        threads = self.threads.get_text().strip()
        if threads:
            argv += ["--threads", threads]
        cpus = self.cpus.get_text().strip()
        if cpus:
            argv += ["--cpus", cpus]
        argv += ["--time", f"{self.seconds.get_value():g}"]
        argv += ["--reps", f"{self.reps.get_value_as_int()}"]
        argv += ["--warmup", f"{self.warmup.get_value():g}"]
        argv.append("--json")

        env = {}
        if self.do_submit.get_active():
            hub = self.hub.get_text().strip()
            # --submit=URL rather than two words: the binary reads a following
            # word as the address unless it starts with '-', and an empty one
            # would swallow the next flag.
            argv.append("--submit=" + hub if hub else "--submit")
            lab = self.run_label.get_text().strip()
            if lab:
                argv += ["--label", lab]
            buf = self.notes.get_buffer()
            notes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            if notes:
                argv += ["--notes", notes]
            token = self.token.get_text()
            if token:
                # $CPCPUB_TOKEN, not --token: the credential stays out of the
                # process list, which is what the binary documents it for.
                env["CPCPUB_TOKEN"] = token
        return argv, env

    def update_command(self):
        argv, env = self.build_argv()
        self.update_estimate()
        # The binary's path is in the form already; what is worth a line is
        # what the rest of the form turned into.
        self.command.set_text(quote_command(argv[1:]))
        self.command_line = quote_command(argv)
        self.token_in_env = "CPCPUB_TOKEN" in env

    def on_copy_command(self, _button):
        # Without the token: it travels in the environment, and a clipboard is
        # no place for it. A shell with $CPCPUB_TOKEN set supplies it anyway.
        self.get_clipboard().set(self.command_line)
        self.status.set_text(
            "Command copied, without the token." if self.token_in_env
            else "Command copied."
        )


    def estimate_seconds(self):
        """How long the current settings should take, and what drives it.

        The per-core sweep runs the suite once per CPU, in turn, so the CPU
        count is most of the answer on anything with many of them.
        """
        per_pass = (
            WARMUPS_PER_PASS * self.warmup.get_value()
            + PHASES_PER_PASS * self.reps.get_value_as_int() * self.seconds.get_value()
            + SETUP_SECONDS_PER_PASS
        )

        # Which CPUs the sweep would walk: the --cpus list if there is one,
        # otherwise 0..threads-1, otherwise every online CPU.
        cpus = count_cpus(self.cpus.get_text())
        if not cpus:
            try:
                cpus = int(self.threads.get_text().strip())
            except ValueError:
                cpus = 0
        if cpus <= 0:
            cpus = os.cpu_count() or 1

        if self.mode_full.get_active():
            passes, what = 1 + cpus, f"1 threaded pass + {cpus} cores"
        elif self.mode_percore.get_active():
            passes, what = cpus, f"{cpus} cores"
        else:
            passes, what = 1, "1 threaded pass"

        variants = max(1, len(self.selected_variants()))
        if variants > 1:
            what += f" x {variants} variants"
        return per_pass * passes * variants, what

    def update_estimate(self):
        seconds, what = self.estimate_seconds()
        self.estimate.set_text(
            f"Estimated run time: {human_duration(seconds)}   ({what})"
        )
        return seconds

    # -- pickers -----------------------------------------------------------

    def on_pick_outdir(self, _button):
        dialog = Gtk.FileDialog(title="Where to save the result")
        current = self.outdir.get_text().strip()
        if current and os.path.isdir(current):
            dialog.set_initial_folder(Gio.File.new_for_path(current))

        def done(dlg, res):
            try:
                folder = dlg.select_folder_finish(res)
            except GLib.Error:
                return  # cancelled
            if folder:
                self.outdir.set_text(folder.get_path() or "")

        dialog.select_folder(self, None, done)

    def on_pick_binary(self, _button):
        dialog = Gtk.FileDialog(title="Select the cpcpub binary")
        current = self.binary.get_text().strip()
        if current and os.path.exists(current):
            dialog.set_initial_file(Gio.File.new_for_path(current))

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f:
                self.binary.set_text(f.get_path() or "")

        dialog.open(self, None, done)

    # -- running -----------------------------------------------------------

    def log(self, text):
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)
        buf.delete_mark(mark)

    def set_text(self, view, text):
        view.get_buffer().set_text(text)

    def fail(self, message):
        self.status.set_text(message)
        self.log("\n" + message + "\n")

    def on_run(self, _button):
        if self.proc is not None:
            return
        binary = self.binary.get_text().strip()
        if not binary or not (os.access(binary, os.X_OK) or GLib.find_program_in_path(binary)):
            self.fail(f"No runnable benchmark at {binary!r} -- pick one with Browse.")
            return
        outdir = self.outdir.get_text().strip()
        if not os.path.isdir(outdir):
            self.fail(f"Output directory {outdir!r} does not exist.")
            return
        if self.do_submit.get_active() and not self.hub.get_text().strip():
            # A released build has a default hub baked in; a tree you built
            # yourself has none, and would fail after measuring rather than now.
            self.log("no hub URL given: relying on the address baked into this build\n")
        hub = ""
        if self.do_submit.get_active():
            # Where the upload will go: the field, the environment, then the
            # binary's own default, the order the binary itself takes them in.
            hub = (self.hub.get_text().strip() or os.environ.get("CPCPUB_HUB", "")
                   or default_hub(binary))
            # The benchmark hands an https upload to curl, which the packages
            # only recommend. A newer benchmark refuses before measuring when
            # it is missing; this says so for any of them, and in a window
            # rather than at the end of a log.
            if hub.startswith("https://") and not shutil.which("curl"):
                detail = (f"{hub} is an https address, and the benchmark uploads "
                          f"over https through curl, which is not installed.\n\n"
                          f"{curl_hint()}\n\nOr untick \u201cUpload the result to a "
                          f"hub\u201d to measure without uploading.")
                self.fail("Uploading needs curl, which is not installed.")
                self.log(detail + "\n")
                Gtk.AlertDialog(message="Uploading needs curl", detail=detail).show(self)
                return

        self.keep_settings()
        argv, env = self.build_argv()
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={**os.environ, **env},
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            self.proc = None
            self.fail(f"Could not start the benchmark: {exc.strerror or exc}")
            return

        self.set_text(self.log_view, "")
        self.set_text(self.report_view, "")
        self.set_text(self.json_view, "")
        self.results.set_placeholder("Running…")
        self.submitting = self.do_submit.get_active()
        self.upload_hub = hub
        self.uploads = []
        self.stopped = False
        self.hub_link.set_visible(False)
        self.stdout_buf = []
        self.exit_status = -1
        self.pending_streams = 3  # two pipes at EOF, plus the reaped process
        self.started = time.monotonic()
        prefix = "CPCPUB_TOKEN=••• " if self.token_in_env else ""
        self.log(f"$ {prefix}{self.command_line}\n\n")

        self.run_btn.set_sensitive(False)
        self.stop_btn.set_visible(True)
        self.notebook.set_current_page(self.page_of[self.log_view])
        self.estimated = max(1.0, self.update_estimate())
        self.progress.set_visible(True)
        self.tick_id = GLib.timeout_add_seconds(1, self.on_tick)
        self.on_tick()

        # A thread per stream and one to reap, each handing what it got to the
        # main loop: a pipe nobody reads fills, and the benchmark then stops
        # dead in the middle of a write.
        for pipe, handler in ((self.proc.stderr, self.on_stderr_line),
                              (self.proc.stdout, self.on_stdout_line)):
            threading.Thread(target=self.read_lines, args=(pipe, handler),
                             daemon=True).start()
        threading.Thread(target=self.reap, args=(self.proc,), daemon=True).start()

    def on_tick(self):
        elapsed = time.monotonic() - self.started
        # The benchmark reports nothing until it is done, so the bar runs off
        # the estimate. Held just short of full so a run that overshoots does
        # not look finished while it is still measuring.
        self.progress.set_fraction(min(0.99, elapsed / self.estimated))
        self.progress.set_text(
            f"{int(elapsed)} s of about {round(self.estimated)} s"
        )
        self.status.set_text("Running")
        return GLib.SOURCE_CONTINUE

    def read_lines(self, pipe, handler):
        """On a thread of its own: read one stream to the end, line by line."""
        with pipe:
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                GLib.idle_add(self.on_main, handler, line)
        GLib.idle_add(self.on_main, self.on_stream_closed)

    def reap(self, proc):
        """On a thread of its own: wait for the process to exit."""
        status = proc.wait()
        # Negative is a signal on POSIX: not an exit, and not a status.
        GLib.idle_add(self.on_main, self.on_finished, status if status >= 0 else -1)

    @staticmethod
    def on_main(func, *args):
        """Run a reader thread's news on the main loop, once."""
        func(*args)
        return GLib.SOURCE_REMOVE

    def on_stream_closed(self):
        self.pending_streams -= 1
        self.maybe_finish()

    def on_stderr_line(self, line):
        self.log(line + "\n")
        # The hub's reply to an upload, printed by the benchmark as it came:
        # the run's id, its page on the hub, and the delete token.
        if line.startswith("uploaded: "):
            try:
                reply = json.loads(line[len("uploaded: "):])
            except ValueError:
                return
            if isinstance(reply, dict):
                self.uploads.append(reply)

    def on_stdout_line(self, line):
        self.stdout_buf.append(line)

    def on_finished(self, status):
        self.exit_status = status
        self.pending_streams -= 1
        self.maybe_finish()

    def maybe_finish(self):
        # Both pipes are at EOF and the process has been reaped. Reading the
        # streams to the end matters: a run that uploaded prints its delete
        # token on stderr and there is no second chance to see it.
        if self.pending_streams > 0:
            return
        self.pending_streams = -1
        self.proc = None
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0
        self.progress.set_fraction(1.0)
        self.progress.set_visible(False)
        self.stop_btn.set_visible(False)
        self.run_btn.set_sensitive(True)
        elapsed = int(time.monotonic() - self.started)

        raw = "\n".join(self.stdout_buf)
        self.set_text(self.json_view, raw)

        docs = None
        if raw.strip():
            try:
                parsed = json.loads(raw)
                docs = parsed if isinstance(parsed, list) else [parsed]
            except ValueError as exc:
                self.log(f"\nthe result document did not parse: {exc}\n")

        if docs:
            self.results.set_docs(docs)
            self.set_text(self.report_view, render(docs))
            saved = self.save(raw)
        else:
            self.results.set_placeholder("No result -- the log says why.")
            saved = None

        where = f", saved to {saved}" if saved else ""
        uploaded = bool(self.submitting and self.uploads)
        if self.stopped:
            self.status.set_text(f"Stopped after {elapsed} s.")
        elif self.exit_status == 0 and docs:
            done = "measured and uploaded" if uploaded else "Done"
            self.status.set_text(f"{done.capitalize()} in {elapsed} s{where}.")
        elif docs and self.submitting:
            # The measurement is fine and kept; the upload is what failed, and
            # the benchmark said why on stderr.
            # The failure first: a long path is cut off at the end of the line.
            self.status.set_text(
                f"The upload failed -- the log says why. Measured in {elapsed} s{where}."
            )
        elif self.exit_status == 0:
            self.status.set_text(f"Finished in {elapsed} s, but produced no result.")
        else:
            self.status.set_text(
                f"The benchmark stopped with an error after {elapsed} s -- the log says why."
            )
        if uploaded:
            self.show_upload()
        # The table stays in view when the run went well; the log comes up
        # when it holds the reason something did not.
        if not self.stopped and (self.exit_status != 0 or not docs):
            self.show_output(self.log_view)
        if SMOKE:
            self.smoke_report(docs=len(docs or []), saved=saved)

    def show_upload(self):
        """A link to the uploaded result on the hub, where it is compared with
        everyone else's. A --variants run uploads one run per variant; the
        first is the baseline's."""
        first = self.uploads[0]
        page = str(first.get("url") or "")
        if page.startswith("/"):
            page = self.upload_hub.rstrip("/") + page
        if page.startswith(("http://", "https://")):
            self.hub_page = page
            self.hub_link.set_visible(True)
        tip = f"Opens {page}." if page else ""
        tokens = [str(r["delete_token"]) for r in self.uploads if r.get("delete_token")]
        if tokens:
            # Shown once by the hub and never again; the log keeps it too.
            tip += ("\n\nTo withdraw the upload later you need its delete token, "
                    "which is in Output > Log: " + ", ".join(tokens))
        self.hub_link.set_tooltip_text(tip.strip())

    def save(self, raw):
        # Local time, and the run itself carries no clock -- this name is for
        # whoever is looking at the directory afterwards.
        stamp = datetime.now(tz=None).astimezone().strftime("%Y%m%d-%H%M%S")
        name = f"cpcpub-{stamp}.json"
        path = os.path.join(self.outdir.get_text().strip(), name)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw if raw.endswith("\n") else raw + "\n")
        except OSError as exc:
            self.log(f"\ncould not write {path}: {exc}\n")
            return None
        self.log(f"\nsaved: {path}\n")
        return path

    def on_stop(self, _button):
        if self.proc is not None:
            self.log("\nstopped\n")
            self.stopped = True
            self.proc.kill()

    # -- the packaging tests' run ------------------------------------------

    def smoke_run(self):
        """The shortest run the form can describe, started as if by hand."""
        self.mode_full.set_active(False)        # leaves the multi-threaded run
        self.threads.set_text("1")
        self.cpus.set_text("0")
        self.seconds.set_value(0.05)
        self.reps.set_value(1)
        self.warmup.set_value(0.0)
        self.outdir.set_text(os.path.dirname(os.path.abspath(SMOKE)))
        self.on_run(None)
        if self.proc is None:                   # it did not start; fail() said why
            self.smoke_report(docs=0, saved=None)
        else:
            # Generous: a run this short takes a second or two, but the first
            # start of a freshly installed program can be slow on Windows.
            GLib.timeout_add_seconds(120, self.smoke_report, 0, None)
        return GLib.SOURCE_REMOVE

    def smoke_report(self, docs, saved):
        buf = self.log_view.get_buffer()
        report = {
            "binary": self.binary.get_text().strip(),
            "variants": self.selected_variants() or [n for n, _ in self.variant_checks],
            "gtk": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}."
                   f"{Gtk.get_micro_version()}",
            "exit": self.exit_status,
            "docs": docs,
            "saved": saved,
            "status": self.status.get_text(),
            "log": buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
        }
        with open(SMOKE, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        if self.proc is not None:
            self.proc.kill()
        app = self.get_application()
        if app is not None:
            app.quit()
        return GLib.SOURCE_REMOVE


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # Installed on Linux the icon is in the theme under this name, put there
        # by the package. The Windows build carries it in a folder of its own.
        Gtk.Window.set_default_icon_name(APP_ID)

    def do_activate(self):
        win = Window(self)
        if FROZEN:
            Gtk.IconTheme.get_for_display(win.get_display()).add_search_path(
                os.path.join(getattr(sys, "_MEIPASS", ""), "icons")
            )
        if SMOKE:
            win.connect("map", lambda *_: GLib.timeout_add(500, win.smoke_run))
        win.present()


if __name__ == "__main__":
    # A windowed Windows build has no console, so nothing to print a traceback
    # to; under the packaging tests it goes beside their report instead.
    if SMOKE and sys.stderr is None:
        # Open for the life of the process, which is what a with-block is not.
        sys.stderr = open(SMOKE + ".log", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.exit(App().run(sys.argv))
