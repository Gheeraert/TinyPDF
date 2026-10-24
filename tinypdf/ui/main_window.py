"""Fenêtre principale : barre d'outils, ouverture, signature, enregistrement, impression."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QByteArray, QRect, QSizeF, Qt
from PySide6.QtGui import QAction, QFont, QGuiApplication, QKeySequence, QPainter, QTransform
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import (QComboBox, QFileDialog, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QSizePolicy, QSpinBox,
                               QToolBar, QToolButton, QWidget)

from .. import APP_NAME
from ..config import Settings
from ..document import PdfDocument, PdfError
from ..signature import SignatureStore
from ..stamping import PARTS_BOTH, PARTS_IMAGE, PARTS_TEXT, BlockRenderer
from .icon import app_icon
from .pdf_view import FIT_PAGE, FIT_WIDTH, PdfView
from .placement import BlockSource
from .qt_convert import pixmap_to_qimage
from .signature_dialog import SignatureDialog

PARTS_LABELS = {
    PARTS_BOTH: "Texte et signature",
    PARTS_IMAGE: "Signature manuscrite seule",
    PARTS_TEXT: "Texte seul",
}
ZOOM_PRESETS = (50, 75, 100, 125, 150, 200, 300, 400)
PRINT_DPI = 300


@contextmanager
def busy():
    QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QGuiApplication.restoreOverrideCursor()


def _is_temporary_folder(folder: Path) -> bool:
    """Pièces jointes ouvertes depuis la messagerie : dossier temporaire à éviter."""
    try:
        folder = folder.resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    text = str(folder).lower()
    return (folder == tmp or tmp in folder.parents
            or "inetcache" in text or "content.outlook" in text)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, store: SignatureStore):
        super().__init__()
        self.settings = settings
        self.store = store
        self.profile = store.load()
        self.source = BlockSource(BlockRenderer(self.profile))
        self.pdf: PdfDocument | None = None
        self._warned_digital_signature = False

        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.view = PdfView(self.source, self)
        self.setCentralWidget(self.view)
        self._build_actions()
        self._build_toolbar()

        v = self.view
        v.currentPageChanged.connect(self._on_page_changed)
        v.zoomChanged.connect(self._on_zoom_changed)
        v.placementsChanged.connect(self._update_state)
        v.placingChanged.connect(self._on_placing)
        v.blockResized.connect(self._remember_block_size)
        v.fileDropped.connect(self.open_file)

        fit = self.settings.get("fit_mode", FIT_WIDTH)
        v.set_fit_mode(fit if fit in (FIT_WIDTH, FIT_PAGE) else None)
        geometry = self.settings.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
        else:
            self.resize(1000, 860)
        self.statusBar().setSizeGripEnabled(True)
        self._update_state()

    # ------------------------------------------------------------------ interface

    def _action(self, text, slot, shortcut=None, tip=None) -> QAction:
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in seqs])
        if tip or shortcut:
            keys = a.shortcut().toString(QKeySequence.NativeText)
            a.setToolTip(f"{tip or text.replace('…', '')}" + (f" ({keys})" if keys else ""))
        self.addAction(a)  # raccourcis actifs même hors barre d'outils
        return a

    def _build_actions(self) -> None:
        self.act_open = self._action("Ouvrir…", lambda: self.open_file(), QKeySequence.Open,
                                     "Ouvrir un PDF")
        self.act_save = self._action("Enregistrer…", self.save, QKeySequence.Save,
                                     "Enregistrer le PDF signé")
        self.act_overwrite = self._action("Remplacer le fichier original…", self.overwrite_original)
        self.act_print = self._action("Imprimer…", self.print_document, QKeySequence.Print)
        self.act_sign = self._action("Signer", lambda: self.sign(PARTS_BOTH), "Ctrl+E",
                                     "Apposer ma signature")
        self.act_sign_image = self._action(PARTS_LABELS[PARTS_IMAGE], lambda: self.sign(PARTS_IMAGE))
        self.act_sign_text = self._action(PARTS_LABELS[PARTS_TEXT], lambda: self.sign(PARTS_TEXT))
        self.act_edit_sig = self._action("Modifier ma signature…", self.edit_signature)
        self.act_undo = self._action("Annuler", self.undo, QKeySequence.Undo,
                                     "Retirer la dernière signature posée")
        self.act_prev = self._action("Page précédente", lambda: self.view.previous_page())
        self.act_next = self._action("Page suivante", lambda: self.view.next_page())
        self.act_zoom_in = self._action("Zoom avant", lambda: self.view.zoom_by(1.25),
                                        ["Ctrl++", "Ctrl+="])
        self.act_zoom_out = self._action("Zoom arrière", lambda: self.view.zoom_by(0.8), "Ctrl+-")
        self.act_fit_width = self._action("Largeur", lambda: self._set_fit(FIT_WIDTH), "Ctrl+2",
                                          "Ajuster à la largeur")
        self.act_fit_page = self._action("Page", lambda: self._set_fit(FIT_PAGE), "Ctrl+0",
                                         "Ajuster à la page")
        self._action("Taille réelle", lambda: self._set_zoom(100), "Ctrl+1")
        self._action("Fermer le document", self.close_document, QKeySequence.Close)
        self._action("Quitter", self.close, "Ctrl+Q")
        for a in (self.act_fit_width, self.act_fit_page):
            a.setCheckable(True)

        self.act_prev.setText("‹")
        self.act_prev.setToolTip("Page précédente (Pg préc)")
        self.act_next.setText("›")
        self.act_next.setToolTip("Page suivante (Pg suiv)")

    def _build_toolbar(self) -> None:
        tb = QToolBar("Outils", self)
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, tb)

        tb.addAction(self.act_open)
        save_btn = self._menu_button(self.act_save, [self.act_overwrite])
        tb.addWidget(save_btn)
        tb.addAction(self.act_print)
        tb.addSeparator()

        sign_btn = self._menu_button(self.act_sign, [self.act_sign_image, self.act_sign_text,
                                                      None, self.act_edit_sig])
        f = QFont(sign_btn.font())
        f.setBold(True)
        sign_btn.setFont(f)
        tb.addWidget(sign_btn)
        tb.addAction(self.act_undo)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        tb.addAction(self.act_prev)
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setButtonSymbols(QSpinBox.NoButtons)
        self.page_spin.setAlignment(Qt.AlignRight)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setFixedWidth(48)
        self.page_spin.setToolTip("Aller à la page")
        self.page_spin.valueChanged.connect(lambda v: self.view.go_to_page(v - 1))
        tb.addWidget(self.page_spin)
        self.page_label = QLabel(" / 0 ")
        tb.addWidget(self.page_label)
        tb.addAction(self.act_next)
        tb.addSeparator()

        tb.addAction(self.act_zoom_out)
        self.act_zoom_out.setText("−")
        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.setInsertPolicy(QComboBox.NoInsert)
        self.zoom_combo.addItems([f"{z} %" for z in ZOOM_PRESETS])
        self.zoom_combo.setFixedWidth(80)
        self.zoom_combo.setToolTip("Zoom (Ctrl + molette)")
        self.zoom_combo.activated.connect(lambda _: self._zoom_from_combo())
        self.zoom_combo.lineEdit().editingFinished.connect(self._zoom_from_combo)
        tb.addWidget(self.zoom_combo)
        tb.addAction(self.act_zoom_in)
        self.act_zoom_in.setText("+")
        tb.addAction(self.act_fit_width)
        tb.addAction(self.act_fit_page)

    def _menu_button(self, main: QAction, extra: list) -> QToolButton:
        btn = QToolButton()
        btn.setDefaultAction(main)
        btn.setPopupMode(QToolButton.MenuButtonPopup)
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        menu = QMenu(btn)
        for a in extra:
            if a is None:
                menu.addSeparator()
            else:
                menu.addAction(a)
        btn.setMenu(menu)
        return btn

    # ------------------------------------------------------------------ état

    def _update_state(self) -> None:
        has_doc = self.pdf is not None
        dirty = self.view.has_placements()
        for a in (self.act_print, self.act_prev, self.act_next, self.act_zoom_in,
                  self.act_zoom_out, self.act_fit_width, self.act_fit_page):
            a.setEnabled(has_doc)
        self.act_save.setEnabled(has_doc)
        self.act_overwrite.setEnabled(has_doc and dirty)
        self.act_undo.setEnabled(dirty or self.view.is_placing())
        self.page_spin.setEnabled(has_doc)
        self.zoom_combo.setEnabled(has_doc)
        title = APP_NAME
        if has_doc:
            title = f"{'● ' if dirty else ''}{self.pdf.path.name} — {APP_NAME}"
        self.setWindowTitle(title)

    def _on_page_changed(self, index: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(index + 1)
        self.page_spin.blockSignals(False)

    def _on_zoom_changed(self, percent: float) -> None:
        self.zoom_combo.lineEdit().setText(f"{percent:.0f} %")
        mode = self.view.fit_mode
        self.act_fit_width.setChecked(mode == FIT_WIDTH)
        self.act_fit_page.setChecked(mode == FIT_PAGE)

    def _set_fit(self, mode: str) -> None:
        self.view.set_fit_mode(mode)
        self.settings.set("fit_mode", mode)
        self._on_zoom_changed(self.view.zoom_percent())

    def _set_zoom(self, percent: float) -> None:
        self.view.set_zoom_percent(percent)
        self.settings.set("fit_mode", None)
        self._on_zoom_changed(self.view.zoom_percent())

    def _zoom_from_combo(self) -> None:
        text = self.zoom_combo.currentText().replace("%", "").replace(",", ".").strip()
        try:
            self._set_zoom(float(text))
        except ValueError:
            self._on_zoom_changed(self.view.zoom_percent())
        self.view.setFocus()

    def _on_placing(self, placing: bool) -> None:
        if placing:
            self.statusBar().showMessage(
                "Cliquez sur la page à l'endroit voulu — Échap pour annuler")
        else:
            self.statusBar().showMessage(
                "Glisser pour déplacer, coin bleu pour redimensionner, flèches pour ajuster, "
                "Suppr pour retirer — Ctrl+S pour enregistrer", 8000)
        self._update_state()

    # ------------------------------------------------------------------ fichiers

    def _ask_password(self, retry: bool) -> str | None:
        prompt = ("Mot de passe incorrect. Réessayez :" if retry
                  else "Ce document est protégé. Mot de passe :")
        text, ok = QInputDialog.getText(self, "Document protégé", prompt, QLineEdit.Password)
        return text if ok else None

    def open_file(self, path: str | None = None) -> bool:
        if not self._confirm_discard():
            return False
        if path is None:
            start = self.settings.get("last_open_dir") or str(Path.home())
            path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un PDF", start,
                                                  "Documents PDF (*.pdf);;Tous les fichiers (*)")
            if not path:
                return False
        try:
            with busy():
                pdf = PdfDocument.open(path, self._ask_password)
        except PdfError as exc:
            QMessageBox.warning(self, "Ouvrir", str(exc))
            return False
        self._set_document(pdf)
        folder = pdf.path.parent
        if not _is_temporary_folder(folder):
            self.settings.set("last_open_dir", str(folder))
        return True

    def _set_document(self, pdf: PdfDocument | None, keep_position: bool = False) -> None:
        old = self.pdf
        self.pdf = pdf
        self._warned_digital_signature = False
        self.view.set_document(pdf, keep_position)
        if old is not None and old is not pdf:
            old.close()
        n = pdf.page_count if pdf else 0
        self.page_spin.setRange(1, max(n, 1))
        self.page_label.setText(f" / {n} ")
        self._update_state()
        self.view.setFocus()

    def close_document(self) -> None:
        if self.pdf and self._confirm_discard():
            self._set_document(None)

    def _confirm_discard(self) -> bool:
        """True si l'on peut abandonner le document courant."""
        if self.view.is_placing():
            self.view.cancel_placement()
        if self.pdf is None or not self.view.has_placements():
            return True
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          "Les signatures apposées n'ont pas été enregistrées.",
                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, self)
        box.setInformativeText("Voulez-vous enregistrer le document signé ?")
        box.setDefaultButton(QMessageBox.Save)
        choice = box.exec()
        if choice == QMessageBox.Save:
            return self.save()
        return choice == QMessageBox.Discard

    def _default_save_path(self) -> Path:
        src = self.pdf.path
        folder = src.parent
        if _is_temporary_folder(folder):
            folder = Path(self.settings.get("last_save_dir") or
                          self.settings.get("last_open_dir") or
                          Path.home() / "Documents")
            if not folder.is_dir():
                folder = Path.home()
        return folder / f"{src.stem}_signe.pdf"

    def save(self) -> bool:
        if self.pdf is None:
            return False
        if not self.view.has_placements():
            self.statusBar().showMessage("Aucune signature à enregistrer : cliquez d'abord sur « Signer ».", 6000)
            return False
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer le PDF signé",
                                              str(self._default_save_path()), "Documents PDF (*.pdf)")
        if not path:
            return False
        target = Path(path)
        if target.suffix.lower() != ".pdf":
            target = target.with_name(target.name + ".pdf")
        if self._same_file(target, self.pdf.path) and not self._confirm_overwrite_original():
            return False
        return self._write(target)

    def overwrite_original(self) -> bool:
        if self.pdf is None or not self.view.has_placements():
            return False
        if not self._confirm_overwrite_original():
            return False
        return self._write(self.pdf.path)

    @staticmethod
    def _same_file(a: Path, b: Path) -> bool:
        try:
            return a.exists() and os.path.samefile(a, b)
        except OSError:
            return False

    def _confirm_overwrite_original(self) -> bool:
        answer = QMessageBox.warning(
            self, "Remplacer l'original",
            f"Le fichier original « {self.pdf.path.name} » va être remplacé par la version signée.\n\n"
            "Cette opération est définitive. Continuer ?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        return answer == QMessageBox.Yes

    def _write(self, target: Path) -> bool:
        pdf = self.pdf
        try:
            with busy():
                saved = pdf.save(target, self.view.placements(), self.source.renderer)
        except PdfError as exc:
            QMessageBox.critical(self, "Enregistrer", str(exc))
            return False
        # On affiche désormais le fichier signé (les signatures y sont intégrées).
        try:
            reopened = PdfDocument.open(saved, lambda retry: None if retry else pdf.password)
        except PdfError as exc:
            QMessageBox.warning(self, "Enregistrer",
                                f"Le document a été enregistré mais n'a pas pu être rouvert :\n{exc}")
            self.view.set_document(pdf, keep_position=True)
            return True
        self._set_document(reopened, keep_position=True)
        if not _is_temporary_folder(saved.parent):
            self.settings.set("last_save_dir", str(saved.parent))
        self.statusBar().showMessage(f"Enregistré : {saved}", 10000)
        return True

    # ------------------------------------------------------------------ signature

    def edit_signature(self) -> bool:
        dlg = SignatureDialog(self.profile, self)
        if dlg.exec() != SignatureDialog.Accepted:
            return False
        profile = dlg.result_profile()
        try:
            self.store.save(profile)
        except OSError as exc:
            QMessageBox.warning(self, "Ma signature",
                                f"La signature n'a pas pu être enregistrée sur le disque :\n{exc}\n\n"
                                "Elle reste utilisable pendant cette session.")
        self.profile = profile
        self.source.renderer = BlockRenderer(profile)
        self.view.refresh_blocks()
        return True

    def sign(self, parts: str = PARTS_BOTH) -> None:
        if self.pdf is None and not self.open_file():
            return
        if not self.pdf.can_modify():
            QMessageBox.warning(self, "Signer",
                                "Ce document est protégé contre les modifications par son auteur : "
                                "il n'est pas possible d'y apposer une signature.")
            return
        if not self.store.is_configured() and not self.edit_signature():
            return
        renderer = self.source.renderer
        effective = renderer.effective_parts(parts)
        if effective is None:
            missing = "de texte" if parts == PARTS_TEXT else "de signature manuscrite"
            answer = QMessageBox.question(self, "Signer",
                                          f"Votre signature ne comporte pas {missing}. La compléter ?")
            if answer != QMessageBox.Yes or not self.edit_signature():
                return
            renderer = self.source.renderer
            effective = renderer.effective_parts(parts)
            if effective is None:
                return
        if self.pdf.has_digital_signature() and not self._warned_digital_signature:
            answer = QMessageBox.warning(
                self, "Document signé électroniquement",
                "Ce document comporte une signature électronique (certificat).\n\n"
                "Votre signature manuscrite sera ajoutée sans altérer la version signée, "
                "mais les lecteurs PDF indiqueront que le document a été modifié après "
                "sa signature électronique.\n\nContinuer ?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return
            self._warned_digital_signature = True
        w, h = renderer.natural_size(effective)
        k = float(self.settings.get("block_scale", {}).get(effective, 1.0))
        self.view.start_placement(effective, QSizeF(w * k, h * k))

    def _remember_block_size(self, parts: str, width: float) -> None:
        natural_w, _ = self.source.renderer.natural_size(parts)
        scales = dict(self.settings.get("block_scale", {}))
        scales[parts] = round(width / natural_w, 4)
        self.settings.set("block_scale", scales)

    def undo(self) -> None:
        if not self.view.undo_last():
            self.statusBar().showMessage("Rien à annuler.", 3000)

    # ------------------------------------------------------------------ impression

    def print_document(self) -> None:
        if self.pdf is None:
            return
        if not self.pdf.can_print():
            QMessageBox.warning(self, "Imprimer", "L'auteur de ce document en interdit l'impression.")
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName(self.pdf.path.stem)
        n = self.pdf.page_count
        printer.setFromTo(1, n)
        dlg = QPrintDialog(printer, self)
        dlg.setMinMax(1, n)
        dlg.setOption(QAbstractPrintDialog.PrintPageRange, True)
        dlg.setOption(QAbstractPrintDialog.PrintCurrentPage, True)
        if dlg.exec() != QPrintDialog.Accepted:
            return
        if printer.printRange() == QPrinter.PageRange:
            pages = range(max(printer.fromPage(), 1) - 1, min(printer.toPage(), n))
        elif printer.printRange() == QPrinter.CurrentPage:
            pages = [self.view.current_page()]
        else:
            pages = range(n)
        try:
            with busy():
                self._print_pages(printer, pages)
        except Exception as exc:
            QMessageBox.critical(self, "Imprimer", f"L'impression a échoué :\n{exc}")
            return
        self.statusBar().showMessage("Document envoyé à l'imprimante.", 5000)

    def _print_pages(self, printer: QPrinter, pages) -> None:
        # Le document imprimé inclut les signatures non encore enregistrées.
        doc = self.pdf.build_signed(self.view.placements(), self.source.renderer)
        dpi = min(printer.resolution(), PRINT_DPI)
        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Imprimante indisponible.")
        try:
            for i, index in enumerate(pages):
                if i:
                    printer.newPage()
                img = pixmap_to_qimage(doc[index].get_pixmap(dpi=dpi, annots=True))
                area = painter.viewport()
                if (img.width() > img.height()) != (area.width() > area.height()):
                    img = img.transformed(QTransform().rotate(90))
                k = min(area.width() / img.width(), area.height() / img.height())
                w, h = int(img.width() * k), int(img.height() * k)
                x, y = (area.width() - w) // 2, (area.height() - h) // 2
                painter.drawImage(QRect(area.x() + x, area.y() + y, w, h), img)
        finally:
            painter.end()
            doc.close()

    # ------------------------------------------------------------------ fenêtre

    def dragEnterEvent(self, event):
        self.view.dragEnterEvent(event)

    def dropEvent(self, event):
        self.view.dropEvent(event)

    def closeEvent(self, event):
        if not self._confirm_discard():
            event.ignore()
            return
        try:
            self.settings.set("geometry", bytes(self.saveGeometry().toBase64()).decode())
        except OSError:
            pass
        if self.pdf:
            self.pdf.close()
        event.accept()
