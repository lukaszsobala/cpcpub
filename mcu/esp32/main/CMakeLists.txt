# The firmware: the shared measurement and front end, this port, and the four
# compilations of the kernels.

# What --version reports; `idf.py -DCPCPUB_VERSION=v1.2 build` sets it.
if(NOT CPCPUB_VERSION)
    set(CPCPUB_VERSION "dev")
endif()

set(BENCH_SRC ${CMAKE_CURRENT_LIST_DIR}/../../../bench/src)
set(MCU_SRC   ${CMAKE_CURRENT_LIST_DIR}/../..)

# The flags bench/Makefile builds the desktop binary with, for this
# repository's sources only; ESP-IDF's own keep ESP-IDF's. gnu2x where the
# desktop has c2x, because ESP-IDF's headers lean on GNU extensions; the same
# dialect otherwise, and the kernels use none of them.
set(CPCPUB_CFLAGS -O3 -std=gnu2x -Wall -Wextra -Wshadow -Wconversion
                  -Wdouble-promotion -Wformat=2 -fno-strict-aliasing)

# The four compilations, as in bench/Makefile. A component compiles each source
# once, so each variant is a one-line file that includes kernels.c, given that
# variant's flags.
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

set(KERNEL_SOURCES)
foreach(v ${VARIANTS})
    set(f ${CMAKE_CURRENT_BINARY_DIR}/kernels-${v}.c)
    file(WRITE ${f}.tmp "#include \"kernels.c\"\n")
    configure_file(${f}.tmp ${f} COPYONLY)
    list(APPEND KERNEL_SOURCES ${f})
endforeach()

set(CPCPUB_SOURCES ${BENCH_SRC}/measure.c ${BENCH_SRC}/report.c
                   ${MCU_SRC}/frontend.c ${CMAKE_CURRENT_LIST_DIR}/port.c)

idf_component_register(SRCS ${CPCPUB_SOURCES} ${KERNEL_SOURCES}
                       PRIV_INCLUDE_DIRS ${BENCH_SRC} ${MCU_SRC})

# Reported in build.flags with the toolchain's own (-mlongcalls, -march), so a
# result says what it was built with. ESP-IDF 6 hands its flags to the
# compiler as a response file (@.../toolchain/cflags), so they are read from
# it; a -specs= path into this build tree is cut to its file name, since where
# someone's build directory was says nothing about the build.
set(TOOLCHAIN_FLAGS "${CMAKE_C_FLAGS}")
if(TOOLCHAIN_FLAGS MATCHES "^@\"?([^\"]+)\"?$")
    if(EXISTS "${CMAKE_MATCH_1}")
        file(READ "${CMAKE_MATCH_1}" TOOLCHAIN_FLAGS)
    else()
        set(TOOLCHAIN_FLAGS "")
    endif()
endif()
string(REPLACE "\"" "" TOOLCHAIN_FLAGS "${TOOLCHAIN_FLAGS}")
string(REGEX REPLACE "[ \t\r\n]+" " " TOOLCHAIN_FLAGS "${TOOLCHAIN_FLAGS}")
string(REGEX REPLACE "-specs=[^ ]*/" "-specs=" TOOLCHAIN_FLAGS "${TOOLCHAIN_FLAGS}")
string(JOIN " " CPCPUB_CFLAGS_STR ${CPCPUB_CFLAGS})
string(STRIP "${TOOLCHAIN_FLAGS} ${CPCPUB_CFLAGS_STR}" BUILD_FLAGS)
string(REGEX REPLACE " +" " " BUILD_FLAGS "${BUILD_FLAGS}")
get_filename_component(BUILD_CC "${CMAKE_C_COMPILER}" NAME)

target_compile_definitions(${COMPONENT_LIB} PRIVATE CPCPUB_ESP=1)
set_source_files_properties(${CPCPUB_SOURCES} PROPERTIES
    COMPILE_OPTIONS "${CPCPUB_CFLAGS}"
    COMPILE_DEFINITIONS "CPCPUB_VERSION=\"${CPCPUB_VERSION}\";BUILD_CC=\"${BUILD_CC}\";BUILD_FLAGS=\"${BUILD_FLAGS}\"")
foreach(v ${VARIANTS})
    string(REPLACE "-" "_" id ${v})
    string(JOIN " " flags ${KV_FLAGS_${v}})
    set_source_files_properties(${CMAKE_CURRENT_BINARY_DIR}/kernels-${v}.c PROPERTIES
        COMPILE_OPTIONS "${CPCPUB_CFLAGS};${KV_FLAGS_${v}}"
        COMPILE_DEFINITIONS "KV_ID=_${id};KV_NAME=\"${v}\";KV_FLAGS=\"${flags}\";KV_VECTORIZE=${KV_VEC_${v}};KV_FMA=${KV_FMA_${v}}")
endforeach()
