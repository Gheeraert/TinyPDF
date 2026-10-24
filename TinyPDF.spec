# -*- mode: python ; coding: utf-8 -*-
# Construction PyInstaller de TinyPDF (utilisé par build.ps1).
# TINYPDF_ONEFILE=1 produit un exécutable unique au lieu d'un dossier.

import os

ONEFILE = os.environ.get("TINYPDF_ONEFILE") == "1"

# Modules jamais utilisés par l'application.
EXCLUDES = [
    "tkinter", "unittest", "pydoc", "numpy", "onnxruntime", "networkx",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL",
    "PySide6.QtSvg", "PySide6.QtDBus",
]

# Fichiers Qt superflus pour une application à base de widgets.
DROP = (
    "opengl32sw.dll",       # OpenGL logiciel (20 Mo)
    "qt6network", "qt6qml", "qt6quick", "qt6opengl", "qt6pdf", "qt6svg", "qt6virtualkeyboard",
    "qtvirtualkeyboard", "networkinformation", "tls", "qmltooling",
)


def keep(entry):
    name = entry[0].replace("\\", "/").lower()
    if any(token in name.rsplit("/", 1)[-1] for token in DROP):
        return False
    # Traductions : seul le français de qtbase est utile.
    if "/translations/" in name and not name.endswith("qtbase_fr.qm"):
        return False
    return True


a = Analysis(
    ["main.py"],
    excludes=EXCLUDES,
    noarchive=False,
)
a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)

common = dict(
    name="TinyPDF",
    icon="assets/tinypdf.ico",
    console=False,
    upx=False,
    version=None,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="TinyPDF", upx=False)
