#!/bin/sh
# Install the packages for this machine's architecture in a container per
# distribution and check them, then the Termux package in Termux's own image:
#
#   packaging/linux/test.sh PKG_DIR RELEASE_DIR [IMAGE...]
#
# See test-inside.sh for what is checked. Debian 12 is run too, the other way
# round: its glibc is older than the binaries need, and the package has to say
# so at install time rather than install something that will not start.
# Termux is an aarch64 image; on any other machine it needs qemu-user
# registered with binfmt_misc, and is skipped, saying so, without it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
pkgs=$(cd "$1" && pwd)
rel=$(cd "$2" && pwd)
shift 2
case $(uname -m) in
    x86_64)  deb=amd64
             default="debian:trixie ubuntu:24.04 fedora:latest opensuse/tumbleweed archlinux:latest" ;;
    # Arch Linux publishes no arm64 image; Arch Linux ARM is a separate project.
    # Nor openSUSE here: its aarch64 Tumbleweed is a port with mirrors of its
    # own, which served the same corrupt package three runs in a row. The
    # x86-64 run covers openSUSE's dependencies and Fedora covers aarch64 rpm.
    aarch64) deb=arm64
             default="debian:trixie ubuntu:24.04 fedora:latest" ;;
    *) echo "no test written for $(uname -m)" >&2; exit 1 ;;
esac
images=${*:-$default}

failed=""
for image in $images; do
    echo "######## $image"
    if docker run --rm -v "$pkgs:/pkgs:ro" -v "$rel:/rel:ro" \
            -v "$here/test-inside.sh:/test.sh:ro" "$image" sh /test.sh; then
        echo "######## $image: ok"
    else
        echo "######## $image: FAILED"
        failed="$failed $image"
    fi
done

echo "######## debian:bookworm (must refuse)"
log=$(mktemp)
if docker run --rm -v "$pkgs:/pkgs:ro" debian:bookworm sh -c \
        "apt-get update -qq && apt-get install -y /pkgs/cpcpub_*_$deb.deb" \
        > "$log" 2>&1; then
    echo "######## debian:bookworm installed a binary its glibc cannot run: FAILED"
    failed="$failed debian:bookworm"
elif grep -q "Depends: libc6 (>= " "$log"; then
    grep "Depends: libc6" "$log"
    echo "######## debian:bookworm: refused over glibc, as it should"
else
    cat "$log"
    echo "######## debian:bookworm: failed, but not over glibc: FAILED"
    failed="$failed debian:bookworm"
fi
rm -f "$log"

echo "######## termux"
if [ "$(uname -m)" != aarch64 ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    echo "######## termux: skipped, nothing here runs an aarch64 image"
# Termux's dpkg runs as Termux's own user, who cannot read a file mounted from
# outside; the package is copied into its temporary directory first.
elif docker run --rm --platform linux/arm64 -v "$pkgs:/pkgs:ro" -v "$rel:/rel:ro" \
        termux/termux-docker:aarch64 bash -c '
        set -eu
        cp /pkgs/cpcpub-termux_*_aarch64.deb "$TMPDIR/cpcpub.deb"
        dpkg -i "$TMPDIR/cpcpub.deb"
        bin="$PREFIX/bin/cpcpub"
        have=$(sha256sum "$bin" | cut -d" " -f1)
        want=$(sha256sum /rel/cpcpub-android-arm64 | cut -d" " -f1)
        [ "$have" = "$want" ] || { echo "$bin is $have, the release is $want"; exit 1; }
        echo "$bin = cpcpub-android-arm64 ($have)"
        cpcpub --threads 1 --cpus 0 --time 0.05 --reps 1 --warmup 0.02 --json \
            > "$TMPDIR/run.json"
        grep -q "\"binary_sha256\": \"$have\"" "$TMPDIR/run.json"
        echo "binary_sha256 matches the installed file"
        dpkg -r cpcpub
        [ ! -e "$bin" ]
        echo "removed cleanly"'; then
    echo "######## termux: ok"
else
    echo "######## termux: FAILED"
    failed="$failed termux"
fi

[ -z "$failed" ] || { echo "failed:$failed" >&2; exit 1; }
echo "all distributions passed"
