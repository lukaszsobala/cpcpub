#!/usr/bin/env python3
"""Draw the Google Play listing's two pictures from cpcpub.svg: the 512px icon
and the 1024x500 feature graphic.

    play.py OUTDIR

Needs inkscape, imagemagick and the Roboto font. The pictures are for the Play
Console, not for the build. The listing's current pair is kept in play/ beside
this script: after changing the logo or the words, run `play.py
packaging/icons/play` and commit what it wrote.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PAPER, GREEN, INK = "#fbfbfa", "#2f6f4f", "#1a1a19"
HERE = Path(__file__).resolve().parent


def chip() -> str:
    """The logo's shapes, without the svg wrapper or its comment."""
    src = (HERE / "cpcpub.svg").read_text()
    inner = src[src.index(">", src.index("<svg")) + 1:src.rindex("</svg>")]
    return re.sub(r"<!--.*?-->", "", inner, flags=re.S)


def render(svg: str, out: Path, width: int, height: int, flat: bool = False) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.svg"
        src.write_text(svg)
        subprocess.run(["inkscape", str(src), f"--export-width={width}",
                        f"--export-height={height}", f"--export-filename={out}"],
                       check=True, capture_output=True)
    if flat:
        # Play takes no alpha channel on the feature graphic; the ground is
        # solid, so dropping it changes no pixel.
        subprocess.run(["convert", str(out), "-alpha", "off", f"PNG24:{out}"],
                       check=True)


def icon() -> str:
    # The logo spans x 12..116, y 4..116 of its 128 box; scaled to fill 70% of
    # the square, and moved so that its middle lands on the square's.
    s = 512 * 0.70 / 112
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            f'<rect width="512" height="512" fill="{PAPER}"/>'
            f'<g transform="translate({256 - 64 * s} {256 - 60 * s}) scale({s})">'
            f'{chip()}</g></svg>')


def graphic() -> str:
    s = 3.0
    font = "font-family=\"Roboto, 'DejaVu Sans', sans-serif\""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 500">'
            f'<rect width="1024" height="500" fill="{PAPER}"/>'
            f'<rect x="0" y="468" width="1024" height="32" fill="{GREEN}"/>'
            f'<g transform="translate({250 - 64 * s} {234 - 60 * s}) scale({s})">'
            f'{chip()}</g>'
            f'<text x="480" y="240" {font} font-weight="700" font-size="112" '
            f'fill="{INK}">cpcpub</text>'
            f'<text x="484" y="305" {font} font-size="40" fill="{GREEN}">'
            f'Benchmark and compare</text>'
            f'<text x="484" y="355" {font} font-size="40" fill="{GREEN}">'
            f'every CPU!</text></svg>')


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    render(icon(), out / "icon-512.png", 512, 512)
    render(graphic(), out / "feature-graphic-1024x500.png", 1024, 500, flat=True)


if __name__ == "__main__":
    main()
