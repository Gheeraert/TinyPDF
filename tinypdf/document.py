"""Chargement, rendu et enregistrement des documents PDF.

Principes de sécurité :
- le fichier est lu une fois en mémoire : il n'est ni verrouillé ni modifié
  pendant la consultation ;
- les signatures sont appliquées à une copie fraîche du document, au moment de
  l'enregistrement ;
- l'enregistrement est incrémental quand c'est possible (le fichier d'origine
  est recopié à l'identique, les ajouts sont écrits à la suite), ce qui
  préserve la structure et les éventuelles signatures électroniques ; une
  page tournée, supprimée ou insérée force une reconstruction complète ;
- l'écriture passe par un fichier temporaire remplacé atomiquement : en cas
  d'échec, la cible n'est jamais laissée à moitié écrite.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from .stamping import BlockRenderer, Placement, apply_placements


# Attention (PyMuPDF 1.28) : lire doc.needs_pass APRÈS doc.authenticate() casse
# le déchiffrement du document. On ne le consulte donc qu'avant authentification.


@dataclass
class PageSlot:
    """Une page du document affiché : d'où elle vient, et sa rotation ajoutée.

    source == 0 désigne le document d'origine ; un autre entier désigne un PDF
    inséré (clé dans PdfDocument._extra_sources). base_rotation est la rotation
    de la page au moment où elle est entrée dans le plan ; rotation_delta est
    ce que l'utilisateur y a ajouté depuis (multiple de 90).
    """
    source: int
    index: int
    base_rotation: int
    rotation_delta: int = 0


class PdfError(Exception):
    """Erreur présentable telle quelle à l'utilisateur."""


