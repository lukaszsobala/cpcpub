// What the microcontroller front end (frontend.c) needs from a chip.
//
// frontend.c is the same on every board: it reads the command line over the
// serial console, sizes the buffers, runs the measurement in bench/src and
// writes the results. Everything that differs between chips is behind the
// functions below. Each port -- mcu/pico/port.c, mcu/esp32/main/port.c,
// mcu/zephyr/src/port.c, and mcu/host/port.c for testing the front end on a
// desktop -- defines all of them, its own entry point, and the three clock
// hooks measure.h asks every front end for (cpu_cur_mhz, dram_mhz, rated_mhz).

#ifndef CPCPUB_MCU_PORT_H
#define CPCPUB_MCU_PORT_H

#include <stddef.h>

#include "measure.h"

// The front end itself, which never returns. Each port's entry point --
// main(), or app_main() under ESP-IDF -- calls it and does nothing else.
void frontend_run(void);

// Bring up whatever the console needs and return once a host is there to read
// it. Called once, first.
void port_init(void);

// The next character from the console, blocking until there is one.
int port_getchar(void);

// Run worker() on each of w[0..n-1] at once, w[i] on core cores[i], and return
// when every one has finished. The caller has already filled each worker in,
// barrier included; cores[] names n distinct cores.
void port_run(worker_t *w, const int *cores, int n);

// Bytes the heap could hand out right now, in one block or several. What the
// memory phases are sized from.
size_t port_heap_bytes(void);

// What a port without a figure of its own can answer that with: malloc asked
// for blocks, largest first, up to `ceiling` bytes each, and all given back.
// The heap must return NULL when it is out, not stop the program.
size_t heap_probe(size_t ceiling);

// How the document describes this machine, all as plain strings:
//   chip      the part, e.g. "RP2040" -- system.machine
//   core      the core's name, e.g. "Cortex-M0+" -- in system.cpu_models
//   target    the normalised instruction set, e.g. "armv6m" -- build.target
//   sdk       what stands in for an operating system, e.g. "pico-sdk"
//   sdk_ver   its version -- system.release
typedef struct {
    const char *chip;
    const char *core;
    const char *target;
    const char *sdk;
    const char *sdk_ver;
} port_info_t;

void port_info(port_info_t *out);

#endif  // CPCPUB_MCU_PORT_H
