#!/usr/bin/env python3
"""Build the Linux and Termux packages from a release's binaries.

    python3 packaging/linux/build.py RELEASE_DIR VERSION OUT_DIR

RELEASE_DIR holds the release assets (cpcpub-linux-*, cpcpub-android-arm64),
VERSION is the release, with or without its "v". Needs nfpm (or $NFPM) and
readelf. $SOURCE_DATE_EPOCH, when set, dates every file in every package and
the release line in the metainfo, so that the same release packaged twice
comes out byte for byte the same.

Every package carries the release binaries byte for byte. A hub marks a result
verified by the digest of the binary that measured it, and that digest is the
release asset's -- so a package that rebuilt the benchmark, or so much as
stripped it, would install a program whose every result comes out unverified.
That is why this is nfpm and not dpkg-buildpackage or rpmbuild: nfpm puts
files into an archive and does nothing else to them, where the distribution
tools strip binaries and split out debug info by default.

What gets built, per architecture the release has a Linux binary for:

  cpcpub      the benchmark as /usr/bin/cpcpub, plus the newer-ISA build of
              that architecture where the release has one (cpcpub-v3 on
              x86-64, cpcpub-rva23 on RISC-V), as .deb, .rpm and an Arch
              package. It depends on nothing but the glibc it was linked
              against, read out of the binary rather than assumed.
  cpcpub-gui  the GTK window, once for every architecture (it is a Python
              script), at exactly the same version of cpcpub.

And one .deb for Termux, holding the static Android binary under Termux's
prefix. nfpm writes the configs it is given here as JSON, which YAML reads.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

TOP = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
APP_ID = "je.qd.cpcpub.Gui"
MAINTAINER = "Łukasz Sobala <61153692+lukaszsobala@users.noreply.github.com>"
HOMEPAGE = "https://github.com/lukaszsobala/cpcpub"
LICENSE = "GPL-3.0-or-later"
# The package revision: bumped when the packaging changes and the release it
# carries does not.
RELEASE = 1

# (the release asset's suffix, nfpm's name for the architecture, the other
# builds for it as {asset suffix: installed name}). nfpm turns its names into
# each format's own -- amd64 is x86_64 to rpm, loong64 is loongarch64.
ARCHES = [
    ("x86_64", "amd64", {"x86_64-v3": "cpcpub-v3"}),
    ("aarch64", "arm64", {}),
    ("riscv64", "riscv64", {"riscv64-rva23": "cpcpub-rva23"}),
    ("loongarch64", "loong64", {}),
    ("ppc64le", "ppc64le", {}),
    ("s390x", "s390x", {}),
]
# Arch Linux has a port for each of the others, official or not; none for Z.
NO_ARCHLINUX = {"s390x"}

SUMMARY = "small, portable CPU benchmark"
DESCRIPTION = f"""{SUMMARY}
cpcpub runs tiny kernels for a fixed slice of wall-clock time each and reports
the rate: integer and floating-point latency and throughput, integer multiply,
memory bandwidth and random-access latency, and indirect-call throughput and
branch-predictor capacity -- with every thread at once and each core on its
own. It can upload a result to a hub that compares it with other machines."""
GUI_DESCRIPTION = """window for the cpcpub CPU benchmark
A GTK window that builds a cpcpub command line, runs it, saves the result and
lays the numbers out in a table with every column explained. It measures
nothing itself: the benchmark does the work and the upload."""

TERMUX_PREFIX = "/data/data/com.termux/files/usr"


def glibc_floor(path):
    """The newest glibc symbol version `path` asks for, e.g. "2.38"."""
    out = subprocess.run(["readelf", "-V", str(path)], capture_output=True,
                         text=True, check=True).stdout
    found = {tuple(int(x) for x in v.split("."))
             for v in re.findall(r"GLIBC_(\d+(?:\.\d+)+)", out)}
    if not found:
        sys.exit(f"{path} asks for no glibc version; is it a static or "
                 "non-glibc binary?")
    return ".".join(str(x) for x in max(found))


def docs(pkg, readme, prefix="/usr"):
    """The README, and the license where each format expects it."""
    return [
        {"src": str(readme), "dst": f"{prefix}/share/doc/{pkg}/README.md"},
        {"src": str(TOP / "LICENSE"), "dst": f"{prefix}/share/doc/{pkg}/copyright",
         "packager": "deb"},
        {"src": str(TOP / "LICENSE"), "dst": f"/usr/share/licenses/{pkg}/LICENSE",
         "packager": "rpm"},
        {"src": str(TOP / "LICENSE"), "dst": f"/usr/share/licenses/{pkg}/LICENSE",
         "packager": "archlinux"},
    ]


def build_time():
    """When the packages say they were made: $SOURCE_DATE_EPOCH, or now."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.datetime.fromtimestamp(int(epoch), datetime.UTC)
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def common(name, arch, version, description):
    return {
        "name": name,
        "arch": arch,
        "platform": "linux",
        "version": version,
        "release": RELEASE,
        "section": "utils",
        "priority": "optional",
        "maintainer": MAINTAINER,
        "vendor": "cpcpub",
        "homepage": HOMEPAGE,
        "license": LICENSE,
        "description": description,
        "rpm": {"summary": description.split("\n", 1)[0]},
        "mtime": build_time().isoformat(),
    }


