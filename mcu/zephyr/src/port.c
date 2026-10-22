// The Zephyr RTOS, on any board it supports: see mcu/port.h.
//
// Each run starts one thread per worker, pinned to its CPU before it starts on
// an SMP build, and joins them all; the stacks are static, so the heap the
// memory phases measure is not shared with them. The console is Zephyr's own
// -- the board's chosen UART, or USB CDC where the board says so.
//
// Nothing portable measures a core's clock here, so the result carries the
// rated one: the device tree's, or else the SysTick rate on a Cortex-M whose
// SysTick counts the processor clock -- which is Zephyr's configuration unless
// the tree says otherwise.

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Zephyr's headers, with the conversion warnings off for them alone: see
// bench/src/platform_zephyr.h.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wsign-conversion"
#include <zephyr/kernel.h>
#include <zephyr/console/console.h>
#include <zephyr/devicetree.h>
#include <zephyr/version.h>
#pragma GCC diagnostic pop

#include "port.h"

int main(void) {
    frontend_run();
    return 0;
}

void port_init(void) {
    console_init();
}

int port_getchar(void) {
    int c;
    while ((c = console_getchar()) < 0) {}
    return c;
}

// ---------------------------------------------------------------------------
// The worker threads
// ---------------------------------------------------------------------------

// Cooperative, so nothing preemptible -- the console thread among them -- can
// take the CPU from a worker; it gives it up only to wait at a barrier.
#define WORKER_PRIO  K_PRIO_COOP(CONFIG_NUM_COOP_PRIORITIES - 1)
#define WORKER_STACK 8192

K_THREAD_STACK_ARRAY_DEFINE(g_stack, CONFIG_MP_MAX_NUM_CPUS, WORKER_STACK);
static struct k_thread g_thread[CONFIG_MP_MAX_NUM_CPUS];

static void worker_entry(void *w, void *unused1, void *unused2) {
    (void)unused1;
    (void)unused2;
    worker(w);
}

void port_run(worker_t *w, const int *cores, int n) {
    for (int i = 0; i < n; ++i) {
        k_thread_create(&g_thread[i], g_stack[i], K_THREAD_STACK_SIZEOF(g_stack[i]),
                        worker_entry, &w[i], NULL, NULL, WORKER_PRIO, K_FP_REGS,
                        K_FOREVER);
#if defined(CONFIG_SCHED_CPU_MASK)
        (void)k_thread_cpu_pin(&g_thread[i], cores[i]);
#else
        (void)cores;
#endif
    }
    for (int i = 0; i < n; ++i) k_thread_start(&g_thread[i]);
    for (int i = 0; i < n; ++i) (void)k_thread_join(&g_thread[i], K_FOREVER);
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

size_t port_heap_bytes(void) {
#if defined(CONFIG_SRAM_SIZE)
    return heap_probe((size_t)CONFIG_SRAM_SIZE * 1024u);
#else
    return heap_probe((size_t)64 << 20);
#endif
}

// ---------------------------------------------------------------------------
// Clocks
// ---------------------------------------------------------------------------

#define CPU0 DT_PATH(cpus, cpu_0)

double cpu_cur_mhz(int cpu) {
    (void)cpu;
    return 0.0;
}

double rated_mhz(int cpu) {
    (void)cpu;
#if DT_NODE_HAS_PROP(CPU0, clock_frequency)
    return (double)DT_PROP(CPU0, clock_frequency) / 1e6;
#elif defined(CONFIG_CORTEX_M_SYSTICK) && \
      !DT_PROP_OR(DT_NODELABEL(systick), external_clock_source, 0)
    return (double)sys_clock_hw_cycles_per_sec() / 1e6;
#else
    return 0.0;
#endif
}

// No DRAM to speak of on the boards this is for, and nothing to read it from.
double dram_mhz(void) { return 0.0; }

// ---------------------------------------------------------------------------
// The machine
// ---------------------------------------------------------------------------

// "arm,cortex-m4f" -> "Cortex-M4F": the core as its maker writes it, from the
// device tree's own compatible string.
static const char *core_name(void) {
    static char out[48];
#if DT_NODE_EXISTS(CPU0) && DT_NODE_HAS_PROP(CPU0, compatible)
    const char *c = DT_PROP_BY_IDX(CPU0, compatible, 0);
    const char *comma = strchr(c, ',');
    if (comma) c = comma + 1;
    // RISC-V cores are named after the ISA, then the variant: "riscv-virt"
    // reads as "RISC-V virt".
    if (!strncmp(c, "riscv", 5)) {
        snprintf(out, sizeof(out), "RISC-V%s%s", c[5] ? " " : "", c[5] ? c + 6 : "");
        return out;
    }
    // The first letter, and everything after the first hyphen, in capitals.
    size_t n = 0;
    for (int up = 1, after = 0; *c && n + 1 < sizeof(out); ++c) {
        out[n++] = (up || after) && *c >= 'a' && *c <= 'z' ? (char)(*c - 'a' + 'A') : *c;
        up = 0;
        after = after || *c == '-';
    }
    out[n] = '\0';
#else
    snprintf(out, sizeof(out), "%s", CONFIG_ARCH);
#endif
    return out;
}

void port_info(port_info_t *out) {
    out->chip = CONFIG_SOC;
    out->core = core_name();
#if defined(CONFIG_ARM64)
    out->target = "aarch64";
#elif defined(CONFIG_ARMV8_M_MAINLINE) || defined(CONFIG_ARMV8_M_BASELINE)
    out->target = "armv8m";
#elif defined(CONFIG_ARMV7_M_ARMV8_M_MAINLINE)
    out->target = "armv7m";
#elif defined(CONFIG_ARMV6_M_ARMV8_M_BASELINE)
    out->target = "armv6m";
#elif defined(CONFIG_RISCV) && defined(CONFIG_64BIT)
    out->target = "riscv64";
#elif defined(CONFIG_RISCV)
    out->target = "riscv32";
#elif defined(CONFIG_XTENSA)
    out->target = "xtensa";
#elif defined(CONFIG_ARCH_POSIX) && defined(__x86_64__)
    out->target = "x86_64";
#elif defined(CONFIG_ARCH_POSIX) && defined(__aarch64__)
    out->target = "aarch64";
#elif defined(CONFIG_X86_64)
    out->target = "x86_64";
#else
    out->target = CONFIG_ARCH;
#endif
    out->sdk = "zephyr";
    out->sdk_ver = KERNEL_VERSION_STRING;
}
