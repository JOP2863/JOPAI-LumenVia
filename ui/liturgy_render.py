from __future__ import annotations

import html
import re

import streamlit as st


def render_liturgy_block(title: str, text: str | None) -> None:
    st.markdown(f"**{title}**")
    if not text:
        st.write("—")
        return

    cleaned = clean_aelf_text_for_display(text)
    # On laisse le HTML (<p>) gérer les retours, tout en échappant le contenu.
    html_body = _to_paragraph_html(cleaned)
    st.markdown(
        f"""
<div class="liturgical-reading">{html_body}</div>
""",
        unsafe_allow_html=True,
    )


def clean_aelf_text_for_display(text: str) -> str:
    """
    Nettoyage "présentation" uniquement (ne change pas la logique API).
    - Normalise retours ligne: \\r\\n -> \\n
    - Nettoie les espaces fin de ligne
    - Évite les blocs trop aérés
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]
    # Pas plus d'une ligne vide consécutive
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln.strip() == "":
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def _to_paragraph_html(text: str) -> str:
    # Sépare par paragraphes sur lignes vides.
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: list[str] = []
    for p in paras:
        # Dans un paragraphe, on remplace les retours simples par des espaces.
        p = " ".join([ln.strip() for ln in p.split("\n") if ln.strip()])
        p = " ".join(p.split())
        # Règles de mise en forme “lectures” (lisibilité) :
        # - Retours à la ligne après ponctuation forte ; : ! ? et après les points avant une majuscule.
        # - Pas de paragraphes artificiels ici : uniquement des <br/> dans le <p>.
        p = re.sub(r"([;:!?])\s+", r"\1\n", p)
        p = re.sub(r"\.\s+(?=[A-ZÀ-ÖØ-Ý«“\"(])", ".\n", p)
        escaped = html.escape(p)
        out.append(f"<p>{escaped.replace(chr(10), '<br/>')}</p>")
    return "\n".join(out)

