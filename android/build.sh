#!/bin/sh
# Build the Android app around a release's Android binary:
#
#   android/build.sh RELEASE_DIR VERSION OUT_DIR
#
# -> OUT_DIR/cpcpub-VERSION-android-arm64.apk. Needs $ANDROID_HOME with
# platforms/android-35 and a build-tools, a JDK (javac, keytool) and python3.
#
# The SDK's own tools and no Gradle: the app is five Java files and no
# libraries, and the steps below are the whole of what Gradle would do for it
# -- except strip the binary, which the Android Gradle plugin does to every
# native library by default and which would make every result the app
# produces unverified.
#
# Signing: with $ANDROID_KEYSTORE (a keystore file), $ANDROID_KEYSTORE_PASSWORD,
# $ANDROID_KEY_ALIAS and $ANDROID_KEY_PASSWORD set, the APK is signed with that
# key -- which must stay the same from release to release, or Android will not
# install an update over the previous one. Without them it is signed with a
# throwaway key made here, and says so.
set -eu
[ $# -eq 3 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
rel=$1
version=${2#v}
out=$3
here=$(cd "$(dirname "$0")" && pwd)
min_sdk=29        # the binary's own floor: bionic gained getloadavg() at 29
target_sdk=35

sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
[ -n "$sdk" ] || { echo "set ANDROID_HOME to the Android SDK" >&2; exit 1; }
platform="$sdk/platforms/android-$target_sdk/android.jar"
[ -f "$platform" ] || { echo "no $platform; install platforms;android-$target_sdk" >&2; exit 1; }
bt=${BUILD_TOOLS:-$(ls -d "$sdk"/build-tools/* | sort -V | tail -1)}
echo "build-tools: $bt"
asset="$rel/cpcpub-android-arm64"
[ -f "$asset" ] || { echo "$asset is missing" >&2; exit 1; }

# 0.3.6 -> 3006: each part gets three digits, so every release counts higher
# than the one before, which is all Android asks of a version code.
code=$(echo "$version" | awk -F. '{ printf "%d", $1 * 1000000 + $2 * 1000 + $3 }')
[ "$code" -gt 0 ] || { echo "version $version gives no version code" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/gen/values" "$work/classes" "$work/dex" "$out"

# The hub the app uploads to unless told otherwise: whichever one the release
# baked into the binary it carries, read out of the binary's own help text, so
# the app and the command line agree. A build with none leaves it blank.
hub=$(python3 - "$asset" <<'PY'
import re, sys
found = re.search(rb" \(default: (https?://[^)\x00\s]+)\)", open(sys.argv[1], "rb").read())
print(found.group(1).decode() if found else "")
PY
)
echo "default hub: ${hub:-(none)}"
cat > "$work/gen/values/build.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="hub_url" translatable="false">$hub</string>
</resources>
EOF

"$bt/aapt2" compile --dir "$here/res" -o "$work/res.zip"
"$bt/aapt2" compile "$work/gen/values/build.xml" -o "$work"
"$bt/aapt2" link -o "$work/base.apk" -I "$platform" \
    --manifest "$here/AndroidManifest.xml" \
    --min-sdk-version "$min_sdk" --target-sdk-version "$target_sdk" \
    --version-code "$code" --version-name "$version" \
    --java "$work/gen" "$work/res.zip" "$work"/values_build.arsc.flat

javac --release 11 -encoding UTF-8 -Xlint:all,-options -Werror \
    -classpath "$platform" -d "$work/classes" \
    "$work"/gen/je/qd/cpcpub/R.java "$here"/src/je/qd/cpcpub/*.java
"$bt/d8" --release --min-api "$min_sdk" --lib "$platform" --output "$work/dex" \
    $(find "$work/classes" -name '*.class')

# The code, and the benchmark as the one native "library". Added with fixed
# timestamps, so the same inputs give the same APK before signing.
python3 - "$work/base.apk" "$work/dex/classes.dex" "$asset" <<'PY'
import sys, zipfile
apk, dex, binary = sys.argv[1:]
with zipfile.ZipFile(apk, "a") as z:
    for name, src in (("classes.dex", dex), ("lib/arm64-v8a/libcpcpub.so", binary)):
        info = zipfile.ZipInfo(name, date_time=(1981, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, open(src, "rb").read())
PY
"$bt/zipalign" -P 16 -f 4 "$work/base.apk" "$work/aligned.apk"

if [ -n "${ANDROID_KEYSTORE:-}" ]; then
    ks=$ANDROID_KEYSTORE
    alias=${ANDROID_KEY_ALIAS:?set ANDROID_KEY_ALIAS}
    : "${ANDROID_KEYSTORE_PASSWORD:?set ANDROID_KEYSTORE_PASSWORD}"
    export ANDROID_KEY_PASSWORD="${ANDROID_KEY_PASSWORD:-$ANDROID_KEYSTORE_PASSWORD}"
    echo "signing with $alias from the given keystore"
else
    ks="$work/throwaway.p12"
    alias=throwaway
    ANDROID_KEYSTORE_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
    ANDROID_KEY_PASSWORD=$ANDROID_KEYSTORE_PASSWORD
    export ANDROID_KEYSTORE_PASSWORD ANDROID_KEY_PASSWORD
    keytool -genkeypair -keystore "$ks" -storetype PKCS12 -alias "$alias" \
        -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=cpcpub throwaway key" \
        -storepass:env ANDROID_KEYSTORE_PASSWORD -keypass:env ANDROID_KEY_PASSWORD \
        >/dev/null 2>&1
    echo "WARNING: no ANDROID_KEYSTORE; signed with a throwaway key, so this APK" \
         "will not install over one signed with any other key"
fi
apk="$out/cpcpub-$version-android-arm64.apk"
"$bt/apksigner" sign --ks "$ks" --ks-key-alias "$alias" \
    --ks-pass env:ANDROID_KEYSTORE_PASSWORD --key-pass env:ANDROID_KEY_PASSWORD \
    --out "$apk" "$work/aligned.apk"
rm -f "$apk.idsig"
"$bt/apksigner" verify --print-certs "$apk" | grep -E "Signer #1 certificate (DN|SHA-256)"

# What the APK carries has to be the release binary, to the byte.
python3 - "$apk" "$asset" <<'PY'
import hashlib, sys, zipfile
apk, asset = sys.argv[1:]
inside = zipfile.ZipFile(apk).read("lib/arm64-v8a/libcpcpub.so")
want = hashlib.sha256(open(asset, "rb").read()).hexdigest()
have = hashlib.sha256(inside).hexdigest()
assert have == want, f"the APK's binary is {have}, the release's is {want}"
print(f"lib/arm64-v8a/libcpcpub.so = {asset.rsplit('/', 1)[-1]} ({have})")
PY
ls -l "$apk"
