"""Bandeau frise 16:9 — sous le titre, muet, gabarit NEXUS. Rien si fichier absent."""

from __future__ import annotations

import streamlit as st

from core.frise_identitaire import chemin_frise_identitaire


def afficher_frise_identitaire() -> None:
    """``st.video`` local, loop + autoplay + muted. Pas de chemin affiché."""
    path = chemin_frise_identitaire()
    if path is None:
        return
    _inject_css_bandeau()
    st.video(str(path), format="video/mp4", loop=True, autoplay=True, muted=True)


def _inject_css_bandeau() -> None:
    try:
        st.html(
            "<style>"
            ".block-container [data-testid='stVideo']:first-of-type video{"
            "width:100%;max-height:280px;object-fit:cover;display:block;"
            "border-radius:10px;border:1px solid rgba(196,165,116,.35);background:#0b2745;}"
            "</style>"
        )
    except Exception:
        pass


def render_titre_puis_frise(*, titre: str, baseline: str) -> None:
    """Étalon : nom + baseline, puis frise. Jamais l’inverse."""
    st.title(titre)
    cap = str(baseline or "").strip()
    if cap:
        st.caption(cap)
    afficher_frise_identitaire()
