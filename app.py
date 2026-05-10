"""
Point d’entrée LumenVia : styles, session, query params, navigation, dispatch des routes.

La logique métier et les caches Streamlit sont dans ``core/*`` et ``ui/streamlit_caches.py``.
Les imports ``import app as ap`` attendent les alias avec préfixe ``_`` ci-dessous (contrat historique).
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from core.audio_mime_utils import count_words as _count_words, ext_from_mime as _ext_from_mime
from core.catechese_section_strip import strip_catechese_bridge as _strip_catechese_bridge
from core.feedback_survey_links import build_feedback_survey_url, wrap_feedback_cta_with_link
from core.french_date_labels import (
    french_day_month_year as _french_day_month_year,
    french_long_date_label as _french_long_date_label,
    french_weekday_day_month_year as _french_weekday_day_month_year,
    offline_cache_caption as _offline_cache_caption,
)
from core.gcp_service_account_fingerprint import service_account_fingerprint as _service_account_fingerprint
from core.liturgy_display_helpers import (
    cycle_year_display as _cycle_year_display,
    explain_liturgical_color as _explain_liturgical_color,
    explain_liturgical_cycle as _explain_liturgical_cycle,
    explain_liturgical_time as _explain_liturgical_time,
    extract_liturgical_week_num as _extract_liturgical_week_num,
    jour_liturgique_nom as _jour_liturgique,
    jopai_mark_html as _jopai_mark_html,
    liturgy_cover_pdf_title as _liturgy_cover_pdf_title,
    liturgy_display_label as _liturgy_display_label,
)
from core.public_listen_url import public_app_listen_url as _public_app_listen_url_core
from core.sunday_existing_outputs import (
    fetch_existing_fascicule_pdf_bytes as _fetch_existing_fascicule_pdf_bytes,
    fetch_existing_readings_audio as _fetch_existing_readings_audio,
    fetch_existing_sunday_bundle as _fetch_existing_sunday_bundle,
    fetch_liturgy_illustration_display_bytes as _fetch_liturgy_illustration_display_bytes,
    fetch_liturgy_illustration_full_bytes as _fetch_liturgy_illustration_full_bytes,
    has_readings_audio_for_gen as _has_readings_audio_for_gen,
    latest_generation_row_for_sunday as _latest_generation_row_for_sunday,
    sheet_day_key as _sheet_day_key,
    synthesis_audio_gcs_path_for_gen as _synthesis_audio_gcs_path_for_gen,
)
from core.sunday_gemini_tts import chunk_text_for_tts as _chunk_text_for_tts, tts_gemini_chunked_bytes as _tts_gemini_chunked_bytes
from core.sunday_readings_tts import (
    compose_readings_tts_text as _compose_readings_tts_text,
    compose_synthesis_tts_text as _compose_synthesis_tts_text,
    plain_readings_for_tts as _plain_readings_for_tts,
)
from core.synthesis_vertex_prompt import build_sunday_vertex_synthesis_prompt as _build_prompt
from ui.navigation import lumenvia_app_origin_url as _lumenvia_app_origin_url, top_nav
from ui.route_dispatch import dispatch_route
from ui.streamlit_caches import (
    adm_feedback_sheet_fetch_cached as _adm_feedback_sheet_fetch_cached,
    cached_aelf,
    load_prompt_templates_cached as _load_prompt_templates_cached,
    load_voix_rules_cached as _load_voix_rules_cached,
)
from ui.styles import set_page_style
from ui.sunday_liturgy_illustration import try_show_liturgy_illustration as _try_show_liturgy_illustration


def _public_app_listen_url(*, date_str: str) -> tuple[str | None, str | None]:
    base = ""
    try:
        s = st.secrets
        base = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip().rstrip("/")
    except Exception:
        pass
    return _public_app_listen_url_core(date_str=date_str, base_public_app_url=base or None)


def lumenvia_feedback_survey_abs_url(origin: str | None, *, recipient_email: str | None = None) -> str:
    """URL absolue page « Donner votre avis » (lien depuis e-mails)."""
    base_raw = ((origin or "").strip() if origin else "") or (_lumenvia_app_origin_url() or "").strip()
    return build_feedback_survey_url(base_url=base_raw.rstrip("/"), recipient_email=recipient_email)


def lumenvia_wrap_feedback_cta_with_link(
    fragment: str, *, origin_for_href: str | None = None, recipient_email: str | None = None
) -> str:
    """Encapsule la phrase 👉 … Donner mon avis … en lien `<a>` (fragment HTML léger ou texte)."""
    url = lumenvia_feedback_survey_abs_url(origin_for_href, recipient_email=recipient_email)
    return wrap_feedback_cta_with_link(fragment, survey_url=url)


def _inject_admin_phone_preview_css() -> None:
    """Admin uniquement : largeur réglable + cadre arrondi type smartphone pour recette bureau."""
    if not st.session_state.get("admin_authenticated"):
        return
    if not st.session_state.get("admin_phone_preview"):
        return
    try:
        wpx = int(st.session_state.get("admin_mobile_preview_width", 390) or 390)
    except Exception:
        wpx = 390
    wpx = max(280, min(560, wpx))
    st.markdown(
        f"""