def core_config(arch, binaries, version):
    """The benchmark: {installed name: release asset} for one architecture."""
    floor = max((glibc_floor(p) for p in binaries.values()),
                key=lambda v: tuple(int(x) for x in v.split(".")))
    conf = common("cpcpub", arch, version, DESCRIPTION)
    conf["contents"] = [
        {"src": str(src), "dst": f"/usr/bin/{name}", "file_info": {"mode": 0o755}}
        for name, src in binaries.items()
    ] + docs("cpcpub", TOP / "bench" / "README.md")
    # curl only for --submit to an https hub, which hands the upload to it
    # (the binary has no TLS), so recommended rather than required: apt and
    # dnf install it by default and let a minimal system leave it out, and the
    # benchmark checks for it before measuring and says how to get it. Arch
    # needs nothing: nfpm writes no optdepends, and pacman itself depends on
    # curl, so every Arch system has it.
    conf["overrides"] = {
        "deb": {"depends": [f"libc6 (>= {floor})"], "recommends": ["curl"]},
        "rpm": {"depends": [f"glibc >= {floor}"], "recommends": ["curl"]},
        "archlinux": {"depends": [f"glibc>={floor}"]},
    }
    return conf, floor


def gui_config(version, metainfo):
    conf = common("cpcpub-gui", "all", version, GUI_DESCRIPTION)
    conf["contents"] = [
        {"src": str(TOP / "gui" / "cpcpub-gui.py"), "dst": "/usr/bin/cpcpub-gui",
         "file_info": {"mode": 0o755}},
        {"src": str(HERE / f"{APP_ID}.desktop"),
         "dst": f"/usr/share/applications/{APP_ID}.desktop"},
        {"src": str(metainfo), "dst": f"/usr/share/metainfo/{APP_ID}.metainfo.xml"},
        {"src": str(TOP / "packaging" / "icons" / "cpcpub.svg"),
         "dst": f"/usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg"},
    ] + docs("cpcpub-gui", TOP / "gui" / "README.md")
    # The window builds the benchmark's command line, so the two are only
    # right together: exactly the same version, not merely a newer one. GTK
    # 4.10 for Gtk.FileDialog.
    #
    # The two rpm families package GTK's introspection data differently, and
    # a missing typelib is only found out when the window starts. openSUSE
    # splits Gtk's into a typelib package that provides typelib(Gtk) and pulls
    # in the cairo typelib Gtk's refers to -- and its libgtk-4-1 provides
    # "gtk4", so asking for gtk4 there installs a GTK Python cannot see.
    # Fedora's gtk4 carries the typelib itself and has no typelib() provides,
    # but leaves cairo's typelib in gobject-introspection, which nothing else
    # pulls in. An "or" of the two is not enough: the solver may take either
    # branch, and zypper takes Fedora's. So: GTK and gobject-introspection
    # everywhere, and the typelib package wherever the GTK library is
    # openSUSE's libgtk-4-1.
    full = f"{version}-{RELEASE}"
    conf["overrides"] = {
        "deb": {"depends": [f"cpcpub (= {full})", "python3 (>= 3.9)", "python3-gi",
                            "gir1.2-gtk-4.0 (>= 4.10)"]},
        "rpm": {"depends": [f"cpcpub = {full}", "python3 >= 3.9", "python3-gobject",
                            "gtk4 >= 4.10", "gobject-introspection",
                            "(typelib(Gtk) = 4.0 if libgtk-4-1)"]},
        "archlinux": {"depends": [f"cpcpub={full}", "python>=3.9", "python-gobject",
                                  "gtk4>=4.10"]},
    }
    return conf


