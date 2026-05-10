"""Secrets administrateur (Streamlit) — séparé pour éviter les imports circulaires avec les pages."""

from __future__ import annotations

import streamlit as st


def admin_login_and_password() -> tuple[str, str]:
    """Identifiant et mot de passe administrateur (exclusivement via `st.secrets`).

    Important sécurité : pas de valeurs par défaut dans le code.
    En environnement public, l’admin doit rester désactivée tant que non configurée.
    """
    try:
        s = st.secrets
        login = str(s.get("ADMIN_LOGIN", s.get("admin_login", ""))).strip().lower()
        password = str(s.get("ADMIN_PASSWORD", s.get("admin_password", ""))).strip()
    except Exception:
        login, password = "", ""
    return login, password
