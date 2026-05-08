"""
PDF « dimanche » complet : couverture + lectures AELF + synthèse + lien d’écoute.

Fusionne la couverture (`pdf_liturgy_cover`) avec un corps multi-pages (ReportLab Platypus),
pied de page **JOPAI©** sur chaque page.
"""

from __future__ import annotations

from io import BytesIO
import re
from xml.sax.saxutils import escape as xml_escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core.pdf_graine_parole_mensuel import strip_light_markdown_to_plain

_CATECHESE_SECTION_TITLE = "Passerelle catéchèse — L’écho des paraboles"
from core.pdf_liturgy_cover import build_liturgy_cover_pdf_bytes, draw_jopai_footer_bar


def _footer_every_page(canvas: object, doc: object) -> None:
    # Liseré (couleur liturgique si fournie via doc)
    try:
        canvas.saveState()
        hx = str(getattr(doc, "_lv_accent_hex", "") or "").strip() or "#D4AF37"
        canvas.setFillColor(colors.HexColor(hx))
        # Bandeau pied de page un peu plus haut : on démarre au-dessus.
        canvas.rect(10 * mm, 9.5 * mm, 2.2 * mm, A4[1] - (9.5 * mm) - (14 * mm), fill=1, stroke=0)
        canvas.restoreState()
    except Exception:
        pass
    draw_jopai_footer_bar(canvas, A4[0], A4[1])


def _to_para_html(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw:
        return "<i>—</i>"
    return xml_escape(raw).replace("\n", "<br/>")


def _to_para_html_aelf(text: str | None) -> str:
    """
    Les textes AELF contiennent souvent des retours à la ligne de mise en forme (wrap),
    qui créent un interligne artificiellement énorme si on les rend en <br/>.
    On transforme donc les retours simples en espaces et on garde les paragraphes (lignes vides).
    """
    raw = (text or "").strip()
    if not raw:
        return "<i>—</i>"
    # Stockage “bloc” (sans retours) + robustesse si l'entrée contient encore des \n.
    p = " ".join([(ln or "").strip() for ln in raw.splitlines() if (ln or "").strip()])
    p = re.sub(r"\s{2,}", " ", p).strip()
    # Règles “lectures” : retours à la ligne après ponctuation forte ; : ! ? et après les points avant une majuscule.
    p = re.sub(r"([;:!?])\s+", r"\1\n", p)
    p = re.sub(r"\.\s+(?=[A-ZÀ-ÖØ-Ý«“\"(])", ".\n", p)
    return xml_escape(p).replace("\n", "<br/>") or "<i>—</i>"


def _add_page_numbers(pdf_bytes: bytes) -> bytes:
    """Ajoute 'page / total' en bas à droite (sauf 1ère et dernière page)."""
    reader = PdfReader(BytesIO(pdf_bytes))
    total = len(reader.pages)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages, start=1):
        stamp_buf = BytesIO()
        c = rl_canvas.Canvas(stamp_buf, pagesize=A4)
        if 1 < i < total:
            # Dans le bandeau bas : texte blanc, aligné à droite (même hauteur que la ligne marque).
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Oblique", 6.5)
            c.drawRightString(A4[0] - 6 * mm, 5.0 * mm, f"{i}/{total}")
        c.showPage()
        c.save()
        stamp_pdf = PdfReader(BytesIO(stamp_buf.getvalue()))
        page.merge_page(stamp_pdf.pages[0])
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()


