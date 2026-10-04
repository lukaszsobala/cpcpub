#!/usr/bin/env python3
"""Build the Windows installers from a release's binaries and the window.

    python3 packaging/windows/build-msi.py RELEASE_DIR GUI_DIR VERSION OUT_DIR

RELEASE_DIR holds the release assets (cpcpub-windows-*.exe), GUI_DIR the
folder build-gui.sh made (dist/cpcpub-gui). Needs wixl, wixl-heat and
msiextract, all from msitools 0.106 or newer, so this runs on Linux, beside the
Linux packaging, from the same staged release files. (0.103, which Ubuntu 24.04
has, crashes on the Environment element that puts the benchmark on PATH.)

One installer per Windows machine, cpcpub-VERSION-windows-x64.msi and
-arm64.msi; see cpcpub.wxs for what goes in and why both are x64 packages.
Each is opened up again afterwards and its benchmark compared with the
release asset, since a result is verified by that file's digest.
"""

import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
TOP = HERE.parents[1]

# (machine, the release asset for it, the x86-64-v3 asset or "")
MACHINES = [
    ("x64", "cpcpub-windows-x86_64.exe", "cpcpub-windows-x86_64-v3.exe"),
    ("arm64", "cpcpub-windows-arm64.exe", ""),
]


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def harvest(gui, work):
    """A WiX fragment listing every file of the window's folder, as the
    component group "Gui" under the directory GUIDIR."""
    files = sorted(p for p in gui.rglob("*") if p.is_file())
    if not (gui / "cpcpub-gui.exe").is_file():
        sys.exit(f"{gui} has no cpcpub-gui.exe; is it the PyInstaller folder?")
    listing = "".join(f"{p}\n" for p in files)
    out = subprocess.run(
        ["wixl-heat", "--var", "var.GuiDir", "--directory-ref", "GUIDIR",
         "--component-group", "Gui", "--prefix", f"{gui}/", "--win64"],
        input=listing, capture_output=True, text=True, check=True,
    ).stdout
    fragment = work / "gui.wxs"
    fragment.write_text(out)
    return fragment, len(files)


def check_wixl():
    out = subprocess.run(["wixl", "--version"], capture_output=True, text=True,
                         check=True).stdout.strip()
    have = tuple(int(x) for x in re.findall(r"\d+", out)[:2])
    if have < (0, 106):
        sys.exit(f"wixl {out} is too old: 0.106 or newer writes the PATH entry")


def main():
    if len(sys.argv) != 5:
        sys.exit((__doc__ or "").split("\n\n")[1])
    check_wixl()
    rel, gui = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]).resolve()
    version, out = sys.argv[3].removeprefix("v"), pathlib.Path(sys.argv[4])
    # Windows Installer compares ProductVersion as up to three numbers.
    if not re.fullmatch(r"\d+(\.\d+){0,2}", version):
        sys.exit(f"version {version!r} is not one to three numbers and dots")
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        fragment, count = harvest(gui, work)
        print(f"the window: {count} files from {gui}")
        for machine, asset, v3 in MACHINES:
            binaries = {"cpcpub.exe": rel / asset}
            if v3:
                binaries["cpcpub-v3.exe"] = rel / v3
            for path in binaries.values():
                if not path.is_file():
                    sys.exit(f"{path} is missing")
            msi = out / f"cpcpub-{version}-windows-{machine}.msi"
            defines = {
                "Version": version, "Arch": machine, "Bin": binaries["cpcpub.exe"],
                "BinV3": binaries.get("cpcpub-v3.exe", ""), "GuiDir": gui,
                "Top": TOP, "Win64": "yes",
            }
            cmd = ["wixl", "--arch", "x64", "--output", str(msi)]
            for key, value in defines.items():
                cmd += ["--define", f"{key}={value}"]
            subprocess.run(cmd + [str(HERE / "cpcpub.wxs"), str(fragment)], check=True)

            # Open it up again: what an installer carries is only known by
            # looking, and the benchmark has to be the release's to the byte.
            unpacked = work / f"unpacked-{machine}"
            unpacked.mkdir()
            subprocess.run(["msiextract", "--directory", str(unpacked), str(msi)],
                           check=True, stdout=subprocess.DEVNULL)
            for name, path in binaries.items():
                found = list(unpacked.rglob(name))
                if len(found) != 1:
                    sys.exit(f"{msi.name} holds {len(found)} copies of {name}")
                if sha256(found[0]) != sha256(path):
                    sys.exit(f"{msi.name}: {name} is not {path.name}")
            if not list(unpacked.rglob("cpcpub-gui.exe")):
                sys.exit(f"{msi.name} holds no cpcpub-gui.exe")
            size = msi.stat().st_size / 2**20
            print(f"{msi.name}: {', '.join(f'{n} = {p.name}' for n, p in binaries.items())}"
                  f"; {size:.1f} MiB")


if __name__ == "__main__":
    main()
