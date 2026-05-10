"""Composants Streamlit réutilisables (overlay, etc.)."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st


def loading_overlay(message: str = "LumenVia travaille pour toi…") -> object:
    """Calque plein écran (glassmorphism) pendant une opération serveur longue."""
    slot = st.empty()
    safe = html_escape(message or "")
    slot.markdown(
        f"""
<div id="lumenvia-loader-overlay" style="position:fixed;inset:0;background:rgba(253,251,247,0.88);backdrop-filter:blur(10px);z-index:999999;display:flex;align-items:center;justify-content:center;">
  <div style="font-family:'Cormorant Garamond',Georgia,serif;font-size:1.35rem;color:#342E29;text-align:center;max-width:min(520px,92vw);padding:1rem 1.25rem;border-bottom:2px solid #D4AF37;">
    ✨ {safe}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    return slot