def _append_markdownish_text(story: list, text: str, *, body_style: ParagraphStyle, sub_style: ParagraphStyle) -> None:
    """Rendu simple : titres Markdown (##/###), **Titre**, emojis, listes."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    buf: list[str] = []

    def flush_buf() -> None:
        if not buf:
            return
        para = "\n".join([b for b in buf if b.strip()]).strip()
        if para:
            story.append(Paragraph(_to_para_html(para), body_style))
            story.append(Spacer(1, 2))
        buf.clear()

    for ln in lines:
        s = ln.strip()
        if not s:
            flush_buf()
            continue
        # Artefacts Markdown résiduels (souvent en fin de synthèse)
        if s in ("###", "##", "#", "---", "—", "----"):
            continue
        # Certains modèles numérotent les sous-parties (ex: "1) ..."). On supprime le préfixe.
        s = re.sub(r"^\s*\d+\)\s*", "", s)
        # ReportLab (polices standard) ne rend pas certains glyphes → carrés.
        # Et certains retours IA peuvent contenir un "n/nn/nnn" parasite (emoji non rendu) parfois précédé d'un carré.
        s = re.sub(r"^[\u200b\uFEFF\u200c\u200d\u2060\uFE0F]+", "", s)  # zero-width / variation selectors
        # Carrés, pastilles, blocs et symboles décoratifs fréquents (PDF → “scorie” brune)
        s = re.sub(
            r"^[■▪◼◾⬛▫▸▹●○►▶◆◇⬜\u2588-\u259F\u25A0-\u25FF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF]+",
            "",
            s,
        ).strip()
        s = re.sub(r"^[^0-9A-Za-zÀ-ÿ]*n{1,3}\b[\s\-•·:]*", "", s)
        # Fallback ultra-simple : si Gemini a littéralement émis "n " en début de ligne, on le retire.
        s = re.sub(r"^n{1,3}\s+", "", s)
        # Ligne du type « … **La Scène Visuelle** » : tout caractère non-alphanumérique avant les astérisques
        if "**" in s:
            s = re.sub(r"^[^\w«“\"(]+\s*\*\*", "**", s, count=1)
        if s in ("**", "*", "•"):
            continue
        # Cas fréquent : sous-titres préfixés par une puce (•/·/◦/▪…).
        # Selon la police PDF, la puce peut s’afficher comme un petit carré : on la retire et on promeut en sous-titre.
        m_bullet_title = re.match(r"^[•·◦‣▪\-–—]\s+(.*)$", s)
        if m_bullet_title:
            cand = (m_bullet_title.group(1) or "").strip()
            if 4 <= len(cand) <= 60 and cand[0].isalpha() and cand[0].isupper() and (":" not in cand) and (not cand.endswith(".")):
                flush_buf()
                story.append(Paragraph(xml_escape(cand), sub_style))
                story.append(Spacer(1, 3))
                continue
            # Sinon, on retire juste la puce pour éviter le carré, et on retombe sur le flux normal.
            s = cand if cand else s
        # Titres markdown
        if s.startswith("### "):
            flush_buf()
            story.append(Paragraph(xml_escape(s[4:].strip()), sub_style))
            story.append(Spacer(1, 3))
            continue
        if s.startswith("## "):
            flush_buf()
            story.append(Paragraph(xml_escape(s[3:].strip()), sub_style))
            story.append(Spacer(1, 3))
            continue
        # Sous-titres : **Titre** (modèle) ou "📍 ..." etc.
        if (s.startswith("**") and s.endswith("**") and len(s) >= 6) or s[:2] in ("📍", "🖼️", "🔑", "💡", "🌱"):
            flush_buf()
            title = s.strip("*").strip()
            # Retire l’emoji (sinon carré), mais conserve le libellé.
            title = re.sub(r"^[📍🖼️🔑💡🌱]\s*", "", title).strip()
            story.append(Paragraph(xml_escape(title), sub_style))
            story.append(Spacer(1, 3))
            continue
        # Listes simples
        if s.startswith("- ") or s.startswith("* "):
            flush_buf()
            story.append(Paragraph(xml_escape("• " + s[2:].strip()), body_style))
            story.append(Spacer(1, 1.5))
            continue
        # Heuristique : lignes courtes type "L'Unité de la Parole" -> sous-titre
        if (
            4 <= len(s) <= 60
            and s[0].isalpha()
            and s.lower() == s  # unlikely
        ):
            pass
        if 4 <= len(s) <= 60 and (":" not in s) and (s.endswith(".") is False) and s[0].isalpha() and s.count(" ") <= 8:
            # Si la ligne ressemble à un titre (sans ponctuation), on la promeut.
            if s[0].isupper():
                flush_buf()
                story.append(Paragraph(xml_escape(s), sub_style))
                story.append(Spacer(1, 4))
                continue
        buf.append(s)

    flush_buf()


def build_liturgy_body_pdf_bytes(
    *,
    premiere_lecture: str | None,
    psaume: str | None,
    deuxieme_lecture: str | None,
    evangile: str | None,
    synthesis_text: str | None,
    audio_listen_url: str | None,
    audio_listen_note: str | None = None,
    about_markdown: str | None = None,
    back_cover_image_bytes: bytes | None = None,
    accent_hex: str | None = None,
    back_cover_highlight_cell_index: int | None = None,
) -> bytes:
    """Pages suivantes : lectures AELF + chapitres (Synthèse, Passerelle)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        onFirstPage=_footer_every_page,
        onLaterPages=_footer_every_page,
    )
    # Hack simple : stocke l’accent sur doc pour le callback footer.
    try:
        doc._lv_accent_hex = str(accent_hex or "").strip() or "#D4AF37"
    except Exception:
        pass
    styles = getSampleStyleSheet()
    hx = str(accent_hex or "").strip() or "#8B6914"
    h = ParagraphStyle(
        name="LVLecH",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=colors.HexColor("#8B6914"),
        spaceBefore=10,
        spaceAfter=7,
    )
    chapter = ParagraphStyle(
        name="LVChapter",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        textColor=colors.HexColor("#8B6914"),
        spaceBefore=4,
        spaceAfter=10,
    )
    body = ParagraphStyle(
        name="LVLecBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=11.4,
        alignment=TA_JUSTIFY,
        textColor=colors.HexColor("#342E29"),
    )
    back_note = ParagraphStyle(
        name="LVBackNote",
        parent=body,
        fontName="Helvetica-Oblique",
        fontSize=9.2,
        leading=13,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#342E29"),
        spaceAfter=10,
    )
    sub = ParagraphStyle(
        name="LVSubH",
        parent=h,
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=14,
        textColor=colors.HexColor("#8B6914"),
        leftIndent=6 * mm,
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True,
    )
    try:
        # Accentue la hiérarchie (proche du site) : titres + chapitres suivent l’accent.
        h.textColor = colors.HexColor(hx)
        chapter.textColor = colors.HexColor(hx)
        sub.textColor = colors.HexColor(hx)
    except Exception:
        pass

    story: list = []
    # Lectures sur 2 pages (moins condensé) :
    # - Page 1 : Première lecture + Psaume
    # - Page 2 : Deuxième lecture + Évangile
    blocks_page1 = [
        ("Première lecture", premiere_lecture),
        ("Psaume", psaume),
    ]
    blocks_page2 = [
        ("Deuxième lecture", deuxieme_lecture),
        ("Évangile", evangile),
    ]

    for title, txt in blocks_page1:
        story.append(Paragraph(xml_escape(title), h))
        story.append(Paragraph(_to_para_html_aelf(txt), body))
        story.append(Spacer(1, 10))

    story.append(PageBreak())
    for title, txt in blocks_page2:
        story.append(Paragraph(xml_escape(title), h))
        story.append(Paragraph(_to_para_html_aelf(txt), body))
        story.append(Spacer(1, 10))

    # Chapitre Synthèse
    story.append(PageBreak())
    story.append(Paragraph("Synthèse (LumenVia)", chapter))
    syn_raw = (synthesis_text or "").strip()
    if syn_raw:
        # Découpe : la Passerelle catéchèse doit être un chapitre séparé.
        syn_part = syn_raw
        cate_part = ""
        try:
            idx = syn_raw.lower().find(_CATECHESE_SECTION_TITLE.lower())
            if idx >= 0:
                syn_part = syn_raw[:idx].strip()
                cate_part = syn_raw[idx:].strip()
        except Exception:
            pass

        if syn_part:
            _append_markdownish_text(
                story,
                syn_part,
                body_style=body,
                sub_style=sub,
            )
        else:
            story.append(Paragraph("<i>—</i>", body))

        if cate_part:
            story.append(PageBreak())
            story.append(Paragraph(_CATECHESE_SECTION_TITLE, chapter))
            # Retire le titre en double si le texte le contient.
            cate_body = cate_part
            if cate_body.lower().startswith(_CATECHESE_SECTION_TITLE.lower()):
                cate_body = cate_body[len(_CATECHESE_SECTION_TITLE) :].lstrip(" \t\r\n-:#")
            _append_markdownish_text(story, cate_body, body_style=body, sub_style=sub)
    else:
        story.append(
            Paragraph(
                "<i>Synthèse non encore générée pour cette date — utilise « Générer la synthèse et l’audio » "
                "dans l’application.</i>",
                body,
            )
        )

    # Page "À propos" (juste avant la page de dos)
    about_raw = (about_markdown or "").strip()
    if about_raw:
        story.append(PageBreak())
        story.append(Paragraph("À propos de JOPAI LumenVia", chapter))
        story.append(Spacer(1, 6))

        # On transforme le Markdown en texte propre, puis on compose des paragraphes/bullets.
        plain = strip_light_markdown_to_plain(about_raw).strip()
        lines = [ln.rstrip() for ln in plain.splitlines()]
        blocks: list[str] = []
        cur: list[str] = []
        for ln in lines:
            s = (ln or "").strip()
            if not s:
                if cur:
                    blocks.append("\n".join(cur).strip())
                    cur = []
                continue
            cur.append(s)
        if cur:
            blocks.append("\n".join(cur).strip())

        # Plusieurs puces Markdown « - … » à la suite sans ligne vide → un seul bloc concaténé.
        # On éclate en une entrée par ligne qui commence par « - » pour retrouver le rendu web (une puce par item).
        expanded_blocks: list[str] = []
        for blk in blocks:
            b = blk.strip()
            if not b:
                continue
            if b.startswith("- ") and "\n- " in b:
                for part in re.split(r"\n(?=- )", b):
                    p = part.strip()
                    if p:
                        expanded_blocks.append(p)
            else:
                expanded_blocks.append(b)

        inner: list = []
        for blk in expanded_blocks:
            # Bullets (avec indentation, pour coller au rendu web)
            if blk.startswith("- "):
                bullet_txt = blk[2:].strip()
                inner.append(
                    Paragraph(
                        xml_escape("• " + bullet_txt),
                        ParagraphStyle(
                            name="LVAboutBullet",
                            parent=body,
                            leftIndent=14,
                            bulletIndent=0,
                            spaceBefore=0,
                            spaceAfter=6,
                        ),
                    )
                )
                continue
            if blk.startswith("• "):
                bullet_txt = blk[2:].strip()
                inner.append(
                    Paragraph(
                        xml_escape("• " + bullet_txt),
                        ParagraphStyle(
                            name="LVAboutBullet2",
                            parent=body,
                            leftIndent=14,
                            bulletIndent=0,
                            spaceBefore=0,
                            spaceAfter=6,
                        ),
                    )
                )
                continue
            # Citation / phrase courte -> italique centré
            if "Ta Parole est une lampe" in blk:
                q = xml_escape(blk.strip("“”\"' ").strip())
                q_style = ParagraphStyle(name="LVAboutQuote", parent=body, alignment=TA_CENTER)
                try:
                    q_style.textColor = colors.HexColor(str(accent_hex or "").strip() or "#0d9488")
                except Exception:
                    pass
                inner.append(Paragraph(f"<i>{q}</i>", q_style))
                # Espace net après la citation (équivalent d’un retour chariot lisible avant le bloc suivant)
                inner.append(Spacer(1, 5 * mm))
                continue
            # Phrase de clôture « À propos »
            if blk.strip().startswith("Puisse cet outil"):
                inner.append(
                    Paragraph(
                        _to_para_html(blk),
                        ParagraphStyle(name="LVAboutClosing", parent=body, alignment=TA_CENTER),
                    )
                )
                inner.append(Spacer(1, 6))
                continue
            # Titres en gras -> sous-titre
            if blk.endswith("?") and len(blk) <= 60:
                inner.append(Paragraph(xml_escape(blk), sub))
                inner.append(Spacer(1, 6))
                continue
            inner.append(Paragraph(_to_para_html(blk), body))
            inner.append(Spacer(1, 6))

        box_w = float(doc.width) * 0.92
        box = Table(
            [[inner]],
            colWidths=[box_w],
        )
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFDF7")),
                    ("BOX", (0, 0), (-1, -1), 1.3, colors.HexColor(str(accent_hex or "").strip() or "#D4AF37")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ]
            )
        )
        # Centrage vertical/horizontal dans la zone restante.
        story.append(
            KeepInFrame(
                float(doc.width),
                float(doc.height) - (22 * mm),
                [box],
                hAlign="CENTER",
                vAlign="MIDDLE",
                mode="shrink",
            )
        )

    # Quatrième de couverture : montage pastel des vignettes (si fourni)
    if back_cover_image_bytes:
        story.append(PageBreak())
        try:
            story.append(
                Paragraph(
                    xml_escape(
                        "Ce canevas déploie les 51 étapes de notre marche liturgique. Chaque vignette est une fenêtre ouverte sur la Parole, "
                        "une escale visuelle pour méditer les mystères de la semaine. Suivez ce fil de lumière, de dimanche en dimanche, "
                        "pour habiter le temps avec espérance"
                    ),
                    back_note,
                )
            )
            # Dimensionnement robuste : ReportLab peut lever LayoutError si l'image dépasse la frame.
            # On calcule à partir de l'image réelle + doc.width/doc.height.
            from PIL import Image as PILImage
            from PIL import ImageDraw

            pil = PILImage.open(BytesIO(back_cover_image_bytes)).convert("RGB")
            # Liseré très fin autour du montage (accent liturgique si disponible)
            try:
                draw0 = ImageDraw.Draw(pil)
                hx = str(accent_hex or "").strip() or "#D4AF37"
                stroke = colors.HexColor(hx)
                # ReportLab -> PIL RGB
                rgb = (
                    int(round(stroke.red * 255)),
                    int(round(stroke.green * 255)),
                    int(round(stroke.blue * 255)),
                )
                w, h0 = pil.size
                # 1px : très fin, discret
                draw0.rectangle([1, 1, max(1, w - 2), max(1, h0 - 2)], outline=rgb, width=1)
            except Exception:
                pass
            # Encadre la vignette correspondant au dimanche du PDF (index dans la grille annuelle)
            if isinstance(back_cover_highlight_cell_index, int) and back_cover_highlight_cell_index >= 0:
                try:
                    cols = 4
                    cell = 200
                    pad = 10
                    title_cell = True
                    start_i = 1 if title_cell else 0
                    i = int(back_cover_highlight_cell_index) + start_i
                    col = i % cols
                    row = i // cols
                    x_cell = pad + col * (cell + pad)
                    y_cell = pad + row * (cell + pad)
                    x1 = x_cell + cell
                    y1 = y_cell + cell
                    draw = ImageDraw.Draw(pil)
                    stroke = (13, 148, 136)  # turquoise JOPAI
                    for w in (7, 5, 3):
                        draw.rectangle([x_cell + w, y_cell + w, x1 - w, y1 - w], outline=stroke, width=2)
                except Exception:
                    pass
            iw, ih = pil.size
            if iw <= 0 or ih <= 0:
                raise RuntimeError("Image back_cover invalide (dimensions nulles).")
            max_w = float(doc.width)
            # On garde un petit espace pour respirer + bandeau footer.
            # Et on réserve aussi de la place pour le paragraphe d'intro (sinon l'image peut basculer page suivante).
            max_h = float(doc.height) - (8 * mm) - (34 * mm)
            scale = min(max_w / float(iw), max_h / float(ih), 1.0)
            dw = float(iw) * scale
            dh = float(ih) * scale
            out_img = BytesIO()
            pil.save(out_img, format="PNG", optimize=True)
            img = RLImage(BytesIO(out_img.getvalue()), width=dw, height=dh)
            img.hAlign = "CENTER"
            story.append(Spacer(1, 4))
            story.append(img)
        except Exception:
            # On ignore : le PDF reste générable même si l’image est corrompue.
            story.append(Paragraph("<i>Quatrième de couverture indisponible.</i>", body))

    # IMPORTANT : sur SimpleDocTemplate, on passe onFirstPage/onLaterPages à build()
    # (sinon le footer n'est pas appliqué sur les pages du corps).
    doc.build(story, onFirstPage=_footer_every_page, onLaterPages=_footer_every_page)
    return buf.getvalue()


