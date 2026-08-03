"""Affichage Streamlit de l’illustration dominicale si présente dans GCS."""

from __future__ import annotations

import io

import streamlit as st

from core.french_date_labels import french_day_month_year
from core.sunday_existing_outputs import fetch_liturgy_illustration_display_bytes


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

    caption = f"Illustration du dimanche {french_day_month_year(date_str)}"
    comment_label = "Commentaire de l’image"
    no_comment = "Aucun commentaire d’illustration pour ce dimanche."
    try:
        from core.sunday_view_locale import sunday_ui

        ui = sunday_ui(pref_langue)
        caption = ui["illustration_caption"].format(date=french_day_month_year(date_str))
        comment_label = ui["image_comment"]
        no_comment = ui["no_image_comment"]
    except Exception:
        pass

    st.caption(caption)
    desc = str(illustration_description or "").strip()
    if desc:
        st.markdown(f"**{comment_label}**")
        st.markdown(desc)
    elif pref_langue and str(pref_langue).upper() not in ("", "FR"):
        st.caption(no_comment)
