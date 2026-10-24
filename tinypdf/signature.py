"""Profil de signature : ligne de texte + image manuscrite, et leur conservation."""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import pymupdf as fitz

from .config import Settings, atomic_write, data_dir
from .fonts import available_fonts, default_font

MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre")

LAYOUT_ABOVE = "above"    # texte au-dessus de la signature
LAYOUT_BESIDE = "beside"  # texte à gauche de la signature


def expand_placeholders(template: str, today: _dt.date | None = None) -> str:
    """Remplace {date} (24/10/2026) et {date_longue} (24 octobre 2026)."""
    d = today or _dt.date.today()
    return (template
            .replace("{date_longue}", f"{d.day} {MONTHS[d.month - 1]} {d.year}")
            .replace("{date}", d.strftime("%d/%m/%Y")))


@dataclass
class SignatureProfile:
    text: str = "Lu et approuvé, le {date}"
    font_name: str = ""
    font_file: str = ""
    font_size: float = 11.0
    color: str = "#000000"
    layout: str = LAYOUT_ABOVE
    image_png: bytes | None = field(default=None, repr=False)  # PNG transparent

    def __post_init__(self):
        if not self.font_name:
            self.font_name, self.font_file = default_font()

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def has_image(self) -> bool:
        return bool(self.image_png)

    def expanded_text(self, today: _dt.date | None = None) -> str:
        return expand_placeholders(self.text, today)

    def rgb(self) -> tuple[float, float, float]:
        h = self.color.lstrip("#")
        try:
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
        except (ValueError, IndexError):
            return (0.0, 0.0, 0.0)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("image_png")
        return d

    @classmethod
    def from_json(cls, d: dict, image_png: bytes | None) -> "SignatureProfile":
        known = {f.name for f in fields(cls)} - {"image_png"}
        p = cls(**{k: v for k, v in d.items() if k in known}, image_png=image_png)
        # La police a pu être désinstallée : on retombe sur une police valide.
        fonts = available_fonts()
        if p.font_name not in fonts or (p.font_file and not Path(p.font_file).is_file()):
            p.font_name, p.font_file = default_font()
        if p.layout not in (LAYOUT_ABOVE, LAYOUT_BESIDE):
            p.layout = LAYOUT_ABOVE
        return p


class SignatureStore:
    """Enregistre le profil dans settings.json et l'image dans signature.png."""

    KEY = "signature"

    def __init__(self, settings: Settings, folder: Path | None = None):
        self.settings = settings
        self.image_path = (folder or data_dir()) / "signature.png"

    def is_configured(self) -> bool:
        return self.settings.get(self.KEY) is not None

    def load(self) -> SignatureProfile:
        image = None
        try:
            image = self.image_path.read_bytes() or None
        except OSError:
            pass
        raw = self.settings.get(self.KEY)
        if not isinstance(raw, dict):
            return SignatureProfile(image_png=image)
        return SignatureProfile.from_json(raw, image)

    def save(self, profile: SignatureProfile) -> None:
        if profile.image_png:
            atomic_write(self.image_path, profile.image_png)
        elif self.image_path.exists():
            self.image_path.unlink()
        self.settings.set(self.KEY, profile.to_json())


# --------------------------------------------------------------------------
# Traitement d'image (fond transparent, recadrage) — sans dépendance à Qt.
# --------------------------------------------------------------------------

def _alpha_table(threshold_hi: int = 235, threshold_lo: int = 150) -> bytes:
    """Luminance -> opacité : blanc = transparent, encre = opaque, rampe douce."""
    table = bytearray(256)
    for v in range(256):
        if v >= threshold_hi:
            a = 0
        elif v <= threshold_lo:
            a = 255
        else:
            a = round(255 * (threshold_hi - v) / (threshold_hi - threshold_lo))
        table[v] = a
    return bytes(table)


def _has_useful_alpha(pix: fitz.Pixmap) -> bool:
    if not pix.alpha:
        return False
    n = pix.n
    alphas = pix.samples[n - 1::n]
    return alphas.count(255) != len(alphas)


def _crop_box(alpha: bytes, w: int, h: int) -> tuple[int, int, int, int] | None:
    top = bottom = None
    left, right = w, 0
    for y in range(h):
        row = alpha[y * w:(y + 1) * w]
        stripped = row.lstrip(b"\x00")
        if not stripped:
            continue
        if top is None:
            top = y
        bottom = y
        left = min(left, w - len(stripped))
        right = max(right, len(row.rstrip(b"\x00")))
    if top is None:
        return None
    return left, top, right, bottom + 1


def clean_signature_image(data: bytes, pad: int = 4) -> bytes:
    """Rend le fond transparent si nécessaire et recadre l'image sur l'encre.

    Accepte PNG/JPEG/etc. Renvoie un PNG RGBA. Lève ValueError si l'image est
    illisible ou vide.
    """
    try:
        pix = fitz.Pixmap(data)
    except Exception as exc:
        raise ValueError("Image illisible.") from exc
    if pix.colorspace is None:
        raise ValueError("Format d'image non pris en charge.")
    if pix.colorspace.n != 3:  # niveaux de gris, CMJN… -> RVB (alpha conservé)
        pix = fitz.Pixmap(fitz.csRGB, pix)

    if not _has_useful_alpha(pix):
        # Image sans transparence (scan, photo) : le blanc devient transparent.
        opaque = fitz.Pixmap(pix, 0) if pix.alpha else pix
        gray = fitz.Pixmap(fitz.csGRAY, opaque)
        alpha = gray.samples.translate(_alpha_table())
        pix = fitz.Pixmap(opaque, 1)
        pix.set_alpha(alpha)

    w, h = pix.width, pix.height
    alpha = bytes(pix.samples[3::4])
    box = _crop_box(alpha, w, h)
    if box is None:
        raise ValueError("L'image ne contient aucun tracé visible.")
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
    if (x0, y0, x1, y1) != (0, 0, w, h):
        # Pixmap.copy recopie une zone vers les mêmes coordonnées de la cible.
        area = fitz.IRect(x0, y0, x1, y1)
        cropped = fitz.Pixmap(pix.colorspace, area, True)
        cropped.copy(pix, area)
        cropped.set_origin(0, 0)
        pix = cropped
    return pix.tobytes("png")


def image_aspect(png: bytes) -> float:
    """Rapport largeur / hauteur de l'image."""
    pix = fitz.Pixmap(png)
    return pix.width / pix.height if pix.height else 1.0