class PdfDocument:
    def __init__(self, path: Path, data: bytes, doc: fitz.Document, password: str | None):
        self.path = path
        self._data = data
        self.doc = doc
        self._password = password
        self._extra_docs: dict[int, fitz.Document] = {}
        self._extra_sources: dict[int, bytes] = {}
        self._next_source = 1
        self._plan: list[PageSlot] = [
            PageSlot(source=0, index=i, base_rotation=doc[i].rotation)
            for i in range(doc.page_count)
        ]

    # -- ouverture ------------------------------------------------------------

    @classmethod
    def open(cls, path: str | os.PathLike,
             ask_password: Callable[[bool], str | None] | None = None) -> "PdfDocument":
        """Ouvre un PDF. ask_password(retry) renvoie un mot de passe ou None."""
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PdfError(f"Impossible de lire le fichier :\n{path}\n\n{exc.strerror or exc}")
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            raise PdfError(f"Ce fichier n'est pas un PDF valide ou il est endommagé :\n{path.name}")
        if not doc.is_pdf:
            raise PdfError(f"Ce fichier n'est pas un PDF :\n{path.name}")

        password = None
        if doc.needs_pass:
            retry = False
            while True:
                password = ask_password(retry) if ask_password else None
                if password is None:
                    raise PdfError("Document protégé par mot de passe : ouverture annulée.")
                if doc.authenticate(password):
                    break
                retry = True
        if doc.page_count == 0:
            raise PdfError("Ce document ne contient aucune page.")
        return cls(path, data, doc, password)

    def close(self) -> None:
        self.doc.close()
        for extra in self._extra_docs.values():
            extra.close()

    # -- informations ---------------------------------------------------------

    @property
    def password(self) -> str | None:
        return self._password

    @property
    def page_count(self) -> int:
        return len(self._plan)

    @property
    def is_edited(self) -> bool:
        """True si l'ordre, le nombre ou la rotation des pages a changé."""
        if self._extra_docs or len(self._plan) != self.doc.page_count:
            return True
        return any(slot.source != 0 or slot.index != i or slot.rotation_delta % 360
                   for i, slot in enumerate(self._plan))

    def _slot_page(self, slot: PageSlot) -> fitz.Page:
        d = self.doc if slot.source == 0 else self._extra_docs[slot.source]
        page = d[slot.index]
        effective = (slot.base_rotation + slot.rotation_delta) % 360
        if page.rotation != effective:
            page.set_rotation(effective)
        return page

    def page_size(self, index: int) -> tuple[float, float]:
        """Taille affichée de la page en points (rotation appliquée)."""
        r = self._slot_page(self._plan[index]).rect
        return r.width, r.height

    @property
    def is_encrypted(self) -> bool:
        return bool(self.doc.is_encrypted or self._password is not None)

    def can_modify(self) -> bool:
        perms = self.doc.permissions
        return bool(perms & (fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ANNOTATE))

    def can_print(self) -> bool:
        return bool(self.doc.permissions & fitz.PDF_PERM_PRINT)

    def has_digital_signature(self) -> bool:
        try:
            if self.doc.get_sigflags() >= 1:
                return True
        except Exception:
            pass
        for page in self.doc:
            for w in page.widgets() or ():
                if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.field_value:
                    return True
        return False

    # -- rendu ----------------------------------------------------------------

    def render(self, index: int, scale: float, clip: fitz.Rect | None = None) -> fitz.Pixmap:
        page = self._slot_page(self._plan[index])
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                               alpha=False, annots=True)

    # -- organisation des pages -------------------------------------------------

    def rotate_page(self, index: int, degrees: int = 90) -> None:
        slot = self._plan[index]
        slot.rotation_delta = (slot.rotation_delta + degrees) % 360

    def rotate_all_pages(self, degrees: int = 90) -> None:
        for slot in self._plan:
            slot.rotation_delta = (slot.rotation_delta + degrees) % 360

    def delete_page(self, index: int) -> None:
        if len(self._plan) <= 1:
            raise PdfError("Impossible de supprimer la seule page du document.")
        del self._plan[index]

    def insert_pdf(self, index: int, path: str | os.PathLike) -> int:
        """Insère les pages de `path` après la page `index`. Renvoie leur nombre."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise PdfError(f"Impossible de lire le fichier :\n{path}\n\n{exc.strerror or exc}")
        try:
            extra = fitz.open(stream=raw, filetype="pdf")
        except Exception:
            raise PdfError(f"Ce fichier n'est pas un PDF valide ou il est endommagé :\n{path.name}")
        if not extra.is_pdf or extra.page_count == 0:
            raise PdfError(f"Ce fichier ne contient aucune page PDF utilisable :\n{path.name}")
        if extra.needs_pass:
            raise PdfError(
                f"Ce document est protégé par un mot de passe ; il ne peut pas être inséré :\n{path.name}")

        sid = self._next_source
        self._next_source += 1
        self._extra_docs[sid] = extra
        self._extra_sources[sid] = raw
        new_slots = [PageSlot(source=sid, index=i, base_rotation=extra[i].rotation)
                    for i in range(extra.page_count)]
        self._plan[index + 1:index + 1] = new_slots
        return len(new_slots)

    # -- production du document signé ----------------------------------------

    def _fresh_copy(self) -> fitz.Document:
        doc = fitz.open(stream=self._data, filetype="pdf")
        if doc.needs_pass and not doc.authenticate(self._password or ""):
            raise PdfError("Impossible de déverrouiller le document pour l'enregistrer.")
        return doc

    def _assemble_plan(self) -> fitz.Document:
        """Reconstruit un document neuf dans l'ordre et la rotation du plan courant."""
        out = fitz.open()
        sources: dict[int, fitz.Document] = {0: self._fresh_copy()}
        for sid, raw in self._extra_sources.items():
            sources[sid] = fitz.open(stream=raw, filetype="pdf")
        try:
            for slot in self._plan:
                out.insert_pdf(sources[slot.source], from_page=slot.index, to_page=slot.index)
                effective = (slot.base_rotation + slot.rotation_delta) % 360
                if effective:
                    out[-1].set_rotation(effective)
        finally:
            for d in sources.values():
                d.close()
        return out

    def build_signed(self, placements: list[Placement], renderer: BlockRenderer) -> fitz.Document:
        """Copie en mémoire avec les signatures (pour l'impression)."""
        doc = self._assemble_plan() if self.is_edited else self._fresh_copy()
        apply_placements(doc, placements, renderer)
        return doc

    def save(self, target: str | os.PathLike, placements: list[Placement],
             renderer: BlockRenderer) -> Path:
        """Enregistre le document signé dans target. Lève PdfError en cas d'échec."""
        target = Path(target)
        folder = target.parent
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".tinypdf-", suffix=".pdf", dir=folder)
        except OSError as exc:
            raise PdfError(f"Impossible d'écrire dans le dossier :\n{folder}\n\n{exc.strerror or exc}")
        tmp = Path(tmp_name)
        try:
            if self.is_edited:
                os.close(fd)
                self._save_rebuilt(tmp, placements, renderer)
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(self._data)
                if not self._save_incremental(tmp, placements, renderer):
                    self._save_full(tmp, placements, renderer)
            self._verify(tmp)
            try:
                os.replace(tmp, target)
            except PermissionError:
                raise PdfError(
                    f"Impossible de remplacer le fichier :\n{target}\n\n"
                    "Il est peut-être ouvert dans un autre programme, ou en lecture seule.")
        except PdfError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise PdfError(f"L'enregistrement a échoué :\n{exc}")
        return target

    def _save_incremental(self, tmp: Path, placements, renderer) -> bool:
        doc = fitz.open(tmp)
        try:
            if doc.needs_pass and not doc.authenticate(self._password or ""):
                return False
            if not doc.can_save_incrementally():
                return False
            apply_placements(doc, placements, renderer)
            doc.saveIncr()
            return True
        except Exception:
            return False
        finally:
            doc.close()

    def _save_full(self, tmp: Path, placements, renderer) -> None:
        doc = self._fresh_copy()
        try:
            apply_placements(doc, placements, renderer)
            encryption = fitz.PDF_ENCRYPT_KEEP if self.is_encrypted else fitz.PDF_ENCRYPT_NONE
            doc.save(tmp, garbage=1, deflate=True, encryption=encryption)
        finally:
            doc.close()

    def _save_rebuilt(self, tmp: Path, placements, renderer) -> None:
        """Pages tournées, supprimées ou insérées : pas d'enregistrement incrémental possible."""
        doc = self._assemble_plan()
        try:
            apply_placements(doc, placements, renderer)
            encryption = fitz.PDF_ENCRYPT_KEEP if self.is_encrypted else fitz.PDF_ENCRYPT_NONE
            doc.save(tmp, garbage=1, deflate=True, encryption=encryption)
        finally:
            doc.close()

    def _verify(self, tmp: Path) -> None:
        """Relit le fichier produit avant de remplacer la cible."""
        try:
            check = fitz.open(tmp)
            if check.needs_pass:
                check.authenticate(self._password or "")
            ok = check.page_count == self.page_count
            check.close()
        except Exception:
            ok = False
        if not ok:
            raise PdfError("Le fichier produit est invalide ; la cible n'a pas été modifiée.")
