# `cpcpub-gui` — a front end for the benchmark

A small GTK 4 window that builds a `cpcpub` command line, runs it, saves the
result document and shows the numbers. It measures nothing itself: the
benchmark does the work and the upload, and the window shows you the exact
command before it runs one.

```sh
gui/cpcpub-gui.py            # or `make gui` from the top of the tree
```

The release packages install it as `cpcpub-gui`, with a menu entry: the
`cpcpub-gui` .deb, .rpm and Arch package on Linux, and the setup on Windows, which
carries its own GTK. See [packaging/](../packaging/README.md).

From the tree it needs GTK 4.10 or newer and PyGObject, which a desktop install
almost certainly already has:

| | |
| --- | --- |
| Debian, Ubuntu | `apt install python3-gi gir1.2-gtk-4.0` |
| Fedora | `dnf install python3-gobject gtk4` |
| Arch | `pacman -S python-gobject gtk4` |

The benchmark itself still needs nothing installed. The window looks for the
`bench/cpcpub` this tree builds, then the `cpcpub` installed beside itself, then
`cpcpub` on `PATH`. Where a package put a newer-ISA build beside the plain one
-- `cpcpub-v3`, `cpcpub-rva23` -- it takes the newest one this processor has
the instructions for, read off `/proc/cpuinfo` (on Windows, the system's own
AVX2 check), and the plain one otherwise. Those builds do not check for
themselves: started on a processor without the instructions, one dies in the
middle of a run. The path is under **Advanced**, with **Browse…** to point it
somewhere else.

## What the controls do

**Variants** — one checkbox per build variant the binary reports from
`--list-variants`; a target where the toggles change nothing dims the names
that are the same code and says so in the tooltip. One variant alone is
`--variant NAME`, several are `--variants=A,B`, and **All** is
`--variants`, every variant that is not the baseline's code again. **All** sums up the boxes under it — ticked when every
variant is, a dash when some are — so ticking the last variant ticks it too.
Untick any variant to leave "all" with the rest still ticked, or untick
**All** to go back to the baseline alone.

**Run** — the multi-threaded run, the per-core sweep (`--per-core`), or both
(`--full`). Both is the default and the one to upload. **Both** works like
**All**: it ticks itself when both runs are ticked, unticking either run leaves
it, and unticking it goes back to the multi-threaded run alone. One of the two
is always ticked — a run has to do something — so unticking the only ticked
run ticks the other instead.

**Save to** — where the result document is written, as
`cpcpub-<date>.json`, through the desktop's own folder picker. A `--variants`
run writes the JSON array of documents that
[schema/cpu-bench-1.md](../schema/cpu-bench-1.md) describes; a hub takes one
element at a time, which the *Submit a result* tab will do for you.

**Upload the result to a hub** — `--submit`, `--label` and `--notes`; the
fields appear only when it is ticked, and the Run button becomes *Run and
upload*. The token goes into
the environment as `$CPCPUB_TOKEN` rather than onto the command line, which is
what the benchmark reads it from and keeps it out of the process list. After an
upload, **See how it compares** opens the run's page on the hub, and
**Withdraw** takes the upload off it again (every variant's, for a
`--variants` run). The hub's reply to each upload, delete token included, is
kept in `cpcpub/uploads/` under the user's configuration folder, readable by
its owner only, under the result's file name. So **Withdraw** also works on a
result reopened later with **Open…**.

With Upload ticked and no hub to send to, the window asks before measuring. No
Hub URL, no `$CPCPUB_HUB` and no address built into the benchmark is the usual
case for a build from this tree. It offers to run without uploading instead.

The hub, token, label, notes, the upload tick and **Save to** are kept for next
time, in `cpcpub/gui.json` under the user's configuration folder
(`~/.config` on Linux, `%LOCALAPPDATA%` on Windows), readable by its owner only.
`$CPCPUB_HUB` and `$CPCPUB_TOKEN`, when set, win over what was kept and are not
written back in its place.

**Advanced** — `--threads`, `--cpus`, `--time`, `--reps`, `--warmup`,
**Cool-down**, and the benchmark the window runs. The window asks that binary
for its variants and default hub in the background, once typing in the path
pauses. Cool-down is `--cooldown`:
seconds of rest before each multi-threaded run and per-core sweep after the
first, for a laptop or fanless machine that slows down as it heats. It is
greyed out for a run with only one of them, the estimate counts it, and the
status line counts each rest down.

## The results

A finished run fills a table under the form: a row for the multi-threaded
total, then the per-core sweep with one row per kind of core, then the clock, the score, and a column per kernel with
its unit under the name. Hover over a heading or a value for what it means in
words; a clock's value also says whether it was measured, rated, given or
estimated. The score and the six columns it is a geometric mean of are in
bold. Click a heading to sort by that column — click again to reverse it. A
`--variants` run gets a page per variant, switched between by the buttons above
the table.

A kind-of-core row is the mean of its cores and unfolds to them. The grouping
is the results hub's: sorted by score, a step of more than 15% starts a new
kind. An Arm CPU line that names several designs ("Cortex-A55 + Cortex-A78")
names the kinds; otherwise they are large, medium and small. Identical cores
are one row, "all 8 cores". A kind of one core, such as a phone's prime core,
is just that core's row.

**Open…** in the header bar shows a result saved earlier, with its hub link
and **Withdraw** if it was uploaded from this window.

The benchmark's warnings appear as a button beside the status line, such as a
busy machine, or a memory test small enough to fit in the cache. The log that
also holds them stays hidden after a run that went well. If the benchmark
crashes, the status line names the signal. If a newer-ISA build dies of an
illegal instruction, it suggests the plain build.

Closing the window during a run asks first, since closing stops the run.

The text behind the table is hidden until you press **Output** in the header
bar: the benchmark's **Log**, a plain-text **Report** of the same numbers to
paste somewhere, and the raw **JSON**. It shows itself when there is something
in it you need — a run that failed, produced nothing, or uploaded.

## The estimated run time

The line under the output pane is what the current settings should cost, and
the small line below it the parameters the benchmark will get, cut to the
width of the window. The button at its end copies the whole command, binary
included, for a terminal — without the token, which the window passes in the
environment. The command is also echoed into the log when a run starts. One pass
of the suite is nine warm-ups and fifteen measured phases — eight phases at the
full `--time`, and an indirect-dispatch ladder of fourteen points at half of it
— plus about a tenth of a second setting up. `--per-core` runs one pass per CPU
in turn and `--full` adds the threaded pass to that, so the CPU count is most
of the estimate on a machine with many of them; variants multiply the whole
thing.

It is arithmetic on the flags, not a measurement, and it does not know about a
machine that thermally throttles or one busy with something else. On this tree
it predicted 2.82 s for a sweep that took 2.83 and 6.55 s for a run that took
6.63. The progress bar during a run is the same estimate: the benchmark prints
nothing between starting and finishing, so there is no real progress to show.
