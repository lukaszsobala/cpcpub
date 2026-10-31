#!/usr/bin/env python3
"""cpcpub.svg as the Android adaptive icon's monochrome layer.

Android 13 and later tint this layer to the home screen's theme and use only
its alpha, so the icon is redrawn in one colour as a VectorDrawable: the pins
and the crown, the chip with the stopwatch face cut out of it -- a light fill
would tint the same as the chip -- and the wedge, the ticks and the hand on
the face, the wedge at the SVG's own opacity. It is placed in the 108dp layer
as make-icons.sh places the foreground: the chip in the middle 52dp.

Reads the plain shapes cpcpub.svg is made of, and no SVG beyond them.

    monochrome.py cpcpub.svg OUT.xml
"""
import re
import sys


def attrs(tag):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def n(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


SCALE = 52 / 128


def rect(a):
    x, y, w, h = (float(a[k]) for k in ("x", "y", "width", "height"))
    r = float(a.get("rx", 0))
    # A corner less than a dp across is not drawn: it cannot be seen, and
    # the pins' would make one path long enough for lint to call it slow.
    if r * SCALE < 1:
        return f"M{n(x)},{n(y)}h{n(w)}v{n(h)}h{n(-w)}z"
    arc = lambda dx, dy: f"a{n(r)},{n(r)} 0,0 1,{n(dx)},{n(dy)}"
    return (f"M{n(x + r)},{n(y)}h{n(w - 2 * r)}{arc(r, r)}v{n(h - 2 * r)}{arc(-r, r)}"
            f"h{n(-(w - 2 * r))}{arc(-r, -r)}v{n(-(h - 2 * r))}{arc(r, -r)}z")


def circle(a):
    cx, cy, r = (float(a[k]) for k in ("cx", "cy", "r"))
    return (f"M{n(cx - r)},{n(cy)}a{n(r)},{n(r)} 0,1 0,{n(2 * r)},0"
            f"a{n(r)},{n(r)} 0,1 0,{n(-2 * r)},0z")


def main(src, out):
    with open(src) as f:
        svg = f.read()
    rects = [attrs(t) for t in re.findall(r"<rect ([^>]*)/>", svg)]
    circles = [attrs(t) for t in re.findall(r"<circle ([^>]*)/>", svg)]
    lines = [attrs(t) for t in re.findall(r"<line ([^>]*)/>", svg)]
    if not (path := re.search(r"<path ([^>]*)/>", svg)):
        sys.exit(f"{src}: no <path/>, the face's wedge")
    wedge = attrs(path.group(1))
    # The chip is the one large rectangle; the rest are its pins and the crown.
    chip = max(rects, key=lambda a: float(a["width"]))
    pins = [a for a in rects if a is not chip]
    face, hub = circles[0], circles[-1]

    white = 'android:fillColor="#FFFFFFFF"'
    paths = [
        f'{white}\n            android:pathData="{" ".join(map(rect, pins))}"',
        (f'{white} android:fillType="evenOdd"\n'
         f'            android:pathData="{rect(chip)} {circle(face)}"'),
        (f'{white} android:fillAlpha="{wedge.get("fill-opacity", "1")}"\n'
         f'            android:pathData="{wedge["d"]}"'),
    ]
    for width in sorted({a["stroke-width"] for a in lines}, key=float):
        d = " ".join(f'M{a["x1"]},{a["y1"]}L{a["x2"]},{a["y2"]}'
                     for a in lines if a["stroke-width"] == width)
        paths.append(f'android:strokeColor="#FFFFFFFF" android:strokeWidth="{width}"\n'
                     f'            android:strokeLineCap="round" android:pathData="{d}"')
    paths.append(f'{white}\n            android:pathData="{circle(hub)}"')

    inset = n((108 - 52) / 2)
    scale = repr(SCALE)
    body = "\n".join(f"        <path {p}/>" for p in paths)
    with open(out, "w") as f:
        f.write(f'''<?xml version="1.0" encoding="utf-8"?>
<!-- The adaptive icon's monochrome layer: packaging/icons/cpcpub.svg in one
     colour, written by make-icons.sh (monochrome.py). Edit the SVG, not this. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp" android:height="108dp"
    android:viewportWidth="108" android:viewportHeight="108">
    <group android:translateX="{inset}" android:translateY="{inset}"
        android:scaleX="{scale}" android:scaleY="{scale}">
{body}
    </group>
</vector>
''')


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: monochrome.py cpcpub.svg OUT.xml")
    main(sys.argv[1], sys.argv[2])
