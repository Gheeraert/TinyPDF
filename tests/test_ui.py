"""Parcours complet dans l'interface (sans affichage) : python -m unittest discover -s tests"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TINYPDF_DATA_DIR"] = tempfile.mkdtemp(prefix="tinypdf-uitest-")

import pymupdf as fitz  # noqa: E402
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tinypdf.config import Settings  # noqa: E402
from tinypdf.signature import SignatureProfile, SignatureStore, clean_signature_image  # noqa: E402
from tinypdf.stamping import PARTS_BOTH  # noqa: E402
from tinypdf.ui.main_window import MainWindow  # noqa: E402

from test_core import make_signature_png, red_bbox  # noqa: E402

app = QApplication.instance() or QApplication([])


class UiFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tinypdf-ui-"))
        settings = Settings(self.tmp / "settings.json")
        store = SignatureStore(settings, self.tmp)
        store.save(SignatureProfile(text="Lu et approuvé, le {date}",
                                    image_png=clean_signature_image(make_signature_png())))
        self.win = MainWindow(settings, store)
        self.win.resize(900, 900)
        self.win.show()
        QTest.qWaitForWindowExposed(self.win)

    def tearDown(self):
        self.win.view.set_document(None)
        self.win.pdf = None
        self.win.close()

    def make_pdf(self, rotation=0) -> Path:
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 72), f"Page {i + 1}")
        doc[1].set_rotation(rotation)
        path = self.tmp / "contrat.pdf"
        doc.save(path)
        return path

    def click_on_page(self, page_index: int, page_point: QPointF):
        view = self.win.view
        view.go_to_page(page_index)
        app.processEvents()
        item = view._pages[page_index]
        vp = view.mapFromScene(item.mapToScene(page_point))
        QTest.mouseMove(view.viewport(), vp)
        QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, vp)
        app.processEvents()

    def run_flow(self, rotation: int, zoom: float | None):
        path = self.make_pdf(rotation)
        self.assertTrue(self.win.open_file(str(path)))
        app.processEvents()
        if zoom:
            self.win._set_zoom(zoom)
        app.processEvents()

        self.win.sign(PARTS_BOTH)
        self.assertTrue(self.win.view.is_placing())
        target = QPointF(300, 500)
        self.click_on_page(1, target)
        self.assertFalse(self.win.view.is_placing())
        pls = self.win.view.placements()
        self.assertEqual(len(pls), 1)
        pl = pls[0]
        self.assertEqual(pl.page, 1)
        cx, cy = (pl.rect[0] + pl.rect[2]) / 2, (pl.rect[1] + pl.rect[3]) / 2
        self.assertAlmostEqual(cx, target.x(), delta=2)
        self.assertAlmostEqual(cy, target.y(), delta=2)
        self.assertIn("●", self.win.windowTitle())

        out = self.tmp / "contrat_signe.pdf"
        self.assertTrue(self.win._write(out))
        self.assertFalse(self.win.view.has_placements())
        doc = fitz.open(out)
        self.assertIn("Lu et approuvé", doc[1].get_text())
        bbox = red_bbox(doc[1], 2)
        self.assertTrue(pl.rect[0] - 3 <= bbox[0] and bbox[2] <= pl.rect[2] + 3, (bbox, pl.rect))
        self.assertTrue(pl.rect[1] - 3 <= bbox[1] and bbox[3] <= pl.rect[3] + 3, (bbox, pl.rect))
        self.assertEqual(self.win.pdf.path, out)

    def test_flow_fit_width(self):
        self.run_flow(0, None)

    def test_flow_zoomed_rotated(self):
        self.run_flow(90, 180)

    def test_flow_landscape_zoomed_out(self):
        self.run_flow(270, 60)

    def test_undo_and_keyboard(self):
        self.win.open_file(str(self.make_pdf()))
        app.processEvents()
        self.win.sign()
        self.click_on_page(0, QPointF(200, 200))
        before = self.win.view.placements()[0].rect
        QTest.keyClick(self.win.view, Qt.Key_Right, Qt.ShiftModifier)
        after = self.win.view.placements()[0].rect
        self.assertAlmostEqual(after[0] - before[0], 10, delta=0.01)
        self.win.undo()
        self.assertFalse(self.win.view.has_placements())
        self.win.sign()
        QTest.keyClick(self.win.view, Qt.Key_Escape)
        self.assertFalse(self.win.view.is_placing())

    def test_screenshot(self):
        self.win.open_file(str(self.make_pdf()))
        app.processEvents()
        self.win.sign()
        self.click_on_page(0, QPointF(380, 330))
        self.win.view.go_to_page(0)
        app.processEvents()
        out = Path(os.environ.get("TINYPDF_SCREENSHOT", self.tmp / "screen.png"))
        self.assertTrue(self.win.grab().save(str(out)))
        self.win.view.undo_last()


if __name__ == "__main__":
    unittest.main()
