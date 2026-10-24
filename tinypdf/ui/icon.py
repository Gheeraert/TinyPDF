"""Icône de l'application, dessinée par programme (aucun fichier à distribuer).

python -m tinypdf.ui.icon assets/tinypdf.ico  -> exporte l'icône pour PyInstaller
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap


def _draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64
    # feuille
    p.setPen(QPen(QColor(90, 90, 90), max(1.0, 1.5 * s)))
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(QRectF(12 * s, 4 * s, 40 * s, 56 * s), 3 * s, 3 * s)
    # lignes de texte
    p.setPen(QPen(QColor(170, 170, 170), max(1.0, 2.5 * s), Qt.SolidLine, Qt.RoundCap))
    for y in (14, 21, 28):
        p.drawLine(QPointF(19 * s, y * s), QPointF(45 * s, y * s))
    # paraphe bleu
    path = QPainterPath(QPointF(16 * s, 47 * s))
    path.cubicTo(QPointF(22 * s, 30 * s), QPointF(27 * s, 56 * s), QPointF(32 * s, 44 * s))
    path.cubicTo(QPointF(36 * s, 34 * s), QPointF(38 * s, 52 * s), QPointF(48 * s, 42 * s))
    p.setPen(QPen(QColor(20, 70, 190), max(1.5, 3.2 * s), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.end()
    return img


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(_draw(size)))
    return icon


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication(sys.argv)
    out = sys.argv[1] if len(sys.argv) > 1 else "tinypdf.ico"
    if not _draw(256).save(out):
        sys.exit(f"Échec de l'écriture de {out}")
    print(out)
