# `cpu-bench/1` — the result document

The only interface between the two halves of this repo. The benchmark
([bench/](../bench/)) writes it; the results hub ([web/](../web/)) reads it.
They share no code, so this file and [tests/test_contract.py](../tests/test_contract.py)
are what keep them in agreement — the test enforces every rule stated here, and
a live sample lives in [testdata/full-run.json](../testdata/full-run.json).

Produced by `cpcpub --json` (also `--per-core --json`, `--full --json`).
Written by `json_open` / `emit_json` in [bench/src/bench.c](../bench/src/bench.c)
and, for each record, `json_result` in [bench/src/report.c](../bench/src/report.c);
read by `validate()` in
[web/server.py](../web/server.py).

## Shape

```json
{
  "schema": "cpu-bench/1",
  "mode": "full",
  "build":  { "compiler": …, "compiler_version": …, "cc": …, "flags": …,
              "target": …, "binary_sha256": …, "vectorize": false, "fma": false },
  "system": { "sysname": …, "release": …, "machine": …, "cpus": 8,
              "cpu_models": …, "cpu_layout": …, "cpus_offline": … },
  "config": { "threads": …, "seconds_per_phase": …, "reps": …,
              "warmup_seconds": …, "mem_bytes_per_thread": …,
              "mem_bytes_per_core_sweep": …, "cooldown_seconds": …, "pin": true,
              "clock": "raw", "seed": 1 },
  "dram":   { "name": …, "mhz_min": …, "mhz_max": … },
  "threads": [ record, … ],
  "total":   record,
  "cores":   [ record, … ],
  "core_spread": 1.46,
  "checksum": "0x…"
}
```

`mode` is one of `threads`, `per-core`, `full`. `threads`/`total` are present
for `threads` and `full`; `cores` for `per-core` and `full`. `dram` appears only
on machines exposing a DRAM devfreq node; `system` is only as complete as the
host will say. `config.mem_bytes_per_core_sweep` appears only when a per-core
sweep ran (`per-core` and `full`) — the two phases size their buffers
differently, N threads sharing the cache that one swept core has to itself, so
`mem_bytes_per_thread` describes the multi-threaded phase where there is one.
`config.cooldown_seconds` appears only when `--cooldown` idled the machine
before each multi-threaded run and per-core sweep after the first; the hub
keeps it in the raw document and reads nothing from it. Each system names its own hardware and the field passes that
through unchanged, so the same silicon reads `aarch64` on Linux, `arm64` on
macOS and `ARM64` on Windows — `build.target` is the normalised one to key on.
`config.clock` records the clock that was *used*: asking for `raw` on a host
with no unadjusted monotonic clock (Windows) yields `mono` here.

`system.cpu_models` is the CPU by name — a brand string where the host has one,
the MIDR implementer/part pair rendered as core names on Arm (`Cortex-A55 +
Cortex-A76`, one entry per distinct core, so a big.LITTLE part names both), and
the board in parentheses where the device tree says (`… (Radxa ROCK 5B)`). It is
**absent, never `null`**, on a host that would only name its instruction set: a
value that repeats `build.target` is not a CPU model, and a reader is better off
seeing the field missing than seeing `aarch64` in a column of CPU names.

`system.cpu_layout` says how many of each core the machine has, in the order
`cpu_models` names them: `2x Cortex-A76 + 6x Cortex-A55`, or `4x Cortex-A55` for
one kind. Read from the same per-CPU MIDR lines, so Arm only for now, and
**absent** elsewhere. It lets a hub tell a 4+4 part from a 2+6 one, which
`cpu_models` names identically. Where the kernel names the chip rather than its
cores, `cpu_models` carries that name and this still counts the cores.

`system.cpus_offline` is how many CPUs the kernel lists as present but not
online, the most seen at the start of the run or at its end; **absent** when
none were, or where the machine will not say (anything but Linux). Such a run
measured less than the whole machine -- a board with its big cluster down
measures as a little one -- so the benchmark warns on stderr, and a hub shows
the run but leaves it out of every comparison.

A `--disp-sweep` dump carries the same `schema` and `mode: "disp-sweep"`. The
hub **rejects** it: it is a diagnostic curve with no summary metrics to rank.

## From a microcontroller

