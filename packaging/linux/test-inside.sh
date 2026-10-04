#!/bin/sh
# Run inside a distribution's container by test.sh, with the packages at /pkgs
# and the release binaries they were built from at /rel. Installs cpcpub and
# cpcpub-gui the way a user would, then checks that what landed is the release
# itself and that the window can find it, run it and read the result back.
set -eu

# Each format's name for this machine, and what the cpcpub package for it
# should have installed: /usr/bin/NAME from release asset FILE, as NAME=FILE.
case $(uname -m) in
    x86_64)  deb=amd64 rpm=x86_64 pac=x86_64
             builds="cpcpub=cpcpub-linux-x86_64 cpcpub-v3=cpcpub-linux-x86_64-v3" ;;
    aarch64) deb=arm64 rpm=aarch64 pac=aarch64
             builds="cpcpub=cpcpub-linux-aarch64" ;;
    *) echo "no test written for $(uname -m)" >&2; exit 1 ;;
esac

say() { printf '\n== %s\n' "$*"; }

say "install"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq /pkgs/cpcpub_*_$deb.deb /pkgs/cpcpub-gui_*_all.deb \
        xvfb desktop-file-utils appstream >/dev/null
    remove="apt-get remove -y -qq cpcpub cpcpub-gui"
elif command -v dnf >/dev/null; then
    dnf install -y -q /pkgs/cpcpub-*.$rpm.rpm /pkgs/cpcpub-gui-*.noarch.rpm \
        xorg-x11-server-Xvfb
    remove="dnf remove -y -q cpcpub cpcpub-gui"
elif command -v zypper >/dev/null; then
    # A font as well: this image has none, and Pango crashes outright with
    # nothing to lay text out in. A desktop install never lacks one.
    zypper --non-interactive -q install --allow-unsigned-rpm \
        /pkgs/cpcpub-*.$rpm.rpm /pkgs/cpcpub-gui-*.noarch.rpm \
        xorg-x11-server-Xvfb dejavu-fonts
    remove="zypper --non-interactive -q remove cpcpub cpcpub-gui"
elif command -v pacman >/dev/null; then
    pacman -Syu --noconfirm --needed xorg-server-xvfb >/dev/null
    pacman -U --noconfirm /pkgs/cpcpub-*-$pac.pkg.tar.zst \
        /pkgs/cpcpub-gui-*-any.pkg.tar.zst
    remove="pacman -R --noconfirm cpcpub-gui cpcpub"
else
    echo "no package manager this test knows" >&2
    exit 1
fi

say "the installed binaries are the release's"
check() {  # check INSTALLED ASSET
    have=$(sha256sum "$1" | cut -d' ' -f1)
    want=$(sha256sum "/rel/$2" | cut -d' ' -f1)
    [ "$have" = "$want" ] || { echo "$1 is $have, $2 is $want" >&2; exit 1; }
    echo "$1 = $2 ($have)"
}
for pair in $builds; do
    check "/usr/bin/${pair%%=*}" "${pair#*=}"
done

say "the benchmark runs and names itself"
cpcpub --version
cpcpub --threads 1 --cpus 0 --time 0.05 --reps 1 --warmup 0.02 --json > /tmp/run.json
python3 - <<'PY'
import hashlib, json
doc = json.load(open("/tmp/run.json"))
actual = hashlib.sha256(open("/usr/bin/cpcpub", "rb").read()).hexdigest()
assert doc["build"]["binary_sha256"] == actual, doc["build"]["binary_sha256"]
print("binary_sha256 matches the installed file")
PY

# Where the tools are there anyway: the apt images install them above.
if command -v desktop-file-validate >/dev/null; then
    say "desktop entry"
    desktop-file-validate /usr/share/applications/je.qd.cpcpub.Gui.desktop
    echo "valid"
fi
if command -v appstreamcli >/dev/null; then
    say "metainfo"
    appstreamcli validate --no-net --pedantic \
        /usr/share/metainfo/je.qd.cpcpub.Gui.metainfo.xml
fi

say "the window runs the benchmark"
Xvfb :99 -screen 0 1280x1024x24 >/dev/null 2>&1 &
xvfb=$!
sleep 1
mkdir -p /tmp/smoke
# Cairo, since a container has no GPU for GTK's default renderer to find.
DISPLAY=:99 GDK_BACKEND=x11 GSK_RENDERER=cairo \
    CPCPUB_GUI_SMOKE=/tmp/smoke/report.json timeout 120 cpcpub-gui \
    2>/tmp/smoke/stderr || true
kill "$xvfb" 2>/dev/null || true
python3 - <<'PY'
import json, sys
try:
    report = json.load(open("/tmp/smoke/report.json"))
except OSError:
    print(open("/tmp/smoke/stderr").read())
    sys.exit("the window wrote no report")
print({k: v for k, v in report.items() if k != "log"})
assert report["binary"] == "/usr/bin/cpcpub", report["binary"]
assert report["exit"] == 0 and report["docs"] == 1, report["log"]
assert report["saved"], "no result document was saved"
print(f"GTK {report['gtk']}: found /usr/bin/cpcpub, ran it, saved the result")
PY

say "remove"
$remove
[ ! -e /usr/bin/cpcpub ] && [ ! -e /usr/bin/cpcpub-gui ] || {
    echo "files left behind after removal" >&2; exit 1; }
echo "removed cleanly"
