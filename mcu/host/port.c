// A port for testing the microcontroller front end on a desktop: see port.h.
//
// Not a way to benchmark a desktop -- bench/cpcpub is that. This exists so the
// part of mcu/ that is the same on every chip (the console, the options, the
// sizing, the document) can run where there is no board: `make check` drives
// it and holds the document it writes to the same contract as bench/cpcpub's,
// and mcu/cpcpub-serial.py can be tested against it through a pseudo-terminal.
//
// The console is stdin and stdout; the end of stdin ends the program. Each
// worker is a pthread pinned to its core, and the heap is a fixed RP2040-sized
// one, so the sizing is exercised at the scale it is written for.

#define _GNU_SOURCE 1
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>

#include "port.h"
#include "sha256.h"

int main(void) {
    frontend_run();
}

void port_init(void) {}

int port_getchar(void) {
    const int c = getchar();
    if (c == EOF) exit(0);
    return c;
}

void port_run(worker_t *w, const int *cores, int n) {
    pthread_t th[16];
    (void)cores;    // worker() pins itself to w[i].cpu, which is cores[i]
    for (int i = 0; i < n && i < 16; ++i) {
        if (pthread_create(&th[i], NULL, worker, &w[i]) != 0) {
            perror("pthread_create");
            exit(1);
        }
    }
    for (int i = 0; i < n && i < 16; ++i) pthread_join(th[i], NULL);
}

// What an RP2040 has left for the heap once the firmware is loaded, unless
// $CPCPUB_HEAP says otherwise. A desktop's memory phases need half a megabyte
// per core before they run at all, so a test that wants them sets it larger.
size_t port_heap_bytes(void) {
    const char *e = getenv("CPCPUB_HEAP");
    return e && *e ? (size_t)strtoull(e, NULL, 0) : (size_t)224 << 10;
}

// No clock to read: the results fall back to the INT-lat estimate, which is
// the path a chip whose port cannot measure its clock would take too.
double cpu_cur_mhz(int cpu) { (void)cpu; return 0.0; }
double rated_mhz(int cpu)   { (void)cpu; return 0.0; }
double dram_mhz(void)       { return 0.0; }

// This test binary's own file, as the desktop benchmark hashes its own, so the
// contract test can check the digest the front end writes is the right one.
int port_image_sha256(char out_hex[65]) {
    return sha256_file("/proc/self/exe", out_hex);
}

void port_info(port_info_t *out) {
    out->chip = "host";
    out->core = "host";
    out->target = "host";
    out->sdk = "cpcpub-mcu-host";
    out->sdk_ver = "test";
}