def termux_config(binary, version):
    """Termux keeps its own prefix and calls the architecture aarch64. The
    binary is the static Android one, so there is nothing to depend on but
    curl, recommended for an https --submit as on Linux."""
    conf = common("cpcpub", "arm64", version, DESCRIPTION)
    # nfpm would write Debian's arm64; Termux's dpkg refuses anything but its
    # own name for the architecture.
    conf["deb"] = {"arch": "aarch64"}
    conf["recommends"] = ["curl"]
    conf["contents"] = [
        {"src": str(binary), "dst": f"{TERMUX_PREFIX}/bin/cpcpub",
         "file_info": {"mode": 0o755}},
    ] + [d for d in docs("cpcpub", TOP / "bench" / "README.md", TERMUX_PREFIX)
         if d.get("packager") in (None, "deb")]
    return conf


def nfpm(conf, packager, target, workdir):
    # Modes stated rather than inherited from a checkout, whose umask may well
    # have made the documentation group-writable.
    for item in conf["contents"]:
        item.setdefault("file_info", {"mode": 0o644})
    path = pathlib.Path(workdir) / f"{conf['name']}-{conf['arch']}-{packager}.yaml"
    path.write_text(json.dumps(conf, indent=2, ensure_ascii=False))
    cmd = os.environ.get("NFPM", "nfpm").split()
    subprocess.run(cmd + ["pkg", "--packager", packager, "--config", str(path),
                          "--target", str(target)], check=True,
                   stdout=subprocess.DEVNULL)


def main():
    if len(sys.argv) != 4:
        sys.exit((__doc__ or "").split("\n\n")[1])
    rel, version, out = (pathlib.Path(sys.argv[1]), sys.argv[2],
                         pathlib.Path(sys.argv[3]))
    version = version.removeprefix("v")
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        # Every format has its own idea of what may follow the numbers; a
        # plain dotted version is the one all of them read the same way.
        sys.exit(f"version {version!r} is not plain numbers and dots")
    out.mkdir(parents=True, exist_ok=True)
    before = set(out.iterdir())

    with tempfile.TemporaryDirectory() as work:
        for suffix, arch, extras in ARCHES:
            plain = rel / f"cpcpub-linux-{suffix}"
            if not plain.is_file():
                sys.exit(f"{plain} is missing: a release without it is a "
                         "release one architecture short")
            binaries = {"cpcpub": plain}
            for extra, name in extras.items():
                binaries[name] = rel / f"cpcpub-linux-{extra}"
                if not binaries[name].is_file():
                    sys.exit(f"{binaries[name]} is missing")
            conf, floor = core_config(arch, binaries, version)
            formats = ["deb", "rpm"] + ([] if suffix in NO_ARCHLINUX else ["archlinux"])
            for packager in formats:
                nfpm(conf, packager, out, work)
            print(f"cpcpub {arch}: {', '.join(binaries)}; glibc >= {floor}; "
                  f"{', '.join(formats)}")

        # A dated release line, which software centres show as "updated".
        metainfo = pathlib.Path(work) / f"{APP_ID}.metainfo.xml"
        date = build_time().date().isoformat()
        metainfo.write_text((HERE / f"{APP_ID}.metainfo.xml").read_text()
                            .replace("@VERSION@", version).replace("@DATE@", date))
        for packager in ("deb", "rpm", "archlinux"):
            nfpm(gui_config(version, metainfo), packager, out, work)
        print("cpcpub-gui: deb, rpm, archlinux")

        android = rel / "cpcpub-android-arm64"
        if not android.is_file():
            sys.exit(f"{android} is missing")
        nfpm(termux_config(android, version), "deb",
             out / f"cpcpub-termux_{version}-{RELEASE}_aarch64.deb", work)
        print("cpcpub for Termux: deb")

    for path in sorted(set(out.iterdir()) - before):
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
