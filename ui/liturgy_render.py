from __future__ import annotations

import html
import re

import streamlit as st

from core.aelf_text_cleanup import clean_aelf_text_for_display


def render_liturgy_block(
    title: str,
    text: str | None,
    *,
    intro_lue: str | None = None,
    ref: str | None = None,
) -> None:
    from core.aelf_reading_meta import reading_caption

    st.markdown(f"**{title}**")
    caption = reading_caption(intro_lue=intro_lue, ref=ref)
    if caption:
        st.markdown(
            f"<p style=\"margin:0.15rem 0 0.65rem;font-size:0.92rem;font-style:italic;"
            f"color:#5f4f3a;text-align:center;\">{html.escape(caption)}</p>",
            unsafe_allow_html=True,
        )
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

