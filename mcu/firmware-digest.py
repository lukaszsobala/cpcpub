#!/usr/bin/env python3
"""The digest a firmware build reports as build.binary_sha256, for the release
manifest: firmware-digest.py pico|esp BUILD_DIR ASSET TARGET OBJDUMP

Prints one manifest entry as JSON -- {"filename", "sha256", "digest_of",
"target", "march"} -- for release.yml to put in verified-builds.json.

The digest is of the image, not of ASSET, the file a user downloads: a .uf2
wraps the image in blocks for the BOOTSEL drive, and an ESP32 .bin puts the
bootloader and the partition table in front of it. So what the chip hashes and
what this script hashes have to be shown to be the same bytes, and to be the
bytes ASSET writes to flash; it fails rather than print a digest no board
running ASSET would report.

  pico  The chip hashes flash from __flash_binary_start to __flash_binary_end
        (mcu/pico/port.c). Here: cpcpub.bin, checked to be exactly that span of
        the ELF and exactly what the .uf2 writes there.
  esp   ESP-IDF appends a SHA-256 of the application image to it, and the
        chip reports that once it has hashed the image in flash again and
        found it matches (mcu/esp32/main/port.c). Here: cpcpub.bin's last 32
        bytes, checked against a hash of the rest, with the image checked to be
        what the merged .bin writes at the application's offset.
"""

import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

FLASH_BASE = 0x10000000          # RP2040 and RP2350 alike: XIP, where the image starts
UF2_MAGIC = (0x0A324655, 0x9E5D5157, 0x0AB16F30)
UF2_NOT_MAIN_FLASH = 0x00000001


def fail(msg):
    sys.exit(f"firmware-digest: {msg}")


def symbols(objdump, elf, names):
    """Addresses of `names` from the ELF's symbol table."""
    out = {}
    table = subprocess.run([objdump, "-t", elf], check=True, capture_output=True,
                           text=True).stdout
    for line in table.splitlines():
        f = line.split()
        if f and f[-1] in names:
            out[f[-1]] = int(f[0], 16)
    missing = set(names) - set(out)
    if missing:
        fail(f"{elf} has no {', '.join(sorted(missing))}")
    return out


def uf2_flash(path):
    """{address: byte block} of what a .uf2 writes to the chip's flash."""
    data = Path(path).read_bytes()
    if len(data) % 512:
        fail(f"{path} is not a whole number of 512-byte UF2 blocks")
    blocks = {}
    for off in range(0, len(data), 512):
        b = data[off:off + 512]
        m0, m1, flags, addr, size = struct.unpack_from("<5I", b, 0)
        (m_end,) = struct.unpack_from("<I", b, 508)
        if (m0, m1, m_end) != UF2_MAGIC:
            fail(f"{path}: block {off // 512} is not a UF2 block")
        if flags & UF2_NOT_MAIN_FLASH:
            continue
        blocks[addr] = b[32:32 + size]
    return blocks


def pico(build, asset, objdump):
    image = (build / "cpcpub.bin").read_bytes()
    sym = symbols(objdump, str(build / "cpcpub.elf"),
                  {"__flash_binary_start", "__flash_binary_end"})
    start, end = sym["__flash_binary_start"], sym["__flash_binary_end"]
    if start != FLASH_BASE:
        fail(f"the image starts at {start:#x}, not at the start of flash")
    if end - start != len(image):
        fail(f"cpcpub.bin is {len(image)} bytes but the ELF's image is {end - start}")

    # Every byte the chip hashes must be one the .uf2 writes, and the same one.
    # Blocks past the image's end (the RP2350's errata block, padding to a
    # whole block) are written but not hashed, so do not matter here.
    written = bytearray(len(image))
    covered = bytearray(len(image))
    for addr, payload in uf2_flash(asset).items():
        lo, hi = max(addr, start), min(addr + len(payload), end)
        if lo >= hi:
            continue
        written[lo - start:hi - start] = payload[lo - addr:hi - addr]
        covered[lo - start:hi - start] = b"\1" * (hi - lo)
    if 0 in covered:
        fail(f"{asset} leaves {covered.count(0)} of the image's bytes unwritten")
    if bytes(written) != image:
        fail(f"{asset} writes different bytes from cpcpub.bin")
    return hashlib.sha256(image).hexdigest()


def esp(build, asset):
    image = (build / "cpcpub.bin").read_bytes()
    # The image header: magic 0xE9 first, and in the extended header that
    # follows the eight-byte common one, hash_appended at offset 23.
    if image[0] != 0xE9:
        fail("cpcpub.bin is not an ESP application image")
    if image[23] != 1:
        fail("cpcpub.bin carries no appended SHA-256 (hash_appended is off)")
    digest = image[-32:]
    if hashlib.sha256(image[:-32]).digest() != digest:
        fail("cpcpub.bin's appended SHA-256 does not match the image")

    # Where flashing puts the application, and so where in the merged file it is.
    offset = None
    for line in (build / "flash_args").read_text().splitlines():
        f = line.split()
        if len(f) == 2 and f[1] == "cpcpub.bin":
            offset = int(f[0], 0)
    if offset is None:
        fail("flash_args does not say where cpcpub.bin goes")
    merged = Path(asset).read_bytes()
    if merged[offset:offset + len(image)] != image:
        fail(f"{asset} does not hold cpcpub.bin at {offset:#x}")
    return digest.hex()


def march(elf):
    """-march / -mcpu from the flags string the firmware reports as build.flags."""
    data = Path(elf).read_bytes()
    for s in re.findall(rb"[\x20-\x7e]{16,}", data):
        if b"-std=gnu2x" in s:
            m = re.search(rb"-m(?:arch|cpu)=(\S+)", s)
            return m.group(1).decode() if m else None
    fail(f"{elf} has no build flags string")


def main(kind, build, asset, target, objdump):
    build = Path(build)
    if kind == "pico":
        digest = pico(build, asset, objdump)
    elif kind == "esp":
        digest = esp(build, asset)
    else:
        fail(f"unknown kind {kind!r}")
    print(json.dumps({"filename": Path(asset).name, "sha256": digest,
                      "digest_of": "image", "target": target,
                      "march": march(build / "cpcpub.elf")}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__.splitlines()[0] + " " + __doc__.splitlines()[1])
    main(*sys.argv[1:])
