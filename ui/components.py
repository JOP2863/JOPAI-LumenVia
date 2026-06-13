"""Composants Streamlit réutilisables (overlay, etc.)."""

from __future__ import annotations

import time
from html import escape as html_escape

import streamlit as st

# Streamlit n’envoie le calque au navigateur qu’après un court yield ;
# sans pause, l’étape 1/5 est écrasée par 2/5 avant d’être visible.
_OVERLAY_FLUSH_S = 0.35


def update_loading_overlay(
    slot: object,
    message: str,
    *,
    hint: str | None = None,
    elapsed_s: float | None = None,
    flush: bool = False,
) -> None:
    """Met à jour un calque plein écran déjà affiché (progression opération longue)."""
    if slot is None:
        return
    safe = html_escape(message or "")
    hint_html = ""
    if hint:
        hint_html = (
            f"<div style='font-family:system-ui,sans-serif;font-size:0.82rem;color:#5c534c;"
            f"margin-top:0.75rem;line-height:1.45;opacity:0.92'>{html_escape(hint)}</div>"
        )
    elapsed_html = ""
    if elapsed_s is not None and elapsed_s >= 0:
        elapsed_html = (
            f"<div style='font-family:system-ui,sans-serif;font-size:0.78rem;color:#8a7f76;"
            f"margin-top:0.55rem'>Temps écoulé : {int(elapsed_s)} s</div>"
        )
    slot.markdown(  # type: ignore[union-attr]
        f"""
<div id="lumenvia-loader-overlay" style="position:fixed;inset:0;background:rgba(253,251,247,0.88);backdrop-filter:blur(10px);z-index:999999;display:flex;align-items:center;justify-content:center;">
  <div style="font-family:'Cormorant Garamond',Georgia,serif;font-size:1.35rem;color:#342E29;text-align:center;max-width:min(520px,92vw);padding:1rem 1.25rem;border-bottom:2px solid #D4AF37;">
    ✨ {safe}
    {hint_html}
    {elapsed_html}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    if flush:
        time.sleep(_OVERLAY_FLUSH_S)


def loading_overlay(message: str = "LumenVia travaille pour toi…", *, flush: bool = True) -> object:
    """Calque plein écran (glassmorphism) pendant une opération serveur longue."""
    slot = st.empty()
    update_loading_overlay(slot, message, flush=flush)
    return slot
