# cpcpub for the Raspberry Pi RP2040 and RP2350, built with the pico-sdk.
#
#   cmake -B build -DPICO_SDK_PATH=/path/to/pico-sdk -DPICO_BOARD=pico
#   cmake --build build                    # -> build/cpcpub.uf2
#
# PICO_BOARD picks the chip: pico / pico_w for the RP2040, pico2 for the
# RP2350's Cortex-M33 cores, and pico2 with -DPICO_PLATFORM=rp2350-riscv for
# its Hazard3 RISC-V cores. See mcu/README.md.

cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()
if(NOT DEFINED PICO_SDK_PATH)
    message(FATAL_ERROR "set PICO_SDK_PATH to a pico-sdk checkout (2.0 or newer)")
endif()
include(${PICO_SDK_PATH}/pico_sdk_init.cmake)

project(cpcpub C CXX ASM)
pico_sdk_init()

set(CPCPUB_VERSION "dev" CACHE STRING "what --version reports")
# usb is the board's own USB port. uart is GPIO 0/1 through a USB-serial
# adapter: no USB interrupt on core 0 every millisecond while it measures.
set(CPCPUB_STDIO "usb" CACHE STRING "console: usb or uart")

set(BENCH_SRC ${CMAKE_CURRENT_LIST_DIR}/../../bench/src)
set(MCU_SRC   ${CMAKE_CURRENT_LIST_DIR}/..)

# The flags bench/Makefile builds the desktop binary with, applied to this
# repository's sources only: the SDK's own, compiled into the same target,
# keep the SDK's flags. Reported in build.flags together with the toolchain's
# -mcpu, so a result says what it was built with.
#
# gnu2x where the desktop has c2x: the SDK's RISC-V headers write inline
# assembly as a bare `asm`, which strict C hides. The same dialect otherwise,
# and nothing in the kernels uses an extension, so their code is the same.
set(CPCPUB_CFLAGS -O3 -std=gnu2x -Wall -Wextra -Wshadow -Wconversion
                  -Wdouble-promotion -Wformat=2 -fno-strict-aliasing)
string(JOIN " " CPCPUB_CFLAGS_STR ${CPCPUB_CFLAGS})
string(STRIP "${CMAKE_C_FLAGS} ${CPCPUB_CFLAGS_STR}" BUILD_FLAGS)
string(REGEX REPLACE " +" " " BUILD_FLAGS "${BUILD_FLAGS}")
get_filename_component(BUILD_CC "${CMAKE_C_COMPILER}" NAME)

# The four compilations of the kernels, as in bench/Makefile. GCC needs no
# -f[no-]slp-vectorize: its -f[no-]tree-vectorize covers both vectorizers.
set(VARIANTS scalar-nofma vector-nofma scalar-fma vector-fma)
set(KV_FLAGS_scalar-nofma -fno-tree-vectorize -ffp-contract=off)
set(KV_FLAGS_vector-nofma -ftree-vectorize -ffp-contract=off)
set(KV_FLAGS_scalar-fma   -fno-tree-vectorize -ffp-contract=fast)
set(KV_FLAGS_vector-fma   -ftree-vectorize -ffp-contract=fast)
set(KV_VEC_scalar-nofma 0)
set(KV_VEC_vector-nofma 1)
set(KV_VEC_scalar-fma   0)
set(KV_VEC_vector-fma   1)
set(KV_FMA_scalar-nofma 0)
set(KV_FMA_vector-nofma 0)
set(KV_FMA_scalar-fma   1)
set(KV_FMA_vector-fma   1)

set(KERNEL_OBJECTS)
foreach(v ${VARIANTS})
    string(REPLACE "-" "_" id ${v})
    string(JOIN " " flags ${KV_FLAGS_${v}})
    add_library(kernels_${id} OBJECT ${BENCH_SRC}/kernels.c)
    target_compile_options(kernels_${id} PRIVATE ${CPCPUB_CFLAGS} ${KV_FLAGS_${v}})
    target_compile_definitions(kernels_${id} PRIVATE
        CPCPUB_PICO=1 KV_ID=_${id} KV_NAME="${v}" KV_FLAGS="${flags}"
        KV_VECTORIZE=${KV_VEC_${v}} KV_FMA=${KV_FMA_${v}})
    list(APPEND KERNEL_OBJECTS $<TARGET_OBJECTS:kernels_${id}>)
endforeach()

set(CPCPUB_SOURCES
    ${BENCH_SRC}/measure.c
    ${BENCH_SRC}/report.c
    ${BENCH_SRC}/sha256.c
    ${MCU_SRC}/frontend.c
    ${CMAKE_CURRENT_LIST_DIR}/port.c)
set_source_files_properties(${CPCPUB_SOURCES} PROPERTIES COMPILE_OPTIONS "${CPCPUB_CFLAGS}")

add_executable(cpcpub ${CPCPUB_SOURCES} ${KERNEL_OBJECTS})
target_include_directories(cpcpub PRIVATE ${BENCH_SRC} ${MCU_SRC})
target_compile_definitions(cpcpub PRIVATE
    CPCPUB_PICO=1
    CPCPUB_VERSION="${CPCPUB_VERSION}"
    BUILD_CC="${BUILD_CC}"
    BUILD_FLAGS="${BUILD_FLAGS}"
    # The fastest clock Raspberry Pi supports, as the ESP32 builds run at their
    # rated maximum: 200 MHz on the RP2040 rather than the SDK's default 125,
    # with the core voltage the SDK raises for it; the RP2350's 150 already is.
    PICO_USE_FASTEST_SUPPORTED_CLOCK=1
    # The soft-float routines the FP kernels call, in SRAM with the kernels,
    # so FP-* measures the arithmetic rather than the flash cache.
    PICO_FLOAT_IN_RAM=1
    PICO_DOUBLE_IN_RAM=1
    # port_heap_bytes() asks malloc how much it can have, so a refusal has to
    # be a NULL and not a panic. The workers cope with one the same way.
    PICO_MALLOC_PANIC=0
    PICO_USE_MALLOC_MUTEX=1
    # Each core's stack fills its own 4 KiB scratch bank, double the SDK's
    # default: a worker frame is small, but a perror on a failed allocation
    # goes through printf, which is not. Separate banks from the heap, too, so
    # a stack is never in the SRAM a memory phase is measuring.
    PICO_STACK_SIZE=0x1000
    PICO_CORE1_STACK_SIZE=0x1000)

# printf from the C library rather than the SDK's small one, so %g, %zu and
# %llu format as they do on a host and the document reads the same.
pico_set_printf_implementation(cpcpub compiler)

if(CPCPUB_STDIO STREQUAL "uart")
    pico_enable_stdio_usb(cpcpub 0)
    pico_enable_stdio_uart(cpcpub 1)
else()
    pico_enable_stdio_usb(cpcpub 1)
    pico_enable_stdio_uart(cpcpub 0)
endif()

target_link_libraries(cpcpub pico_stdlib pico_multicore pico_sync hardware_clocks)
pico_add_extra_outputs(cpcpub)
