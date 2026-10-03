# `cpcpub-gui` — a front end for the benchmark

A small GTK 4 window that builds a `cpcpub` command line, runs it, saves the
result document and shows the numbers. It measures nothing itself: the
benchmark does the work and the upload, and the window shows you the exact
command before it runs one.

```sh
gui/cpcpub-gui.py            # or `make gui` from the top of the tree
```

Linux only for now. It needs GTK 4 and PyGObject, which a desktop install
almost certainly already has:

| | |
| --- | --- |
| Debian, Ubuntu | `apt install python3-gi gir1.2-gtk-4.0` |
| Fedora | `dnf install python3-gobject gtk4` |
| Arch | `pacman -S python-gobject gtk4` |

The benchmark itself still needs nothing installed. The window looks for
`bench/cpcpub` beside this directory, then `cpcpub` on `PATH`; point it
somewhere else with **Browse…**.

## What the controls do

**Variants** — one checkbox per build variant the binary reports from
`--list-variants`; a target where the toggles change nothing dims the names
that are the same code and says so in the tooltip. Checking several runs
`--variants=A,B`; **All** runs `--variants=all`. One variant alone is
`--variant NAME`.

**Run** — the multi-threaded run, the per-core sweep (`--per-core`), or both
(`--full`). Both is the default and the one to upload.

**Save to** — where the result document is written, as
`cpcpub-<date>.json`, through the desktop's own folder picker. A `--variants`
run writes the JSON array of documents that
[schema/cpu-bench-1.md](../schema/cpu-bench-1.md) describes; a hub takes one
element at a time, which the *Submit a result* tab will do for you.

**Upload the result to a hub** — `--submit`, `--label` and `--notes`; the
fields appear only when it is ticked, and the Run button becomes *Run and
submit*. The token goes into
the environment as `$CPCPUB_TOKEN` rather than onto the command line, which is
what the benchmark reads it from and keeps it out of the process list. The hub's
reply, including the delete token an anonymous upload needs to withdraw itself,
lands in the **Log** tab and nowhere else — copy it before closing the window.

**Advanced** — `--threads`, `--cpus`, `--time`, `--reps`, `--warmup`.

## The estimated run time

The line under the output pane is what the current settings should cost, and
the ellipsized line below it is the command that will run — the whole of it is
in the tooltip, and it is echoed into the **Log** when a run starts. One pass
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
