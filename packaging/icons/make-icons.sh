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
