"""Recherche des polices TrueType/OpenType installées sous Windows."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

BUILTIN_FONT = "Helvetica (intégrée)"

_REG_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_SUFFIXES = (" (TrueType)", " (OpenType)")


def _read_registry() -> dict[str, str]:
    import winreg

    found: dict[str, str] = {}
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, _REG_KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(value, str) or "&" in name:
                    continue  # collections .ttc : non prises en charge
                if not value.lower().endswith((".ttf", ".otf")):
                    continue
                for suffix in _SUFFIXES:
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
                path = Path(value) if os.path.isabs(value) else windows_fonts / value
                if path.is_file():
                    found[name.strip()] = str(path)
    return found


@lru_cache(maxsize=1)
def available_fonts() -> dict[str, str]:
    """Nom affiché -> chemin du fichier. La police intégrée a un chemin vide."""
    fonts: dict[str, str] = {}
    if sys.platform == "win32":
        try:
            fonts = _read_registry()
        except Exception:  # registre illisible : on se contente de la police intégrée
            fonts = {}
    result = {BUILTIN_FONT: ""}
    result.update(sorted(fonts.items(), key=lambda kv: kv[0].lower()))
    return result


def default_font() -> tuple[str, str]:
    fonts = available_fonts()
    for name in ("Arial", "Calibri", "Segoe UI"):
        if name in fonts:
            return name, fonts[name]
    return BUILTIN_FONT, ""