def build_liturgy_sunday_pdf_bytes(
    *,
    image_bytes: bytes | None,
    week_title: str,
    date_line: str,
    meta_line: str | None = None,
    premiere_lecture: str | None,
    psaume: str | None,
    deuxieme_lecture: str | None,
    evangile: str | None,
    synthesis_text: str | None,
    audio_listen_url: str | None,
    audio_listen_note: str | None = None,
    about_markdown: str | None = None,
    back_cover_image_bytes: bytes | None = None,
    accent_hex: str | None = None,
    back_cover_highlight_cell_index: int | None = None,
) -> bytes:
    """Couverture + corps fusionnés en un seul PDF."""
    cover = build_liturgy_cover_pdf_bytes(
        image_bytes=image_bytes,
        week_title=week_title,
        date_line=date_line,
        meta_line=meta_line,
        audio_listen_url=audio_listen_url,
        accent_hex=accent_hex,
    )
    body = build_liturgy_body_pdf_bytes(
        premiere_lecture=premiere_lecture,
        psaume=psaume,
        deuxieme_lecture=deuxieme_lecture,
        evangile=evangile,
        synthesis_text=synthesis_text,
        audio_listen_url=audio_listen_url,
        audio_listen_note=audio_listen_note,
        about_markdown=about_markdown,
        back_cover_image_bytes=back_cover_image_bytes,
        accent_hex=accent_hex,
        back_cover_highlight_cell_index=back_cover_highlight_cell_index,
    )
    writer = PdfWriter()
    for page in PdfReader(BytesIO(cover)).pages:
        writer.add_page(page)
    for page in PdfReader(BytesIO(body)).pages:
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    writer.close()
    return _add_page_numbers(out.getvalue())
