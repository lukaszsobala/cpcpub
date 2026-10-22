# cpcpub on microcontrollers

The benchmark as firmware, for chips with no operating system. The kernels,
the timed driver and the scoring are the desktop binary's own
(`bench/src/kernels.c`, `measure.c`, `report.c`), compiled for the chip; what is
here is the front end that stands in for `bench.c`: it takes the same options
over the serial console, prints the same tables, and writes the same
`cpu-bench/1` document.

| Chip | Cores | FP32 | Port |
| --- | --- | --- | --- |
| RP2040 (Pico, Pico W) | 2x Cortex-M0+, 200 MHz | software | [pico/](pico/) |
| RP2350 (Pico 2), Arm | 2x Cortex-M33, 150 MHz | hardware, with FMA | [pico/](pico/) |
| RP2350 (Pico 2), RISC-V | 2x Hazard3, 150 MHz | software | [pico/](pico/) |
| ESP32 | 2x Xtensa LX6, 240 MHz | hardware, with FMA | [esp32/](esp32/) |
| ESP32-S2 | 1x Xtensa LX7, 240 MHz | software | [esp32/](esp32/) |
| ESP32-S3 | 2x Xtensa LX7, 240 MHz | hardware, with FMA | [esp32/](esp32/) |
| ESP32-C3, ESP32-C6 | 1x RISC-V, 160 MHz | software | [esp32/](esp32/) |
| ESP32-P4 | 2x RISC-V, 400 MHz | hardware, with FMA | [esp32/](esp32/) |

Each runs at the fastest clock its maker supports. `FP-*` -- double precision
-- is software on every one of them. Any board with one of these chips works;
the build is per chip, not per board.

## Raspberry Pi RP2040 and RP2350

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

# RP2040: -DPICO_BOARD=pico. Hazard3: the same, pointed at the RISC-V toolchain
cmake -S mcu/pico -B mcu/pico/build-rv -DPICO_SDK_PATH=~/pico-sdk -DPICO_BOARD=pico2 \
      -DPICO_PLATFORM=rp2350-riscv -DPICO_TOOLCHAIN_PATH=~/riscv-toolchain
