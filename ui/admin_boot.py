"""Prélude des pages admin lourdes : charge les données avant le premier bloc visible."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st

_BOOT_CSS = """
<style>
.lv-admin-boot-screen {
  min-height: 14rem;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #6b5918;
  font-family: Lora, Georgia, serif;
  font-size: 1.05rem;
  line-height: 1.45;
  text-align: center;
  padding: 1.5rem 1rem;
  opacity: 0.95;
}
</style>
"""


def admin_initial_load(message: str = "Chargement…") -> object:
    """Placeholder plein écran jusqu’à ``admin_initial_load_done`` (évite l’effet morceau par morceau)."""
    slot = st.empty()
    slot.markdown(
        _BOOT_CSS + f'<div class="lv-admin-boot-screen">✨ {html_escape(message)}</div>',
        unsafe_allow_html=True,
    )
    return slot


def admin_initial_load_done(slot: object | None) -> None:
    if slot is None:
        return
    try:
        slot.empty()  # type: ignore[union-attr]
    except Exception:
        pass