# cpcpub for Android

An app for the release's static Android binary. Like the desktop window, it
measures nothing itself. It builds the command line, runs the benchmark, and
reads back what it prints. You choose:

- the run: multi-threaded, per-core, or Both, which ticks the two;
- the variants: the baseline, or all four compared;
- a cool-down between runs, 30 s unless you pick otherwise;
- the hub fields;
- the timing settings.

The result appears as a table. Tap a heading for what that column means; the
score and the columns it is a geometric mean of are in bold. Every result is
also kept in `Android/data/je.qd.cpcpub/files/results/`, and can be saved
elsewhere or shared as JSON.

It needs Android 10 (API 29) or newer on a 64-bit Arm device, which is nearly
every phone sold since 2019.

## How the benchmark gets in, unchanged

The release's `cpcpub-android-arm64` rides in the APK as
`lib/arm64-v8a/libcpcpub.so`, with `extractNativeLibs` on.

- **Why disguise it as a library:** since Android 10 an app may not execute
  files it wrote itself. It may execute what the package manager extracted
  into its native library directory, and the package manager copies the file
  as it is.
- **Why it stays verifiable:** the benchmark reports the digest of the
  release asset, and a hub verifies results by that digest. `build.sh`
  checks the APK's copy against the release before it finishes.
- **Why no Gradle:** the Android Gradle plugin strips every native library by
  default, which would change the digest. `build.sh` uses the SDK's own
  tools instead (aapt2, javac, d8, zipalign, apksigner). The app is four Java
  files with no libraries, so those tools are all it needs.

## Uploading

The app uploads a result itself instead of passing `--submit` to the
benchmark. The benchmark has no TLS of its own: it hands an https URL to
`curl`, and Android has no `curl`. [Hub.java](src/je/qd/cpcpub/Hub.java)
makes the same request `submit_document()` in
[bench.c](../bench/src/bench.c) makes, and in the same way:

- the same trimming of the label and notes;
- the variant appended to the label when all four run;
- the same percent-encoding;
- redirects are not followed;
- each document is uploaded exactly as the benchmark printed it.

After an upload, "See how it compares" opens the run's page on the hub. The
hub's reply, delete token included, goes to the log, which opens by itself
only when an upload fails. The default hub is whichever one the release baked
into the binary: `build.sh` reads it out of the binary's help text.

## Measuring on a phone

Keep the app on screen while it runs, which it helps with by keeping the
screen on. If the app leaves the screen, Android moves it to the slower cores
and may pause it. The log says so if that happened during a run.

A phone slows down as it heats, and half a minute of every core at once heats
it. Without a rest, the per-core sweep after the multi-threaded run, and each
variant after the first, would be measured on a throttled phone. So the app
passes `--cooldown`, which rests before each multi-threaded run and per-core
sweep after the first. It offers Off, 30 s, 1 min and 2 min, starts on 30 s,
and greys the choice out when there is only one run to make. While the
benchmark rests, the status line counts the rest down. A binary from before
`--cooldown` would refuse the flag, so the app reads the binary's help once and
hides the choice if the flag is not there.

## Building

```sh
tag=$(gh release view --repo lukaszsobala/cpcpub --json tagName -q .tagName)
gh release download "$tag" --repo lukaszsobala/cpcpub --pattern 'cpcpub-android-*' --dir rel
ANDROID_HOME=~/Android/Sdk android/build.sh rel "$tag" out
```

It needs `platforms;android-35`, a build-tools, a JDK and python3. Without a
keystore in the environment the APK is signed with a throwaway key; see
[packaging/README.md](../packaging/README.md#the-android-signing-key).

## What has been tested where

- **The emulator (API 35, x86-64) tested the app itself:**
  - the layout under Android 15's edge-to-edge drawing;
  - the extracted binary being byte for byte the release's;
  - a run, with a stand-in for the binary that prints a real result
    document;
  - the table, the variant pages and the explanations;
  - the local copy of each result;
  - an upload of a two-variant result to a real hub.
- **The real binary has not run on the emulator.** Its translation from Arm
  to x86 cannot run a static Arm binary, not even `--version`.
- **Termux's arm64 image ran the real binary natively** (the `linux-test`
  CI job). That is the same file the APK carries.
- **A phone ran the APK from CI** with the real binary, and it ran fine.
