"""Rendu HTML des e-mails newsletter LumenVia (corps + gabarit LV)."""

from __future__ import annotations

import re
from html import escape as html_escape

from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE


def linkify_html_urls(text: str) -> str:
    def repl(m: re.Match) -> str:
        u = m.group(0)
        return f'<a href="{u}" target="_blank" rel="noopener noreferrer">{u}</a>'

    return re.sub(r"(https?://[^\s<]+)", repl, text or "")

def email_body_to_minimal_html(body0: str) -> str:
    b = (body0 or "").strip()
    if re.search(r"(?is)<\s*(html|body|div|p|table|br|a)\b", b):
        return b
    b = linkify_html_urls(b)
    paras = [p.strip() for p in b.split("\n\n") if p.strip()]
    out: list[str] = []
    for p in paras:
        out.append("<p>" + p.replace("\n", "<br>\n") + "</p>")
    inner = ("\n".join(out) if out else "<p></p>")
    dn_ml = html_escape(LUMENVIA_DEVELOPMENT_NOTICE)
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>"
        "body{font-family:Arial,Helvetica,sans-serif;line-height:1.45;color:#0b2745;}"
        "p{margin:0 0 12px 0;}"
        "a{color:#0d9488;text-decoration:underline;}"
        "img{max-width:100%;height:auto;display:block;margin:10px 0;}"
        "</style>"
        "</head><body>\n"
        f"{inner}\n"
        f'<p style="color:#7F8C8D;font-size:10px;line-height:1.4;margin-top:18px;"><em>{dn_ml}</em></p>\n'
        "</body></html>"
    )

