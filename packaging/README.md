# Packaging

Installable packages of cpcpub, made from a release's binaries by
[packages.yml](../.github/workflows/packages.yml) and attached to the release
beside them:

| | package | holds |
| --- | --- | --- |
| Debian, Ubuntu | `cpcpub_VERSION-1_ARCH.deb` | `/usr/bin/cpcpub`, and `cpcpub-v3` (x86-64) or `cpcpub-rva23` (RISC-V) |
| | `cpcpub-gui_VERSION-1_all.deb` | the window, with a menu entry |
| Fedora, openSUSE | `cpcpub-VERSION-1.ARCH.rpm`, `cpcpub-gui-VERSION-1.noarch.rpm` | the same |
| Arch Linux | `cpcpub-VERSION-1-ARCH.pkg.tar.zst`, `cpcpub-gui-VERSION-1-any.pkg.tar.zst` | the same |
| Termux | `cpcpub-termux_VERSION-1_aarch64.deb` | the static Android binary, under Termux's prefix |
| Windows | `cpcpub-VERSION-windows-x64.msi`, `-arm64.msi` | `cpcpub.exe` on PATH (and `cpcpub-v3.exe` on x64), the window in the Start menu |

The Linux packages exist for every architecture the release has a Linux
binary for: x86-64, AArch64, RISC-V, LoongArch, ppc64le and s390x (no Arch
package for s390x, which Arch has no port for).

```sh
sudo apt install ./cpcpub_*_amd64.deb ./cpcpub-gui_*_all.deb
sudo dnf install ./cpcpub-*.x86_64.rpm ./cpcpub-gui-*.noarch.rpm
sudo pacman -U cpcpub-*-x86_64.pkg.tar.zst cpcpub-gui-*-any.pkg.tar.zst
apt install ./cpcpub-termux_*_aarch64.deb          # inside Termux
```

## The one rule: the binaries go in untouched

A hub marks a result *verified* when the digest of the binary that measured it
matches one in the release's `verified-builds.json`. So every package carries
the release binaries byte for byte. A package that rebuilt the benchmark, or
only stripped it, would install a program whose every result comes out
unverified.

That decides the tools:

- **nfpm for Linux.** dpkg-buildpackage and rpmbuild strip binaries and split
  out debug info by default. nfpm puts files into an archive and does nothing
  else to them.
- **wixl for Windows.** It builds the MSI on Linux, from the same staged
  files.
- **Nothing is signed.** Signing a Windows binary rewrites it.

Every package is then installed and checked. The installed file's digest has
to match the release asset's, and so does the digest the benchmark reports
for itself.

## What is checked before a release ships

`packages.yml` installs every package on the system it is for, and the release
is published only if all of them pass:

- **Linux:** [linux/test.sh](linux/test.sh) runs Debian 13, Ubuntu 24.04,
  Fedora, openSUSE Tumbleweed and Arch in containers, on both x86-64 and arm64
  runners. In each it:
  - installs both packages;
  - checks the digests;
  - runs the benchmark;
  - validates the desktop entry and AppStream data;
  - starts the window under Xvfb, which finds the benchmark, runs it and saves
    the result (see `CPCPUB_GUI_SMOKE` in the window's source);
  - uninstalls.

  Debian 12 must *refuse* the package (see below). The Termux package is
  installed in Termux's own image.
- **Windows:** [windows/test-msi.ps1](windows/test-msi.ps1) installs each MSI
  on an x64 and an Arm64 runner, and checks:
  - the digests;
  - PATH, the Start-menu shortcut and the entry in Apps;
  - that the benchmark runs;
  - the window's own run;
  - a clean uninstall;
  - that the other machine's MSI refuses to install.

## Building them yourself

From a directory holding a release's assets:

```sh
gh release download v0.3.6 --repo lukaszsobala/cpcpub --dir rel
python3 packaging/linux/build.py rel v0.3.6 out    # needs nfpm and readelf
packaging/linux/test.sh out rel                    # needs docker
```

The Windows window has to be frozen on Windows. Run
[windows/build-gui.sh](windows/build-gui.sh) in an MSYS2 UCRT64 shell, which
leaves `dist/cpcpub-gui`. The MSIs can then be built anywhere msitools 0.106
or newer runs:

```sh
python3 packaging/windows/build-msi.py rel dist/cpcpub-gui v0.3.6 out
```

`SOURCE_DATE_EPOCH` dates everything inside the Linux packages, so the same
commit packages to the same bytes. The workflow sets it to the commit's time.

## Choices worth knowing about

- **Two x86-64 builds, one package.** `/usr/bin/cpcpub` (and `cpcpub.exe` on
  Windows) is always the baseline build: it runs everywhere, and it is what
  results compare against. The `-v3` build goes beside it, and the RISC-V
  package does the same with `-rva23`. The window's Benchmark field mentions
  the other build in its tooltip, and Browse picks it. Choosing one quietly by
  CPU would hide which build produced a result.
- **The glibc floor is read from the binaries.** The binaries currently need
  glibc 2.38: with `-std=c2x`, glibc maps `strtol` and friends to
  `__isoc23_*` symbols. So the packages depend on `libc6 (>= 2.38)`, and
  Debian 12, Ubuntu 22.04 and RHEL 9 refuse them. This floor also rules out
  Alpine: gcompat has no `__isoc23_*` either. Building the benchmark so that
  it avoids those symbols would bring all three back.
- **GUI dependencies differ between the rpm distributions.** The rpm names
  GTK's introspection data so that both Fedora (`gtk4` plus
  `gobject-introspection`, for cairo's typelib) and openSUSE (the separate
  `typelib(Gtk)` package) end up with a window that starts.
  [linux/build.py](linux/build.py) says why a plain "or" was not enough.
- **Both MSIs are x64 packages.** wixl cannot write an Arm64 one, and
  Windows 11 on Arm installs x64 packages. The arm64 MSI carries the native
  arm64 benchmark, so the measuring is native. The window is the same x64
  build in both, and on Arm it runs under emulation; all it does is start the
  benchmark and read what it prints. A launch condition reads the machine's
  real architecture from the registry and refuses the wrong installer.
- **The MSI has no wizard.** wixl has no installer UI. Opening the MSI asks
  for elevation and shows a progress bar, and that is all. The Publisher reads
  "Lukasz Sobala" without the diacritic, because wixl drops non-ASCII
  properties.
- **Unsigned.** Windows SmartScreen warns about an unsigned MSI it has not
  seen before. Signing would need a certificate, and must apply to the MSI,
  never to the `cpcpub.exe` inside it.
- **Not packaged:** Flatpak and Snap, whose sandboxes get in the way of the
  `/sys` reads and CPU pinning the benchmark depends on; and macOS.
