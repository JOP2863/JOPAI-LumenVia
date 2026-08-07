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
    from core.emailing import replace_mission_quote_in_text
    from core.prompt_locale import coerce_aip_langue

    _lg_mail = coerce_aip_langue(values0.get("pref_langue"))
    intro_text = replace_mission_quote_in_text(intro_text or "", pref_langue=_lg_mail)

    prenom = (values0.get("prenom") or "").strip() or "—"
    nom = (values0.get("nom") or "").strip() or ""
    origin0 = (values0.get("origin") or "").strip()
    url_pdf0 = (values0.get("url_pdf") or "").strip()
    url_audio0 = (values0.get("url_audio") or "").strip()
    url_audio_readings0 = (values0.get("url_audio_readings") or "").strip()
    url_app0 = (values0.get("url_app") or "").strip()
    url_illu0 = (values0.get("url_illustration") or "").strip()
    illu_desc0 = (values0.get("illustration_description") or "").strip()
    # Variantes pour filtrer la légende du corps (FR pivot + version localisée).
    _illu_desc_match: list[str] = [illu_desc0] if illu_desc0 else []
    if illu_desc0 and _lg_mail != "FR":
        _low_desc = illu_desc0.casefold().replace("’", "'")
        _still_fr = any(
            m in _low_desc
            for m in (
                "l'image",
                "composition",
                "fond clair",
                "bordure dor",
                "eucharist",
                "évoquent",
                "evoquent",
                "encadrent",
                "ornée",
                "ornee",
            )
        )
        if _still_fr:
            try:
                from core.emailing import localize_illustration_description_for_email

                _loc = localize_illustration_description_for_email(
                    illu_desc0, pref_langue=_lg_mail, cfg=None
                )
                if _loc and _loc.strip():
                    if _loc.strip() != illu_desc0:
                        _illu_desc_match.append(_loc.strip())
                    illu_desc0 = _loc.strip()
            except Exception:
                pass
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

    def _norm_line(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip())

    def _is_illustration_description_line(ln: str) -> bool:
        """True si la ligne est (ou porte) la légende IA — rendue sous l’image, pas dans le corps."""
        s = (ln or "").strip()
        if not s:
            return False
        if re.search(r"(?is)\{\{\s*illustration_description\s*\}\}", s):
            return True
        if not _illu_desc_match:
            return False
        nl = _norm_line(s)
        for cand in _illu_desc_match:
            nd = _norm_line(cand)
            if not nl or not nd:
                continue
            if nl == nd:
                return True
            # Même texte tronqué (aperçu client mail) ou variante apostrophe.
            if len(nl) >= 40 and (nd.startswith(nl) or nl.startswith(nd)):
                return True
        return False

    # On retire les lignes techniques / URLs signées du corps (l’illustration est rendue en image seule sous le texte).
    raw_lines = [ln.strip() for ln in (intro_text or "").replace("\r\n", "\n").split("\n")]
    raw_lines = [ln for ln in raw_lines if ln]
    filtered: list[str] = []
    for ln in raw_lines:
        if re.match(
            r"(?i)^(bonjour|hallo|guten\s+tag|hello|hi|hola|ciao|buongiorno)\b",
            ln,
        ):
            continue
        if re.match(r"(?i)^illustration\s*:\s*https?://", ln):
            continue
        # Évite d'afficher des URLs signées interminables en clair
        if "X-Goog-Algorithm=" in ln or "X-Goog-Credential=" in ln or "X-Goog-Signature=" in ln:
            continue
        if re.search(r"(?is)\{\{\s*affichage.*illustration", ln):
            continue
        if _is_illustration_description_line(ln):
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
        # DE / EN / ES / IT
        if re.search(
            r"(?i)(sie erhalten diese e-?mail|you are receiving this e-?mail|"
            r"recibes este correo|ricevi questa e-?mail)",
            s,
        ):
            return True
        if re.search(
            r"(?i)(abmelden|unsubscribe|darse de baja|cancellare l['’]iscrizione|"
            r"preferenzen|preferences|preferencias|preferenze)",
            s,
        ) and re.search(r"(?i)(hier|here|aqu[ií]|qui|cliquez|click|klicken)", s):
            return True
        return False

    def _is_feedback_survey_bullet(ln: str) -> bool:
        """True uniquement pour la ligne CTA questionnaire (pas « L'Expérience Sonore »)."""
        if "👉" not in ln:
            return False
        if re.match(
            r"(?i)^(l['’]exp[eé]rience\s+sonore|die\s+klangerfahrung|"
            r"the\s+sound\s+experience|la\s+experiencia\s+sonora|"
            r"l['’]esperienza\s+sonora)\b",
            ln.strip(),
        ):
            return False
        return bool(
            re.search(
                r"(?i)donner\s+(mon\s+)?avis|avis\s+sur\s+cette\s+exp[eé]rience|"
                r"questionnaire|feedback|meinung|ihre\s+meinung|"
                r"leave\s+(your\s+)?feedback|give\s+(us\s+)?your\s+(feedback|opinion)|"
                r"dejar\s+(tu|su)\s+opini[oó]n|dare\s+(il\s+)?tuo\s+parere|"
                r"geben\s+sie\s+(uns\s+)?ihre\s+meinung",
                ln,
            )
        )

    def _is_resource_bullet_line(raw: str) -> bool:
        """Section média (PDF / audio / illustration) — titres FR + DE/EN/ES/IT ou ligne 👉 CTA."""
        lead = raw.lstrip("-•").lstrip()
        if raw.startswith(("-", "•")):
            return True
        if re.match(
            r"(?i)^("
            # FR
            r"la\s+synth[eè]se|l['’]essentiel|l['’]exp[eé]rience\s+sonore|la\s+parole|"
            r"l['’]audio\s+des\s+lectures|"
            # DE
            r"die\s+(illustrierte\s+)?zusammenfassung|das\s+wesentliche|das\s+wort|"
            r"die\s+klangerfahrung|"
            # EN
            r"the\s+(illustrated\s+)?summary|the\s+essentials?|the\s+word|"
            r"the\s+sound\s+experience|"
            # ES
            r"la\s+s[ií]ntesis|lo\s+esencial|la\s+palabra|la\s+experiencia\s+sonora|"
            # IT
            r"la\s+sintesi|l['’]essenziale|la\s+parola|l['’]esperienza\s+sonora"
            r")\b",
            lead,
        ):
            return True
        if re.match(
            r"(?i)^(l['’]illustration|die\s+illustration|the\s+illustration|"
            r"la\s+ilustraci[oó]n|l['’]illustrazione)\b",
            lead,
        ) and "👉" in raw:
            return True
        # Filet : toute ligne 👉 liée à un média (templates traduits sans tiret).
        if "👉" in raw and re.search(
            r"(?i)\b("
            r"pdf|audio|illustration|image|bild|"
            r"synth[eè]se|zusammenfassung|summary|s[ií]ntesis|sintesi|"
            r"lesungen|lectures|readings|lecturas|letture|"
            r"h[öo]ren|listen|[ée]couter|escuch|ascolt|"
            r"t[eé]l[eé]charg|download|herunterlad|descarg|scaric"
            r")\b",
            raw,
        ):
            return True
        return False

    legal_notice_line = ""

    # Ordre du template : paragraphes et puces entrelacés (évite tout le prose puis toute la liste).
    segments: list[tuple[str, str]] = []
    for ln in filtered:
        if _is_list_unsubscribe_line(ln):
            legal_notice_line = ln.strip()
            continue
        raw = ln.strip()
        lead = raw.lstrip("-•").lstrip()
        if _is_resource_bullet_line(raw):
            segments.append(("li", lead))
            continue
        if _is_feedback_survey_bullet(raw):
            # Pas en <ul>/<li> : évite une puce • avant le CTA questionnaire (absente du template).
            segments.append(("cta", lead))
            continue
        segments.append(("p", raw))

    fb_url0 = ap.lumenvia_feedback_survey_abs_url(origin0, recipient_email=email0 or None, lang=_lg_mail)

    def _newsletter_cta_href(bb0: str, right: str) -> str:
        """URL du bouton pill pour une ligne « … 👉 libellé » (corps newsletter)."""
        href = ""
        if url_pdf0 and re.search(
            r"(?i)\bpdf\b|synth[èe]se.*pdf|zusammenfassung.*pdf|summary.*pdf|"
            r"s[ií]ntesis.*pdf|sintesi.*pdf",
            bb0,
        ):
            href = url_pdf0
        elif url_audio_readings0 and re.search(
            r"(?i)parole.*audio|lectures|textes\s+bibliques|[ée]critures|"
            r"das\s+wort|lesungen|readings|biblical|la\s+palabra|lecturas|"
            r"la\s+parola|letture",
            bb0,
        ):
            href = url_audio_readings0
        elif url_audio0 and re.search(
            r"(?i)audio|essentiel|wesentliche|essentials?|s[ií]ntesis|sintesi|"
            r"zusammenfassung|[ée]couter|h[öo]ren|listen",
            bb0,
        ) and not re.search(
            r"(?i)lectures|lesungen|readings|lecturas|letture|"
            r"parole.*\(lectures\)|das\s+wort|la\s+palabra|la\s+parola|"
            r"textes\s+bibliques",
            bb0,
        ):
            href = url_audio0
        elif url_illu0 and re.search(
            r"(?i)image|illustration|bild|ilustraci[oó]n|illustrazione", bb0
        ):
            href = url_illu0
        if not href and fb_url0 and re.search(
            r"(?i)donner\s+mon\s+avis|avis\s+sur\s+cette\s+exp[eé]rience|"
            r"donner\s+votre\s+avis|feedback|meinung|opinion|opini[oó]n|parere",
            right,
        ):
            href = fb_url0
        return href

    _cta_button_labels: list[str] = []
    for _k, _ch in segments:
        if _k not in ("li", "cta"):
            continue
        _bb = (_ch or "").strip()
        if "👉" not in _bb:
            continue
        _, _rt = _bb.split("👉", 1)
        _rt = (_rt or "").strip().lstrip("👉").strip()
        if _newsletter_cta_href(_bb, _rt):
            _cta_button_labels.append(_rt)

    def _uniform_pill_width_px(labels: list[str]) -> int | None:
        if not labels:
            return None
        longest = max(labels, key=len)
        # ~14px bold sans-serif : ordre de grandeur ~8px/caractère (FR) + padding horizontal du <a>
        w = int(len(longest) * 8.2) + 52
        return min(480, max(216, w))

    newsletter_cta_uniform_px = _uniform_pill_width_px(_cta_button_labels)

    _wrap_lo: int | None = None
    _wrap_hi: int | None = None
    for _wi, (_wk, _wch) in enumerate(segments):
        if _wk == "p" and re.match(
            r"(?i)^("
            r"beau\s+chemin\s+vers\s+dimanche|"
            r"sch[öo]nen?\s+weg\s+(zum\s+)?sonntag|"
            r"have\s+a\s+(blessed|good)\s+(path\s+)?(toward\s+)?sunday|"
            r"buen\s+camino\s+(hacia\s+)?(el\s+)?domingo|"
            r"buon\s+cammino\s+(verso\s+)?(la\s+)?domenica"
            r")\b",
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

    def _pill_cta_button(*, href: str, label: str, uniform_width_px: int | None) -> str:
        """Bouton type « carte » LumenVia (turquoise #0d9488, texte blanc gras) — compatible clients mail (table)."""
        h = html_escape((href or "").strip())
        lab = html_escape((label or "").strip())
        if not h or not lab:
            return ""
        w = uniform_width_px if uniform_width_px and uniform_width_px > 0 else None
        td_wh = ""
        a_disp = 'display:inline-block;padding:11px 22px;'
        if w:
            td_wh = f"width:{w}px;min-width:{w}px;"
            a_disp = (
                "display:block;width:100%;box-sizing:border-box;text-align:center;"
                "padding:11px 16px;"
            )
        a_style = (
            f"{a_disp}"
            "font-family:Montserrat,Helvetica,Arial,sans-serif;"
            "font-size:14px;font-weight:700;line-height:1.35;color:#ffffff !important;text-decoration:none;"
            "border-radius:10px;mso-line-height-rule:exactly;"
        )
        return (
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
            'style="margin:10px 0 14px 0;border-collapse:separate;">'
            '<tr><td align="left" style="border-radius:10px;background:#0d9488;mso-padding-alt:0;'
            f'{td_wh}">'
            f'<a href="{h}" target="_blank" rel="noopener noreferrer" style="{a_style}">'
            f"{lab}</a>"
            "</td></tr></table>"
        )

    def _bullet_html(b: str) -> str:
        bb0 = (b or "").strip()
        if "👉" in bb0:
            left, right = bb0.split("👉", 1)
            left = left.strip()
            right = (right or "").strip().lstrip("👉").strip()
            href = _newsletter_cta_href(bb0, right)
            if href:
                btn = _pill_cta_button(
                    href=href, label=right, uniform_width_px=newsletter_cta_uniform_px
                )
                if left:
                    return (f"{_esc(left)}{btn}").strip()
                return btn.strip()
            if left:
                return f"{_esc(left)}<br><strong>{_esc(right)}</strong>".strip()
            return f"<strong>{_esc(right)}</strong>".strip()
        return _esc(bb0)

    intro_html = ""
    _ul_items: list[str] = []
    _max_intro_paras = 40
    _max_intro_li = 16
    _max_intro_cta = 6
    _p_used = 0
    _li_used = 0
    _cta_used = 0

    # Bloc illustration (image + légende) — injecté sous le CTA questionnaire, avant le bandeau légal.
    _illu_cards_html = ""
    if url_illu0:
        _illu_href = html_escape(url_app0 or url_illu0)
        _illu_src = html_escape(url_illu0)
        _alt = (
            html_escape((illu_desc0[:180] + "…") if len(illu_desc0) > 180 else illu_desc0)
            if illu_desc0
            else ""
        )
        _illu_parts: list[str] = [
            "<div style=\"margin:16px 0;text-align:center;\">"
            f"<a href=\"{_illu_href}\" target=\"_blank\" rel=\"noopener noreferrer\">"
            f"<img src=\"{_illu_src}\" alt=\"{_alt}\" "
            "style=\"border-radius:12px;max-width:260px;width:100%;height:auto;display:inline-block;border:0;\">"
            "</a></div>"
        ]
        if illu_desc0:
            _illu_parts.append(
                "<p style=\"margin:8px auto 0 auto;max-width:32rem;text-align:center;"
                "font-size:13px;line-height:1.5;color:#475569;\">"
                f"{html_escape(illu_desc0)}</p>"
            )
        _illu_cards_html = "".join(_illu_parts)

    _illu_cards_appended = False
    _inject_illu_after_seg: int | None = None

    def _append_illustration_block() -> None:
        nonlocal intro_html, _illu_cards_appended
        if _illu_cards_appended or not _illu_cards_html:
            return
        intro_html += _illu_cards_html
        _illu_cards_appended = True

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
                    lang=_lg_mail,
                )
                pp = linkify_html_urls(pp)
                # Met en valeur JOPAI© comme dans le footer (couleurs/typo).
                pp = re.sub(
                    r"(?i)\bJOPAI\b",
                    '<span class="jopai-inline"><span class="jop">JOP</span><span class="ai">AI</span><sup class="ai">©</sup></span>',
                    pp,
                )
                for kw in (
                    "LumenVia",
                    "PDF",
                    "Audio",
                    "Illustration",
                    "messe",
                    "Parole",
                    "Zusammenfassung",
                    "Lesungen",
                    "Summary",
                    "Readings",
                ):
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
                if _is_feedback_survey_bullet((chunk or "").strip()):
                    _inject_illu_after_seg = _seg_i
        else:
            if _li_used < _max_intro_li:
                _ul_items.append(_bullet_html(chunk))
                _li_used += 1
        if _wrap_hi is not None and _seg_i == _wrap_hi:
            intro_html += "</div>"
        if _inject_illu_after_seg is not None and _seg_i == _inject_illu_after_seg:
            _append_illustration_block()

    _flush_ul()

    if not _illu_cards_appended and _illu_cards_html:
        _append_illustration_block()

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

    # Citation mise en valeur — localisée ; remplace d’éventuels restes FR du template.
    from core.emailing import (
        LUMENVIA_MISSION_QUOTE_BY_LANG,
        lumenvia_mission_quote_for_lang,
    )

    quote_txt = lumenvia_mission_quote_for_lang(_lg_mail)
    _quote_variants: list[str] = []
    for _q in LUMENVIA_MISSION_QUOTE_BY_LANG.values():
        if not _q:
            continue
        for _qv in (_q, _q.replace("'", "’"), _q.replace("’", "'")):
            if _qv and _qv not in _quote_variants:
                _quote_variants.append(_qv)
    for _q in _quote_variants:
        if _q != quote_txt and _q in intro_html:
            intro_html = intro_html.replace(_q, quote_txt)
        _qe = html_escape(_q)
        _qt_esc = html_escape(quote_txt)
        if _qe != _qt_esc and _qe in intro_html:
            intro_html = intro_html.replace(_qe, _qt_esc)
    if quote_txt not in intro_html and html_escape(quote_txt) not in intro_html:
        intro_html += (
            "<p style=\"margin-top:14px;padding:10px 12px;border-left:4px solid #0d9488;"
            "background:#f0fdfa;color:#0b2745;border-radius:10px;\">"
            f"<em>{html_escape(quote_txt)}</em></p>"
        )

    footer_links = []
    # Liens de footer (cibles fixes)
    if origin0:
        _access_lbl = {
            "FR": "Accéder à LumenVia",
            "DE": "Zu LumenVia",
            "EN": "Go to LumenVia",
            "ES": "Ir a LumenVia",
            "IT": "Vai a LumenVia",
        }.get(_lg_mail, "Accéder à LumenVia")
        footer_links.append(
            f'<a href="{origin0.rstrip("/")}/?route=about" target="_blank" rel="noopener noreferrer">'
            f"{html_escape(_access_lbl)}</a>"
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
    _hello = {
        "FR": "Bonjour",
        "DE": "Hallo",
        "EN": "Hello",
        "ES": "Hola",
        "IT": "Ciao",
    }.get(_lg_mail, "Bonjour")
    parts.append(f"<p><strong>{_hello} {who},</strong></p>")
    try:
        from core.emailing import format_weekly_actualite_paragraph
    except Exception:  # pragma: no cover
        format_weekly_actualite_paragraph = None  # type: ignore[assignment]
    actu_raw = ""
    if format_weekly_actualite_paragraph is not None:
        actu_raw = format_weekly_actualite_paragraph(
            str(values0.get("message_actualite") or values0.get("actualite_lumenvia") or ""),
            pref_langue=_lg_mail,
        )
    if actu_raw:
        # Plusieurs lignes → <br> ; blocs séparés par ligne vide → <p> distincts.
        for block in re.split(r"\n\s*\n", actu_raw):
            block = (block or "").strip()
            if not block:
                continue
            parts.append(
                "<p style=\"margin:0 0 12px 0;font-style:italic;\">"
                + html_escape(block).replace("\n", "<br>")
                + "</p>"
            )
    parts.append(intro_html)
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
