"""Réglage confort de lecture : trois niveaux de taille de texte (session Streamlit + CSS)."""

from __future__ import annotations

import streamlit as st

_LV_COMFORT_OPTS: tuple[str, ...] = ("standard", "large", "xlarge")


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
/* Ne pas agrandir le Menu popover (entrées admin / captions) — sinon le panneau
   déborde sous le footer sans scroll utilisable, et « Grand » révèle paradoxalement plus d’items. */
[data-testid="stPopoverBody"] [data-testid="stMarkdownContainer"] p,
[data-testid="stPopoverBody"] [data-testid="stMarkdownContainer"] li,
[data-testid="stPopoverContent"] [data-testid="stMarkdownContainer"] p,
[data-testid="stPopoverContent"] [data-testid="stMarkdownContainer"] li {{
  font-size: 0.9rem !important;
  line-height: 1.35 !important;
}}
</style>
        """.strip(),
        unsafe_allow_html=True,
    )


def render_reading_comfort_expander() -> None:
    """Expander confort de lecture + sélecteur de langue UI public (FR/DE/EN/ES/IT/PT)."""
    try:
        from core.ui_locale import SUPPORTED_UI_LANGS, get_ui_lang, set_ui_lang, t
    except Exception:
        t = None  # type: ignore[assignment]
        get_ui_lang = None  # type: ignore[assignment]
        set_ui_lang = None  # type: ignore[assignment]
        SUPPORTED_UI_LANGS = ("FR",)  # type: ignore[assignment]

    title = t("comfort.title") if t else "Confort de lecture — taille du texte"
    caption = t("comfort.caption") if t else (
        "Agrandit les textes des pages et des lectures pour un meilleur confort visuel."
    )
    size_label = t("comfort.size_label") if t else "Taille du texte"
    labels = {
        "standard": t("comfort.standard") if t else "Standard",
        "large": t("comfort.large") if t else "Grand",
        "xlarge": t("comfort.xlarge") if t else "Très grand",
    }
    with st.container(key="lv_reading_comfort_wrap"):
        with st.expander(title, expanded=False):
            st.caption(caption)
            st.radio(
                size_label,
                options=list(_LV_COMFORT_OPTS),
                format_func=lambda v: labels.get(v, v),
                key="lv_text_comfort",
                horizontal=True,
                label_visibility="visible",
            )
            if get_ui_lang and set_ui_lang and t:
                cur = get_ui_lang()
                opts = list(SUPPORTED_UI_LANGS)
                idx = opts.index(cur) if cur in opts else 0
                pick = st.selectbox(
                    t("comfort.language_label"),
                    options=opts,
                    index=idx,
                    key="lv_ui_lang_picker",
                    help=t("comfort.language_caption"),
                )
                if pick and pick != cur:
                    try:
                        from core.ui_locale import switch_public_lang

                        switch_public_lang(pick, sync_sunday=True)
                    except Exception:
                        set_ui_lang(pick)
                        if pick in SUPPORTED_UI_LANGS:
                            st.session_state["sunday_view_pref_langue"] = pick
                    st.rerun()
