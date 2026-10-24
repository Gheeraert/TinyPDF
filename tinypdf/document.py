"""Chargement, rendu et enregistrement des documents PDF.

Principes de sécurité :
- le fichier est lu une fois en mémoire : il n'est ni verrouillé ni modifié
  pendant la consultation ;
- les signatures sont appliquées à une copie fraîche du document, au moment de
  l'enregistrement ;
- l'enregistrement est incrémental quand c'est possible (le fichier d'origine
  est recopié à l'identique, les ajouts sont écrits à la suite), ce qui
  préserve la structure et les éventuelles signatures électroniques ;
- l'écriture passe par un fichier temporaire remplacé atomiquement : en cas
  d'échec, la cible n'est jamais laissée à moitié écrite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from .stamping import BlockRenderer, Placement, apply_placements


# Attention (PyMuPDF 1.28) : lire doc.needs_pass APRÈS doc.authenticate() casse
# le déchiffrement du document. On ne le consulte donc qu'avant authentification.


class PdfError(Exception):
    """Erreur présentable telle quelle à l'utilisateur."""


class PdfDocument:
    def __init__(self, path: Path, data: bytes, doc: fitz.Document, password: str | None):
        self.path = path
        self._data = data
        self.doc = doc
        self._password = password

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

    # -- informations ---------------------------------------------------------

    @property
    def password(self) -> str | None:
        return self._password

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def page_size(self, index: int) -> tuple[float, float]:
        """Taille affichée de la page en points (rotation appliquée)."""
        r = self.doc[index].rect
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
        page = self.doc[index]
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                               alpha=False, annots=True)

    # -- production du document signé ----------------------------------------

    def _fresh_copy(self) -> fitz.Document:
        doc = fitz.open(stream=self._data, filetype="pdf")
        if doc.needs_pass and not doc.authenticate(self._password or ""):
            raise PdfError("Impossible de déverrouiller le document pour l'enregistrer.")
        return doc

    def build_signed(self, placements: list[Placement], renderer: BlockRenderer) -> fitz.Document:
        """Copie en mémoire avec les signatures (pour l'impression)."""
        doc = self._fresh_copy()
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
