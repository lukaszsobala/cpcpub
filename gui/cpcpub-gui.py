#!/usr/bin/env python3
"""A small GTK4 front end for the cpcpub benchmark.

It builds a `cpcpub` command line from the form, runs it with `--json`,
streams the binary's prose to the log pane, saves the result document to the
chosen output directory, and renders the numbers. Everything the benchmark
does -- including the upload -- is still done by the benchmark itself; this
process only assembles argv and reads the two streams back.

Needs PyGObject and GTK 4 (python3-gi / gtk4 on Debian and Ubuntu, python3-gobject
/ gtk4 on Fedora and Arch). No other dependency, and none for the benchmark.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "je.qd.cpcpub.Gui"

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
# them, so a result read here and a result read there say the same thing.
METRICS = [
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
    ("score", "score", "geomean"),
]

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
        return f"about {round(seconds)} s ({seconds / 60.0:.1f} min)"
    if seconds < 3600:
        return f"{seconds / 60.0:.1f} min"
    return f"{seconds / 3600.0:.1f} h ({seconds / 60.0:.0f} min)"


def find_binary():
    """The benchmark, looked for beside this file first and then on PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for cand in (
        os.path.join(root, "bench", "cpcpub"),
        os.path.join(root, "bench", "cpcpub.exe"),
        os.path.join(here, "cpcpub"),
    ):
        if os.access(cand, os.X_OK):
            return cand
    return GLib.find_program_in_path("cpcpub") or ""


