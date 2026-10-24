"""Bloc de signature affiché sur une page : aperçu, déplacement, redimensionnement."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QMenu

from ..stamping import BlockRenderer, Placement
from .qt_convert import pixmap_to_qimage


class BlockSource:
    """Renderer partagé par tous les blocs (remplacé quand la signature change)."""

    def __init__(self, renderer: BlockRenderer):
        self.renderer = renderer


def _view_scale(item: QGraphicsItem) -> float:
    scene = item.scene()
    if scene and scene.views():
        t = scene.views()[0].transform()
        return math.hypot(t.m11(), t.m12()) or 1.0
    return 1.0


class BlockItem(QGraphicsObject):
    """Aperçu du bloc, rendu par PyMuPDF à la résolution d'affichage."""

    def __init__(self, source: BlockSource, parts: str, size: QSizeF, parent=None):
        super().__init__(parent)
        self.source = source
        self.parts = parts
        self._size = QSizeF(size)
        self._image = None
        self._image_key = None

    def boundingRect(self) -> QRectF:
        return QRectF(QPointF(0, 0), self._size)

    def block_size(self) -> QSizeF:
        return QSizeF(self._size)

    def set_block_size(self, size: QSizeF) -> None:
        self.prepareGeometryChange()
        self._size = QSizeF(size)
        self.update()

    def aspect(self) -> float:
        return self.source.renderer.aspect(self.parts)

    def paint(self, painter, option, widget=None):
        t = painter.worldTransform()
        dpr = widget.devicePixelRatioF() if widget else 1.0
        px = max(1, round(self._size.width() * math.hypot(t.m11(), t.m12()) * dpr))
        key = (id(self.source.renderer), self.parts, px)
        if key != self._image_key:
            self._image = pixmap_to_qimage(self.source.renderer.render(self.parts, px))
            self._image_key = key
        painter.setRenderHint(painter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.boundingRect(), self._image)


class PlacementItem(BlockItem):
    """Bloc posé sur une page (enfant de l'élément de page)."""

    changed = Signal()
    resized = Signal()
    removeRequested = Signal(object)

    HANDLE_PX = 12
    MIN_WIDTH = 20.0

    def __init__(self, source: BlockSource, parts: str, size: QSizeF, page_item):
        super().__init__(source, parts, size, page_item)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemIsFocusable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.SizeAllCursor)
        self._resizing = False

    # -- modèle ---------------------------------------------------------------

    def placement(self) -> Placement:
        p, s = self.pos(), self._size
        return Placement(self.parentItem().index,
                         (p.x(), p.y(), p.x() + s.width(), p.y() + s.height()), self.parts)

    def refit(self) -> None:
        """Recalcule la hauteur après modification de la signature."""
        w = self._size.width()
        self.set_block_size(QSizeF(w, w / self.aspect()))
        self._clamp_size()
        self.setPos(self.pos())  # ré-applique le confinement dans la page

    def _page_rect(self) -> QRectF:
        return self.parentItem().boundingRect()

    def _clamp_size(self) -> None:
        page = self._page_rect()
        a = self.aspect()
        max_w = min(page.width(), page.height() * a)
        if self._size.width() > max_w:
            self.set_block_size(QSizeF(max_w, max_w / a))

    # -- géométrie ------------------------------------------------------------

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self.parentItem() is not None:
            page = self._page_rect()
            x = min(max(value.x(), 0.0), page.width() - self._size.width())
            y = min(max(value.y(), 0.0), page.height() - self._size.height())
            return QPointF(max(x, 0.0), max(y, 0.0))
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.changed.emit()
        return super().itemChange(change, value)

    def _handle_rect(self) -> QRectF:
        s = self.HANDLE_PX / _view_scale(self)
        return QRectF(self._size.width() - s, self._size.height() - s, s, s)

    def _in_handle(self, pos: QPointF) -> bool:
        return self.isSelected() and self._handle_rect().adjusted(-2, -2, 2, 2).contains(pos)

    def hoverMoveEvent(self, event):
        self.setCursor(Qt.SizeFDiagCursor if self._in_handle(event.pos()) else Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._in_handle(event.pos()):
            self._resizing = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._resizing:
            super().mouseMoveEvent(event)
            return
        page, a, p = self._page_rect(), self.aspect(), self.pos()
        w = max(event.pos().x(), event.pos().y() * a, self.MIN_WIDTH)
        w = min(w, page.width() - p.x(), (page.height() - p.y()) * a)
        self.set_block_size(QSizeF(w, w / a))

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            self.changed.emit()
            self.resized.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu()
        remove = menu.addAction("Retirer ce bloc")
        if menu.exec(event.screenPos()) is remove:
            self.removeRequested.emit(self)

    # -- dessin ---------------------------------------------------------------

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not self.isSelected():
            return
        accent = QColor(0, 120, 215)
        pen = QPen(accent, 1, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.boundingRect())
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRect(self._handle_rect())
