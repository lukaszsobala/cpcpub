// The Espressif ESP32 family under ESP-IDF: see mcu/port.h.
//
// One worker task per core, created once at start, pinned to its core and
// waiting for a job; port_run() hands each its worker and waits until all of
// them are done. The tasks are created with static stacks before anything is
// sized, so the heap the memory phases measure is not shared with them, and
// they are never deleted: a deleted task's static memory is only released by
// the idle task some time later, and reusing it before then would corrupt it.
//
// The console is the board's primary one, UART0 behind a USB bridge or the
// USB Serial/JTAG port, read through its driver so a read blocks.

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// ESP-IDF's headers, with the conversion warnings off for them alone: see
// bench/src/platform_esp.h.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wsign-conversion"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_clk_tree.h"
#include "esp_cpu.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#else
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#endif
#pragma GCC diagnostic pop

#include "port.h"

void app_main(void) {
    frontend_run();
}

// ---------------------------------------------------------------------------
// The worker tasks
// ---------------------------------------------------------------------------

// Above every task an application has, and below the system's own (the timer
// and inter-core call tasks), which are idle while nothing here asks them for
// anything.
#define WORKER_PRIO  (configMAX_PRIORITIES - 4)
#define WORKER_STACK 8192

static StackType_t      g_stack[portNUM_PROCESSORS][WORKER_STACK / sizeof(StackType_t)];
static StaticTask_t     g_tcb[portNUM_PROCESSORS];
static TaskHandle_t     g_task[portNUM_PROCESSORS];
static worker_t        *g_job[portNUM_PROCESSORS];
static StaticSemaphore_t g_done_storage;
static SemaphoreHandle_t g_done;

static void core_task(void *arg) {
    const int core = (int)(intptr_t)arg;
    for (;;) {
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        worker(g_job[core]);
        xSemaphoreGive(g_done);
    }
}

void port_run(worker_t *w, const int *cores, int n) {
    for (int i = 0; i < n; ++i) g_job[cores[i]] = &w[i];
    for (int i = 0; i < n; ++i) xTaskNotifyGive(g_task[cores[i]]);
    for (int i = 0; i < n; ++i) (void)xSemaphoreTake(g_done, portMAX_DELAY);
}

// ---------------------------------------------------------------------------
// The console
// ---------------------------------------------------------------------------

void port_init(void) {
    g_done = xSemaphoreCreateCountingStatic(portNUM_PROCESSORS, 0, &g_done_storage);
    for (int c = 0; c < portNUM_PROCESSORS; ++c) {
        g_task[c] = xTaskCreateStaticPinnedToCore(core_task, "cpcpub", WORKER_STACK,
                                                  (void *)(intptr_t)c, WORKER_PRIO,
                                                  g_stack[c], &g_tcb[c], c);
    }
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    usb_serial_jtag_vfs_use_driver();
#else
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 512, 0, 0, NULL, 0));
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
}

int port_getchar(void) {
    uint8_t c = 0;
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    while (usb_serial_jtag_read_bytes(&c, 1, portMAX_DELAY) != 1) {}
#else
    while (uart_read_bytes(CONFIG_ESP_CONSOLE_UART_NUM, &c, 1, portMAX_DELAY) != 1) {}
#endif
    return c;
}

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

// The internal heap, in all its regions: frontend.c then checks the blocks
// are there as well as the bytes.
size_t port_heap_bytes(void) {
    return heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

// ---------------------------------------------------------------------------
// Clocks
// ---------------------------------------------------------------------------

// Measured, not configured: the cycles this core counts in 2 ms of the
// crystal-driven timer. Called from a worker on the core it measures, or from
// the console on core 0 for `cpu` 0, so the counter read is that core's.
double cpu_cur_mhz(int cpu) {
    (void)cpu;
    const int64_t t0 = esp_timer_get_time();
    const uint32_t c0 = (uint32_t)esp_cpu_get_cycle_count();
    int64_t t1;
    while ((t1 = esp_timer_get_time()) - t0 < 2000) {}
    const uint32_t c1 = (uint32_t)esp_cpu_get_cycle_count();
    return (double)(uint32_t)(c1 - c0) / (double)(t1 - t0);
}

// What the clock tree is set to: the rated figure, used only if nothing was
// measured.
double rated_mhz(int cpu) {
    (void)cpu;
    uint32_t hz = 0;
    if (esp_clk_tree_src_get_freq_hz(SOC_MOD_CLK_CPU, ESP_CLK_TREE_SRC_FREQ_PRECISION_CACHED,
                                     &hz) != ESP_OK) {
        return 0.0;
    }
    return (double)hz / 1e6;
}

// No DRAM: the memory phases measure internal SRAM.
double dram_mhz(void) { return 0.0; }

// ---------------------------------------------------------------------------
// The machine
// ---------------------------------------------------------------------------

void port_info(port_info_t *out) {
    // "esp32s3" -> "ESP32-S3": the part as Espressif writes it.
    static char chip[16];
    const char *t = CONFIG_IDF_TARGET;
    size_t n = 0;
    for (const char *p = t; *p && n + 2 < sizeof(chip); ++p) {
        if (p - t == 5) chip[n++] = '-';
        chip[n++] = (*p >= 'a' && *p <= 'z') ? (char)(*p - 'a' + 'A') : *p;
    }
    chip[n] = '\0';
    out->chip = chip;
#if CONFIG_IDF_TARGET_ESP32
    out->core = "Xtensa LX6";
    out->target = "xtensa";
#elif CONFIG_IDF_TARGET_ARCH_XTENSA
    out->core = "Xtensa LX7";
    out->target = "xtensa";
#else
    out->core = "RISC-V";
    out->target = "riscv32";
#endif
    out->sdk = "esp-idf";
    out->sdk_ver = esp_get_idf_version();
}
