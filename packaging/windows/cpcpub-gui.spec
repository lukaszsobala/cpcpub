# PyInstaller spec for the Windows build of the window. Run by build-gui.sh in
# an MSYS2 UCRT64 shell, whose Python, PyGObject and GTK 4 it bundles:
#
#     pyinstaller --noconfirm packaging/windows/cpcpub-gui.spec
#
# One folder, not one file: a one-file build unpacks the whole of GTK into a
# temporary directory on every start, which is slow, and is also the shape
# antivirus heuristics distrust most. The MSI installs the folder as it is.
# ruff: noqa: F821 -- Analysis, PYZ, EXE, COLLECT and SPECPATH are PyInstaller's.

import os

top = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
icons = os.path.join(top, "packaging", "icons")

a = Analysis(
    [os.path.join(top, "gui", "cpcpub-gui.py")],
    hooksconfig={
        "gi": {
            # GTK 4, not the 3 PyInstaller's hook assumes without being told.
            "module-versions": {"Gtk": "4.0"},
            # The symbolic icons the window uses -- the copy button, the
            # token field's eye -- are Adwaita's; GTK itself has only a few.
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            # The window is in English and says nothing GTK would translate
            # beyond its file dialogs, which are Windows' own.
            "languages": [],
        },
    },
    excludes=["tkinter", "unittest", "pydoc", "test"],
)
# The window icon, by the name the window asks the icon theme for; frozen, the
# window adds this folder to the theme's search path.
a.datas += [("icons/je.qd.cpcpub.Gui.svg", os.path.join(icons, "cpcpub.svg"), "DATA")]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cpcpub-gui",
    console=False,            # a window, with no console behind it
    icon=os.path.join(icons, "cpcpub.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="cpcpub-gui", upx=False)
