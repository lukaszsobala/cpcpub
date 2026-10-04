#!/bin/sh
# Regenerate every raster icon from cpcpub.svg. The outputs are committed, so
# a release build needs none of these tools; run this only after editing the
# SVG. Needs inkscape and python3.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
top=$(cd "$here/../.." && pwd)
svg="$here/cpcpub.svg"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

png() {  # png SIZE OUT
    inkscape "$svg" --export-width="$1" --export-height="$1" \
        --export-filename="$2" >/dev/null 2>&1
}

# Windows: one .ico holding the sizes Explorer, the taskbar and the Start menu
# ask for, each rendered from the vector rather than scaled from the largest.
# Packed by hand with PNG entries, which every Windows since Vista reads: the
# tools that write ICO store at least the 256px entry as a raw bitmap, a
# quarter of a megabyte for one picture.
sizes="16 20 24 32 40 48 64 256"
for s in $sizes; do png "$s" "$tmp/$s.png"; done
python3 "$here/pack-ico.py" "$here/cpcpub.ico" \
    $(for s in $sizes; do echo "$tmp/$s.png"; done)
echo "wrote packaging/icons/cpcpub.ico"

# Android. The launcher icon at each density, for launchers that take it as it
# is, and the adaptive icon's foreground layer for the ones that mask it: a
# 108dp square of which a circle as small as 66dp across may be all that
# shows, so the chip sits in the middle 52dp, where a round mask still leaves
# its pins whole. The background layer is the colour in values/.
res="$top/android/res"
python3 - "$svg" "$tmp/foreground.svg" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
inner = src[src.index(">", src.index("<svg")) + 1:src.rindex("</svg>")]
inner = re.sub(r"<!--.*?-->", "", inner, flags=re.S)
scale = 52 / 128
open(sys.argv[2], "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 108 108" '
    'width="108" height="108">'
    f'<g transform="translate({(108 - 52) / 2} {(108 - 52) / 2}) scale({scale})">'
    f'{inner}</g></svg>\n')
PY
for d in mdpi:1 hdpi:1.5 xhdpi:2 xxhdpi:3 xxxhdpi:4; do
    name=${d%%:*}
    mult=${d#*:}
    mkdir -p "$res/mipmap-$name"
    png "$(python3 -c "print(round(48 * $mult))")" "$res/mipmap-$name/ic_launcher.png"
    inkscape "$tmp/foreground.svg" \
        --export-width="$(python3 -c "print(round(108 * $mult))")" \
        --export-filename="$res/mipmap-$name/ic_launcher_foreground.png" >/dev/null 2>&1
done
echo "wrote android/res/mipmap-*/"
