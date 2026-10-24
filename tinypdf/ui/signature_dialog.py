"""Fenêtre unique de création / modification de la signature."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QRadioButton, QSlider, QVBoxLayout, QWidget)

from ..fonts import available_fonts
from ..signature import (LAYOUT_ABOVE, LAYOUT_BESIDE, SignatureProfile,
                         clean_signature_image, expand_placeholders)
from ..stamping import PARTS_BOTH, BlockRenderer
from .qt_convert import pixmap_to_qimage, qimage_to_png

INK_COLORS = {"Noir": QColor(20, 20, 20), "Bleu": QColor(20, 50, 160)}
EXPORT_SCALE = 3  # résolution de l'image enregistrée par rapport au tracé à l'écran


class SignaturePad(QWidget):
    """Zone de dessin à la souris (ou au stylet)."""

    changed = Signal()
    PAD_SIZE = QSize(560, 210)
    MARGIN = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.PAD_SIZE)
        self.setCursor(Qt.CrossCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._strokes: list[list[QPointF]] = []
        self._base: QImage | None = None  # signature existante ou importée
        self.color = INK_COLORS["Noir"]
        self.pen_width = 3.0
        self.modified = False

    # -- contenu --------------------------------------------------------------

    def is_empty(self) -> bool:
        return not self._strokes and self._base is None

    def clear(self) -> None:
        self._strokes, self._base = [], None
        self._touch()

    def set_base_image(self, img: QImage | None, modified: bool = True) -> None:
        self._strokes = []
        self._base = img
        if modified:
            self._touch()
        else:
            self.update()

    def set_ink(self, color: QColor | None = None, width: float | None = None) -> None:
        if color is not None:
            self.color = color
        if width is not None:
            self.pen_width = width
        if self._strokes:
            self._touch()
        else:
            self.update()

    def _touch(self) -> None:
        self.modified = True
        self.update()
        self.changed.emit()

    # -- dessin ---------------------------------------------------------------

    def _base_rect(self, scale: float = 1.0) -> QRectF:
        area = QRectF(self.rect()).adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        w, h = self._base.width(), self._base.height()
        k = min(area.width() / w, area.height() / h)
        r = QRectF(0, 0, w * k, h * k)
        r.moveCenter(area.center())
        return QRectF(r.x() * scale, r.y() * scale, r.width() * scale, r.height() * scale)

    def _paint_content(self, p: QPainter, scale: float) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._base is not None:
            p.drawImage(self._base_rect(scale), self._base)
        pen = QPen(self.color, self.pen_width * scale, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for stroke in self._strokes:
            pts = [QPointF(pt.x() * scale, pt.y() * scale) for pt in stroke]
            if len(pts) == 1:
                p.drawPoint(pts[0])
                continue
            # Lissage : courbes quadratiques passant par les milieux des segments.
            path = QPainterPath(pts[0])
            for a, b in zip(pts[1:-1], pts[2:]):
                path.quadTo(a, (a + b) / 2)
            path.lineTo(pts[-1])
            p.drawPath(path)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.white)
        guide = QPen(QColor(200, 200, 200), 1, Qt.DashLine)
        p.setPen(guide)
        y = int(self.height() * 0.72)
        p.drawLine(self.MARGIN * 2, y, self.width() - self.MARGIN * 2, y)
        if self.is_empty():
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect().adjusted(0, 0, 0, -self.height() // 3), Qt.AlignCenter,
                       "Signez ici avec la souris")
        self._paint_content(p, 1.0)
        p.setPen(QPen(QColor(170, 170, 170), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def export_image(self) -> QImage | None:
        """Image transparente haute résolution (None si la zone est vide)."""
        if self.is_empty():
            return None
        img = QImage(self.size() * EXPORT_SCALE, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        self._paint_content(p, EXPORT_SCALE)
        p.end()
        return img

    # -- souris / stylet ------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._strokes.append([event.position()])
            self._touch()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._strokes:
            self._strokes[-1].append(event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.changed.emit()


class SignatureDialog(QDialog):
    def __init__(self, profile: SignatureProfile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ma signature")
        self._profile = profile
        self._image_png = profile.image_png
        self._text_color = QColor(profile.color)

        # --- signature manuscrite
        self.pad = SignaturePad()
        if profile.image_png:
            self.pad.set_base_image(QImage.fromData(profile.image_png), modified=False)
        self.pad.changed.connect(self._schedule_preview)

        ink_group = QButtonGroup(self)
        ink_row = QHBoxLayout()
        ink_row.addWidget(QLabel("Encre :"))
        for i, name in enumerate(INK_COLORS):
            rb = QRadioButton(name)
            rb.setChecked(i == 0)
            ink_group.addButton(rb)
            rb.toggled.connect(lambda on, n=name: on and self.pad.set_ink(color=INK_COLORS[n]))
            ink_row.addWidget(rb)
        ink_row.addSpacing(16)
        ink_row.addWidget(QLabel("Épaisseur :"))
        width = QSlider(Qt.Horizontal)
        width.setRange(1, 8)
        width.setValue(3)
        width.setFixedWidth(110)
        width.valueChanged.connect(lambda v: self.pad.set_ink(width=float(v)))
        ink_row.addWidget(width)
        ink_row.addStretch(1)
        clear_btn = QPushButton("Effacer")
        clear_btn.clicked.connect(self.pad.clear)
        import_btn = QPushButton("Importer une image…")
        import_btn.clicked.connect(self._import_image)
        ink_row.addWidget(clear_btn)
        ink_row.addWidget(import_btn)

        hand_box = QGroupBox("Signature manuscrite")
        hv = QVBoxLayout(hand_box)
        hv.addWidget(self.pad, 0, Qt.AlignHCenter)
        hv.addLayout(ink_row)

        # --- texte
        self.text_edit = QLineEdit(profile.text)
        self.text_edit.setPlaceholderText("Laisser vide pour ne pas ajouter de texte")
        self.text_edit.textChanged.connect(self._schedule_preview)
        hint = QLabel("{date} → %s     {date_longue} → %s"
                      % (expand_placeholders("{date}"), expand_placeholders("{date_longue}")))
        hint.setStyleSheet("color: gray;")

        self.font_combo = QComboBox()
        self.font_combo.addItems(list(available_fonts()))
        self.font_combo.setCurrentText(profile.font_name)
        self.font_combo.setMaxVisibleItems(20)
        self.font_combo.currentTextChanged.connect(self._schedule_preview)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(5, 48)
        self.size_spin.setDecimals(1)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(profile.font_size)
        self.size_spin.setMinimumWidth(90)
        self.size_spin.valueChanged.connect(self._schedule_preview)

        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(60)
        self.color_btn.clicked.connect(self._pick_color)
        self._update_color_button()

        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Texte au-dessus de la signature", LAYOUT_ABOVE)
        self.layout_combo.addItem("Texte à gauche de la signature", LAYOUT_BESIDE)
        self.layout_combo.setCurrentIndex(0 if profile.layout == LAYOUT_ABOVE else 1)
        self.layout_combo.currentIndexChanged.connect(self._schedule_preview)

        style_row = QHBoxLayout()
        style_row.addWidget(self.font_combo, 1)
        style_row.addWidget(self.size_spin)
        style_row.addWidget(self.color_btn)

        text_box = QGroupBox("Texte")
        form = QFormLayout(text_box)
        form.addRow("Texte :", self.text_edit)
        form.addRow("", hint)
        form.addRow("Police :", style_row)
        form.addRow("Disposition :", self.layout_combo)

        # --- aperçu
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(110)
        self.preview.setStyleSheet("background: white; border: 1px solid #bbb;")
        preview_box = QGroupBox("Aperçu")
        QVBoxLayout(preview_box).addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Enregistrer")
        buttons.button(QDialogButtonBox.Cancel).setText("Annuler")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(hand_box)
        root.addWidget(text_box)
        root.addWidget(preview_box)
        root.addWidget(buttons)

        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._update_preview)
        self._update_preview()

    # -- actions --------------------------------------------------------------

    def _import_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Importer une signature", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                png = clean_signature_image(f.read())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Importer une signature", f"Image inutilisable :\n{exc}")
            return
        self.pad.set_base_image(QImage.fromData(png))

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(self._text_color, self, "Couleur du texte")
        if c.isValid():
            self._text_color = c
            self._update_color_button()
            self._schedule_preview()

    def _update_color_button(self) -> None:
        self.color_btn.setStyleSheet(
            f"background: {self._text_color.name()}; border: 1px solid #888; min-height: 20px;")
        self.color_btn.setToolTip(f"Couleur du texte ({self._text_color.name()})")

    # -- profil ---------------------------------------------------------------

    def _current_image(self) -> bytes | None:
        if not self.pad.modified:
            return self._image_png
        img = self.pad.export_image()
        if img is None:
            return None
        try:
            return clean_signature_image(qimage_to_png(img))
        except ValueError:
            return None

    def profile(self) -> SignatureProfile:
        name = self.font_combo.currentText()
        return replace(
            self._profile,
            text=self.text_edit.text(),
            font_name=name,
            font_file=available_fonts().get(name, ""),
            font_size=self.size_spin.value(),
            color=self._text_color.name(),
            layout=self.layout_combo.currentData(),
            image_png=self._current_image(),
        )

    def _schedule_preview(self, *_):
        self._timer.start()

    def _update_preview(self) -> None:
        prof = self.profile()
        renderer = BlockRenderer(prof)
        parts = renderer.effective_parts(PARTS_BOTH)
        if parts is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("(signature vide)")
            return
        w, h = renderer.natural_size(parts)
        dpr = self.devicePixelRatioF()
        # Afficher à ~150 % de la taille réelle, en restant dans la zone d'aperçu.
        scale = min(2.0, 520 / w, 140 / h)
        img = pixmap_to_qimage(renderer.render(parts, round(w * scale * dpr)))
        img.setDevicePixelRatio(dpr)
        self.preview.setPixmap(QPixmap.fromImage(img))

    def _accept(self) -> None:
        prof = self.profile()
        if not prof.has_text and not prof.has_image:
            QMessageBox.information(self, "Ma signature",
                                    "Dessinez une signature ou saisissez un texte.")
            return
        self._result = prof
        self.accept()

    def result_profile(self) -> SignatureProfile:
        return self._result
