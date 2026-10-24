"""Construction du bloc de signature et intégration dans les pages PDF.

Le bloc (texte + image manuscrite) est d'abord composé comme une petite page
PDF vectorielle : le texte reste du vrai texte (police incorporée en
sous-ensemble), l'image garde sa transparence (SMask). Cette page est ensuite
posée sur la page cible sous forme de Form XObject (show_pdf_page) : le contenu
d'origine n'est jamais rastérisé ni réécrit.

La même page sert à l'aperçu à l'écran : ce que l'on voit est ce qui est
enregistré.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

import pymupdf as fitz

from .signature import LAYOUT_BESIDE, SignatureProfile, image_aspect

PARTS_BOTH = "both"
PARTS_TEXT = "text"
PARTS_IMAGE = "image"

IMAGE_HEIGHT = 48.0  # hauteur « naturelle » de la signature manuscrite, en points
PADDING = 2.0


@dataclass(frozen=True)
class Placement:
    """Un bloc posé sur une page.

    rect est exprimé en points, dans le repère de la page *telle qu'affichée*
    (rotation appliquée, origine en haut à gauche de la CropBox) — c'est-à-dire
    exactement le repère de page.rect / get_pixmap().
    """
    page: int
    rect: tuple[float, float, float, float]
    parts: str = PARTS_BOTH


class BlockRenderer:
    """Compose (et met en cache) le bloc de signature pour un profil donné."""

    def __init__(self, profile: SignatureProfile):
        self.set_profile(profile)

    def set_profile(self, profile: SignatureProfile) -> None:
        self.profile = profile
        self._cache: dict[tuple[str, _dt.date], fitz.Document] = {}
        self._font: fitz.Font | None = None
        self._aspect = image_aspect(profile.image_png) if profile.has_image else 1.0

    # -- contenu disponible ---------------------------------------------------

    def effective_parts(self, parts: str) -> str | None:
        """Éléments réellement affichables (None si le bloc serait vide)."""
        t, i = self.profile.has_text, self.profile.has_image
        if parts == PARTS_BOTH:
            return PARTS_BOTH if (t and i) else PARTS_TEXT if t else PARTS_IMAGE if i else None
        if parts == PARTS_TEXT:
            return PARTS_TEXT if t else None
        return PARTS_IMAGE if i else None

    # -- géométrie ------------------------------------------------------------

    def font(self) -> fitz.Font:
        if self._font is None:
            try:
                self._font = (fitz.Font(fontfile=self.profile.font_file)
                              if self.profile.font_file else fitz.Font("helv"))
            except Exception:
                self._font = fitz.Font("helv")
        return self._font

    def _layout(self, parts: str, today: _dt.date):
        p = self.profile
        show_text = parts in (PARTS_BOTH, PARTS_TEXT) and p.has_text
        show_img = parts in (PARTS_BOTH, PARTS_IMAGE) and p.has_image
        font, fs = self.font(), float(p.font_size)

        lines = p.expanded_text(today).split("\n") if show_text else []
        lh = fs * 1.2
        tw = max((font.text_length(line, fontsize=fs) for line in lines), default=0.0)
        th = lh * len(lines)
        ih = IMAGE_HEIGHT if show_img else 0.0
        iw = ih * self._aspect

        text_xy = (0.0, 0.0)
        img_xy = (0.0, 0.0)
        if lines and show_img:
            if p.layout == LAYOUT_BESIDE:
                gap = fs * 0.8
                w, h = tw + gap + iw, max(th, ih)
                text_xy, img_xy = (0.0, (h - th) / 2), (tw + gap, (h - ih) / 2)
            else:
                gap = fs * 0.3
                w, h = max(tw, iw), th + gap + ih
                img_xy = (0.0, th + gap)
        elif lines:
            w, h = tw, th
        else:
            w, h = iw, ih

        return {
            "size": (w + 2 * PADDING, h + 2 * PADDING),
            "lines": lines, "lh": lh, "fs": fs,
            "text_xy": (text_xy[0] + PADDING, text_xy[1] + PADDING),
            "img_rect": (fitz.Rect(img_xy[0], img_xy[1], img_xy[0] + iw, img_xy[1] + ih)
                         + (PADDING, PADDING, PADDING, PADDING)) if show_img else None,
        }

    def natural_size(self, parts: str) -> tuple[float, float]:
        r = self.block_doc(parts)[0].rect
        return r.width, r.height

    def aspect(self, parts: str) -> float:
        w, h = self.natural_size(parts)
        return w / h if h else 1.0

    # -- composition ----------------------------------------------------------

    def block_doc(self, parts: str, today: _dt.date | None = None) -> fitz.Document:
        """Document d'une page contenant le bloc, à sa taille naturelle."""
        today = today or _dt.date.today()
        key = (parts, today)
        doc = self._cache.get(key)
        if doc is not None:
            return doc

        lay = self._layout(parts, today)
        w, h = lay["size"]
        doc = fitz.open()
        page = doc.new_page(width=max(w, 1.0), height=max(h, 1.0))

        if lay["lines"]:
            font, fs = self.font(), lay["fs"]
            writer = fitz.TextWriter(page.rect, color=self.profile.rgb())
            x, y = lay["text_xy"]
            for i, line in enumerate(lay["lines"]):
                if line:
                    writer.append((x, y + i * lay["lh"] + fs * font.ascender), line,
                                  font=font, fontsize=fs)
            writer.write_text(page)
        if lay["img_rect"] is not None:
            page.insert_image(lay["img_rect"], stream=self.profile.image_png,
                              keep_proportion=True)
        try:
            doc.subset_fonts()  # n'incorporer que les glyphes utilisés
        except Exception:
            pass  # police incorporée en entier : plus lourd mais correct
        if lay["lines"]:
            _fix_space_tounicode(doc, self.font().has_glyph(0x20))
        self._cache[key] = doc
        return doc

    def render(self, parts: str, px_width: int) -> fitz.Pixmap:
        """Aperçu RGBA (fond transparent, alpha prémultiplié) de px_width pixels."""
        page = self.block_doc(parts)[0]
        scale = max(px_width, 1) / page.rect.width
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)