[mcu/](../mcu/) writes the same document from its own front end
(`emit_json` in [mcu/frontend.c](../mcu/frontend.c)), with the records written
by the same `json_result`, and the contract test holds it to every rule here.
Where there is no operating system, `system` describes what stands in for one:
`sysname` is the SDK or RTOS (`pico-sdk`, `esp-idf`, `zephyr`), `release` its version, and
`machine` the chip (`RP2040`, `RP2350`, `ESP32-S3`); `cpu_models` is the core
and the chip (`Cortex-M0+ (RP2040)`, `Xtensa LX7 (ESP32-S3)`), `cpu_layout`
counts the cores. Because the chip in parentheses is also `machine`, the hub
keeps it as part of the CPU, where it drops a Linux board's name: a Cortex-M4F
in two different chips is two CPUs on the board. `build.target` is `armv6m` (Cortex-M0+), `armv8m`
(Cortex-M33), `xtensa` (the ESP32, S2 and S3) or `riscv32` (Hazard3 and the
RISC-V ESP32s), and under Zephyr whatever else the board's architecture is
(`armv7m` for a Cortex-M3, M4 or M7, `aarch64`, `riscv64`).
`build.binary_sha256` is the firmware's image as it sits in flash, hashed by
the firmware itself on the Pico and ESP32 builds; see below. A Zephyr build
leaves it out. `config.pin` is always `true`, because each worker runs on the core
it was started on and nothing can move it. `mhz` is `measured` by the chip's
own frequency counter or cycle counter -- except under Zephyr, which has no
portable way to measure it, where it is `rated` from the device tree or the
SysTick rate.

What the memory phases measure is different there, which is why it says so in
the document rather than in a column: on-chip SRAM with no cache in front of
it (behind an L1 cache on the ESP32-P4), so `mem_lat_ns` is an SRAM access and
not a trip to DRAM.

## `build.binary_sha256`

The SHA-256 of the binary that produced the document, in 64 lowercase hex
digits — the benchmark hashing its own executable at report time, found through
`/proc/self/exe`, `_NSGetExecutablePath` or `GetModuleFileName` depending on the
host. **Absent, never `null`**, where none of those can answer: a key carrying an
empty digest would match the next empty digest and mean the opposite of what it
says.

On a microcontroller there is no file, so it is the digest of the firmware's
image in flash, which the firmware reads and hashes itself: on the Pico builds
the image from its first byte to its last, which is the `cpcpub.bin` a build
writes; on an ESP32 the application image less the SHA-256 the build appends to
it, and only if the two agree. Neither is the digest of the file a user
downloads -- a `.uf2` wraps the image, and an ESP32 `.bin` puts the bootloader
in front of it -- so a release's manifest lists the image's digest for each
firmware file, marked `"digest_of": "image"`, and its `SHA256SUMS` the file's.

It exists so a hub can tell a run that says it came from a published build from
one that says nothing. The hub holds the digests a release attached and marks a
matching run as that release's build.

