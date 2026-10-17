#!/usr/bin/env python3
"""Raspberry Pi OS's C runtime, unpacked as a sysroot for an ARMv6 link:

    bench/armv6/sysroot.py DIR [SUITE]

The ARMv6 build (the first Raspberry Pi and the Pi Zero) is best effort.
Ubuntu's armhf cross-gcc compiles ARMv6 code happily, but what it links in
beside that code -- the start-up objects and libgcc -- is ARMv7 Thumb-2, and an
ARM1176 dies on the first instruction of it. Raspberry Pi OS builds the same
pieces for ARMv6, so the link takes them from there instead:

    make CC=arm-linux-gnueabihf-gcc MARCH="armv6+fp -marm" \\
         LDFLAGS="$(bench/armv6/sysroot.py /tmp/rpi)"

prints the LDFLAGS that point the link at DIR, after filling it.

The archive is served over plain HTTP, so nothing from it is taken on trust:
the suite's InRelease must carry a good signature from the key committed
beside this file (raspbian-archive.asc, fingerprint A0DA 38D0 D76E 8B5D 6388
7281 9165 938D 90FD DD2E), the package index must match the digest that
signed file gives it, and every package the digest the index gives it.
SUITE defaults to trixie, whose glibc (2.41) is new enough for the benchmark.
"""
import hashlib
import lzma
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

ARCHIVE = "http://archive.raspbian.org/raspbian"
HERE = pathlib.Path(__file__).resolve().parent
KEY = HERE / "raspbian-archive.asc"
# What a dynamically linked C program needs at link time: the headers' kernel
# half, glibc's start-up objects and libc.so, and libgcc. The newest libgcc
# the suite has is taken, whichever GCC that is.
PACKAGES = ["libc6", "libc6-dev", "linux-libc-dev"]
LIBGCC_RE = re.compile(r"^libgcc-(\d+)-dev$")
TRIPLE = "arm-linux-gnueabihf"


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def verified_release(suite, work):
    """The InRelease text, once gpgv has checked it against the committed key."""
    signed = work / "InRelease"
    signed.write_bytes(fetch(f"{ARCHIVE}/dists/{suite}/InRelease"))
    keyring = work / "keyring.gpg"
    subprocess.run(["gpg", "--batch", "--quiet", "--dearmor", "--output",
                    str(keyring), str(KEY)], check=True)
    out = work / "Release"
    subprocess.run(["gpgv", "--quiet", "--keyring", str(keyring), "--output",
                    str(out), str(signed)], check=True)
    return out.read_text()


def stanzas(text):
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ":" in line:
                k, v = line.split(":", 1)
                fields[k] = v.strip()
        if fields:
            yield fields


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit((__doc__ or "").split("\n\n")[0])
    root = pathlib.Path(sys.argv[1]).resolve()
    suite = sys.argv[2] if len(sys.argv) == 3 else "trixie"
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        release = verified_release(suite, work)
        index = "main/binary-armhf/Packages.xz"
        want = re.search(rf"^ ([0-9a-f]{{64}}) +\d+ {re.escape(index)}$", release, re.M)
        if not want:
            sys.exit(f"{suite}'s InRelease lists no SHA256 for {index}")
        raw = fetch(f"{ARCHIVE}/dists/{suite}/{index}")
        if hashlib.sha256(raw).hexdigest() != want.group(1):
            sys.exit(f"{index} does not match its signed digest")
        packages = {s["Package"]: s for s in stanzas(lzma.decompress(raw).decode())}
        versions = {int(m.group(1)): name for name in packages
                    if (m := LIBGCC_RE.match(name))}
        if not versions:
            sys.exit(f"{suite} has no libgcc-N-dev")
        gcc_version = max(versions)
        libgcc = versions[gcc_version]
        root.mkdir(parents=True, exist_ok=True)
        for name in PACKAGES + [libgcc]:
            entry = packages[name]
            deb = fetch(f"{ARCHIVE}/{entry['Filename']}")
            if hashlib.sha256(deb).hexdigest() != entry["SHA256"]:
                sys.exit(f"{entry['Filename']} does not match its signed digest")
            path = work / f"{name}.deb"
            path.write_bytes(deb)
            subprocess.run(["dpkg-deb", "-x", str(path), str(root)], check=True)
            print(f"{name} {entry['Version']}", file=sys.stderr)
    # A merged /usr, as the system these come from has: libc.so names
    # /lib/arm-linux-gnueabihf/libc.so.6, and the dynamic loader is looked for
    # at /lib/ld-linux-armhf.so.3.
    if not (root / "lib").exists():
        (root / "lib").symlink_to("usr/lib")
    gcc_dir = root / "usr" / "lib" / "gcc" / TRIPLE / str(gcc_version)
    # -B for the start-up objects (crt1.o and crti.o from glibc, crtbegin.o
    # from GCC), -L for libgcc.a: both searched before the cross-gcc's own.
    print(f"--sysroot={root} -B{gcc_dir}/ -B{root}/usr/lib/{TRIPLE}/ -L{gcc_dir}")


if __name__ == "__main__":
    main()
