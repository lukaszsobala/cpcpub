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
# registered with binfmt_misc, and is skipped, saying so, without it. The armhf
# packages are tested the same way, in Debian's armhf image, on any machine
# where qemu-user is registered for 32-bit Arm.
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

# 32-bit Arm, which no runner is: Debian's armhf image under qemu-user, where
# binfmt_misc has it registered, and skipped, saying so, where not.
echo "######## debian:trixie (armhf)"
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]; then
    echo "######## debian:trixie (armhf): skipped, nothing here runs an armhf image"
elif docker run --rm --platform linux/arm/v7 -v "$pkgs:/pkgs:ro" -v "$rel:/rel:ro" \
        -v "$here/test-inside.sh:/test.sh:ro" debian:trixie sh /test.sh; then
    echo "######## debian:trixie (armhf): ok"
else
    echo "######## debian:trixie (armhf): FAILED"
    failed="$failed debian:trixie-armhf"
fi

# Termux, in its own image, for each architecture it has a package for. Its
# dpkg runs as Termux's own user, who cannot read a file mounted from outside,
# so the package is copied into its temporary directory first.
termux() {  # termux TERMUX_ARCH DOCKER_PLATFORM QEMU_NAME ASSET
    echo "######## termux ($1)"
    if [ "$(uname -m)" != "$3" ] && [ ! -e "/proc/sys/fs/binfmt_misc/qemu-$3" ]; then
        echo "######## termux ($1): skipped, nothing here runs a $3 image"
        return
    fi
    # The two names are written into the script rather than passed with -e:
    # the image's entrypoint starts the command afresh, without them.
    if docker run --rm --platform "$2" -v "$pkgs:/pkgs:ro" -v "$rel:/rel:ro" \
            "termux/termux-docker:$1" bash -c '
            set -eu
            ARCH='"$1"' ASSET='"$4"'
            cp /pkgs/cpcpub-termux_*_"$ARCH".deb "$TMPDIR/cpcpub.deb"
            dpkg -i "$TMPDIR/cpcpub.deb"
            bin="$PREFIX/bin/cpcpub"
            have=$(sha256sum "$bin" | cut -d" " -f1)
            want=$(sha256sum "/rel/$ASSET" | cut -d" " -f1)
            [ "$have" = "$want" ] || { echo "$bin is $have, the release is $want"; exit 1; }
            echo "$bin = $ASSET ($have)"
            cpcpub --threads 1 --cpus 0 --time 0.05 --reps 1 --warmup 0.02 --json \
                > "$TMPDIR/run.json"
            grep -q "\"binary_sha256\": \"$have\"" "$TMPDIR/run.json"
            echo "binary_sha256 matches the installed file"
            dpkg -r cpcpub
            [ ! -e "$bin" ]
            echo "removed cleanly"'; then
        echo "######## termux ($1): ok"
    else
        echo "######## termux ($1): FAILED"
        failed="$failed termux-$1"
    fi
}
# Not the 32-bit package: its image hangs in its own start-up under qemu,
# and the binary in it is the APK's armv7 one, which the release runs.
termux aarch64 linux/arm64 aarch64 cpcpub-android-arm64

[ -z "$failed" ] || { echo "failed:$failed" >&2; exit 1; }
echo "all distributions passed"
