#!/usr/bin/env python3
"""Check a firmware build the way CI does: check-firmware.py OBJDUMP ELF LOG

Two things a build can get wrong while still producing a file that flashes and
runs, so nothing else would notice:

- a compiler warning in this repository's own sources (the SDK's are its own
  business), which the desktop build would not let through either;
- a kernel left in flash. The kernels are placed in RAM so that the flash
  cache is not what gets measured (see mcu/README.md); if a toolchain or SDK
  change drops the placement, every number quietly changes meaning. Each of
  the four compilations has to be there, and none of it in a flash section;
- a kernel in RAM that slows down when both cores run from it at once. On the
  ESP32-S3 that is SRAM0: with ESP-IDF's default 16 KiB instruction cache the
  kernels land there, and each core then runs at about two thirds of its speed
  alone, so the both-cores pass measures the memory, not the cores. Which chip
  a build is for comes from the sdkconfig.json ESP-IDF writes beside the ELF.
"""

import json
import os
import re
import subprocess
import sys

# The sections code runs from flash in: .text on the Pico builds, where RAM
# code goes to .data; .flash.text under ESP-IDF, where it goes to .iram0.text.
FLASH_SECTIONS = {".text", ".flash.text"}
VARIANTS = ("scalar_nofma", "vector_nofma", "scalar_fma", "vector_fma")
KERNEL = re.compile(rf"^\w+_kernel\w*_({'|'.join(VARIANTS)})$")
OUR_WARNING = re.compile(r"(?:^|/)((?:bench/src|mcu)/[^:\s]+:\d+(?::\d+)?): warning: (.*)")
# Instruction-bus ranges each chip's kernels must stay out of: [start, end),
# and what is there.
SHARED_CODE_RAM = {
    "esp32s3": [(0x40370000, 0x40378000, "SRAM0 (see sdkconfig.defaults.esp32s3)")],
}


def esp_idf_target(elf):
    """The chip an ESP-IDF build is for, or None for any other build."""
    path = os.path.join(os.path.dirname(elf), "config", "sdkconfig.json")
    try:
        with open(path) as f:
            return json.load(f).get("IDF_TARGET")
    except FileNotFoundError:
        return None


def main(objdump, elf, log):
    failed = False

    with open(log, errors="replace") as f:
        warnings = sorted({m.group(1) + ": " + m.group(2)
                           for m in map(OUR_WARNING.search, f) if m})
    for w in warnings:
        print("warning in this repository's code:", w)
        failed = True

    table = subprocess.run([objdump, "-t", elf], check=True, capture_output=True,
                           text=True).stdout
    avoid = SHARED_CODE_RAM.get(esp_idf_target(elf) or "", [])
    found = {v: 0 for v in VARIANTS}
    for line in table.splitlines():
        # "20000580 l     F .data	00000054 fp_kernel_lat_scalar_nofma"
        f = line.split()
        if len(f) < 5 or "F" not in f[1:-3]:
            continue
        m = KERNEL.match(f[-1])
        if not m:
            continue
        section = f[-3]
        found[m.group(1)] += 1
        if section in FLASH_SECTIONS:
            print(f"{f[-1]} is in {section}: in flash, not RAM")
            failed = True
        addr = int(f[0], 16)
        for start, end, what in avoid:
            if start <= addr < end:
                print(f"{f[-1]} is at {addr:#010x}, in {what}")
                failed = True
    for v, n in found.items():
        if n == 0:
            print(f"no kernels of the {v} compilation")
            failed = True

    if not failed:
        print(f"ok: {sum(found.values())} kernels, all in RAM; no warnings in this repository's code")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit((__doc__ or "").partition("\n")[0])
    sys.exit(main(*sys.argv[1:]))