<style>
/* Aperçu smartphone — activé depuis la tuile Simulateur mobile (cadre téléphone) */
[data-testid="stAppViewContainer"] {{
  background: linear-gradient(165deg, #4a4a52 0%, #1e1e22 55%, #121214 100%) !important;
  min-height: 100vh !important;
}}
[data-testid="stHeader"] {{
  background: transparent !important;
}}
section[data-testid="stMain"] {{
  max-width: {wpx}px !important;
  width: 100% !important;
  margin-left: auto !important;
  margin-right: auto !important;
  margin-top: 0.75rem !important;
  margin-bottom: 1.5rem !important;
  box-sizing: border-box !important;
  border: 12px solid #0d0d0f !important;
  border-radius: 40px !important;
  box-shadow:
    0 0 0 1px rgba(255, 255, 255, 0.07) inset,
    0 22px 56px rgba(0, 0, 0, 0.48) !important;
  min-height: min(88vh, 844px) !important;
  overflow-x: hidden !important;
  background: var(--liturgie-cream, #fdfbf7) !important;
}}
section[data-testid="stMain"] .block-container {{
  padding-left: max(0.65rem, env(safe-area-inset-left, 0px)) !important;
  padding-right: max(0.65rem, env(safe-area-inset-right, 0px)) !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )


def _lumenvia_narrow_nav_from_query() -> bool:
    """`?lumenvia_narrow_nav=1` : iframe où le viewport CSS ne reflète pas la largeur utile."""
    try:
        v = str(st.query_params.get("lumenvia_narrow_nav") or "").strip().lower()
    except Exception:
        v = ""
    return v in ("1", "true", "yes", "on")


def main() -> None:
    set_page_style()
    if _lumenvia_narrow_nav_from_query():
        st.session_state["lumenvia_narrow_nav"] = True
    if st.session_state.pop("_lumenvia_enable_phone_preview", False):
        st.session_state["admin_phone_preview"] = True
    _inject_admin_phone_preview_css()

    if "route" not in st.session_state:
        st.session_state.route = "about"

    params = st.query_params
    adm = (params.get("admin") or "").strip().lower()
    if adm == "1":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_resources"
        else:
            st.session_state.route = "admin_login"
    elif adm == "login":
        st.session_state.route = "admin_login"
    elif adm == "step3":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_step3"
        else:
            st.session_state.route = "admin_login"
    elif adm == "cdc":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_cdc"
        else:
            st.session_state.route = "admin_login"
    elif adm in ("mob", "mobile"):
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_mobile_sim"
        else:
            st.session_state.route = "admin_login"
    elif adm == "refactor":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_refactor"
        else:
            st.session_state.route = "admin_login"
    elif adm in ("recette", "recette_continue", "tests"):
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_recette_continue"
        else:
            st.session_state.route = "admin_login"
    elif adm == "granularity":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_granularity"
        else:
            st.session_state.route = "admin_login"

    sun_qp = (params.get("sunday") or "").strip()
    if sun_qp and len(sun_qp) >= 10:
        try:
            date.fromisoformat(sun_qp[:10])
            st.session_state.route = "sunday"
            st.session_state["_lumenvia_sunday_qs"] = sun_qp[:10]
        except Exception:
            pass
        try:
            del st.query_params["sunday"]
        except Exception:
            pass
        try:
            if "open_cal" in st.query_params:
                del st.query_params["open_cal"]
        except Exception:
            pass

    try:
        rte_q = str(params.get("route") or "").strip().lower()
    except Exception:
        rte_q = ""
    if rte_q in ("feedback", "avis"):
        st.session_state.route = "feedback"
    elif rte_q in ("account", "compte"):
        st.session_state.route = "account"
    elif rte_q in ("join", "nous_rejoindre", "nousrejoindre"):
        st.session_state.route = "join"
    elif rte_q in ("reset_password", "reset", "pwd_reset"):
        st.session_state.route = "reset_password"
    elif rte_q == "admin_refactor":
        st.session_state.route = "admin_refactor"
    elif rte_q in ("admin_recette_continue", "admin_recette"):
        st.session_state.route = "admin_recette_continue"
    elif rte_q == "admin_granularity":
        st.session_state.route = "admin_granularity"

    if adm in ("1", "login", "step3", "cdc", "mob", "mobile", "refactor", "recette", "recette_continue", "tests", "granularity"):
        try:
            del st.query_params["admin"]
        except Exception:
            pass

    route = top_nav()
    st.divider()

    dispatch_route(route)


if __name__ == "__main__":
    main()