def list_variants(binary):
    """Ask the binary which variants it carries. Falls back to the four names."""
    if not binary or not os.access(binary, os.X_OK):
        return FALLBACK_VARIANTS
    try:
        done = subprocess.run(
            [binary, "--list-variants"], capture_output=True, timeout=10, check=False,
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


def render(docs):
    """One monospace report for the whole run: a column per measured scope."""
    # Two fixed columns -- the metric and its unit -- and then one per scope.
    NAME_W, UNIT_W, COL_W = 12, 8, 12
    lead = " " * (NAME_W + 1 + UNIT_W)
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

        # `total` and `cores` are both present in a --full document, and one
        # of the two in the narrower modes; `threads` is the per-thread detail
        # behind a total, worth a column only when there is no total to show.
        cols = []
        if doc.get("total"):
            cols.append(("total", doc["total"]))
        for rec in doc.get("cores") or []:
            cols.append((f"cpu{rec.get('cpu')}", rec))
        if not cols:
            for rec in doc.get("threads") or []:
                cols.append((f"thr{rec.get('cpu')}", rec))
        if not cols:
            out.append("(no records)")
            out.append("")
            continue

        def row(name, unit, values):
            cells = "".join(f"{v:>{COL_W}}" for v in values)
            return f"{name:<{NAME_W}} {unit:<{UNIT_W}}{cells}"

        head = row("", "", [c[0] for c in cols])
        out.append(head)
        out.append("-" * len(head))
        out.append(row("clock", "MHz", [fmt(c[1].get("mhz")) for c in cols]))
        # How much those clocks are worth -- measured, rated, given, estimated
        # -- which schema/cpu-bench-1.md warns are not the same claim.
        srcs = sorted({c[1].get("mhz_src") for c in cols if c[1].get("mhz_src")})
        if srcs:
            out.append(f"{lead}  ({', '.join(srcs)})")
        for key, name, unit in METRICS:
            out.append(row(name, unit, [fmt(c[1].get(key)) for c in cols]))
        if doc.get("core_spread") is not None:
            out.append("")
            out.append(f"core spread: {fmt(doc['core_spread'])} x")
        out.append("")
    return "\n".join(out)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="cpcpub")
        self.set_default_size(720, 620)

        self.proc = None
        self.started = 0.0
        self.estimated = 1.0
        self.tick_id = 0
        self.stdout_buf = []
        self.pending_streams = 0
        self.exit_status = -1

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
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

        self.reload_variants()
        self.update_command()
        self.advanced.connect("notify::expanded", lambda *_: self.fit_form())
        # Once, after the first layout pass: until then the form has no width
        # to wrap against and nothing to measure.
        self.connect("map", lambda *_: GLib.idle_add(self.fit_form))

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

        def field(text, widget, span=1):
            nonlocal row
            if text:
                lab = Gtk.Label(label=text, xalign=0, valign=Gtk.Align.BASELINE_CENTER)
                lab.add_css_class("dim-label")
                grid.attach(lab, 0, row, 1, 1)
                grid.attach(widget, 1, row, span, 1)
            else:
                grid.attach(widget, 0, row, span + 1, 1)
            row += 1
            return widget

        # Benchmark binary.
        binrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.binary = Gtk.Entry(hexpand=True, placeholder_text="path to cpcpub")
        self.binary.set_text(find_binary())
        self.binary.connect(
            "changed", lambda *_: (self.reload_variants(), self.update_command())
        )
        binrow.append(self.binary)
        pick = Gtk.Button(label="Browse…")
        pick.connect("clicked", self.on_pick_binary)
        binrow.append(pick)
        field("Benchmark", binrow)

        # Variants: the master check, then one per variant, wrapped to the
        # width there is rather than fixed at a shape a narrow window breaks.
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.all_variants = Gtk.CheckButton(label="All")
        self.all_variants.set_tooltip_text(
            "Run every variant this binary carries (--variants=all) and compare them."
        )
        self.all_variants.connect("toggled", self.on_all_variants)
        vbox.append(self.all_variants)
        self.variant_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
            row_spacing=2, column_spacing=10, margin_start=16,
        )
        vbox.append(self.variant_flow)
        self.variant_checks = []
        field("Variants", vbox)

        # What to run.
        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.mode_threads = Gtk.CheckButton(label="Multi-threaded")
        self.mode_percore = Gtk.CheckButton(label="Per-core", group=self.mode_threads)
        self.mode_full = Gtk.CheckButton(label="Both", group=self.mode_threads)
        self.mode_full.set_tooltip_text(
            "--full: the multi-threaded run and the per-core sweep. Use this for uploads."
        )
        self.mode_threads.set_tooltip_text("The default: every thread at once.")
        self.mode_percore.set_tooltip_text("--per-core: the suite on each CPU in turn.")
        self.mode_full.set_active(True)
        for w in (self.mode_threads, self.mode_percore, self.mode_full):
            w.connect("toggled", lambda *_: self.update_command())
            modes.append(w)
        field("Run", modes)

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

        self.hub = Gtk.Entry(hexpand=True, placeholder_text="https://cpcpub.qd.je:30210")
        self.hub.set_text(os.environ.get("CPCPUB_HUB", ""))
        self.token = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
        self.token.set_text(os.environ.get("CPCPUB_TOKEN", ""))
        self.token.set_tooltip_text(
            "From the hub's Account tab. Passed in the environment, never on the "
            "command line.\nLeave empty to upload anonymously -- every hub accepts that."
        )
        self.run_label = Gtk.Entry(hexpand=True, placeholder_text="short name for this machine")
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
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
            ("Threads", self.threads, "--threads: how many to run at once. Blank leaves it to the binary."),
            ("CPUs", self.cpus, "--cpus: pin to this list, e.g. 0-3,6."),
            ("Seconds/phase", self.seconds, "--time: measured seconds in each phase."),
            ("Repetitions", self.reps, "--reps: runs per phase; the best is kept."),
            ("Warm-up", self.warmup, "--warmup: unmeasured seconds before each phase."),
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
        field("", exp)
        return grid

    def build_output(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.notebook = Gtk.Notebook(vexpand=True)
        self.results_view = self.text_page("Results")
        self.log_view = self.text_page("Log")
        self.json_view = self.text_page("JSON")
        box.append(self.notebook)

        # What the form adds up to, kept out of the scrolling half so it is
        # always in view: how long it should take, and the command that does
        # it. The command is one ellipsized line -- the whole of it is in the
        # tooltip, and it is echoed into the log when a run starts.
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        foot.set_margin_start(12)
        foot.set_margin_end(12)
        foot.set_margin_top(6)
        self.estimate = Gtk.Label(xalign=0, wrap=True)
        self.estimate.add_css_class("heading")
        foot.append(self.estimate)
        self.command = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.command.add_css_class("dim-label")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new("monospace"))
        self.command.set_attributes(attrs)
        foot.append(self.command)
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
        box.append(status)
        return box

    def text_page(self, title):
        view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        view.set_left_margin(8)
        view.set_top_margin(6)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(view)
        self.notebook.append_page(scroll, Gtk.Label(label=title))
        return view

    # -- form behaviour ----------------------------------------------------

    def reload_variants(self):
        child = self.variant_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.variant_flow.remove(child)
            child = nxt
        self.variant_checks = []
        for name, flags, distinct in list_variants(self.binary.get_text().strip()):
            check = Gtk.CheckButton(label=name)
            # Which variants the toggles actually change on this target is the
            # binary's answer, not ours; a target with no vector unit carries
            # four names that are one piece of code.
            check.set_tooltip_text(
                flags if distinct else flags + "  --  same code as the baseline here"
            )
            if not distinct:
                check.add_css_class("dim-label")
            check.variant_name = name
            check.connect("toggled", self.on_variant_toggled)
            self.variant_flow.append(check)
            self.variant_checks.append(check)
        if self.variant_checks:
            self.variant_checks[0].set_active(True)
        self.on_all_variants()

    def on_all_variants(self, *_):
        self.variant_flow.set_sensitive(not self.all_variants.get_active())
        self.update_command()

    def on_variant_toggled(self, *_):
        self.update_command()

    def fit_form(self):
        """Give the form the height it now wants, up to most of the window.

        Opening the hub fields or Advanced roughly doubles it, and a fixed
        split would leave half the form behind a scrollbar for no reason.
        """
        # Measured for the width it actually has: the variant checks wrap, so
        # the unconstrained natural height is a line short of the truth.
        width = self.form_box.get_width()
        natural = self.form_box.measure(Gtk.Orientation.VERTICAL, width or -1)[1]
        height = self.get_height()
        cap = int(height * 0.62) if height > 0 else 380
        self.paned.set_position(min(max(natural, 150), cap))

    def on_submit_toggled(self, *_):
        self.submit_grid.set_visible(self.do_submit.get_active())
        self.fit_form()
        self.run_btn.set_label("Run and submit" if self.do_submit.get_active() else "Run")
        self.update_command()

    def selected_variants(self):
        return [c.variant_name for c in self.variant_checks if c.get_active()]

    def build_argv(self):
        """argv for the run, plus the environment the token travels in."""
        binary = self.binary.get_text().strip()
        argv = [binary or "cpcpub"]

        if self.all_variants.get_active():
            argv.append("--variants=all")
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
        line = " ".join(shlex.quote(a) for a in argv)
        if "CPCPUB_TOKEN" in env:
            line = "CPCPUB_TOKEN=••• " + line
        self.command.set_text(line)
        self.command.set_tooltip_text(line)  # the label itself is ellipsized


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

        if self.all_variants.get_active():
            variants = max(1, len(self.variant_checks))
        else:
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

        argv, env = self.build_argv()
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        )
        for key, value in env.items():
            launcher.setenv(key, value, True)
        try:
            self.proc = launcher.spawnv(argv)
        except GLib.Error as exc:
            self.proc = None
            self.fail(f"Could not start the benchmark: {exc.message}")
            return

        self.set_text(self.log_view, "")
        self.set_text(self.results_view, "")
        self.set_text(self.json_view, "")
        self.stdout_buf = []
        self.exit_status = -1
        self.pending_streams = 3  # two pipes at EOF, plus the reaped process
        self.started = time.monotonic()
        self.log(f"$ {self.command.get_text()}\n\n")

        self.run_btn.set_sensitive(False)
        self.stop_btn.set_visible(True)
        self.notebook.set_current_page(1)
        self.estimated = max(1.0, self.update_estimate())
        self.progress.set_visible(True)
        self.tick_id = GLib.timeout_add_seconds(1, self.on_tick)
        self.on_tick()

        self.read_lines(self.proc.get_stderr_pipe(), self.on_stderr_line)
        self.read_lines(self.proc.get_stdout_pipe(), self.on_stdout_line)
        self.proc.wait_async(None, self.on_finished)

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
        stream = Gio.DataInputStream.new(pipe)
        stream.set_newline_type(Gio.DataStreamNewlineType.ANY)

        def pump(src, res):
            try:
                line, _length = src.read_line_finish_utf8(res)
            except GLib.Error:
                line = None
            if line is None:
                self.pending_streams -= 1
                self.maybe_finish()
                return
            handler(line)
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, pump)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, pump)

    def on_stderr_line(self, line):
        self.log(line + "\n")

    def on_stdout_line(self, line):
        self.stdout_buf.append(line)

    def on_finished(self, proc, res):
        try:
            proc.wait_finish(res)
            self.exit_status = proc.get_exit_status() if proc.get_if_exited() else -1
        except GLib.Error:
            self.exit_status = -1
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
            self.set_text(self.results_view, render(docs))
            self.notebook.set_current_page(0)
            saved = self.save(raw)
        else:
            saved = None

        if self.exit_status == 0 and docs:
            note = f"Done in {elapsed} s"
            if saved:
                note += f" -- saved to {saved}"
            self.status.set_text(note)
        elif self.exit_status == 0:
            self.status.set_text(f"Finished in {elapsed} s, but produced no result.")
        else:
            # A non-zero exit after a full run means the upload did not land;
            # the reason is already on stderr, so point at the log rather than
            # guessing at it here.
            self.status.set_text(
                f"Exit status {self.exit_status} after {elapsed} s -- see the Log tab."
            )

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
            self.proc.force_exit()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        Window(self).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
