"""
Page de garde PDF pour fascicules / exports liturgiques : illustration hebdomadaire + titres.

À brancher sur un générateur PDF plus large : cette fonction ne produit qu’une **première page**
(fond crème, image optionnelle depuis les octets GCS, titre semaine & date). Les variantes visuelles
se lisent d’une semaine à l’autre via l’illustration dominicale déjà stockée sur GCS.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE

# Footer marque JOPAI© — charte : JOP (gras) + AI (italique) + © (exposant)
_JOPAI_PETROLE = colors.HexColor("#0b2745")
_JOPAI_TURQUOISE = colors.HexColor("#0d9488")
_JOPAI_FOOTER_TEXT_REST = " LumenVia - 2026 | TOUS DROITS RESERVES"
_DEV_NOTICE_GRAY = colors.HexColor("#7F8C8D")


def draw_lumenvia_pdf_dev_notice(c: canvas.Canvas, page_width: float, page_height: float) -> None:
    """Mention développement : italique ~8 pt, au-dessus du bandeau marque, alignée à droite (chaque page)."""

    hbar = 9.0 * mm
    pad_r = 5 * mm
    txt = str(LUMENVIA_DEVELOPMENT_NOTICE or "").strip()
    if not txt:
        return
    c.saveState()
    c.setFillColor(_DEV_NOTICE_GRAY)
    c.setFont("Helvetica-Oblique", 8)
    baseline = hbar + 1 * mm
    max_w = float(page_width) - 2 * pad_r
    if c.stringWidth(txt, "Helvetica-Oblique", 8) <= max_w:
        c.drawRightString(page_width - pad_r, baseline, txt)
    else:
        # Retour automatique très simple (~2 lignes max sur A4).
        mid = txt.find(" — ")
        if mid > 8:
            left, right = txt[:mid].strip(), txt[mid + 3 :].strip()
            c.drawRightString(page_width - pad_r, baseline + 9, left)
            c.drawRightString(page_width - pad_r, baseline, right)
        else:
            half = len(txt) // 2
            cut = txt.rfind(" ", 8, max(len(txt) - 8, half))
            if cut <= 0:
                cut = half
            c.drawRightString(page_width - pad_r, baseline + 9, txt[:cut].strip())
            c.drawRightString(page_width - pad_r, baseline, txt[cut:].strip())
    c.restoreState()


def draw_jopai_footer_bar(c: canvas.Canvas, page_width: float, page_height: float) -> None:
    """Bandeau bas pleine largeur + texte marque immuable."""
    hbar = 9.0 * mm
    c.saveState()
    c.setFillColor(_JOPAI_PETROLE)
    c.rect(0, 0, page_width, hbar, fill=1, stroke=0)

    # Texte : JOP (bold) + AI (italic) + © (superscript) + reste (blanc)
    base_y = 3.2 * mm
    jop = "JOP"
    ai = "AI"
    copy = "©"
    rest = _JOPAI_FOOTER_TEXT_REST
    w_jop = c.stringWidth(jop, "Helvetica-Bold", 7.8)
    w_ai = c.stringWidth(ai, "Helvetica-Oblique", 7.8)
    w_copy = c.stringWidth(copy, "Helvetica", 5.6)
    w_rest = c.stringWidth(rest, "Helvetica-Oblique", 7.2)
    total_w = w_jop + w_ai + w_copy + w_rest
    x0 = (page_width - total_w) / 2

    c.setFillColor(_JOPAI_TURQUOISE)
    c.setFont("Helvetica-Bold", 7.8)
    c.drawString(x0, base_y, jop)
    c.setFont("Helvetica-Oblique", 7.8)
    c.drawString(x0 + w_jop, base_y, ai)
    c.setFont("Helvetica", 5.6)
    c.drawString(x0 + w_jop + w_ai, base_y + 1.4, copy)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Oblique", 7.2)
    c.drawString(x0 + w_jop + w_ai + w_copy, base_y, rest)
    c.restoreState()
    draw_lumenvia_pdf_dev_notice(c, page_width, page_height)


def build_liturgy_cover_pdf_bytes(
    *,
    image_bytes: bytes | None,
    week_title: str,
    date_line: str,
    meta_line: str | None = None,
    audio_listen_url: str | None = None,
    accent_hex: str | None = None,
    footer: str | None = None,
) -> bytes:
    """
    Retourne un PDF d’une page A4 (couverture).

    - ``image_bytes`` : PNG/JPEG/WebP lisible par ReportLab (``ImageReader``).
    - ``week_title`` : ex. « 14ème Dimanche du Temps Ordinaire\n(semaine II du Psautier) ».
    - ``date_line`` : ex. « Dimanche 23 mars 2026 ».
    - ``footer`` : ignoré (conservé pour compatibilité) ; le pied de page est toujours la marque **JOP AI Production**.
    """
    buf = BytesIO()
    w, h = A4
    c = canvas.Canvas(buf, pagesize=A4)
    # Fond crème (charte LumenVia)
    c.setFillColorRGB(0.992, 0.984, 0.969)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    margin = 16 * mm
    img_w, img_h = 165 * mm, 105 * mm
    top_y = h - margin - img_h

    # Liseré (couleur liturgique si connue, sinon or)
    hx = (accent_hex or "").strip() or "#D4AF37"
    c.saveState()
    try:
        c.setFillColor(colors.HexColor(hx))
    except Exception:
        c.setFillColor(colors.HexColor("#D4AF37"))
    c.rect(10 * mm, 18 * mm, 2.2 * mm, h - (18 * mm) - (14 * mm), fill=1, stroke=0)
    c.restoreState()

    if image_bytes:
        try:
            ir = ImageReader(BytesIO(image_bytes))
            c.drawImage(ir, margin, top_y, width=img_w, height=img_h, preserveAspectRatio=True)
        except Exception:
            pass

    c.setFillColorRGB(0.204, 0.180, 0.161)
    c.setFont("Helvetica-Bold", 18)
    title = (week_title or "").strip()[:260]
    # Titre sur 2 lignes si nécessaire
    t_lines = [ln.strip() for ln in title.splitlines() if ln.strip()]
    if len(t_lines) >= 2:
        # Même police / taille / graisse pour les deux lignes (comme demandé)
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(w / 2, h / 2 - 3.5 * mm, t_lines[0][:160])
        c.drawCentredString(w / 2, h / 2 - 13.5 * mm, t_lines[1][:200])
    else:
        c.drawCentredString(w / 2, h / 2 - 8 * mm, (t_lines[0] if t_lines else ""))

    c.setFont("Helvetica", 11)
    dline = (date_line or "").strip()[:200]
    c.drawCentredString(w / 2, h / 2 - 22 * mm, dline)

    ml = (meta_line or "").strip()
    if ml:
        c.setFont("Helvetica-Oblique", 9.5)
        c.drawCentredString(w / 2, h / 2 - 32 * mm, ml[:220])

    # Audio : lien cliquable sur la couverture (remonte "Écouter la synthèse" en 1ère page).
    url = (audio_listen_url or "").strip()
    if url:
        y = h / 2 - 44 * mm
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0x15 / 255.0, 0x65 / 255.0, 0xC0 / 255.0)
        label = "Écouter la synthèse audio"
        tw = c.stringWidth(label, "Helvetica-Bold", 10)
        x = (w - tw) / 2
        c.drawString(x, y, label)
        c.linkURL(url, (x, y - 2, x + tw, y + 10), relative=0)

    draw_jopai_footer_bar(c, w, h)

    c.showPage()
    c.save()
    return buf.getvalue()
