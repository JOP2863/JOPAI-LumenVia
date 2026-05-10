"""Affichage Streamlit de l’illustration dominicale si présente dans GCS."""

from __future__ import annotations

import io

import streamlit as st

from core.french_date_labels import french_day_month_year
from core.sunday_existing_outputs import fetch_liturgy_illustration_display_bytes


def try_show_liturgy_illustration(*, gcs: object, cfg: object, date_str: str) -> None:
    """Étape produit 3 : affiche une image si présente dans GCS (vignette ou originale)."""
    img_b = fetch_liturgy_illustration_display_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
    if img_b:
        st.image(io.BytesIO(img_b), use_container_width=True)
        st.caption(f"Illustration du dimanche {french_day_month_year(date_str)}")
