#!/usr/bin/env python3
"""The downloads table at the top of a release's notes.

    python3 packaging/release-notes.py TAG BASE_URL FILE...

GitHub lists a release's assets by name and nothing else, which puts the rpm
for s390x beside the Windows installer. This writes the same files as
Markdown, grouped as the README's "Get it" groups them: packages by system,
standalone binaries by OS and architecture, firmware by chip, then the
checksums. BASE_URL is the release's download URL, .../releases/download/TAG.

A file no rule below knows is listed under "Other files" rather than left
out, so a new asset is never missing from the notes -- only unsorted.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Architectures in the order the README lists them, and what each packaging
# system calls them.
ARCHES = ["x86-64", "Arm64", "32-bit Arm", "RISC-V", "LoongArch", "ppc64le", "s390x"]
DEB = {"amd64": "x86-64", "arm64": "Arm64", "armhf": "32-bit Arm", "riscv64": "RISC-V",
       "loong64": "LoongArch", "ppc64el": "ppc64le", "s390x": "s390x"}
RPM = {"x86_64": "x86-64", "aarch64": "Arm64", "armv7hl": "32-bit Arm", "riscv64": "RISC-V",
       "loongarch64": "LoongArch", "ppc64le": "ppc64le", "s390x": "s390x"}
PKG = {"x86_64": "x86-64", "aarch64": "Arm64", "armv7h": "32-bit Arm", "riscv64": "RISC-V",
       "loong64": "LoongArch", "ppc64le": "ppc64le", "s390x": "s390x"}

# Standalone binaries: cpcpub-OS-ARCH[-variant][.exe], by the README's rows.
BIN_ROWS = [
    ("x86-64", ["x86_64", "x86_64-v3"]),
    ("Arm64", ["aarch64", "arm64"]),
    ("32-bit Arm", ["armv7", "armv7-neon", "armv6"]),
    ("RISC-V", ["riscv64", "riscv64-rva23"]),
    ("Other", ["loongarch64", "ppc64le", "s390x"]),
]
BIN_OSES = ["linux", "macos", "windows", "android"]

# Firmware: cpcpub-CHIP.uf2 / .bin, and the name each chip goes by.
CHIPS = [
    ("Raspberry Pi", [("rp2040", "RP2040"), ("rp2350-arm", "RP2350, Arm"),
                      ("rp2350-riscv", "RP2350, RISC-V")]),
    ("ESP32", [("esp32", "ESP32"), ("esp32s2", "ESP32-S2"), ("esp32s3", "ESP32-S3"),
               ("esp32c3", "ESP32-C3"), ("esp32c6", "ESP32-C6"), ("esp32p4", "ESP32-P4")]),
]


def notes(tag: str, base: str, names: list[str]) -> str:
    left = set(names)
    repo = base.split("/releases/")[0]
    version = re.escape(tag.removeprefix("v"))

    def link(label: str, name: str) -> str:
        left.discard(name)
        return f"[{label}]({base}/{name})"

    def take(pattern: str) -> dict[str, str]:
        """{first group: file name} for the files matching `pattern`."""
        out = {}
        for n in sorted(names):
            m = re.fullmatch(pattern, n)
            if m:
                out[m.group(1)] = n
        return out

    def by_arch(found: dict[str, str], alias: dict[str, str]) -> str:
        named = {alias[k]: v for k, v in found.items() if k in alias}
        return " · ".join(link(a, named[a]) for a in ARCHES if a in named)

    lines = ["## Downloads", ""]

    # Packages.
    rows = []
    for system, arch_re, alias, gui_re in (
            ("Debian, Ubuntu", rf"cpcpub_{version}-\d+_([^_.]+)\.deb", DEB,
             rf"cpcpub-gui_{version}-\d+_all\.deb"),
            ("Fedora, openSUSE", rf"cpcpub-{version}-\d+\.([^.]+)\.rpm", RPM,
             rf"cpcpub-gui-{version}-\d+\.noarch\.rpm"),
            ("Arch Linux", rf"cpcpub-{version}-\d+-([^-]+)\.pkg\.tar\.zst", PKG,
             rf"cpcpub-gui-{version}-\d+-any\.pkg\.tar\.zst")):
        cells = by_arch(take(arch_re), alias)
        gui = [n for n in names if re.fullmatch(gui_re, n)]
        if cells or gui:
            rows.append((system, cells, link("any architecture", gui[0]) if gui else ""))
    setup = [n for n in names if re.fullmatch(rf"cpcpub-{version}-windows-setup\.exe", n)]
    if setup:
        rows.append(("Windows", link("x64 and Arm64", setup[0]), "included"))
    app = [link(label, n) for label, ext in (("app", "apk"), ("Google Play bundle", "aab"))
           for n in names if n == f"cpcpub-{tag.removeprefix('v')}-android.{ext}"]
    if app:
        rows.append(("Android", " · ".join(app), "included"))
    termux = take(rf"cpcpub-termux_{version}-\d+_([^_.]+)\.deb")
    if termux:
        rows.append(("Termux", by_arch(termux, {"aarch64": "Arm64", "arm": "32-bit Arm"}), ""))
    if rows:
        lines += [("**Installers and packages**"), "",
                  "| | benchmark | window |", "| --- | --- | --- |"]
        lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
        lines.append("")

    # Standalone binaries.
    table = []
    for row, arches in BIN_ROWS:
        cells = []
        for os_ in BIN_OSES:
            got = []
            for a in arches:
                exe = ".exe" if os_ == "windows" else ""
                n = f"cpcpub-{os_}-{a}{exe}"
                if n in names:
                    got.append(link(f"`{a}`", n))
            cells.append(" · ".join(got))
        if any(cells):
            table.append(f"| {row} | " + " | ".join(cells) + " |")
    if table:
        lines += [("**Standalone binaries**, which needs nothing installed. The `-v3`, "
                   "`-rva23` and `-neon` builds need a newer CPU and will not start on an "
                   "older one; the plain build runs everywhere."), "",
                  "| | Linux | macOS | Windows | Android |",
                  "| --- | --- | --- | --- | --- |"] + table + [""]

    # Firmware.
    fw = []
    for family, chips in CHIPS:
        got = [link(label, n) for key, label in chips
               for n in (f"cpcpub-{key}.uf2", f"cpcpub-{key}.bin") if n in names]
        if got:
            fw.append(f"| {family} | " + " · ".join(got) + " |")
    if fw:
        lines += [("**Microcontroller firmware**: a `.uf2` is copied to the Pico's drive, "
                   f"a `.bin` written at 0x0. See [mcu/README.md]({repo}/blob/{tag}/mcu/README.md)."),
                  "", "| | chip |", "| --- | --- |"] + fw + [""]

    # Checksums.
    sums = [link(f"`{n}`", n) for n in ("SHA256SUMS", "verified-builds.json") if n in names]
    if sums:
        lines += ["**Check a download** against " + sums[0]
                  + (", and load " + sums[1] + " into a hub so results from these "
                     "builds show which build they came from." if len(sums) > 1 else "."),
                  ""]

    if left:
        lines += ["**Other files**: " + " · ".join(link(f"`{n}`", n) for n in sorted(left)), ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    tag, base, files = argv[0], argv[1].rstrip("/"), argv[2:]
    print(notes(tag, base, sorted({Path(f).name for f in files})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
