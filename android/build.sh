#!/bin/sh
# Build the Android app around a release's Android binaries:
#
#   android/build.sh RELEASE_DIR VERSION OUT_DIR
#
# -> OUT_DIR/cpcpub-VERSION-android.apk, to install directly, and
#    OUT_DIR/cpcpub-VERSION-android.aab, the app bundle Google Play takes.
# Both carry the release's arm64 and armv7 binaries, one per ABI: a device gets
# the one it runs natively, so a 32-bit phone gets the armv7 build. Needs
# $ANDROID_HOME with platforms/android-37.0 and a build-tools, a JDK (javac,
# keytool, jarsigner), python3, and bundletool ($BUNDLETOOL, a jar).
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
# throwaway key made here, and says so. The bundle is signed with the upload
# key Play knows, $ANDROID_UPLOAD_KEYSTORE and its three companions the same
# way, where those are set, and otherwise with the APK's key.
set -eu
[ $# -eq 3 ] || { sed -n '2,4p' "$0" >&2; exit 2; }
rel=$1
version=${2#v}
out=$3
here=$(cd "$(dirname "$0")" && pwd)
min_sdk=29        # the binary's own floor: bionic gained getloadavg() at 29
target_sdk=37     # what Google Play asks of a new app
platform_name=android-37.0

sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
[ -n "$sdk" ] || { echo "set ANDROID_HOME to the Android SDK" >&2; exit 1; }
platform="$sdk/platforms/$platform_name/android.jar"
[ -f "$platform" ] || { echo "no $platform; install platforms;$platform_name" >&2; exit 1; }
bt=${BUILD_TOOLS:-$(ls -d "$sdk"/build-tools/* | sort -V | tail -1)}
echo "build-tools: $bt"
bundletool=${BUNDLETOOL:?set BUNDLETOOL to bundletool-all-VERSION.jar}
asset="$rel/cpcpub-android-arm64"
asset32="$rel/cpcpub-android-armv7"
for a in "$asset" "$asset32"; do
    [ -f "$a" ] || { echo "$a is missing" >&2; exit 1; }
done

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
# Linked twice from the same inputs: once as an APK's binary resources, once
# in the protocol-buffer form a bundle module holds.
link() {
    target=$1
    shift
    "$bt/aapt2" link -o "$target" -I "$platform" "$@" \
        --manifest "$here/AndroidManifest.xml" \
        --min-sdk-version "$min_sdk" --target-sdk-version "$target_sdk" \
        --version-code "$code" --version-name "$version" \
        "$work/res.zip" "$work"/values_build.arsc.flat
}
link "$work/base.apk" --java "$work/gen"
link "$work/proto.apk" --proto-format

javac --release 11 -encoding UTF-8 -Xlint:all,-options -Werror \
    -classpath "$platform" -d "$work/classes" \
    "$work"/gen/io/github/lukaszsobala/cpcpub/R.java "$here"/src/io/github/lukaszsobala/cpcpub/*.java
"$bt/d8" --release --min-api "$min_sdk" --lib "$platform" --output "$work/dex" \
    $(find "$work/classes" -name '*.class')

# The code, and the benchmark as the one native "library" of each ABI, into
# the APK and into the bundle's one module -- whose layout is the bundle's own:
# the manifest under manifest/, the code under dex/. Added with fixed
# timestamps, so the same inputs give the same files before signing.
python3 - "$work" "$asset" "$asset32" <<'PY'
import sys, zipfile
work, binary, binary32 = sys.argv[1:]
payload = [("classes.dex", f"{work}/dex/classes.dex"),
           ("lib/arm64-v8a/libcpcpub.so", binary),
           ("lib/armeabi-v7a/libcpcpub.so", binary32)]

def put(z, name, data):
    info = zipfile.ZipInfo(name, date_time=(1981, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)

with zipfile.ZipFile(f"{work}/base.apk", "a") as z:
    for name, src in payload:
        put(z, name, open(src, "rb").read())

with zipfile.ZipFile(f"{work}/proto.apk") as linked, \
     zipfile.ZipFile(f"{work}/module.zip", "w") as module:
    for item in sorted(linked.namelist()):
        data = linked.read(item)
        put(module, "manifest/AndroidManifest.xml" if item == "AndroidManifest.xml"
                    else item, data)
    for name, src in payload:
        put(module, "dex/" + name if name.endswith(".dex") else name,
            open(src, "rb").read())
PY
"$bt/zipalign" -P 16 -f 4 "$work/base.apk" "$work/aligned.apk"

# Native code left compressed in what Play hands out. Its default stores it
# uncompressed and tells the device not to extract it, to be run from inside
# the APK -- and the benchmark is not a library to be mapped but a program to
# be executed, which Android allows only once it is extracted to the app's
# native library directory.
cat > "$work/BundleConfig.json" <<'EOF'
{"optimizations": {"uncompressNativeLibraries": {"enabled": false}}}
EOF
java -jar "$bundletool" build-bundle --modules="$work/module.zip" \
    --config="$work/BundleConfig.json" --output="$work/unsigned.aab"

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
apk="$out/cpcpub-$version-android.apk"
"$bt/apksigner" sign --ks "$ks" --ks-key-alias "$alias" \
    --ks-pass env:ANDROID_KEYSTORE_PASSWORD --key-pass env:ANDROID_KEY_PASSWORD \
    --out "$apk" "$work/aligned.apk"
rm -f "$apk.idsig"
"$bt/apksigner" verify --print-certs "$apk" | grep -E "certificate (DN|SHA-256 digest):"

# The bundle: with the upload key where one is given, else the APK's key.
if [ -n "${ANDROID_UPLOAD_KEYSTORE:-}" ]; then
    uks=$ANDROID_UPLOAD_KEYSTORE
    ualias=${ANDROID_UPLOAD_KEY_ALIAS:?set ANDROID_UPLOAD_KEY_ALIAS}
    : "${ANDROID_UPLOAD_KEYSTORE_PASSWORD:?set ANDROID_UPLOAD_KEYSTORE_PASSWORD}"
    export ANDROID_UPLOAD_KEY_PASSWORD="${ANDROID_UPLOAD_KEY_PASSWORD:-$ANDROID_UPLOAD_KEYSTORE_PASSWORD}"
    echo "signing the bundle with upload key $ualias"
else
    uks=$ks ualias=$alias
    ANDROID_UPLOAD_KEYSTORE_PASSWORD=$ANDROID_KEYSTORE_PASSWORD
    ANDROID_UPLOAD_KEY_PASSWORD=$ANDROID_KEY_PASSWORD
    export ANDROID_UPLOAD_KEYSTORE_PASSWORD ANDROID_UPLOAD_KEY_PASSWORD
fi
aab="$out/cpcpub-$version-android.aab"
jarsigner -keystore "$uks" -storepass:env ANDROID_UPLOAD_KEYSTORE_PASSWORD \
    -keypass:env ANDROID_UPLOAD_KEY_PASSWORD -sigalg SHA256withRSA \
    -digestalg SHA-256 -signedjar "$aab" "$work/unsigned.aab" "$ualias" >/dev/null
jarsigner -verify "$aab" >/dev/null && echo "bundle signed by $ualias"

# What the APK carries has to be the release binaries, to the byte -- and so
# does what Play would hand a 64-bit and a 32-bit phone out of the bundle,
# compressed, for the device to extract.
# bundletool reads the passwords from files, not the command line, where any
# process on the machine could see them.
umask 077
printf '%s' "$ANDROID_KEYSTORE_PASSWORD" > "$work/ks-pass"
printf '%s' "$ANDROID_KEY_PASSWORD" > "$work/key-pass"
for spec in arm64-v8a armeabi-v7a; do
    printf '{"supportedAbis": ["%s"], "supportedLocales": ["en"], "screenDensity": 420, "sdkVersion": %s}\n' \
        "$spec" "$target_sdk" > "$work/device-$spec.json"
    java -jar "$bundletool" build-apks --bundle="$aab" --output="$work/$spec.apks" \
        --device-spec="$work/device-$spec.json" --ks="$ks" --ks-key-alias="$alias" \
        --ks-pass=file:"$work/ks-pass" --key-pass=file:"$work/key-pass"
done
python3 - "$apk" "$work" "$asset" "$asset32" <<'PY'
import hashlib, io, sys, zipfile
apk, work, asset, asset32 = sys.argv[1:]
digest = lambda data: hashlib.sha256(data).hexdigest()
for abi, src in (("arm64-v8a", asset), ("armeabi-v7a", asset32)):
    want = digest(open(src, "rb").read())
    name = f"lib/{abi}/libcpcpub.so"
    have = digest(zipfile.ZipFile(apk).read(name))
    assert have == want, f"the APK's {abi} binary is {have}, the release's is {want}"
    # Play's split APKs for a device of that ABI, as bundletool makes them.
    found = []
    with zipfile.ZipFile(f"{work}/{abi}.apks") as apks:
        for split in apks.namelist():
            if not split.endswith(".apk"):
                continue
            with zipfile.ZipFile(io.BytesIO(apks.read(split))) as z:
                if name in z.namelist():
                    info = z.getinfo(name)
                    assert info.compress_type == zipfile.ZIP_DEFLATED, \
                        f"{split} stores {name} uncompressed, so it is never extracted"
                    found.append(digest(z.read(name)))
    assert found == [want], f"the bundle gives a {abi} device {found}, not {want}"
    print(f"{name} = {src.rsplit('/', 1)[-1]} ({want}), in the APK and the bundle")
PY
ls -l "$apk" "$aab"
