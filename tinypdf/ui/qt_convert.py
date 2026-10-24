"""Conversions entre PyMuPDF et Qt."""

from __future__ import annotations

import pymupdf as fitz
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage


def pixmap_to_qimage(pix: fitz.Pixmap) -> QImage:
    # MuPDF produit un alpha prémultiplié.
    fmt = QImage.Format_RGBA8888_Premultiplied if pix.alpha else QImage.Format_RGB888
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
    return img.copy()  # détache l'image du tampon de PyMuPDF


def qimage_to_png(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())