def _fix_space_tounicode(doc: fitz.Document, space_gid: int) -> None:
    """Fait correspondre le glyphe espace à U+0020 dans la table ToUnicode.

    Beaucoup de polices (Arial, Times…) utilisent le même glyphe pour l'espace
    et l'espace insécable ; MuPDF retient alors U+00A0, ce qui gêne la
    recherche et le copier-coller du texte dans les autres lecteurs.
    """
    if not space_gid:
        return
    pattern = re.compile(rb"(<%04X>\s*)<00A0>" % space_gid, re.IGNORECASE)
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        stream = doc.xref_stream(xref)
        if b"beginbfchar" in stream:
            fixed = pattern.sub(rb"\g<1><0020>", stream)
            if fixed != stream:
                doc.update_stream(xref, fixed)


def insert_block(page: fitz.Page, rect: fitz.Rect, block: fitz.Document) -> None:
    """Pose le bloc sur la page, rect étant exprimé dans le repère affiché.

    show_pdf_page attend des coordonnées non tournées et se trompe lorsque la
    page combine rotation et CropBox décalée : on calcule donc le rectangle
    non tourné, on neutralise /Rotate le temps de l'insertion, puis on le
    rétablit, en tournant le bloc du même angle que la page.
    """
    rotation = page.rotation
    target = fitz.Rect(rect) * page.derotation_matrix
    if not page.is_wrapped:
        page.wrap_contents()  # isole le contenu existant (q/Q équilibrés)
    if rotation:
        page.set_rotation(0)
    try:
        page.show_pdf_page(target, block, 0, keep_proportion=True,
                           overlay=True, rotate=rotation)
    finally:
        if rotation:
            page.set_rotation(rotation)


def apply_placements(doc: fitz.Document, placements: list[Placement],
                     renderer: BlockRenderer) -> None:
    for pl in placements:
        parts = renderer.effective_parts(pl.parts)
        if parts is None:
            continue
        insert_block(doc[pl.page], fitz.Rect(pl.rect), renderer.block_doc(parts))
