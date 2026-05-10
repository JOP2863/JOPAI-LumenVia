"""Normalisation des libellés liturgiques et injection CSS `--liturgie-accent` (indépendant du module ``app``)."""

from __future__ import annotations

import re
import unicodedata

import streamlit as st


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def norm_key(s: str | None) -> str:
    t = strip_accents((s or "").strip().lower())
    return "".join(ch if ch.isalnum() else "_" for ch in t).strip("_")


def liturgical_accent_hex(couleur: str | None) -> str:
    k = norm_key(couleur)
    palette: dict[str, str] = {
        "vert": "#27AE60",
        "violet": "#8E44AD",
        "blanc": "#D4AF37",
        "rouge": "#C0392B",
        "rose": "#C0879C",
        "noir": "#2C3E50",
    }
    return palette.get(k, "#D4AF37")


def inject_liturgical_accent_style(couleur: str | None) -> None:
    hx = liturgical_accent_hex(couleur)
    if not re.match(r"^#[0-9A-Fa-f]{6}$", hx):
        hx = "#D4AF37"
    st.markdown(
        f"""
<style>
:root {{
  --liturgie-accent: {hx};
}}
button[kind="primary"] {{
  background-color: var(--liturgie-accent) !important;
  border-color: var(--liturgie-accent) !important;
}}
[data-testid="stBaseButton-segmented_controlActive"] {{
  background-color: var(--liturgie-accent) !important;
}}
.liturgical-reading {{
  border-left-color: var(--liturgie-accent) !important;
}}
button[kind="primary"]:hover {{
  filter: brightness(0.93);
}}
input:focus, textarea:focus {{
  border-color: var(--liturgie-accent) !important;
  box-shadow: 0 0 0 1px var(--liturgie-accent) !important;
}}
[data-testid="stAlert"] {{
  border-color: var(--liturgie-accent) !important;
}}
</style>
""",
        unsafe_allow_html=True,
    )
