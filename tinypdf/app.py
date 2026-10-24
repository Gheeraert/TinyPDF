"""Démarrage de l'application."""

from __future__ import annotations

import sys
import traceback
from datetime import datetime

from PySide6.QtCore import QLibraryInfo, Qt, QTimer, QTranslator
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, __version__
from .config import Settings, data_dir
from .signature import SignatureStore


def _install_excepthook() -> None:
    """Une erreur imprévue est journalisée et signalée, sans fermer l'application."""
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(data_dir() / "erreurs.log", "a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} (v{__version__})\n{text}\n")
        except OSError:
            pass
        if QApplication.instance() is not None:
            QMessageBox.critical(None, APP_NAME,
                                 f"Une erreur inattendue s'est produite :\n{exc}\n\n"
                                 f"Détails enregistrés dans {data_dir() / 'erreurs.log'}")
    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    # Boutons standard (Oui, Annuler…) en français.
    translator = QTranslator(app)
    if translator.load("qtbase_fr", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)

    _install_excepthook()

    from .ui.main_window import MainWindow  # import après QApplication

    settings = Settings()
    window = MainWindow(settings, SignatureStore(settings))
    window.show()

    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        QTimer.singleShot(0, lambda: window.open_file(files[0]))
    return app.exec()
