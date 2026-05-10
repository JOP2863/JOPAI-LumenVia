"""Réglage confort de lecture : trois niveaux de taille de texte (session Streamlit + CSS)."""

from __future__ import annotations

import streamlit as st

_LV_COMFORT_OPTS: tuple[str, ...] = ("standard", "large", "xlarge")
_LABELS: dict[str, str] = {
    "standard": "Standard",
    "large": "Grand",
    "xlarge": "Très grand",
}


def inject_reading_comfort_css() -> None:
    """Injecte des règles selon ``st.session_state['lv_text_comfort']`` (après la charte globale)."""
    raw = str(st.session_state.get("lv_text_comfort") or "standard").strip().lower()
    tier = raw if raw in _LV_COMFORT_OPTS else "standard"

    # Shell discret autour de l’expander (marges + libellé summary un peu plus fin)
    shell_css = """
div[class*="st-key-lv_reading_comfort_wrap"] [data-testid="stExpander"] {
  margin-bottom: 0.45rem !important;
}
div[class*="st-key-lv_reading_comfort_wrap"] [data-testid="stExpander"] summary {
  font-size: 0.94rem !important;
  font-weight: 600 !important;
  letter-spacing: 0.02em !important;
}
""".strip()

    if tier == "standard":
        st.markdown(
            f"<style>{shell_css}</style>",
            unsafe_allow_html=True,
        )
        return

    if tier == "large":
        reading_rem = "1.0625rem"
        prose_rem = "1.0425rem"
        lh_reading = "1.66"
    else:
        reading_rem = "1.125rem"
        prose_rem = "1.075rem"
        lh_reading = "1.72"

    st.markdown(
        f"""
<style>
{shell_css}
/* Confort lecture — {tier} : lectures liturgiques et paragraphes principaux */
section[data-testid="stMain"] .liturgical-reading,
section[data-testid="stMain"] .liturgical-reading p {{
  font-size: {reading_rem} !important;
  line-height: {lh_reading} !important;
}}
section[data-testid="stMain"] .liturgy-block {{
  font-size: {prose_rem} !important;
}}
section[data-testid="stMain"] [data-testid="stMarkdownContainer"] p,
section[data-testid="stMain"] [data-testid="stMarkdownContainer"] li {{
  font-size: {prose_rem} !important;
}}
</style>
        """.strip(),
        unsafe_allow_html=True,
    )


def render_reading_comfort_expander() -> None:
    """Expander « Confort de lecture » avec radio 3 niveaux (state clé ``lv_text_comfort``)."""
    with st.container(key="lv_reading_comfort_wrap"):
        with st.expander("Confort de lecture — taille du texte", expanded=False):
            st.caption(
                "Agrandit les textes des pages et des lectures pour un meilleur confort visuel."
            )
            st.radio(
                "Taille du texte",
                options=list(_LV_COMFORT_OPTS),
                format_func=lambda v: _LABELS.get(v, v),
                key="lv_text_comfort",
                horizontal=True,
                label_visibility="visible",
            )