```

Hold BOOTSEL while plugging the board in, and copy `cpcpub.uf2` to the drive
that appears. The board restarts as a USB serial port (`/dev/ttyACM0`) and
waits for a terminal.

`-DCPCPUB_STDIO=uart` puts the console on GPIO 0/1 for a USB-serial adapter
instead of the board's own USB port; see below for why you might want that.

## Espressif ESP32

Needs [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) 5.0 or newer,
installed for the chip with its own `install.sh`, which brings the compilers.

```sh
. ~/esp-idf/export.sh
idf.py -C mcu/esp32 set-target esp32s3          # esp32, esp32s2, esp32c3, esp32c6, esp32p4
idf.py -C mcu/esp32 build flash                 # add -p PORT if there is more than one
```

The console is the chip's UART0, which on most development boards is the USB
port with a USB-serial bridge behind it (`/dev/ttyUSB0`). For a board wired to
the chip's own USB Serial/JTAG port instead (many ESP32-C3, C6 and S3 boards),
choose it as the console before building: `idf.py -C mcu/esp32 menuconfig`,
*Component config → ESP System Settings → Channel for console output*.

## Running a board

From a desktop, with the board plugged in:

```sh
python3 mcu/cpcpub-serial.py                    # --full; saves cpcpub-<chip>-<time>.json
python3 mcu/cpcpub-serial.py -o run.json -- --variants --time 1
python3 mcu/cpcpub-serial.py --port /dev/ttyUSB0 -- --per-core
```

It finds the board, waits for its prompt, types the options after `--` with
`--json` added, shows the board's output as it comes, and writes the document.
A `--variants` run writes an array, as `cpcpub --variants --json` does.
Standard library only on Linux and macOS; on Windows it needs
`pip install pyserial`.

Or open the port in any terminal (`picocom /dev/ttyACM0`, `idf.py monitor`,
PuTTY) and type at the `cpcpub>` prompt: the options are the desktop's —
`--full`, `--per-core`, `--cpus 1`, `--time`, `--reps`, `--variants`, `--json`,
`--help` — less the ones with nothing to act on here (`--submit`,
`--disp-sweep`, `--cooldown`). A `--json` result is printed between
`-----BEGIN CPCPUB JSON-----` and `-----END CPCPUB JSON-----` lines.

A default `--full` run on a two-core chip is three passes of the suite -- both
cores at once, then each alone -- at 17 half-second phases a pass and three
repetitions each: about a minute and a half of measuring. A one-core chip runs
two passes.

## What the numbers mean here

Read [bench/README.md](../bench/README.md) first; this is what changes.

- **Memory is on-chip SRAM.** `MEM` is the SRAM bus and `MEMlat` an SRAM access
  a few cycles long; nothing here is DRAM, and on in-order cores that stall on
  every load, expect `MLP` near 1. Every chip here reaches its SRAM directly
  except the ESP32-P4, whose internal memory sits behind an L1 cache, so there
  the memory phases measure a cache and the SRAM behind it, as on a desktop.
  PSRAM, where a board has it, is not used. The buffer is sized from the
  internal heap that is free; on the Pico builds that is:

  | | heap | per core, both cores | one core alone |
  | --- | --- | --- | --- |
  | RP2040 | 222 KiB | 39 KiB | 142 KiB |
  | RP2350, Cortex-M33 | 479 KiB | 167 KiB | 399 KiB |
  | RP2350, Hazard3 | 457 KiB | 156 KiB | 377 KiB |

  What the dispatch phases hold beside it (64 KiB a core) and a 16 KiB margin
  come out first, and on an ESP32, whose heap is several regions, the size is
  then cut until the blocks fit as well as the bytes. `--mem-per-thread`
  overrides all of it.
- **`FP-*` is double precision in software on every chip here.** The RP2040,
  and every ESP32, run the routines in the chip's mask ROM; the RP2350's
  Cortex-M33 runs the SDK's on its double-precision coprocessor; Hazard3 the
  compiler's own. That is the point of the `FP32-*` pair (outside the score):
  on the chips the table marks as hardware it is the FPU, and the `scalar-fma`
  variant is a real fused multiply-add (`vfma.f32`, `madd.s`, `fmadd.s`), so
  `--variants` there compares two genuinely different builds. Elsewhere
  `FP32-*` is software too.
- **The kernels run from RAM, not flash.** Code normally executes from flash
  through a cache -- 16 KiB on the RP2040, shared by both cores -- so the
  kernels, and on the Pico builds the soft-float routines they call, are put in
  SRAM (or the ESP32's instruction RAM) so that cache is not what is measured.
- **The clock is measured**, so `mhz` is `measured`: on the Pico builds by the
  chip's frequency counter against the crystal, on an ESP32 as the cycles a
  core counts against the crystal-driven timer over 2 ms.
- **Each worker has a core to itself**, so `pin` is always true and there is no
  scheduler to argue with. A core that is waiting sleeps (WFE on the Pico
  builds, FreeRTOS's idle task on an ESP32) rather than polls, so it takes no
  bus cycles from the one still measuring.
- **Interrupts while measuring.** On the Pico builds the USB console is served
  by an interrupt on core 0 about once a millisecond, a cost core 1 never pays
  -- the per-core sweep shows it as core 0 reading slightly slower -- and a
  `CPCPUB_STDIO=uart` build has none. On an ESP32 the console raises nothing
  while it is quiet, but FreeRTOS's tick interrupts each core 100 times a
  second. The task watchdog is off, since a run holds every core for longer
  than it allows.
- **Expect little or no indirect branch prediction.** These cores have small
  front ends, if any, so `DISPcap` may well read `none`. That is a result, not
  a failure.

The document says what it came from: `system.sysname` is `pico-sdk` or
`esp-idf` and `release` its version, `machine` the chip, `build.target`
`armv6m`, `armv8m`, `xtensa` or `riscv32`; there is no `binary_sha256`. See
[schema/cpu-bench-1.md](../schema/cpu-bench-1.md).

## Layout

| | |
| --- | --- |
| [frontend.c](frontend.c) | the front end: console, options, sizing, tables, document. The same on every chip |
| [port.h](port.h) | what a chip has to supply to it |
| [pico/](pico/) | the RP2040/RP2350 port and its CMake build; its platform layer is `bench/src/platform_pico.h` |
| [esp32/](esp32/) | the ESP32 port as an ESP-IDF project; its platform layer is `bench/src/platform_esp.h` |
| [host/](host/) | a port that runs the front end on a desktop, for testing it: `make check` holds its document to the same contract as `bench/cpcpub`'s |
| [cpcpub-serial.py](cpcpub-serial.py) | runs a board from a desktop and saves the result |