def build_lv_newsletter_email_html(*, subject0: str, values0: dict[str, str], intro_text: str) -> str:
    import app as ap

    prenom = (values0.get("prenom") or "").strip() or "—"
    nom = (values0.get("nom") or "").strip() or ""
    origin0 = (values0.get("origin") or "").strip()
    url_pdf0 = (values0.get("url_pdf") or "").strip()
    url_audio0 = (values0.get("url_audio") or "").strip()
    url_audio_readings0 = (values0.get("url_audio_readings") or "").strip()
    url_app0 = (values0.get("url_app") or "").strip()
    url_illu0 = (values0.get("url_illustration") or "").strip()
    optout0 = (values0.get("optout_url") or "").strip()
    email0 = (values0.get("email") or "").strip().lower()

    pref_url = ""
    if origin0 and email0:
        try:
            from urllib.parse import quote_plus as _q
        except Exception:  # pragma: no cover
            _q = None  # type: ignore[assignment]
        enc = _q(email0) if _q else email0
        pref_url = origin0.rstrip("/") + "/?route=join&email=" + enc

    # Le template newsletter est rédigé "Bonjour {{prenom}}," : on force donc le prénom seul.
    who = (prenom or "—").strip()
    # On retire les lignes techniques / URLs signées du corps (l’illustration est rendue en image seule sous le texte).
    raw_lines = [ln.strip() for ln in (intro_text or "").replace("\r\n", "\n").split("\n")]
    raw_lines = [ln for ln in raw_lines if ln]
    filtered: list[str] = []
    for ln in raw_lines:
        if re.match(r"(?i)^bonjour\b", ln):
            continue
        if re.match(r"(?i)^illustration\s*:\s*https?://", ln):
            continue
        # Évite d'afficher des URLs signées interminables en clair
        if "X-Goog-Algorithm=" in ln or "X-Goog-Credential=" in ln or "X-Goog-Signature=" in ln:
            continue
        if re.search(r"(?is)\{\{\s*affichage.*illustration", ln):
            continue
        filtered.append(ln)
    if not filtered:
        filtered = ["La fin de semaine approche : voici votre préparation dominicale."]

    def _is_list_unsubscribe_line(ln: str) -> bool:
        s = (ln or "").strip()
        if not s:
            return False
        if re.search(r"(?i)vous recevez cet e-mail", s):
            return True
        if re.search(r"(?i)préférences ou vous désabonner", s):
            return True
        if re.search(r"(?i)membre de la communauté\s+LumenVia", s) and re.search(
            r"(?i)cliquez\s+ici", s
        ):
            return True
        return False

    def _is_feedback_survey_bullet(ln: str) -> bool:
        """True uniquement pour la ligne CTA questionnaire (pas « L'Expérience Sonore »)."""
        if "👉" not in ln:
            return False
        if re.match(r"(?i)^l['’]exp[eé]rience\s+sonore\b", ln.strip()):
            return False
        return bool(
            re.search(r"(?i)donner\s+(mon\s+)?avis|avis\s+sur\s+cette\s+expérience", ln)
            or re.search(r"(?i)questionnaire", ln)
        )

    legal_notice_line = ""

    # Ordre du template : paragraphes et puces entrelacés (évite tout le prose puis toute la liste).
    segments: list[tuple[str, str]] = []
    for ln in filtered:
        if _is_list_unsubscribe_line(ln):
            legal_notice_line = ln.strip()
            continue
        raw = ln.strip()
        lead = raw.lstrip("-•").lstrip()
        if re.match(
            r"(?i)^(la synth[eè]se|l['’]essentiel|l['’]exp[eé]rience\s+sonore|la\s+parole"
            r"|l['’]audio\s+des\s+lectures|l['’]illustration)\b",
            lead,
        ) or raw.startswith(("-", "•")):
            segments.append(("li", lead))
            continue
        if _is_feedback_survey_bullet(raw):
            # Pas en <ul>/<li> : évite une puce • avant le CTA questionnaire (absente du template).
            segments.append(("cta", lead))
            continue
        segments.append(("p", raw))

    _wrap_lo: int | None = None
    _wrap_hi: int | None = None
    for _wi, (_wk, _wch) in enumerate(segments):
        if _wk == "p" and re.match(
            r"(?i)^beau\s+chemin\s+vers\s+dimanche",
            (_wch or "").strip(),
        ):
            _wrap_lo = _wi
            break
    if _wrap_lo is not None:
        _wrap_hi = _wrap_lo
        _wj = _wrap_lo + 1
        while _wj < len(segments):
            _nk, _ = segments[_wj]
            if _nk in ("p", "cta"):
                _wrap_hi = _wj
                _wj += 1
                continue
            break

    def _esc(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _bullet_html(b: str) -> str:
        bb0 = (b or "").strip()
        if "👉" in bb0:
            left, right = bb0.split("👉", 1)
            left = left.strip()
            right = right.strip()
            href = ""
            if url_pdf0 and re.search(r"(?i)synth[èe]se.*pdf|pdf", bb0):
                href = url_pdf0
            elif url_audio_readings0 and re.search(
                r"(?i)parole.*audio|lectures|textes\s+bibliques|[ée]critures", bb0
            ):
                href = url_audio_readings0
            elif url_audio0 and re.search(
                r"(?i)audio|essentiel|[ée]couter",
                bb0,
            ) and not re.search(r"(?i)lectures|parole.*\(lectures\)|textes\s+bibliques", bb0):
                href = url_audio0
            elif url_illu0 and re.search(r"(?i)image|illustration", bb0):
                href = url_illu0
            fb_url = ap.lumenvia_feedback_survey_abs_url(origin0, recipient_email=email0 or None)
            if not href and fb_url and re.search(
                r"(?i)donner\s+mon\s+avis|avis\s+sur\s+cette\s+expérience|donner\s+votre\s+avis",
                right,
            ):
                href = fb_url
            if href:
                if left:
                    return (
                        f"{_esc(left)}<br>"
                        f"👉 <a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\"><strong>{_esc(right)}</strong></a>"
                    ).strip()
                return (
                    f"👉 <a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\"><strong>{_esc(right)}</strong></a>"
                ).strip()
            if left:
                return f"{_esc(left)}<br>👉 <strong>{_esc(right)}</strong>".strip()
            return f"👉 <strong>{_esc(right)}</strong>".strip()
        return _esc(bb0)

    intro_html = ""
    _ul_items: list[str] = []
    _max_intro_paras = 40
    _max_intro_li = 16
    _max_intro_cta = 6
    _p_used = 0
    _li_used = 0
    _cta_used = 0

    def _flush_ul() -> None:
        nonlocal intro_html, _ul_items
        if not _ul_items:
            return
        blk = "".join([f"<li style=\"margin:8px 0;\">{x}</li>" for x in _ul_items])
        intro_html += f"<ul style=\"margin:10px 0 6px 18px;padding:0;\">{blk}</ul>"
        _ul_items = []

    _wrap_div_open = (
        '<div style="border:1px solid #e7e5e4;border-radius:14px;padding:14px 16px;'
        'margin:14px 0;background:#fdfcfa;">'
    )

    for _seg_i, (kind, chunk) in enumerate(segments):
        if _wrap_lo is not None and _seg_i == _wrap_lo:
            intro_html += _wrap_div_open
        if kind == "p":
            _flush_ul()
            if _p_used < _max_intro_paras:
                pp = ap.lumenvia_wrap_feedback_cta_with_link(
                    (chunk or "").strip(),
                    origin_for_href=origin0,
                    recipient_email=email0 or None,
                )
                pp = linkify_html_urls(pp)
                # Met en valeur JOPAI© comme dans le footer (couleurs/typo).
                pp = re.sub(
                    r"(?i)\bJOPAI\b",
                    '<span class="jopai-inline"><span class="jop">JOP</span><span class="ai">AI</span><sup class="ai">©</sup></span>',
                    pp,
                )
                for kw in ("LumenVia", "PDF", "Audio", "Illustration", "messe", "Parole"):
                    pp = re.sub(
                        rf"(?i)\b{re.escape(kw)}\b",
                        lambda m: f"<strong>{m.group(0)}</strong>",
                        pp,
                    )
                _in_fb = (
                    _wrap_lo is not None
                    and _wrap_hi is not None
                    and _wrap_lo <= _seg_i <= _wrap_hi
                )
                if _in_fb:
                    _psty = (
                        "margin:0;"
                        if _seg_i == _wrap_hi
                        else "margin:0 0 10px 0;"
                    )
                    intro_html += f'<p style="{_psty}">{pp}</p>'
                else:
                    intro_html += f"<p>{pp}</p>"
                _p_used += 1
        elif kind == "cta":
            _flush_ul()
            if _cta_used < _max_intro_cta:
                _cta_margin = (
                    "10px 0 0 0"
                    if (
                        _wrap_lo is not None
                        and _wrap_hi is not None
                        and _wrap_lo <= _seg_i <= _wrap_hi
                    )
                    else "8px 0 0 0"
                )
                intro_html += (
                    f'<p style="margin:{_cta_margin};padding:0;">{_bullet_html(chunk)}</p>'
                )
                _cta_used += 1
        else:
            if _li_used < _max_intro_li:
                _ul_items.append(_bullet_html(chunk))
                _li_used += 1
        if _wrap_hi is not None and _seg_i == _wrap_hi:
            intro_html += "</div>"

    _flush_ul()

    prefs_link = (pref_url or optout0 or "").strip()

    def _legal_subscription_notice_html(line: str, link: str) -> str:
        s = (line or "").strip()
        if not s:
            return ""
        esc = html_escape(s)
        if link:
            esc = re.sub(
                r"(?i)cliquez\s+ici\b",
                lambda m: (
                    f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
                    'style="color:#0d9488;text-decoration:underline;">'
                    f"{html_escape(m.group(0))}</a>"
                ),
                esc,
            )
        return (
            f"<p style=\"color:#64748b;font-size:12px;line-height:1.45;margin:16px 0 0 0;\">{esc}</p>"
        )

    # Citation mise en valeur (seulement si absente du corps — le template Sheets peut déjà la porter)
    quote_txt = (
        "LumenVia n'est pas là pour remplacer la rencontre, mais pour la préparer, "
        "afin que chaque messe devienne une rencontre plus consciente avec le Christ."
    )
    if not re.search(r"remplacer la rencontre", intro_html, flags=re.I):
        intro_html += (
            "<p style=\"margin-top:14px;padding:10px 12px;border-left:4px solid #0d9488;"
            "background:#f0fdfa;color:#0b2745;border-radius:10px;\">"
            f"<em>{html_escape(quote_txt)}</em></p>"
        )

    # Bloc cartes : uniquement l’image illustrée (les PDF/audio sont déjà couverts par le corps / puces).
    cards: list[str] = []
    if url_illu0:
        _illu_href = html_escape(url_app0 or url_illu0)
        _illu_src = html_escape(url_illu0)
        cards.append(
            "<div style=\"margin:16px 0;text-align:center;\">"
            f"<a href=\"{_illu_href}\" target=\"_blank\" rel=\"noopener noreferrer\">"
            f"<img src=\"{_illu_src}\" alt=\"\" "
            "style=\"border-radius:12px;max-width:260px;width:100%;height:auto;display:inline-block;border:0;\">"
            "</a></div>"
        )

    footer_links = []
    # Liens de footer (cibles fixes)
    if origin0:
        footer_links.append(
            f'<a href="{origin0.rstrip("/")}/?route=about" target="_blank" rel="noopener noreferrer">Accéder à LumenVia</a>'
        )
    footer_html = " • ".join(footer_links)

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append("<html><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append("<style>")
    parts.append("body{font-family:Montserrat,Helvetica,Arial,sans-serif;line-height:1.55;color:#2F3640;background:#ffffff;}")
    parts.append(".wrap{max-width:640px;margin:0 auto;padding:18px;}")
    parts.append(".title{font-family:'Playfair Display',Georgia,'Times New Roman',serif;font-size:20px;font-weight:900;margin:0 0 6px 0;color:#2F3640;}")
    parts.append(".sub{color:#334155;margin:0 0 14px 0;}")
    parts.append(".hr{height:1px;background:#e7e5e4;margin:14px 0;}")
    parts.append("a{color:#2F3640;}")
    # Identité JOPAI© (immuable) dans l'e-mail
    parts.append(".jopai{font-family:Montserrat,Helvetica,Arial,sans-serif;font-size:12px;letter-spacing:0.3px;}")
    parts.append(".jopai .jop{font-weight:800;color:#0d9488;}")
    parts.append(".jopai .ai{font-style:italic;color:#0b2745;}")
    parts.append(".jopai .rest{color:#0b2745;}")
    parts.append(".jopai-inline{font-family:Montserrat,Helvetica,Arial,sans-serif;letter-spacing:0.3px;white-space:nowrap;}")
    parts.append(".jopai-inline .jop{font-weight:800;color:#0d9488;}")
    parts.append(".jopai-inline .ai{font-style:italic;color:#0b2745;}")
    parts.append("</style></head><body><div class=\"wrap\">")
    parts.append(f"<p><strong>Bonjour {who},</strong></p>")
    parts.append(intro_html)
    parts.append("".join(cards))
    if footer_html:
        parts.append("<div class=\"hr\"></div>")
        parts.append(f"<p style=\"color:#475569;font-size:12px;\">{footer_html}</p>")
    if legal_notice_line:
        parts.append(_legal_subscription_notice_html(legal_notice_line, prefs_link))
    parts.append("<div class=\"hr\"></div>")
    parts.append(
        "<div class=\"jopai\">"
        "<span class=\"jop\">JOP</span><span class=\"ai\">AI</span><sup class=\"ai\">©</sup>"
        "<span class=\"rest\"> LumenVia - 2026 | TOUS DROITS RESERVES</span>"
        "</div>"
    )
    dn_email = html_escape(LUMENVIA_DEVELOPMENT_NOTICE)
    parts.append(
        f"<p style=\"color:#7F8C8D;font-size:10px;line-height:1.4;margin:14px 0 0 0;\"><em>{dn_email}</em></p>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)
