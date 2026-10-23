# cpcpub for any board the Zephyr RTOS supports, as a Zephyr application.
#
#   west build -b nrf52840dk/nrf52840 mcu/zephyr
#   west flash
#
# See mcu/README.md.

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cpcpub C)

# What --version reports; `west build ... -- -DCPCPUB_VERSION=v1.2` sets it.
if(NOT CPCPUB_VERSION)
    set(CPCPUB_VERSION "dev")
endif()

set(BENCH_SRC ${CMAKE_CURRENT_LIST_DIR}/../../bench/src)
set(MCU_SRC   ${CMAKE_CURRENT_LIST_DIR}/..)

# The flags bench/Makefile builds the desktop binary with, for this
# repository's sources only; Zephyr's own keep Zephyr's. gnu2x where the
# desktop has c2x, as in the other microcontroller builds: the same dialect,
# and the kernels use no extension.
set(CPCPUB_CFLAGS -O3 -std=gnu2x -Wall -Wextra -Wshadow -Wconversion
                  -Wdouble-promotion -Wformat=2 -fno-strict-aliasing)

# The four compilations, as in bench/Makefile: one source compiled once per
# variant, so each is a one-line file including kernels.c with that variant's
# flags.
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

set(CPCPUB_SOURCES ${BENCH_SRC}/measure.c ${BENCH_SRC}/report.c
                   ${MCU_SRC}/frontend.c ${CMAKE_CURRENT_LIST_DIR}/src/port.c)
target_sources(app PRIVATE ${CPCPUB_SOURCES})
target_include_directories(app PRIVATE ${BENCH_SRC} ${MCU_SRC})
target_compile_definitions(app PRIVATE CPCPUB_ZEPHYR=1)

# Reported in build.flags with the architecture's own (-mcpu, -mfpu, -march)
# that Zephyr picked for the board, so a result says what it was built with.
# A --sysroot into the toolchain's install is cut to its last part: where
# someone unpacked the SDK says nothing about the build.
string(JOIN " " BUILD_FLAGS ${TOOLCHAIN_C_FLAGS} ${CPCPUB_CFLAGS})
string(REGEX REPLACE "--sysroot=[^ ]*/" "--sysroot=" BUILD_FLAGS "${BUILD_FLAGS}")
get_filename_component(BUILD_CC "${CMAKE_C_COMPILER}" NAME)
set_source_files_properties(${CPCPUB_SOURCES} PROPERTIES
    COMPILE_OPTIONS "${CPCPUB_CFLAGS}"
    COMPILE_DEFINITIONS "CPCPUB_VERSION=\"${CPCPUB_VERSION}\";BUILD_CC=\"${BUILD_CC}\";BUILD_FLAGS=\"${BUILD_FLAGS}\"")

foreach(v ${VARIANTS})
    string(REPLACE "-" "_" id ${v})
    string(JOIN " " flags ${KV_FLAGS_${v}})
    set(f ${CMAKE_CURRENT_BINARY_DIR}/kernels-${v}.c)
    file(WRITE ${f}.tmp "#include \"kernels.c\"\n")
    configure_file(${f}.tmp ${f} COPYONLY)
    target_sources(app PRIVATE ${f})
    set_source_files_properties(${f} PROPERTIES
        COMPILE_OPTIONS "${CPCPUB_CFLAGS};${KV_FLAGS_${v}}"
        COMPILE_DEFINITIONS "KV_ID=_${id};KV_NAME=\"${v}\";KV_FLAGS=\"${flags}\";KV_VECTORIZE=${KV_VEC_${v}};KV_FMA=${KV_FMA_${v}}")
endforeach()
