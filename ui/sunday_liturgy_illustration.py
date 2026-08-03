"""Affichage Streamlit de l’illustration dominicale si présente dans GCS."""

from __future__ import annotations

import io
from hashlib import sha256

import streamlit as st

from core.french_date_labels import french_day_month_year
from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue
from core.sunday_existing_outputs import fetch_liturgy_illustration_display_bytes


_MONTHS: dict[str, tuple[str, ...]] = {
    "FR": (
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ),
    "DE": (
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
    "EN": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "ES": (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ),
    "IT": (
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    ),
}


def _localized_sunday_date(date_str: str, pref_langue: object | None) -> str:
    lg = normalize_pref_langue(pref_langue)
    s = str(date_str or "").strip()[:10]
    try:
        y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        months = _MONTHS.get(lg) or _MONTHS[DEFAULT_PREF_LANGUE]
        month = months[m - 1]
        if lg == "EN":
            return f"{month} {d}, {y}"
        if lg == "DE":
            return f"{d}. {month} {y}"
        return f"{d} {month} {y}"
    except Exception:
        return french_day_month_year(date_str)


def _translate_illustration_comment(
    *,
    text_fr: str,
    pref_langue: object | None,
    date_str: str,
    cfg: object | None,
) -> str:
    """Traduit le commentaire ILUS (FR) vers la langue d’affichage ; cache session."""
    lg = normalize_pref_langue(pref_langue)
    src = (text_fr or "").strip()
    if not src or lg == DEFAULT_PREF_LANGUE:
        return src
    digest = sha256(src.encode("utf-8")).hexdigest()[:16]
    cache_key = f"ilus_desc_tr_{date_str}_{lg}_{digest}"
    cached = st.session_state.get(cache_key)
    if isinstance(cached, str) and cached.strip():
        return cached

    vertex = None
    try:
        sa = getattr(cfg, "gcp_service_account", None) if cfg is not None else None
        if sa:
            from core.vertex_gemini import VertexGeminiClient

            vertex = VertexGeminiClient(service_account_info=sa)
    except Exception:
        vertex = None

    from core.prompt_translate import translate_plain_fr_to

    out = translate_plain_fr_to(src, target_lang=lg, vertex_client=vertex)
    if out.strip():
        st.session_state[cache_key] = out
    return out or src


def try_show_liturgy_illustration(
    *,
    gcs: object,
    cfg: object,
    date_str: str,
    pref_langue: object | None = None,
    illustration_description: str | None = None,
    **_ignored: object,
) -> None:
    """Étape produit 3 : affiche une image si présente dans GCS (vignette ou originale)."""
    img_b = fetch_liturgy_illustration_display_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
    if not img_b:
        return
    try:
        st.image(io.BytesIO(img_b), use_container_width=True)
    except TypeError:
        try:
            st.image(io.BytesIO(img_b), use_column_width=True)
        except TypeError:
            st.image(io.BytesIO(img_b))

    date_lbl = _localized_sunday_date(date_str, pref_langue)
    caption = f"Illustration du dimanche {date_lbl}"
    comment_label = "Commentaire de l’image"
    no_comment = "Aucun commentaire d’illustration pour ce dimanche."
    try:
        from core.sunday_view_locale import sunday_ui

        ui = sunday_ui(pref_langue)
        caption = ui["illustration_caption"].format(date=date_lbl)
        comment_label = ui["image_comment"]
        no_comment = ui["no_image_comment"]
    except Exception:
        pass

    st.caption(caption)
    desc_fr = str(illustration_description or "").strip()
    if desc_fr:
        desc = _translate_illustration_comment(
            text_fr=desc_fr,
            pref_langue=pref_langue,
            date_str=date_str,
            cfg=cfg,
        )
        st.markdown(f"**{comment_label}**")
        st.markdown(desc)
    elif pref_langue and str(pref_langue).upper() not in ("", "FR"):
        st.caption(no_comment)
