#!/bin/sh
# Build the Windows window: a PyInstaller folder holding Python, PyGObject,
# GTK 4 and the script, at dist/cpcpub-gui/. Run from the top of the tree in
# an MSYS2 UCRT64 shell with these installed:
#
#   mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-python-gobject
#   mingw-w64-ucrt-x86_64-pyinstaller
#
# MSYS2 rather than python.org's Python with a hand-built GTK: MSYS2 builds
# GTK 4 and PyGObject against each other and against one C runtime (the UCRT,
# as the benchmark's own Windows builds are), and PyInstaller's GTK hooks know
# its layout. The versions bundled are whatever MSYS2 has that day, printed
# below so a release's log says what went into it.
set -eu
python -c 'import sys; print("python", sys.version.split()[0])'
python - <<'PY'
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
print("pygobject", gi.__version__)
print("gtk", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}."
              f"{Gtk.get_micro_version()}")
PY
pyinstaller --version
pyinstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
    packaging/windows/cpcpub-gui.spec
# The window's whole footprint, for the log: the installer carries all of it.
du -sh dist/cpcpub-gui
find dist/cpcpub-gui -type f | wc -l
