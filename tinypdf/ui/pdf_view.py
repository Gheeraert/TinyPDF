"""Vue continue du document : rendu paresseux, zoom, navigation, placement."""

from __future__ import annotations

import bisect
import math
from collections import OrderedDict
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QTransform
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsScene, QGraphicsView

from ..document import PdfDocument
from .placement import BlockItem, BlockSource, PlacementItem
from .qt_convert import pixmap_to_qimage

PAGE_GAP = 12.0           # espace entre les pages, en points
MAX_FULL_PIXELS = 12e6    # au-delà, on ne rend que la zone visible
CACHE_PAGES = 10          # nombre de pages gardées en mémoire
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0  # en proportion de la taille réelle

FIT_WIDTH = "width"
FIT_PAGE = "page"


class PageItem(QGraphicsItem):
    """Une page. Repère local = points PDF de la page affichée (rotation incluse)."""

    def __init__(self, view: "PdfView", index: int, width: float, height: float):
        super().__init__()
        self.view = view
        self.index = index
        self._rect = QRectF(0, 0, width, height)
        self._cache: tuple[float, QRectF, object] | None = None
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption)

    def boundingRect(self) -> QRectF:
        return self._rect

    def drop_cache(self) -> None:
        self._cache = None

    def _image_for(self, scale: float, exposed: QRectF):
        c = self._cache
        if c and abs(c[0] - scale) <= scale * 1e-3 and c[1].contains(exposed):
            return c
        r = self._rect
        if r.width() * r.height() * scale * scale <= MAX_FULL_PIXELS:
            clip = QRectF(r)
        else:
            m = max(exposed.width(), exposed.height()) * 0.5
            clip = exposed.adjusted(-m, -m, m, m).intersected(r)
        pix = self.view.document.render(
            self.index, scale, fitz.Rect(clip.left(), clip.top(), clip.right(), clip.bottom()))
        self._cache = (scale, clip, pixmap_to_qimage(pix))
        self.view._touch(self)
        return self._cache

    def paint(self, painter: QPainter, option, widget=None):
        painter.fillRect(self._rect, Qt.white)
        t = painter.worldTransform()
        dpr = widget.devicePixelRatioF() if widget else 1.0
        scale = math.hypot(t.m11(), t.m12()) * dpr
        exposed = option.exposedRect.intersected(self._rect)
        if not exposed.isEmpty() and self.view.document is not None:
            _, clip, img = self._image_for(scale, exposed)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawImage(clip, img)
        pen = QPen(QColor(0, 0, 0, 70), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(self._rect)


class PdfView(QGraphicsView):
    currentPageChanged = Signal(int)       # index 0-based
    zoomChanged = Signal(float)            # pourcentage de la taille réelle
    placementsChanged = Signal()
    placingChanged = Signal(bool)
    blockResized = Signal(str, float)      # parts, largeur en points
    fileDropped = Signal(str)

    def __init__(self, source: BlockSource, parent=None):
        super().__init__(parent)
        self.source = source
        self.document: PdfDocument | None = None
        self._pages: list[PageItem] = []
        self._tops: list[float] = []
        self._placements: list[PlacementItem] = []
        self._lru: OrderedDict[int, PageItem] = OrderedDict()
        self._scale = 1.0
        self._fit_mode: str | None = FIT_WIDTH
        self._current = -1
        self._placing: str | None = None
        self._ghost: BlockItem | None = None
        self._ghost_size = QSizeF()

        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QFrame.NoFrame)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setAcceptDrops(True)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        bg = self.palette().color(QPalette.Window).darker(118)
        self.setBackgroundBrush(bg)
        self.verticalScrollBar().valueChanged.connect(self._update_current_page)

    # ------------------------------------------------------------------ document

    def set_document(self, document: PdfDocument | None, keep_position: bool = False) -> None:
        page = self._current if keep_position else 0
        self.cancel_placement()
        self.scene().clear()
        self._pages, self._tops, self._placements = [], [], []
        self._lru.clear()
        self._ghost = None
        self.document = document
        self._current = -1
        if document is None:
            self.scene().setSceneRect(QRectF())
            self.viewport().update()
            self.placementsChanged.emit()
            return
        sizes = [document.page_size(i) for i in range(document.page_count)]
        max_w = max(w for w, _ in sizes)
        y = PAGE_GAP
        for i, (w, h) in enumerate(sizes):
            item = PageItem(self, i, w, h)
            item.setPos(-w / 2, y)
            self.scene().addItem(item)
            self._pages.append(item)
            self._tops.append(y)
            y += h + PAGE_GAP
        self.scene().setSceneRect(QRectF(-max_w / 2 - PAGE_GAP, 0, max_w + 2 * PAGE_GAP, y))
        self.placementsChanged.emit()
        self._apply_fit()
        # Le défilement n'est fiable qu'une fois la mise en page appliquée.
        QTimer.singleShot(0, lambda: self.go_to_page(min(page, len(self._pages) - 1)))

    def _touch(self, item: PageItem) -> None:
        self._lru.pop(item.index, None)
        self._lru[item.index] = item
        while len(self._lru) > CACHE_PAGES:
            _, old = self._lru.popitem(last=False)
            old.drop_cache()

    @property
    def page_count(self) -> int:
        return len(self._pages)

    # ------------------------------------------------------------------ zoom

    def _dpi_ratio(self) -> float:
        return self.logicalDpiX() / 72.0  # échelle correspondant à 100 %

    def zoom_percent(self) -> float:
        return self._scale / self._dpi_ratio() * 100

    def _set_scale(self, scale: float, anchor=QGraphicsView.AnchorViewCenter) -> None:
        r = self._dpi_ratio()
        scale = min(max(scale, ZOOM_MIN * r), ZOOM_MAX * r)
        if math.isclose(scale, self._scale, rel_tol=1e-4) and not self.transform().isIdentity():
            return
        self.setTransformationAnchor(anchor)
        self._scale = scale
        self.setTransform(QTransform.fromScale(scale, scale))
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.zoomChanged.emit(self.zoom_percent())
        self._update_current_page()

    def zoom_by(self, factor: float, anchor=QGraphicsView.AnchorViewCenter) -> None:
        self._fit_mode = None
        self._set_scale(self._scale * factor, anchor)

    def set_zoom_percent(self, percent: float) -> None:
        self._fit_mode = None
        self._set_scale(percent / 100 * self._dpi_ratio())

    def set_fit_mode(self, mode: str | None) -> None:
        self._fit_mode = mode
        self._apply_fit()
        if mode == FIT_PAGE and self._current >= 0:
            self.go_to_page(self._current)

    @property
    def fit_mode(self) -> str | None:
        return self._fit_mode

    def _apply_fit(self) -> None:
        if not self._pages or self._fit_mode is None:
            return
        vp = self.viewport().size()
        margin = 2 * PAGE_GAP
        if self._fit_mode == FIT_WIDTH:
            max_w = max(p.boundingRect().width() for p in self._pages)
            # Laisser la place d'une barre de défilement verticale.
            avail = vp.width() - (0 if self.verticalScrollBar().isVisible()
                                  else self.verticalScrollBar().sizeHint().width())
            self._set_scale(avail / (max_w + margin))
        else:
            page = self._pages[max(self._current, 0)].boundingRect()
            self._set_scale(min(vp.width() / (page.width() + margin),
                                vp.height() / (page.height() + margin)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_fit()

    # ------------------------------------------------------------------ navigation

    def current_page(self) -> int:
        return self._current

    def _update_current_page(self) -> None:
        if not self._pages:
            return
        center = self.mapToScene(self.viewport().rect().center()).y()
        idx = max(0, min(bisect.bisect_right(self._tops, center) - 1, len(self._pages) - 1))
        if idx != self._current:
            self._current = idx
            self.currentPageChanged.emit(idx)

    def go_to_page(self, index: int) -> None:
        if not self._pages:
            return
        index = max(0, min(index, len(self._pages) - 1))
        top = self.mapFromScene(QPointF(0, self._tops[index] - PAGE_GAP / 2)).y()
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + top)
        if self._current != index:
            self._current = index
            self.currentPageChanged.emit(index)

    def next_page(self) -> None:
        self.go_to_page(self._current + 1)

    def previous_page(self) -> None:
        self.go_to_page(self._current - 1)

    # ------------------------------------------------------------------ placement

    def is_placing(self) -> bool:
        return self._placing is not None

    def start_placement(self, parts: str, size: QSizeF) -> None:
        """Mode placement : un aperçu suit la souris, un clic le pose."""
        if not self._pages:
            return
        self.cancel_placement()
        self.scene().clearSelection()
        self._placing = parts
        self._ghost_size = QSizeF(size)
        self._ghost = BlockItem(self.source, parts, size)
        self._ghost.setOpacity(0.6)
        self._ghost.setZValue(1000)
        self._ghost.setVisible(False)
        self.scene().addItem(self._ghost)
        self.setDragMode(QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.CrossCursor)
        self.setFocus()
        self.placingChanged.emit(True)
        # Afficher l'aperçu tout de suite si la souris est déjà sur une page.
        pos = self.viewport().mapFromGlobal(self.cursor().pos())
        if self.viewport().rect().contains(pos):
            self._move_ghost(self.mapToScene(pos))

    def cancel_placement(self) -> None:
        if self._placing is None:
            return
        self._placing = None
        if self._ghost is not None:
            self.scene().removeItem(self._ghost)
            self._ghost = None
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.viewport().unsetCursor()
        self.placingChanged.emit(False)

    def _page_at(self, scene_pos: QPointF) -> PageItem | None:
        for item in self._pages:
            if item.sceneBoundingRect().contains(scene_pos):
                return item
        return None

    def _fitted_rect(self, page: PageItem, scene_pos: QPointF) -> QRectF:
        """Rectangle (repère de la page) centré sur le clic et confiné à la page."""
        pr = page.boundingRect()
        w, h = self._ghost_size.width(), self._ghost_size.height()
        if w > pr.width() or h > pr.height():
            k = min(pr.width() / w, pr.height() / h)
            w, h = w * k, h * k
        c = page.mapFromScene(scene_pos)
        x = min(max(c.x() - w / 2, 0.0), pr.width() - w)
        y = min(max(c.y() - h / 2, 0.0), pr.height() - h)
        return QRectF(x, y, w, h)

    def _move_ghost(self, scene_pos: QPointF) -> None:
        page = self._page_at(scene_pos)
        if page is None:
            self._ghost.setVisible(False)
            return
        r = self._fitted_rect(page, scene_pos)
        self._ghost.set_block_size(r.size())
        self._ghost.setPos(page.mapToScene(r.topLeft()))
        self._ghost.setVisible(True)

    def _drop_block(self, scene_pos: QPointF) -> None:
        page = self._page_at(scene_pos)
        if page is None:
            return
        r = self._fitted_rect(page, scene_pos)
        parts = self._placing
        self.cancel_placement()
        item = PlacementItem(self.source, parts, r.size(), page)
        item.setPos(r.topLeft())
        item.changed.connect(self.placementsChanged)
        item.resized.connect(lambda it=item: self.blockResized.emit(it.parts, it.block_size().width()))
        item.removeRequested.connect(self.remove_placement)
        self._placements.append(item)
        item.setSelected(True)
        item.setFocus()
        self.placementsChanged.emit()

    def placements(self):
        return [it.placement() for it in self._placements]

    def has_placements(self) -> bool:
        return bool(self._placements)

    def remove_placement(self, item: PlacementItem) -> None:
        if item in self._placements:
            self._placements.remove(item)
            self.scene().removeItem(item)
            self.placementsChanged.emit()

    def undo_last(self) -> bool:
        if self._placing is not None:
            self.cancel_placement()
            return True
        if not self._placements:
            return False
        self.remove_placement(self._placements[-1])
        return True

    def remove_selected(self) -> None:
        for item in [it for it in self._placements if it.isSelected()]:
            self.remove_placement(item)

    def refresh_blocks(self) -> None:
        """La signature a changé : redessiner et réajuster les blocs posés."""
        for item in list(self._placements):
            if self.source.renderer.effective_parts(item.parts) is None:
                self.remove_placement(item)
            else:
                item.refit()
                item.update()
        self.placementsChanged.emit()

    # ------------------------------------------------------------------ événements

    def mouseMoveEvent(self, event):
        if self._placing is not None:
            self._move_ghost(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self._placing is not None:
            if event.button() == Qt.LeftButton:
                self._drop_block(self.mapToScene(event.position().toPoint()))
            else:
                self.cancel_placement()
            event.accept()
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        if self._ghost is not None:
            self._ghost.setVisible(False)
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            steps = event.angleDelta().y() / 120
            if steps:
                self.zoom_by(1.15 ** steps, QGraphicsView.AnchorUnderMouse)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        selected = [it for it in self._placements if it.isSelected()]
        if key == Qt.Key_Escape:
            if self._placing is not None:
                self.cancel_placement()
            else:
                self.scene().clearSelection()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace) and selected:
            self.remove_selected()
        elif key in (Qt.Key_Return, Qt.Key_Enter) and selected:
            self.scene().clearSelection()  # valide le placement
        elif selected and key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            step = 10.0 if mods & Qt.ShiftModifier else 1.0
            # Comparaisons explicites : selon la version de PySide6, key est un
            # int ou un Qt.Key, qui n'ont pas le même hash (dict inutilisable).
            dx = -step if key == Qt.Key_Left else step if key == Qt.Key_Right else 0.0
            dy = -step if key == Qt.Key_Up else step if key == Qt.Key_Down else 0.0
            for it in selected:
                it.setPos(it.pos() + QPointF(dx, dy))
        elif key in (Qt.Key_PageDown, Qt.Key_Right) and not mods:
            self.next_page()
        elif key in (Qt.Key_PageUp, Qt.Key_Left) and not mods:
            self.previous_page()
        elif key == Qt.Key_Home and not mods:
            self.go_to_page(0)
        elif key == Qt.Key_End and not mods:
            self.go_to_page(len(self._pages) - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # Glisser-déposer d'un fichier PDF
    def _pdf_from(self, event) -> str | None:
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    return url.toLocalFile()
        return None

    def dragEnterEvent(self, event):
        if self._pdf_from(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._pdf_from(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        path = self._pdf_from(event)
        if path:
            event.acceptProposedAction()
            self.fileDropped.emit(str(Path(path)))

    def drawForeground(self, painter: QPainter, rect: QRectF):
        if self._pages:
            return
        painter.save()
        painter.resetTransform()
        painter.setPen(self.palette().color(QPalette.WindowText))
        f = painter.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        painter.setFont(f)
        painter.drawText(self.viewport().rect(), Qt.AlignCenter,
                         "Ouvrez un PDF (Ctrl+O)\nou glissez-le ici")
        painter.restore()
