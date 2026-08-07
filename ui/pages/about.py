"""Page publique « À propos » (JOPAI LumenVia)."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st


def render_about() -> None:
    try:
        from core.pdf_locale import about_markdown_for_lang
        from core.ui_locale import get_ui_lang, t

        lg = get_ui_lang()
        body = about_markdown_for_lang(lg)
        # "JOPAI LumenVia" est un nom de marque — pas de traduction (identique dans les 5 langues).
        title = "JOPAI LumenVia"
        sources_h = t("about.references_title")
    except Exception:
        from core.pdf_locale import about_markdown_for_lang

        body = about_markdown_for_lang("FR")
        title = "JOPAI LumenVia"
        sources_h = "Références & sources"
        lg = "FR"

    st.title(title)
    try:
        st.image("Parole.jpg", use_container_width=True)
    except Exception:
        pass

    md = (body or "").strip()
    try:
        quote, rest = md.split("\n\n", 1)
    except Exception:
        quote, rest = md, ""
    qtxt = quote.strip().strip("«").strip("»").strip()
    if qtxt:
        st.markdown(
            f"<div style='text-align:center;color:var(--liturgie-accent);font-style:italic;"
            f"font-size:1.02rem;line-height:1.55;margin:0.25rem auto 0.95rem;max-width:min(44rem,95vw);'>"
            f"« {html_escape(qtxt.strip('* ').strip())} »</div>",
            unsafe_allow_html=True,
        )
    if rest.strip():
        st.markdown(rest.strip())
    st.subheader(sources_h)
    # Tableau sources : libellés FR (admin/technique) — inchangé ; contenu métier déjà multi-langues.
    st.markdown(
        """
**Lectures liturgiques**

| Langue | Source | Usage LumenVia |
|--------|--------|----------------|
| **FR** | [AELF](https://api.aelf.org/) (Association Épiscopale Liturgique pour les pays Francophones) | Source de production — textes de la messe via l’API publique (pas de clé). |
| **DE / EN / ES / IT / PT** | [Evangelizo — Reader Feed](https://feed.evangelizo.org/v2/reader.php) (*L’Évangile au Quotidien*) | Complément multi-langues : textes natifs complets (pas de traduction maison depuis l’AELF). Affichage dans l’app + cache RDC ; redistribution e-mail / TTS / PDF des textes hors FR soumise à confirmation des conditions Evangelizo. |

**Audios**

- **Voix** : synthèse vocale Google (Vertex / Gemini TTS) — lecture des textes déjà disponibles.
- **Ambiance** (intro / outro / fond) : clips **libres de droits** déposés par l’équipe dans l’Atelier audio — licences **CC0**, **domaine public** ou **CC-BY** (attribution). Aucune musique commerciale non licenciée.

Les illustrations et contenus générés par IA sont des aides à la méditation ; les textes liturgiques restent ceux des sources ci-dessus.
        """.strip()
    )
