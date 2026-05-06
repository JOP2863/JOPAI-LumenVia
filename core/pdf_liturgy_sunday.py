"""
PDF « dimanche » complet : couverture + lectures AELF + synthèse + lien d’écoute.

Fusionne la couverture (`pdf_liturgy_cover`) avec un corps multi-pages (ReportLab Platypus),
pied de page **JOP AI Production** (aligné Memoria) sur chaque page.
"""

from __future__ import annotations

from io import BytesIO
from xml.sax.saxutils import escape as xml_escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from core.pdf_graine_parole_mensuel import strip_light_markdown_to_plain
from core.pdf_liturgy_cover import build_liturgy_cover_pdf_bytes, draw_jopai_production_footer_bar


def _footer_every_page(canvas: object, doc: object) -> None:
    draw_jopai_production_footer_bar(canvas, A4[0], A4[1])


def _to_para_html(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw:
        return "<i>—</i>"
    return xml_escape(raw).replace("\n", "<br/>")


def build_liturgy_body_pdf_bytes(
    *,
    premiere_lecture: str | None,
    psaume: str | None,
    deuxieme_lecture: str | None,
    evangile: str | None,
    synthesis_text: str | None,
    audio_listen_url: str | None,
    audio_listen_note: str | None = None,
) -> bytes:
    """Pages suivantes : lectures, synthèse, bloc audio (lien cliquable si URL fournie)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=14 * mm,
        bottomMargin=18 * mm,
        onFirstPage=_footer_every_page,
        onLaterPages=_footer_every_page,
    )
    styles = getSampleStyleSheet()
    h = ParagraphStyle(
        name="LVLecH",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        textColor=colors.HexColor("#342E29"),
        spaceBefore=8,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        name="LVLecBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        alignment=TA_JUSTIFY,
        textColor=colors.HexColor("#342E29"),
    )
    syn_h = ParagraphStyle(
        name="LVSynH",
        parent=h,
        textColor=colors.HexColor("#8B6914"),
    )
    link_style = ParagraphStyle(
        name="LVLink",
        parent=body,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#1565c0"),
    )

    story: list = []
    blocks = [
        ("Première lecture", premiere_lecture),
        ("Psaume", psaume),
        ("Deuxième lecture", deuxieme_lecture),
        ("Évangile", evangile),
    ]
    for title, txt in blocks:
        story.append(Paragraph(xml_escape(title), h))
        story.append(Paragraph(_to_para_html(txt), body))
        story.append(Spacer(1, 10))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Synthèse (LumenVia)", syn_h))
    syn_plain = (synthesis_text or "").strip()
    if syn_plain:
        story.append(Paragraph(_to_para_html(strip_light_markdown_to_plain(syn_plain)), body))
    else:
        story.append(
            Paragraph(
                "<i>Synthèse non encore générée pour cette date — utilise « Générer la synthèse et l’audio » "
                "dans l’application.</i>",
                body,
            )
        )

    story.append(Spacer(1, 14))
    story.append(Paragraph("Écouter la synthèse", syn_h))
    note = (audio_listen_note or "").strip()
    url = (audio_listen_url or "").strip()
    if url:
        safe_u = xml_escape(url)
        story.append(
            Paragraph(
                f'<a href="{safe_u}" color="#1565c0"><u>Écouter la synthèse audio</u></a> '
                f"<font color='#342E29'> — ouvre LumenVia sur ce dimanche lorsque l’URL publique est configurée.</font>",
                link_style,
            )
        )
        if note:
            story.append(Spacer(1, 6))
            story.append(Paragraph(_to_para_html(note), body))
    else:
        story.append(
            Paragraph(
                "<i>Aucune URL publique d’application configurée (<code>PUBLIC_APP_URL</code> dans les secrets). "
                "Ouvre LumenVia → « La Lumière du Dimanche », choisis ce dimanche, puis lis ou génère la synthèse audio.</i>",
                body,
            )
        )

    doc.build(story)
    return buf.getvalue()


def build_liturgy_sunday_pdf_bytes(
    *,
    image_bytes: bytes | None,
    week_title: str,
    date_line: str,
    premiere_lecture: str | None,
    psaume: str | None,
    deuxieme_lecture: str | None,
    evangile: str | None,
    synthesis_text: str | None,
    audio_listen_url: str | None,
    audio_listen_note: str | None = None,
) -> bytes:
    """Couverture + corps fusionnés en un seul PDF."""
    cover = build_liturgy_cover_pdf_bytes(
        image_bytes=image_bytes,
        week_title=week_title,
        date_line=date_line,
    )
    body = build_liturgy_body_pdf_bytes(
        premiere_lecture=premiere_lecture,
        psaume=psaume,
        deuxieme_lecture=deuxieme_lecture,
        evangile=evangile,
        synthesis_text=synthesis_text,
        audio_listen_url=audio_listen_url,
        audio_listen_note=audio_listen_note,
    )
    writer = PdfWriter()
    for page in PdfReader(BytesIO(cover)).pages:
        writer.add_page(page)
    for page in PdfReader(BytesIO(body)).pages:
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()
