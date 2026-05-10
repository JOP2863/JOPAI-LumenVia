"""Écran de connexion administration."""

from __future__ import annotations

import streamlit as st

from ui.admin.test_resources import collapse_admin_test_resources_expanders
from ui.admin_secrets import admin_login_and_password


def render_admin_login() -> None:
    st.title("Connexion administration")
    login_ok, pwd_ok = admin_login_and_password()
    if not (login_ok and pwd_ok):
        st.error(
            "Administration désactivée : configure `ADMIN_LOGIN` et `ADMIN_PASSWORD` dans `st.secrets` "
            "(Streamlit Cloud → Secrets) pour activer la connexion."
        )
        return
    with st.form("admin_login_form"):
        login_id = st.text_input("Identifiant", key="adm_login_id", autocomplete="username")
        pwd = st.text_input("Mot de passe", type="password", key="adm_login_pwd", autocomplete="current-password")
        submitted = st.form_submit_button("Connexion", type="primary", use_container_width=True)
    if submitted:
        if login_id.strip().lower() == login_ok and pwd == pwd_ok:
            st.session_state.admin_authenticated = True
            collapse_admin_test_resources_expanders()
            st.session_state.route = "admin_step3"
            st.rerun()
        else:
            st.error("Identifiant ou mot de passe incorrect.")
