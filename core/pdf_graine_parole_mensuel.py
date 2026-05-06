"""
PDF mensuel « Graine de Parole » : mémos du mois + encart doré des résolutions.

Sources : lignes Sheets ``memos`` + corps Markdown sur GCS (voir ``render_memo`` dans ``app.py``).
"""

from __future__ import annotations

import re
from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


_GOLD = colors.HexColor("#D4AF37")
_CREAM = colors.HexColor("#FDFBF7")
_TEXT = colors.HexColor("#342E29")


def markdownish_to_paragraph_html(text: str) -> str:
    """Échappement + retours ligne pour ReportLab Paragraph."""
    t = str(text or "").strip()
    if not t:
        return ""
    t = xml_escape(t)
    return t.replace("\n", "<br/>")


def build_graine_parole_monthly_pdf_bytes(
    *,
    month_label_fr: str,
    items: list[dict],
    resolutions: list[tuple[str, str]],
    footer: str = "JOPAI LumenVia — Graine de Parole",
) -> bytes:
    """
    ``items`` : ``title``, ``date_str``, ``body_plain`` (texte déjà plat ou markdown léger).
    ``resolutions`` : ``(date_iso, résolution)`` pour l’encart final (peut être vide).
    """
    _ml = 18 * mm
    _mr = 18 * mm
    _usable_w = A4[0] - _ml - _mr
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=_ml,
        rightMargin=_mr,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="LVTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        textColor=_TEXT,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    sub_style = ParagraphStyle(
        name="LVSub",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=13,
        textColor=_TEXT,
        alignment=TA_CENTER,
        spaceAfter=18,
    )
    h_style = ParagraphStyle(
        name="LVH",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=_TEXT,
        spaceBefore=10,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        name="LVBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=_TEXT,
        alignment=TA_JUSTIFY,
    )
    foot_style = ParagraphStyle(
        name="LVFoot",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=8,
        textColor=_TEXT,
        alignment=TA_CENTER,
    )

    story: list = []
    story.append(Paragraph("Graine de Parole", title_style))
    story.append(Paragraph(month_label_fr, sub_style))
    story.append(Spacer(1, 6))

    if not items:
        story.append(Paragraph("<i>Aucun mémo pour ce mois.</i>", body_style))
    else:
        for it in items:
            title = str(it.get("title") or "Mémo").strip()
            ds = str(it.get("date_str") or "").strip()
            body = str(it.get("body_plain") or "").strip()
            story.append(Paragraph(f"<b>{xml_escape(title)}</b> · {xml_escape(ds)}", h_style))
            story.append(Paragraph(markdownish_to_paragraph_html(body) or "—", body_style))
            story.append(Spacer(1, 10))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Résolutions du mois", h_style))

    res_body_style = ParagraphStyle(
        name="LVResBody",
        parent=body_style,
        fontSize=11,
        leading=15,
        textColor=_TEXT,
        alignment=TA_JUSTIFY,
    )

    res_chunks: list[str] = []
    for ds, txt in resolutions:
        t = str(txt or "").strip()
        if not t:
            continue
        res_chunks.append(f"<b>{xml_escape(ds)}</b> — {markdownish_to_paragraph_html(t)}")
    if not res_chunks:
        story.append(Paragraph("<i>Aucune résolution renseignée pour ce mois.</i>", body_style))
    else:
        cell_html = "<br/><br/>".join(res_chunks)
        tbl_data = [[Paragraph(cell_html, res_body_style)]]
        t = Table(tbl_data, colWidths=[_usable_w])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _CREAM),
                    ("BOX", (0, 0), (-1, -1), 1.2, _GOLD),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ]
            )
        )
        story.append(t)

    story.append(Spacer(1, 20))
    story.append(Paragraph(xml_escape(footer), foot_style))

    doc.build(story)
    return buf.getvalue()


def strip_light_markdown_to_plain(md: str) -> str:
    """Retrait minimal des marqueurs courants des mémos Markdown."""
    t = str(md or "")
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"#{1,6}\s*", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return t.strip()
