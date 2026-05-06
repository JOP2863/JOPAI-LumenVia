"""
Page de garde PDF pour fascicules / exports liturgiques : illustration hebdomadaire + titres.

À brancher sur un générateur PDF plus large : cette fonction ne produit qu’une **première page**
(fond crème, image optionnelle depuis les octets GCS, titre semaine & date). Les variantes visuelles
se lisent d’une semaine à l’autre via l’illustration dominicale déjà stockée sur GCS.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# Aligné sur Memoria (`memoria_core/services/pdf_album_service.py`) — bandeau pied de page PDF marque.
_JOPAI_FOOTER_BG = (12 / 255.0, 74 / 255.0, 94 / 255.0)
JOPAI_PRODUCTION_FOOTER_LINE = "JOP AI  PRODUCTION   2026 | TOUS DROITS RESERVES"


def draw_jopai_production_footer_bar(c: canvas.Canvas, page_width: float, page_height: float) -> None:
    """Bandeau bas pleine largeur (pétrole) + texte blanc — standard JOPAI Production PDF."""
    hbar = 5.5 * mm
    c.saveState()
    c.setFillColorRGB(*_JOPAI_FOOTER_BG)
    c.rect(0, 0, page_width, hbar, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Oblique", 6.5)
    c.drawCentredString(page_width / 2, 1.85 * mm, JOPAI_PRODUCTION_FOOTER_LINE)
    c.restoreState()


def build_liturgy_cover_pdf_bytes(
    *,
    image_bytes: bytes | None,
    week_title: str,
    date_line: str,
    footer: str | None = None,
) -> bytes:
    """
    Retourne un PDF d’une page A4 (couverture).

    - ``image_bytes`` : PNG/JPEG/WebP lisible par ReportLab (``ImageReader``).
    - ``week_title`` : ex. « Semaine 12 · Temps ordinaire ».
    - ``date_line`` : ex. « Dimanche 23 mars 2026 ».
    - ``footer`` : ignoré (conservé pour compatibilité) ; le pied de page est toujours la marque **JOP AI Production**.
    """
    buf = BytesIO()
    w, h = A4
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFillColorRGB(0.992, 0.984, 0.969)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    margin = 16 * mm
    img_w, img_h = 165 * mm, 105 * mm
    top_y = h - margin - img_h

    if image_bytes:
        try:
            ir = ImageReader(BytesIO(image_bytes))
            c.drawImage(ir, margin, top_y, width=img_w, height=img_h, preserveAspectRatio=True)
        except Exception:
            pass

    c.setFillColorRGB(0.204, 0.180, 0.161)
    c.setFont("Helvetica-Bold", 20)
    title = (week_title or "").strip()[:200]
    c.drawCentredString(w / 2, h / 2 - 8 * mm, title)

    c.setFont("Helvetica", 11)
    dline = (date_line or "").strip()[:200]
    c.drawCentredString(w / 2, h / 2 - 22 * mm, dline)

    draw_jopai_production_footer_bar(c, w, h)

    c.showPage()
    c.save()
    return buf.getvalue()
