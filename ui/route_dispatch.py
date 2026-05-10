"""Dispatch des routes publiques et admin (allège app.py)."""

from __future__ import annotations

import streamlit as st

from ui.admin.accounts import render_admin_accounts
from ui.admin.cahier_charges import render_admin_cahier_charges
from ui.admin.emailing import render_admin_emailing
from ui.admin.feedback_insights import render_admin_feedback_insights
from ui.admin.granularity_audit import render_admin_granularity_audit
from ui.admin.illustration_vertex import render_admin_step3
from ui.admin.login import render_admin_login
from ui.admin.mobile_simulator import render_admin_mobile_simulator
from ui.admin.plan_consolide import render_admin_plan_consolide
from ui.admin.readings_cache import render_admin_readings_cache
from ui.admin.recette_continue import render_admin_recette_continue
from ui.admin.scheduler import render_admin_scheduler
from ui.admin.test_resources import render_admin_test_resources
from ui.admin.thumbs import render_admin_thumbs
from ui.admin.vision_text import render_admin_vision_text
from ui.pages.about import render_about
from ui.pages.feedback import render_feedback
from ui.pages.join_account import render_join, render_reset_password
from ui.pages.memo import render_memo
from ui.pages.sunday import render_sunday
from ui.refactor_migration_control import render_admin_refactor_migration


def dispatch_route(route: str) -> None:
    """Affiche la page correspondant à ``route`` (session)."""
    if route == "about":
        render_about()
    elif route == "sunday":
        render_sunday()
    elif route == "memo":
        render_memo()
    elif route == "join":
        render_join()
    elif route == "feedback":
        render_feedback()
    elif route == "account":
        render_join()
    elif route == "reset_password":
        render_reset_password()
    elif route == "admin_login":
        render_admin_login()
    elif route == "admin_step3":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_step3"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_step3()
    elif route == "admin_thumbs":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_thumbs"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_thumbs()
    elif route == "admin_resources":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_res"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_test_resources()
    elif route == "admin_plan":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_plan"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_plan_consolide()
    elif route == "admin_cdc":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_cdc"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_cahier_charges()
    elif route == "admin_vision":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_vision"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_vision_text()
    elif route == "admin_accounts":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_accounts"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_accounts()
    elif route == "admin_emailing":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_emailing"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_emailing()
    elif route == "admin_feedback_insights":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_fb_ins"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_feedback_insights()
    elif route == "admin_scheduler":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_scheduler"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_scheduler()
    elif route == "admin_readings_cache":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_readings_cache"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_readings_cache()
    elif route == "admin_refactor":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_refactor"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_refactor_migration()
    elif route == "admin_recette_continue":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_recette_continue"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_recette_continue()
    elif route == "admin_granularity":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_granularity"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_granularity_audit()
    elif route == "admin_mobile_sim":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_mobile_sim"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_mobile_simulator()
    else:
        render_about()
