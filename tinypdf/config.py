"""Emplacement des données personnelles et préférences persistantes (JSON)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import APP_NAME


def data_dir() -> Path:
    """Dossier des données de l'application (%APPDATA%\\TinyPDF sous Windows)."""
    override = os.environ.get("TINYPDF_DATA_DIR")  # utile pour les tests
    if override:
        base = Path(override)
    elif os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / APP_NAME
    else:
        base = Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def atomic_write(path: Path, data: bytes) -> None:
    """Écrit un fichier sans jamais laisser de version tronquée sur le disque."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Settings:
    """Préférences clé/valeur stockées dans settings.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "settings.json"
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def save(self) -> None:
        payload = json.dumps(self._data, indent=2, ensure_ascii=False)
        atomic_write(self.path, payload.encode("utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()
