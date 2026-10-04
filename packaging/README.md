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
| Windows | `cpcpub-VERSION-windows-setup.exe`, for x64 and Arm64 | `cpcpub.exe` on PATH (and `cpcpub-v3.exe` on x64), the window in the Start menu |
| Android | `cpcpub-VERSION-android-arm64.apk` | an app around the static Android binary; see [android/](../android/README.md) |

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
- **Inno Setup for Windows.** It copies files into the setup program and
  back out of it unchanged.
- **The SDK's own tools for Android, not Gradle.** The Gradle plugin strips
  native libraries.
- **Nothing inside is signed.** Signing a Windows binary rewrites it. The APK
  is signed as a whole, which leaves the binary inside it alone; signing the
  Windows setup would leave the files inside it alone in the same way.

Every package is then installed and checked. The installed file's digest has
to match the release asset's, and so does the digest the benchmark reports
for itself.

## What is checked before a release ships

`packages.yml` installs every package on the system it is for, and the release
is published only if all of them pass:

- **Linux:** [linux/test.sh](linux/test.sh) runs Debian 13, Ubuntu 24.04
  and Fedora in containers on both x86-64 and arm64 runners, and openSUSE
  Tumbleweed and Arch on x86-64 only (test.sh says why). In each it:
  - installs both packages;
  - checks the digests;
  - runs the benchmark;
  - validates the desktop entry and AppStream data;
  - starts the window under Xvfb, which finds the benchmark, runs it and saves
    the result (see `CPCPUB_GUI_SMOKE` in the window's source);
  - uninstalls.

  Debian 12 must *refuse* the package (see below). The Termux package is
  installed in Termux's own image, natively on the arm64 runner.
- **Windows:** [windows/test-installer.ps1](windows/test-installer.ps1)
  installs the setup silently on an x64 and an Arm64 runner, and checks:
  - that each got its own machine's benchmark, by digest, and its own
    machine's window;
  - PATH, the Start-menu shortcut and the entry in Apps;
  - that the benchmark runs;
  - the window's own run;
  - that installing again over it leaves one install and one PATH entry;
  - a clean uninstall.
- **Android:** the APK is checked rather than installed. There is no
  emulator on the arm64 runners, and an x86 emulator's Arm translation cannot
  run a static binary. The checks are its signature, its manifest and the
  embedded binary's digest. The same binary runs in Termux's image above.
  [android/README.md](../android/README.md) says what was tried on an
  emulator by hand.

## The Android signing key

Android installs an update only if it is signed with the same key as the
version installed. The release workflow therefore signs with a key kept in four
repository secrets. Until they exist, every build gets a throwaway key, and the
run carries a warning that says so. Make the key once and keep a copy
somewhere safe: losing it means users must uninstall to update.

```sh
keytool -genkeypair -keystore cpcpub-release.p12 -storetype PKCS12 \
    -alias cpcpub -keyalg RSA -keysize 4096 -validity 36500 \
    -dname "CN=Łukasz Sobala"
base64 -w0 cpcpub-release.p12 | gh secret set ANDROID_KEYSTORE
gh secret set ANDROID_KEYSTORE_PASSWORD     # prompts; the one keytool asked for
gh secret set ANDROID_KEY_ALIAS --body cpcpub
gh secret set ANDROID_KEY_PASSWORD          # the same password, for a PKCS12 store
```

## Building them yourself

From a directory holding a release's assets:

```sh
gh release download v0.3.6 --repo lukaszsobala/cpcpub --dir rel
python3 packaging/linux/build.py rel v0.3.6 out    # needs nfpm and readelf
packaging/linux/test.sh out rel                    # needs docker
```

The Windows setup is built on Windows. Freeze the window on each machine
with [windows/build-gui.sh](windows/build-gui.sh), in an MSYS2 UCRT64 shell on
x64 and a CLANGARM64 one on Arm64; each leaves `dist/cpcpub-gui`. Then, with
Inno Setup 6.3 or newer installed:

```powershell
pwsh packaging/windows/build-installer.ps1 -Release rel `
    -GuiX64 gui-x64 -GuiArm64 gui-arm64 -Version v0.3.6 -Out out
```

The APK: `android/build.sh rel v0.3.6 out`, with `ANDROID_HOME` set; see
[android/README.md](../android/README.md#building).

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
- **curl is recommended, not required.** The benchmark has no TLS of its
  own and hands an upload to an https hub to curl. apt and dnf install
  recommended packages by default, and a minimal system can leave curl out
  and still measure. Uploading without curl stops before the run and says
  how to install it, in the window and on the command line. Arch needs
  nothing, since pacman itself depends on curl.
- **GUI dependencies differ between the rpm distributions.** The rpm names
  GTK's introspection data so that both Fedora (`gtk4` plus
  `gobject-introspection`, for cairo's typelib) and openSUSE (the separate
  `typelib(Gtk)` package) end up with a window that starts.
  [linux/build.py](linux/build.py) says why a plain "or" was not enough.
- **One Windows setup for both machines.** It carries the x64 and the Arm64
  builds of the benchmark and of the window, and installs the ones native to
  the machine. On Arm, Windows would also run the x64 benchmark, under
  emulation, and report the emulator's speed as the processor's. The setup
  is about 50 MB, twice what one machine's would be, which seemed a fair price
  for nobody having to know which one to download.
- **A setup program, not an MSI.** An MSI is for one machine type, so it
  would take two downloads. The only MSI builder that runs on Linux, wixl,
  cannot write an Arm64 one at all. An MSI matters to managed deployment
  (Intune, Group Policy), which is not how a benchmark gets installed, and
  winget takes Inno Setup installers as they are. Silent installs work:
  `/VERYSILENT /SUPPRESSMSGBOXES`.
- **Unsigned.** Windows SmartScreen warns about an unsigned setup it has not
  seen before. Signing would need a certificate, and must apply to the setup,
  never to the `cpcpub.exe` inside it.
- **Not packaged:** Flatpak and Snap, whose sandboxes get in the way of the
  `/sys` reads and CPU pinning the benchmark depends on; and macOS.
