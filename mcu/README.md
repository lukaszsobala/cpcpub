# cpcpub on microcontrollers

The benchmark as firmware, for chips with no operating system. The kernels,
the timed driver and the scoring are the desktop binary's own
(`bench/src/kernels.c`, `measure.c`, `report.c`), compiled for the chip; what is
here is the front end that stands in for `bench.c`: it takes the same options
over the serial console, prints the same tables, and writes the same
`cpu-bench/1` document.

| Board | Chip | Cores | Build |
| --- | --- | --- | --- |
| Raspberry Pi Pico, Pico W | RP2040 | 2x Cortex-M0+ | `-DPICO_BOARD=pico` |
| Raspberry Pi Pico 2 | RP2350 | 2x Cortex-M33 | `-DPICO_BOARD=pico2` |
| Raspberry Pi Pico 2, RISC-V | RP2350 | 2x Hazard3 | `-DPICO_BOARD=pico2 -DPICO_PLATFORM=rp2350-riscv` |

Other RP2040 and RP2350 boards work the same way with their own `PICO_BOARD`.

## Build

Needs the [pico-sdk](https://github.com/raspberrypi/pico-sdk) 2.0 or newer,
CMake and a cross-compiler: `arm-none-eabi-gcc` for the Arm cores (Debian and
Ubuntu: `gcc-arm-none-eabi libnewlib-arm-none-eabi
libstdc++-arm-none-eabi-newlib`), and for Hazard3 the RISC-V toolchain
Raspberry Pi publishes in
[pico-sdk-tools](https://github.com/raspberrypi/pico-sdk-tools/releases).
The SDK fetches and builds `picotool` itself if none is installed.

```sh
cmake -S mcu/pico -B mcu/pico/build -DPICO_SDK_PATH=~/pico-sdk -DPICO_BOARD=pico2
cmake --build mcu/pico/build                    # -> mcu/pico/build/cpcpub.uf2

# Hazard3: the same, pointed at the RISC-V toolchain
cmake -S mcu/pico -B mcu/pico/build-rv -DPICO_SDK_PATH=~/pico-sdk -DPICO_BOARD=pico2 \
      -DPICO_PLATFORM=rp2350-riscv -DPICO_TOOLCHAIN_PATH=~/riscv-toolchain
```

`-DCPCPUB_STDIO=uart` puts the console on GPIO 0/1 for a USB-serial adapter
instead of the board's USB port; see below for why you might want that.

## Run

Hold BOOTSEL while plugging the board in, and copy `cpcpub.uf2` to the drive
that appears. The board restarts as a USB serial port and waits for a terminal.
Then, from a desktop:

```sh
python3 mcu/cpcpub-serial.py                    # --full; saves cpcpub-<chip>-<time>.json
python3 mcu/cpcpub-serial.py -o run.json -- --variants --time 1
python3 mcu/cpcpub-serial.py --port /dev/ttyACM0 -- --per-core
```

It finds the board, types the options after `--` at its prompt with `--json`
added, shows the board's output as it comes, and writes the document. A
`--variants` run writes an array, as `cpcpub --variants --json` does. Standard
library only on Linux and macOS; on Windows it needs `pip install pyserial`.

Or open the port in any terminal (`picocom /dev/ttyACM0`, `screen`, PuTTY) and
type at the `cpcpub>` prompt: the options are the desktop's — `--full`,
`--per-core`, `--cpus 1`, `--time`, `--reps`, `--variants`, `--json`, `--help`
— less the ones with nothing to act on here (`--submit`, `--disp-sweep`,
`--cooldown`). A `--json` result is printed between `-----BEGIN CPCPUB
JSON-----` and `-----END CPCPUB JSON-----` lines.

A default `--full` run is three passes of the suite -- both cores at once,
then each alone -- at 17 half-second phases a pass and three repetitions each:
about a minute and a half of measuring, the same arithmetic as the desktop's.

## What the numbers mean here

Read [bench/README.md](../bench/README.md) first; this is what changes.

- **Memory is on-chip SRAM, with no cache in front of it.** `MEM` is the SRAM
  bus and `MEMlat` an SRAM access a few cycles long; nothing here is DRAM, and
  on in-order cores that stall on every load, expect `MLP` near 1. The buffer
  is sized from the heap that is free:

  | | heap | per core, both cores | one core alone |
  | --- | --- | --- | --- |
  | RP2040 | 222 KiB | 39 KiB | 142 KiB |
  | RP2350, Cortex-M33 | 479 KiB | 167 KiB | 399 KiB |
  | RP2350, Hazard3 | 457 KiB | 156 KiB | 377 KiB |

  What the dispatch phases hold beside it (64 KiB a core) and a 16 KiB margin
  come out first; `--mem-per-thread` overrides it.
- **`FP-*` is double precision in software on all three.** The RP2040 runs the
  routines in its boot ROM, the RP2350's Cortex-M33 the SDK's routines on its
  double-precision coprocessor, and Hazard3 the compiler's soft-float. That is
  the point of the `FP32-*` pair (outside the score): on the Cortex-M33 it is
  the hardware FPU, and its `scalar-fma` variant is a real `vfma.f32`, so
  `--variants` there compares two genuinely different builds. On the RP2040 and
  Hazard3, `FP32-*` is software too.
- **The kernels run from SRAM, not flash.** Code normally executes from flash
  through a 16 KiB cache both cores share; the kernels and the soft-float
  routines they call are copied to SRAM at boot so that cache is not what gets
  measured.
- **The clock is measured** by the chip's own frequency counter against the
  crystal, so `mhz` is `measured` and exact, not a rated figure.
- **Each worker has a core to itself**, so `pin` is always true and there is no
  scheduler to argue with. Waiting cores sleep (WFE) rather than poll, so they
  take no bus cycles from the one still measuring.
- **USB interrupts core 0.** The USB console is serviced by an interrupt on
  core 0 about once a millisecond, a cost core 1 never pays — the per-core
  sweep shows it as core 0 reading slightly slower. A `CPCPUB_STDIO=uart` build
  has no such interrupt while it measures.
- **Expect little or no indirect branch prediction.** These cores have small
  front ends, if any, so `DISPcap` may well read `none`. That is a result, not
  a failure.

The document says what it came from: `system.sysname` is `pico-sdk`, `machine`
the chip, `build.target` `armv6m`, `armv8m` or `riscv32`; there is no
`binary_sha256`. See [schema/cpu-bench-1.md](../schema/cpu-bench-1.md).

## Layout

| | |
| --- | --- |
| [frontend.c](frontend.c) | the front end: console, options, sizing, tables, document. The same on every chip |
| [port.h](port.h) | what a chip has to supply to it |
| [pico/](pico/) | the RP2040/RP2350 port and its CMake build; the platform layer is `bench/src/platform_pico.h` |
| [host/](host/) | a port that runs the front end on a desktop, for testing it: `make check` holds its document to the same contract as `bench/cpcpub`'s |
| [cpcpub-serial.py](cpcpub-serial.py) | runs a board from a desktop and saves the result |