**It is a claim, not a proof, and the hub presents it as one.** This field is
inside a document its submitter wrote, so copying an official digest into a
hand-edited file produces exactly the same bytes as running the official build.
Nor could the benchmark sign its own output to fix that: a key inside a public
binary is a key anyone can extract. Evidence would have to come from somewhere
the submitter does not control, and there is no such place that is also a
benchmark you run with one command — so the hub does not claim one; see
[web/README.md](../web/README.md#what-a-result-proves). What this field buys on
its own is that two results claiming one digest were produced by the same
machine code, which is the comparability question rather than the honesty one.

## More than one document

`cpcpub --variants` measures the same machine once per build variant and
writes a JSON **array** of the documents above, one per variant, in the order
they ran. Each element is an ordinary `cpu-bench/1` document and the array is
the only thing that is new — so this is not a schema change, and the hub is not
asked to understand it.

That works because a variant *is* `build.vectorize` and `build.fma`, which the
hub already stores per run and indexes on. Four variants are four runs of one
machine, which is exactly the shape the leaderboard needs to put them side by
side. Posting the array as one body would instead need a run to hold four of
every metric, and every comparison in the hub to learn about a dimension it does
not have.

`validate()` takes one document, so the splitting happens before the upload.
`cpcpub --variants --submit URL` never builds the array at all: it posts each
variant's document as it finishes, appending the variant name to the label.
Anything else feeding the hub has to split it too — `jq '.[0]'`, or a loop.

## Records

One object per measured core, thread, or whole-machine total. Every key below is
always present; anything not measured is `null`, never absent and never `0`.

`scope` (`cpu`|`thread`|`total`), `cpu`, `mhz`, `mhz_src`, `int_lat_mops`,
`int_thr_mops`, `ilp`, `mul_thr_mmul_s`, `fp_lat_mflops`, `fp_thr_mflops`,
`filp`, `mem_gbps`, `mem_lat_ns`, `mem_lat8_ns`, `mlp`, `disp_thr_mcall_s`,
`disp_cap_calls`, `disp_prediction`, `disp_gain`, `disp_span`, `score`,
`fp32_lat_mflops`, `fp32_thr_mflops`.

`fp32_lat_mflops` and `fp32_thr_mflops` are the two FP kernels again in single
precision — same step, same lanes, same fixed point. They are **not** in
`score` and do not change it. They are there for cores whose FPU is
single-precision only (Cortex-M33, ESP32), where `fp_*` measures a soft-float
library and these measure the hardware; on a core with a double-precision FPU
the two pairs usually read close to each other. The hub ranks them like any
other metric and sets them apart from the ones the score is made of.

`mhz_src` says how much the clock beside it is worth: `measured` was sampled
off the machine while the core was under load, `given` was asserted by whoever
ran it with `--mhz`, `rated` is a ceiling the machine declares rather than a
number anybody observed (cpufreq's max, the device tree's `clock-frequency`,
the Windows registry's `~MHz`, or macOS's `hw.cpufrequency`) — a throttling
core ran below it — and `estimated` was inferred from `int_lat_mops`, which
assumes the core retires one dependent op per cycle and reads low on one that
does not. A machine with no cpufreq driver and no clock in its device tree has
nothing else to offer, which is what `--mhz` is for.

`score` means something different by `scope`: on a `cpu` or `thread` record it is
a single core's composite, on the `total` it is the same geomean over the summed
throughputs and so scales with core count. Both are the same key with the same
unit, so nothing rejects a comparison between them — the hub keeps them apart by
only ever ranking a scope against itself.

### Four of them are arithmetic on the others

These are not independent measurements, and the hub **recomputes them and
refuses an upload where they disagree**:

| field | is | 
| --- | --- |
| `ilp` | `int_thr_mops / int_lat_mops` |
| `filp` | `fp_thr_mflops / fp_lat_mflops` |
| `mlp` | `mem_lat_ns / mem_lat8_ns` |
| `score` | geomean of `int_thr_mops`, `fp_thr_mflops`, `mul_thr_mmul_s`×4, `disp_thr_mcall_s`×20, (`disp_span`+1)×2000, (1000/`mem_lat8_ns`)×100 |

On a `total` record the last of the score's six terms is multiplied by
`config.threads` as well — n threads chase n sets of pointers at once. Terms
that are not positive drop out of the geomean rather than taking it to zero,
and `disp_span` is written as `null` both when there was no dispatch
measurement and when the span came out at zero: `disp_prediction` is `unknown`
in the first case and the term drops, and anything else keeps it. Numbers are
written at six significant figures, so the hub allows 0.1% either way.

This is why the two halves must not drift: change `core_score()`,
`machine_score()` or `summarize()` in [bench/src/measure.c](../bench/src/measure.c)
without changing `score_terms()` in [web/server.py](../web/server.py) and every
honest upload starts being refused.
[tests/test_contract.py](../tests/test_contract.py) runs the hub's check over a
live run of the binary, which is what notices.

**What it is for, and what it is not.** A document edited after the benchmark
wrote it disagrees with itself, and this catches that — which is how a
leaderboard actually gets cheated. It is not evidence: rescale every input,
recompute the four, and it passes. Nothing running on a machine its owner
controls can do better, and the hub claims nothing for it — there is no badge,
no tier, and a passing document simply uploads.

### The key set

The set is exact in **both** directions — a record with an extra key fails the
contract test, and so does one missing a key. A field only one side knows about
is measured and then silently dropped, or stored as `NULL` forever; neither is
visible in the numbers.

## What the hub drops

`core_spread` is emitted and not ingested: it summarises the `cores` array, and
the hub already stores every record it is computed from. It is listed in
`UNCONSUMED_TOP` in the contract test — the classification is deliberate, so a
*newly* unread field trips the test instead of joining it unnoticed.

## Adding a metric

Five edits, in one commit, because a partial change is invisible at runtime:

1. `json_result()` in [bench/src/report.c](../bench/src/report.c) — emit the key.
2. `CORE_FIELDS` in [web/server.py](../web/server.py) — JSON key → column.
3. `METRICS` in [web/server.py](../web/server.py) — only if it should be
   rankable; this is what `/api/metrics` serves and what the front end builds
   its columns from, so no front-end change is needed. Leave `headline` off
   unless the metric belongs in every table by default: the tables show the
   headline set plus whatever is being sorted on, and the rest are one select
   away.
4. `cores` in [web/schema.sql](../web/schema.sql) — the column.
5. `Store.ADDED_CORE_COLUMNS` in [web/server.py](../web/server.py) — the
   migration that adds the column to a database made by an older build.
   (`Store.ADDED_COLUMNS` is the same for a new `runs` column.)

Then the record key list above, `make testdata`, and `make check`.

`schema.sql` creates `cores` with `IF NOT EXISTS`, so it never widens a table
that is already there; step 5 is what does. The contract test checks a *fresh*
database, which always has every column, so it cannot notice a missed step 5 —
`CoresMigrationTest` in [web/test_server.py](../web/test_server.py) does, for the
columns listed there.

## Changing the version

The hub accepts exactly one `schema` string, so the version is a hard gate, not
a hint.

- **Additive** — a new field, or a new optional section. Stays `cpu-bench/1`.
  An older hub ignores what it does not know; an older benchmark's documents
  still validate, and the new column is `NULL` for them.
- **Breaking** — a renamed or removed field, a changed unit, a changed meaning
  for the same key. Bump to `cpu-bench/2`, and have the hub accept both for at
  least one release: uploads come from machines that were built months ago and
  cross-compiled elsewhere, and a rejected upload is a lost measurement.

A changed unit with an unchanged key is the dangerous case — nothing rejects it
and the leaderboard silently mixes two populations. Rename the key instead
(`mem_lat_ns` → `mem_lat_us`) so the contract test fails loudly.
