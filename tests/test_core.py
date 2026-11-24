"""Tests du cœur (sans interface) : python -m unittest discover -s tests"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TINYPDF_DATA_DIR"] = tempfile.mkdtemp(prefix="tinypdf-test-")

import pymupdf as fitz  # noqa: E402

from tinypdf.config import Settings  # noqa: E402
from tinypdf.document import PdfDocument, PdfError  # noqa: E402
from tinypdf.signature import (LAYOUT_BESIDE, SignatureProfile, SignatureStore,  # noqa: E402
                               clean_signature_image, expand_placeholders)
from tinypdf.stamping import (PARTS_BOTH, PARTS_IMAGE, PARTS_TEXT,  # noqa: E402
                              BlockRenderer, Placement)


def make_signature_png() -> bytes:
    """Signature rouge vif sur fond blanc opaque (comme un scan)."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 100), False)
    pix.clear_with(255)
    for x in range(40, 260):
        for dy in range(-3, 4):
            pix.set_pixel(x, 50 + dy + (x % 20 - 10) // 3, (230, 0, 0))
    return pix.tobytes("png")


def red_bbox(page: fitz.Page, zoom: float = 1.0):
    """Boîte englobante (en points affichés) des pixels rouges de la page."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    s, n, w = pix.samples, pix.n, pix.width
    xs, ys = [], []
    for i in range(0, len(s), n):
        if s[i] > 180 and s[i + 1] < 90 and s[i + 2] < 90:
            p = i // n
            xs.append(p % w)
            ys.append(p // w)
    if not xs:
        return None
    return (min(xs) / zoom, min(ys) / zoom, (max(xs) + 1) / zoom, (max(ys) + 1) / zoom)


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="tinypdf-pdf-"))
        png = clean_signature_image(make_signature_png())
        cls.profile = SignatureProfile(text="Lu et approuvé, le {date}", color="#000000",
                                       image_png=png)
        cls.renderer = BlockRenderer(cls.profile)

    def write(self, doc: fitz.Document, name: str, **kw) -> Path:
        p = self.tmp / name
        doc.save(p, **kw)
        doc.close()
        return p

    # -- signature ------------------------------------------------------------

    def test_placeholders(self):
        d = dt.date(2026, 3, 5)
        self.assertEqual(expand_placeholders("le {date}", d), "le 05/03/2026")
        self.assertEqual(expand_placeholders("le {date_longue}", d), "le 5 mars 2026")

    def test_clean_image_transparent_and_cropped(self):
        pix = fitz.Pixmap(self.profile.image_png)
        self.assertTrue(pix.alpha)
        self.assertLess(pix.width, 300)  # recadrée sur le tracé
        self.assertEqual(pix.pixel(0, 0)[3], 0)  # coin transparent

    def test_clean_image_rejects_blank(self):
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), False)
        pix.clear_with(255)
        with self.assertRaises(ValueError):
            clean_signature_image(pix.tobytes("png"))

    def test_store_roundtrip(self):
        folder = Path(tempfile.mkdtemp())
        store = SignatureStore(Settings(folder / "s.json"), folder)
        self.assertFalse(store.is_configured())
        prof = SignatureProfile(text="Bon pour accord", font_size=14, layout=LAYOUT_BESIDE,
                                image_png=self.profile.image_png)
        store.save(prof)
        again = SignatureStore(Settings(folder / "s.json"), folder).load()
        self.assertEqual(again.text, "Bon pour accord")
        self.assertEqual(again.layout, LAYOUT_BESIDE)
        self.assertEqual(again.image_png, prof.image_png)

    def test_parts_and_aspect(self):
        r = self.renderer
        w_both, h_both = r.natural_size(PARTS_BOTH)
        w_img, h_img = r.natural_size(PARTS_IMAGE)
        _, h_txt = r.natural_size(PARTS_TEXT)
        self.assertGreater(h_both, h_img)
        self.assertGreater(h_img, h_txt)
        text_only = BlockRenderer(SignatureProfile(text="x", image_png=None))
        self.assertEqual(text_only.effective_parts(PARTS_BOTH), PARTS_TEXT)
        self.assertIsNone(text_only.effective_parts(PARTS_IMAGE))

    def test_preview_has_transparent_background(self):
        pix = self.renderer.render(PARTS_BOTH, 300)
        self.assertTrue(pix.alpha)
        self.assertEqual(pix.width, 300)
        self.assertEqual(pix.pixel(pix.width - 1, 0)[3], 0)

    # -- intégration dans les PDF --------------------------------------------

    def sign_and_reload(self, path: Path, placements, out_name: str) -> fitz.Document:
        pdf = PdfDocument.open(path, lambda retry: "secret")
        out = pdf.save(self.tmp / out_name, placements, self.renderer)
        pdf.close()
        doc = fitz.open(out)
        if doc.needs_pass:
            doc.authenticate("secret")
        return doc

    def test_multipage_text_stays_text_and_image_transparent(self):
        src = fitz.open()
        for i in range(3):
            src.new_page().insert_text((72, 72), f"Page {i + 1}")
        path = self.write(src, "multi.pdf")
        original = path.read_bytes()

        rect = (100, 600, 300, 700)
        doc = self.sign_and_reload(path, [Placement(1, rect)], "multi_signe.pdf")
        self.assertEqual(doc.page_count, 3)
        self.assertEqual(path.read_bytes(), original, "l'original ne doit pas changer")
        self.assertIn("Lu et approuvé", doc[1].get_text())
        self.assertNotIn("Lu et approuvé", doc[0].get_text() + doc[2].get_text())
        # Pas de rastérisation de la page : le texte d'origine est toujours là.
        self.assertIn("Page 2", doc[1].get_text())
        # L'image manuscrite est incorporée avec un masque alpha.
        xobjs = [x for x in range(1, doc.xref_length())
                 if doc.xref_get_key(x, "Subtype")[1] == "/Image"]
        self.assertTrue(any(doc.xref_get_key(x, "SMask")[0] == "xref" for x in xobjs))
        # Enregistrement incrémental : le fichier commence par l'original intact.
        self.assertTrue((self.tmp / "multi_signe.pdf").read_bytes().startswith(original))

    def test_exact_position_rotation_and_cropbox(self):
        cases = [(0, None), (90, None), (180, None), (270, None),
                 (90, (40, 70, 560, 800)), (270, (30, 20, 500, 760)), (180, (10, 10, 400, 600))]
        for rot, crop in cases:
            with self.subTest(rotation=rot, crop=crop):
                src = fitz.open()
                page = src.new_page(width=595, height=842)
                page.insert_text((60, 60), "Contenu")
                if crop:
                    page.set_cropbox(fitz.Rect(crop))
                page.set_rotation(rot)
                pw, ph = page.rect.width, page.rect.height
                path = self.write(src, f"rot{rot}_{bool(crop)}.pdf")

                renderer = BlockRenderer(SignatureProfile(text="", image_png=self.profile.image_png))
                aspect = renderer.aspect(PARTS_IMAGE)
                w = 150.0
                x0, y0 = pw * 0.5, ph * 0.6
                rect = (x0, y0, x0 + w, y0 + w / aspect)
                pdf = PdfDocument.open(path)
                out = pdf.save(self.tmp / f"rot{rot}_{bool(crop)}_s.pdf",
                               [Placement(0, rect, PARTS_IMAGE)], renderer)
                bbox = red_bbox(fitz.open(out)[0], zoom=2)
                self.assertIsNotNone(bbox)
                # Le tracé (avec sa petite marge transparente) tient dans le rectangle,
                # et l'orientation est conservée (plus large que haut).
                tol = 3
                self.assertGreaterEqual(bbox[0], rect[0] - tol)
                self.assertGreaterEqual(bbox[1], rect[1] - tol)
                self.assertLessEqual(bbox[2], rect[2] + tol)
                self.assertLessEqual(bbox[3], rect[3] + tol)
                self.assertGreater(bbox[2] - bbox[0], (rect[2] - rect[0]) * 0.8)
                self.assertGreater(bbox[2] - bbox[0], bbox[3] - bbox[1])

    def test_landscape_and_mixed_sizes(self):
        src = fitz.open()
        src.new_page(width=595, height=842)
        src.new_page(width=842, height=595)
        src.new_page(width=300, height=400)
        path = self.write(src, "mixed.pdf")
        renderer = BlockRenderer(SignatureProfile(text="", image_png=self.profile.image_png))
        a = renderer.aspect(PARTS_IMAGE)
        pls = [Placement(1, (600, 400, 750, 400 + 150 / a), PARTS_IMAGE),
               Placement(2, (20, 20, 120, 20 + 100 / a), PARTS_IMAGE)]
        pdf = PdfDocument.open(path)
        doc = fitz.open(pdf.save(self.tmp / "mixed_s.pdf", pls, renderer))
        self.assertIsNone(red_bbox(doc[0]))
        for pl in pls:
            b = red_bbox(doc[pl.page], 2)
            self.assertTrue(pl.rect[0] - 3 <= b[0] and b[2] <= pl.rect[2] + 3)

    def test_form_fields_preserved(self):
        src = fitz.open()
        page = src.new_page()
        w = fitz.Widget()
        w.field_name, w.field_type = "nom", fitz.PDF_WIDGET_TYPE_TEXT
        w.rect, w.field_value = fitz.Rect(72, 100, 300, 120), "Dupont"
        page.add_widget(w)
        path = self.write(src, "form.pdf")
        doc = self.sign_and_reload(path, [Placement(0, (72, 500, 272, 580))], "form_s.pdf")
        fields = {w.field_name: w.field_value for w in doc[0].widgets()}
        self.assertEqual(fields.get("nom"), "Dupont")

    def test_scanned_page(self):
        src = fitz.open()
        page = src.new_page()
        scan = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 600, 800), False)
        scan.clear_with(240)
        page.insert_image(page.rect, pixmap=scan)
        path = self.write(src, "scan.pdf")
        doc = self.sign_and_reload(path, [Placement(0, (300, 650, 500, 750))], "scan_s.pdf")
        self.assertIn("Lu et approuvé", doc[0].get_text())

    def test_encrypted_document(self):
        src = fitz.open()
        src.new_page().insert_text((72, 72), "Confidentiel")
        perm = fitz.PDF_PERM_PRINT | fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ANNOTATE
        path = self.write(src, "enc.pdf", encryption=fitz.PDF_ENCRYPT_AES_256,
                          owner_pw="owner", user_pw="secret", permissions=perm)
        with self.assertRaises(PdfError):
            PdfDocument.open(path, lambda retry: None)
        doc = self.sign_and_reload(path, [Placement(0, (72, 500, 272, 580))], "enc_s.pdf")
        self.assertIn("Lu et approuvé", doc[0].get_text())
        self.assertIn("Confidentiel", doc[0].get_text())
        # Le document signé reste chiffré.
        self.assertTrue(fitz.open(self.tmp / "enc_s.pdf").needs_pass)

    def test_signed_flag_detected(self):
        src = fitz.open()
        src.new_page()
        cat = src.pdf_catalog()
        src.xref_set_key(cat, "AcroForm", "<< /Fields [] /SigFlags 3 >>")
        path = self.write(src, "signed.pdf")
        self.assertTrue(PdfDocument.open(path).has_digital_signature())
        fresh = fitz.open()
        fresh.new_page()
        self.assertFalse(PdfDocument.open(self.write(fresh, "plain.pdf")).has_digital_signature())

    def test_invalid_file(self):
        bad = self.tmp / "bad.pdf"
        bad.write_bytes(b"not a pdf at all")
        with self.assertRaises(PdfError):
            PdfDocument.open(bad)

    # -- organisation des pages ------------------------------------------------

    def test_rotate_page(self):
        src = fitz.open()
        src.new_page(width=595, height=842).insert_text((60, 60), "Page 1")
        src.new_page(width=595, height=842).insert_text((60, 60), "Page 2")
        path = self.write(src, "rotate.pdf")
        pdf = PdfDocument.open(path)
        self.assertEqual(pdf.page_size(0), (595, 842))
        pdf.rotate_page(0, 90)
        self.assertEqual(pdf.page_size(0), (842, 595))
        self.assertTrue(pdf.is_edited)
        out = pdf.save(self.tmp / "rotate_s.pdf", [], self.renderer)
        doc = fitz.open(out)
        self.assertEqual(doc[0].rotation, 90)
        self.assertEqual(doc[1].rotation, 0)
        self.assertEqual(doc.page_count, 2)

    def test_rotate_all_pages(self):
        src = fitz.open()
        for _ in range(2):
            src.new_page(width=595, height=842)
        path = self.write(src, "rotate_all.pdf")
        pdf = PdfDocument.open(path)
        pdf.rotate_all_pages(90)
        out = pdf.save(self.tmp / "rotate_all_s.pdf", [], self.renderer)
        doc = fitz.open(out)
        self.assertTrue(all(p.rotation == 90 for p in doc))

    def test_delete_page(self):
        src = fitz.open()
        for i in range(3):
            src.new_page().insert_text((72, 72), f"Page {i + 1}")
        path = self.write(src, "delete.pdf")
        pdf = PdfDocument.open(path)
        pdf.delete_page(1)
        self.assertEqual(pdf.page_count, 2)
        out = pdf.save(self.tmp / "delete_s.pdf", [], self.renderer)
        doc = fitz.open(out)
        self.assertEqual(doc.page_count, 2)
        self.assertIn("Page 1", doc[0].get_text())
        self.assertIn("Page 3", doc[1].get_text())
        # L'original n'est pas touché.
        self.assertEqual(fitz.open(path).page_count, 3)

    def test_delete_last_remaining_page_refused(self):
        src = fitz.open()
        src.new_page()
        path = self.write(src, "single.pdf")
        pdf = PdfDocument.open(path)
        with self.assertRaises(PdfError):
            pdf.delete_page(0)

    def test_insert_pdf(self):
        a = fitz.open()
        a.new_page().insert_text((72, 72), "A1")
        a.new_page().insert_text((72, 72), "A2")
        path_a = self.write(a, "a.pdf")

        b = fitz.open()
        b.new_page().insert_text((72, 72), "B1")
        path_b = self.write(b, "b.pdf")

        pdf = PdfDocument.open(path_a)
        n = pdf.insert_pdf(0, path_b)
        self.assertEqual(n, 1)
        self.assertEqual(pdf.page_count, 3)
        out = pdf.save(self.tmp / "insert_s.pdf", [], self.renderer)
        doc = fitz.open(out)
        self.assertEqual(doc.page_count, 3)
        self.assertIn("A1", doc[0].get_text())
        self.assertIn("B1", doc[1].get_text())
        self.assertIn("A2", doc[2].get_text())

    def test_save_overwrite_original(self):
        src = fitz.open()
        src.new_page()
        path = self.write(src, "inplace.pdf")
        pdf = PdfDocument.open(path)
        pdf.save(path, [Placement(0, (72, 72, 272, 152))], self.renderer)
        self.assertIn("Lu et approuvé", fitz.open(path)[0].get_text())
        self.assertEqual([p for p in self.tmp.glob(".tinypdf-*")], [])


if __name__ == "__main__":
    unittest.main()
