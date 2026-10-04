# cpcpub

`cpcpub` — a small, portable CPU benchmark in C, and a place to compare what it measures. Made by heavily human-directed Claude.

It runs a set of tiny kernels for a fixed wall-clock slice each and reports the rate achieved: integer and floating-point latency and throughput (and the instruction-level parallelism their ratio exposes), integer multiply, **memory bandwidth** and **random-access latency**, the memory-level parallelism behind it, and indirect-call throughput and branch-predictor capacity.

## Get it

Download a binary from [the latest release][rel] — each is self-contained and needs nothing installed:

| | Linux | macOS | Windows | Android |
| --- | --- | --- | --- | --- |
| x86-64 | `linux-x86_64`, `linux-x86_64-v3` | `macos-x86_64` | `windows-x86_64.exe`, `windows-x86_64-v3.exe` | |
| Arm64 | `linux-aarch64` | `macos-arm64` | `windows-arm64.exe` | `android-arm64` |
| RISC-V | `linux-riscv64`, `linux-riscv64-rva23` | | | |
| Other | `linux-loongarch64`, `linux-ppc64le`, `linux-s390x` | | | |

Every asset name is prefixed `cpcpub-`. Check what you downloaded against the
`SHA256SUMS` published beside it:

```sh
base=https://github.com/lukaszsobala/cross-platform-benchmark/releases/latest/download
curl -fLO $base/cpcpub-linux-x86_64
curl -fLs $base/SHA256SUMS | sha256sum -c --ignore-missing
chmod +x cpcpub-linux-x86_64
```

Or install a package from the same release. Each one carries those same binaries byte for byte, so its results verify just the same:

| | |
| --- | --- |
| Debian, Ubuntu | `cpcpub_*.deb`, and `cpcpub-gui_*_all.deb` for the window |
| Fedora, openSUSE | `cpcpub-*.rpm`, `cpcpub-gui-*.noarch.rpm` |
| Arch Linux | `cpcpub-*.pkg.tar.zst`, `cpcpub-gui-*-any.pkg.tar.zst` |
| Windows | `cpcpub-*-windows-setup.exe`, for x64 and Arm64 alike: the benchmark on PATH, the window in the Start menu |
| Android | `cpcpub-*-android-arm64.apk`, an app; or `cpcpub-termux_*.deb` for Termux |

[packaging/README.md](packaging/README.md) has the details, including why the Linux packages need glibc 2.38 or newer.

The `-v3` and `-rva23` builds need a newer ISA than the plain ones and will not start on older hardware; the unsuffixed build runs everywhere. On macOS a binary fetched by a browser is quarantined and needs `xattr -d com.apple.quarantine cpcpub-macos-arm64` before it will run — `curl` does not set that attribute.

Or build it. Needs only `libc`, `libm` and `pthreads`; C2x with GCC 13+ or Clang:

```sh
make                # -> bench/cpcpub
make native         # tuned for this machine, and comparable with nothing else
```

`make rva23`, `make loongarch`, `make sg2000` and `make MARCH=x86-64-v3` select
other targets; see [bench/README.md](bench/README.md).

## Run it

```sh
bench/cpcpub --full                 # one core, every core, and the machine at once
bench/cpcpub --full --variants=all  # run all variants for a full picture
bench/cpcpub --full -v              # every metric explained
bench/cpcpub --version              # which build this is, and what it is running on
bench/cpcpub --help
```

One binary carries four compilations of the kernels — auto-vectorization off/on rossed with FMA contraction off/on — and `--variants` runs each one the host ISA can tell apart, then compares them. The default is the scalar, unfused build that cross-ISA comparisons need.

On a desktop, [gui/](gui/) is the same flags in a window — the variants to
run, per-core or not, where to put the result, the hub fields, and a live
estimate of how long the settings will take. It comes with the `cpcpub-gui`
package on Linux and in the setup on Windows; on Android the app is
[android/](android/README.md). From the tree:

```sh
make gui            # or gui/cpcpub-gui.py
```

[bench/README.md](bench/README.md) covers building for each target, what every metric means, and what each platform can and cannot report. Read it before reading a result — several of the metrics say something other than what their name suggests. `--json` output is specified in
[schema/cpu-bench-1.md](schema/cpu-bench-1.md).

## Share it

[web/](web/) is an optional addition: a Python-stdlib service that collects results and puts them side by side. The benchmark uploads its own, straight after measuring:

```sh
bench/cpcpub --full --submit --token YOUR-TOKEN --label "my box"
```

A released binary uploads to the public hub at <https://cpcpub.qd.je:30210> when `--submit` is given no address of its own. The port is needed over IPv4; over IPv6 the bare `https://cpcpub.qd.je` works too. Send it elsewhere with `--submit http://my.server:8782`.

The token comes from the hub's Account tab and puts the run on your name. Without one the upload is anonymous, which every hub accepts.
`$CPCPUB_HUB` and `$CPCPUB_TOKEN` stand in for the two flags. A tree you built yourself has no default hub: pass the address, or bake one in with `make HUB_URL=https://cpcpub.qd.je:30210`.

To run one yourself:

```sh
make serve          # the hub on http://127.0.0.1:8080
make submit         # build, measure, upload -- one row on the board
```

A machine that cannot reach the hub itself can hand its `run.json` to the *Submit a result* tab instead — drag it on, or click, or paste.

An account is optional and adds three things: your name on the runs you upload, withdrawing them from any browser rather than only from the one holding a delete token, and the upload token above. Anonymous uploads are not second-class and rank the same. See [web/README.md](web/README.md).

## What a result means

A run carries the SHA-256 of the binary that produced it, so a run made with a published release build is labelled with that release.

[rel]: https://github.com/lukaszsobala/cross-platform-benchmark/releases/latest

## License

cpcpub is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE).
