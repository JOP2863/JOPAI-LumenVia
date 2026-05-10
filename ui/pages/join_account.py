"""Pages inscription newsletter, compte, réinitialisation mot de passe."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re

import streamlit as st

from core.auth import hash_password, verify_password
from core.config import load_config
from core.sheets_db import (
    append_immutable_row,
    build_gspread_client,
    ensure_table,
    fetch_records,
    get_table_spec,
    sheet_row_status_is_live,
    utc_now_iso,
)
from core.subscriptions_util import latest_subscription_record, subscription_is_active
from ui.admin_secrets import admin_login_and_password
from ui.components import loading_overlay


def render_join() -> None:
    # Cette page sert à la fois à l'inscription newsletter et au "Mon compte" via lien e-mail.
    # /?route=account&email=... pré-remplit l'email.
    try:
        qp_email = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        qp_email = ""
    auth_em0 = str(st.session_state.get("auth_email_lc") or "").strip().lower()
    # "Mon compte" doit être accessible via route dédiée,
    # et ne doit pas remplacer l'écran newsletter quand l'utilisateur est connecté.
    cur_route = str(st.session_state.get("route") or "").strip().lower()
    is_account_view = cur_route == "account"
    st.title("Mon compte" if is_account_view else "S'inscrire à la newsletter")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante — inscription indisponible.")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    # Supersession immuable : avant d'ajouter une nouvelle ligne `users` pour un email,
    # on marque les lignes précédentes encore Actives comme Inactif (append-only + “une seule version live”).
    def _supersede_users_by_email(email_lc: str) -> None:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return
        try:
            from core.sheets_db import _resolve_table_name, compute_concat, SHEETS_ROW_STATUS_INACTIVE
        except Exception:
            return
        try:
            sh0 = gs.open_by_key(cfg.gsheet_id)
            ws0 = sh0.worksheet(_resolve_table_name(sh=sh0, table="users"))
            header0 = ws0.row_values(1)
            if not header0 or "status" not in header0:
                return
            col_status = header0.index("status") + 1
            col_concat = header0.index("concat") + 1 if "concat" in header0 else 0
            recs = ws0.get_all_records(numericise_ignore=["all"])
        except Exception:
            return
        for ix, r in enumerate(recs):
            if str(r.get("email") or "").strip().lower() != em0:
                continue
            if not sheet_row_status_is_live(r.get("status")):
                continue
            merged = dict(r)
            merged["status"] = SHEETS_ROW_STATUS_INACTIVE
            row_num = ix + 2
            try:
                ws0.update_cell(row_num, col_status, SHEETS_ROW_STATUS_INACTIVE)
                if col_concat:
                    ws0.update_cell(row_num, col_concat, compute_concat(merged, header=header0))
            except Exception:
                continue
    users: list[dict] = []
    subs: list[dict] = []
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=4000)
        subs = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=4000)
    except Exception:
        pass

    # --- Mon compte : connexion / création / activation (newsletter → compte) ---
    if is_account_view:
        if "auth_user_entity_id" not in st.session_state:
            st.session_state.auth_user_entity_id = ""
        if "auth_email_lc" not in st.session_state:
            st.session_state.auth_email_lc = ""

        user_entity_id = str(st.session_state.get("auth_user_entity_id") or "").strip()
        st.subheader("Connexion")
        if user_entity_id:
            email_disp = str(st.session_state.get("auth_email_lc") or "").strip()
            st.caption(f"Session active pour **{email_disp or 'ton compte'}**.")
            if st.button("Se déconnecter", type="secondary", key="acct_logout"):
                for k in ("auth_user_entity_id", "auth_email_lc"):
                    st.session_state.pop(k, None)
                st.session_state.pop("admin_authenticated", None)
                st.rerun()

            st.divider()
            st.subheader("Mes informations")
            em_acct = str(st.session_state.get("auth_email_lc") or "").strip().lower()
            adm_login0, _pw_adm = admin_login_and_password()
            is_admin_sess = bool(em_acct and em_acct == str(adm_login0 or "").strip().lower())

            def _live_user_profile() -> dict:
                rows = [
                    u
                    for u in users
                    if str(u.get("email") or "").strip().lower() == em_acct
                    and sheet_row_status_is_live(u.get("status"))
                ]
                if not rows:
                    return {}
                return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]

            rp = _live_user_profile()
            if is_admin_sess:
                st.info("Session administrateur : pas de fiche « utilisateur » à éditer ici.")
            elif not rp:
                st.warning("Aucune fiche utilisateur active trouvée pour cet e-mail.")
            else:
                with st.form("acct_edit_profile"):
                    e_fn = st.text_input("Prénom", value=str(rp.get("first_name") or "").strip(), key="acct_edit_fn")
                    e_ln = st.text_input("Nom", value=str(rp.get("last_name") or "").strip(), key="acct_edit_ln")
                    e_ph = st.text_input(
                        "Téléphone (optionnel, format international)",
                        value=str(rp.get("phone_e164") or "").strip(),
                        key="acct_edit_ph",
                        placeholder="+33612345678",
                    )
                    save_pf = st.form_submit_button(
                        "Enregistrer mes informations", type="primary", use_container_width=True
                    )
                if save_pf:
                    ph_ok = True
                    if e_ph.strip():
                        ph_ok = bool(re.match(r"^\+\d{8,15}$", e_ph.strip()))
                        if not ph_ok:
                            st.error("Téléphone invalide. Format E.164, ex. +33612345678.")
                    if ph_ok:
                        ov_pf = loading_overlay("Enregistrement du profil…")
                        try:
                            try:
                                next_ver = int(str(rp.get("version") or "1")) + 1
                            except ValueError:
                                next_ver = 2
                            _supersede_users_by_email(em_acct)
                            append_immutable_row(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table="users",
                                values_by_col={
                                    "entity_id": str(rp.get("entity_id") or "").strip(),
                                    "email": em_acct,
                                    "first_name": e_fn.strip(),
                                    "last_name": e_ln.strip(),
                                    "phone_e164": e_ph.strip(),
                                    "country": str(rp.get("country") or "FR").strip() or "FR",
                                    "source": str(rp.get("source") or "compte").strip() or "compte",
                                    "password_salt_b64": str(rp.get("password_salt_b64") or ""),
                                    "password_hash_b64": str(rp.get("password_hash_b64") or ""),
                                    "version": next_ver,
                                },
                                version=next_ver,
                            )
                            st.success("Informations enregistrées.")
                            st.rerun()
                        finally:
                            ov_pf.empty()

            if not is_admin_sess and rp:
                st.divider()
                st.subheader("Newsletter")
                auth_uid_ac = str(user_entity_id).strip()
                latest_ac_sub = latest_subscription_record(subs, auth_uid_ac, "weekly_friday")
                cur_o = str((latest_ac_sub or {}).get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes")
                want_o = st.checkbox(
                    "Je souhaite recevoir les e-mails du vendredi (opt-in)",
                    value=bool(cur_o),
                    key="acct_news_optin",
                )
                if st.button("Enregistrer les préférences newsletter", type="secondary", key="acct_news_save"):
                    ov_n = loading_overlay("Enregistrement…")
                    try:
                        cur_act = str((latest_ac_sub or {}).get("active") or "").strip().lower() in (
                            "true",
                            "1",
                            "oui",
                            "yes",
                            "active",
                        )
                        if (bool(want_o) != cur_o) or (bool(want_o) != cur_act):
                            sub_ent = sha256(f"sub|{auth_uid_ac}|acct|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                            append_immutable_row(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table="subscriptions",
                                values_by_col={
                                    "entity_id": sub_ent,
                                    "user_entity_id": auth_uid_ac,
                                    "type": "weekly_friday",
                                    "zone": "france",
                                    "length_pref": str((latest_ac_sub or {}).get("length_pref") or "250"),
                                    "opt_in": "true" if want_o else "false",
                                    "active": "true" if want_o else "false",
                                },
                            )
                        st.success("Préférences enregistrées.")
                        st.rerun()
                    finally:
                        ov_n.empty()
        else:
            # Contrôle “standard” (pilotable) plutôt que `st.tabs` (qui ne permet pas de basculer via un bouton).
            mode = st.segmented_control(
                " ",
                options=["login", "signup"],
                default=str(st.session_state.get("acct_mode") or "login"),
                key="acct_mode",
                format_func=lambda x: "Se connecter" if x == "login" else "Créer / activer un compte",
            )

            def _latest_user_record(users0: list[dict], email_lc: str) -> dict | None:
                rows0 = [u for u in users0 if str(u.get("email", "")).strip().lower() == email_lc]
                if not rows0:
                    return None
                rows_sorted0 = sorted(rows0, key=lambda r: str(r.get("created_at", "")), reverse=True)
                return rows_sorted0[0]

            if mode == "login":
                email_login = st.text_input("Email", key="acct_email_login").strip().lower()
                password_login = st.text_input("Mot de passe", type="password", key="acct_password_login")
            else:
                # Pré-remplissage (avant instanciation des widgets) via on_change sur l'e-mail.
                def _prefill_acct_profile_from_existing() -> None:
                    em0 = str(st.session_state.get("acct_email_signup") or "").strip().lower()
                    if not em0:
                        return
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em0):
                        return
                    ex0 = _latest_user_record(users, em0)
                    if not ex0:
                        return
                    # Ne pré-remplit que si l'utilisateur n'a rien saisi
                    st.session_state.setdefault("acct_first_name", str(ex0.get("first_name") or "").strip())
                    st.session_state.setdefault("acct_last_name", str(ex0.get("last_name") or "").strip())
                    st.session_state.setdefault("acct_phone_e164", str(ex0.get("phone_e164") or "").strip())

                email_signup = st.text_input(
                    "Email",
                    key="acct_email_signup",
                    on_change=_prefill_acct_profile_from_existing,
                ).strip().lower()
                password_signup = st.text_input("Mot de passe", type="password", key="acct_password_signup")
                c1, c2 = st.columns([1, 1], gap="small")
                with c1:
                    first_name_su = st.text_input("Prénom", key="acct_first_name").strip()
                with c2:
                    last_name_su = st.text_input("Nom", key="acct_last_name").strip()
                phone_e164_su = st.text_input(
                    "Téléphone (optionnel, format international)",
                    key="acct_phone_e164",
                    placeholder="+33612345678",
                ).strip()
                want_opt_in_su = st.checkbox(
                    "Je souhaite recevoir les e-mails du vendredi (opt-in)",
                    value=True,
                    key="acct_optin",
                )

                is_email_ok = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_signup)) if email_signup else False
                if email_signup.strip() and not is_email_ok:
                    st.error("Merci d’indiquer une adresse e-mail valide (ex. nom@domaine.fr).")
                is_phone_ok = True
                if phone_e164_su:
                    is_phone_ok = bool(re.match(r"^\+\d{8,15}$", phone_e164_su))
                    if not is_phone_ok:
                        st.error("Téléphone invalide. Utilise le format E.164, ex. +33612345678.")

            if mode == "login":
                if st.button("Se connecter", type="primary", disabled=not (email_login and password_login), use_container_width=True, key="acct_login_btn"):
                    ov = loading_overlay("LumenVia vérifie tes identifiants…")
                    try:
                        adm_login, adm_pwd = admin_login_and_password()
                        if email_login.strip().lower() == adm_login and password_login == adm_pwd:
                            admin_canon = f"{adm_login}@admin.lumenvia"
                            st.session_state.auth_user_entity_id = sha256(admin_canon.encode("utf-8")).hexdigest()[:24]
                            st.session_state.auth_email_lc = adm_login
                            st.session_state.admin_authenticated = True
                            st.success("Connecté (administrateur).")
                            st.rerun()
                        rec = _latest_user_record(users, email_login)
                        if not rec or not rec.get("password_salt_b64") or not rec.get("password_hash_b64"):
                            st.error("Compte introuvable ou mot de passe non défini. Clique sur « Créer / activer un compte ».")
                            if email_login.strip():
                                if st.button("Créer / activer avec cet email", key="acct_go_signup_from_login"):
                                    st.session_state["acct_mode"] = "signup"
                                    st.session_state["acct_email_signup"] = email_login.strip().lower()
                                    st.rerun()
                            return
                        ok = verify_password(
                            password_login,
                            salt_b64=str(rec.get("password_salt_b64")),
                            hash_b64=str(rec.get("password_hash_b64")),
                        )
                        if not ok:
                            st.error("Mot de passe incorrect.")
                            return
                        st.session_state.auth_user_entity_id = sha256(email_login.encode("utf-8")).hexdigest()[:24]
                        st.session_state.auth_email_lc = email_login
                        st.session_state.pop("admin_authenticated", None)
                        st.success("Connecté.")
                        st.rerun()
                    finally:
                        ov.empty()

                # Réinitialisation mot de passe (envoi e-mail)
                st.caption("Réinitialisation : tu recevras un lien valable **2 heures**.")
                if st.button("Réinitialiser le mot de passe", type="secondary", disabled=not bool(email_login.strip()), key="acct_pwd_reset_btn"):
                    em0 = email_login.strip().lower()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em0):
                        st.error("Merci d’indiquer un e-mail valide.")
                    else:
                        ov = loading_overlay("Envoi de l’e-mail de réinitialisation…")
                        try:
                            from secrets import token_urlsafe
                            from datetime import datetime, timedelta, timezone
                            from core.outbound import SmtpConfig, send_smtp_email

                            ensure_table(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table=get_table_spec("password_resets"),
                            )
                            tok = token_urlsafe(32)
                            tok_h = sha256(tok.encode("utf-8")).hexdigest()
                            exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
                            append_immutable_row(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table="password_resets",
                                values_by_col={
                                    "entity_id": sha256(f"pwdreset|{em0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                    "email": em0,
                                    "token_hash": tok_h,
                                    "expires_at": exp,
                                    "used": "false",
                                },
                            )

                            origin = _lumenvia_app_origin_url() or ""
                            link = (origin.rstrip("/") + "/?route=reset_password&email=" + em0 + "&token=" + tok) if origin else ""

                            # SMTP
                            def _secret_get(*keys: str) -> str:
                                try:
                                    s = st.secrets
                                except Exception:
                                    return ""
                                for k in keys:
                                    v = s.get(k)
                                    if v is not None and str(v).strip():
                                        return str(v).strip()
                                return ""

                            smtp_cfg = SmtpConfig(
                                host=_secret_get("SMTP_HOST"),
                                port=int(_secret_get("SMTP_PORT") or 587),
                                username=_secret_get("SMTP_USER"),
                                password=_secret_get("SMTP_PASSWORD"),
                                from_email=_secret_get("SMTP_FROM"),
                                use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower() not in ("0", "false", "no", "off"),
                            )
                            if not smtp_cfg.host or not smtp_cfg.from_email:
                                raise RuntimeError("SMTP non configuré (SMTP_HOST/SMTP_FROM).")
                            if not link:
                                raise RuntimeError("URL publique introuvable (PUBLIC_APP_URL requis) pour générer le lien.")

                            subj = "LumenVia — Réinitialisation du mot de passe"
                            body_txt = (
                                "Voici le lien pour réinitialiser ton mot de passe (valide 2 heures) :\n"
                                f"{link}\n\n"
                                "Si tu n'es pas à l'origine de cette demande, ignore cet e-mail."
                            )
                            send_smtp_email(cfg=smtp_cfg, to_email=em0, subject=subj, body_text=body_txt, body_html=None)
                            st.success(f"E-mail envoyé à **{em0}**. Consulte ta boîte de réception.")
                        except Exception as ex:
                            st.error(str(ex))
                        finally:
                            ov.empty()

            else:
                existing = _latest_user_record(users, email_signup) if (email_signup and is_email_ok) else None
                has_pwd = bool(
                    existing
                    and str(existing.get("password_hash_b64") or "").strip()
                    and str(existing.get("password_salt_b64") or "").strip()
                )
                can_activate = bool(existing and not has_pwd)
                if can_activate:
                    st.info(
                        "Cet email est déjà inscrit à la newsletter mais tu peux l'activer en tant que compte "
                        "pour avoir accès aux services réservés aux utilisateurs connectés (aide mémoire etc.). "
                        "Avec les informations renseignées clique juste sur **Activer mon compte**."
                    )

                # Activation (newsletter → compte) : ne force pas prénom/nom si la fiche n'en a pas encore.
                # Création “nouveau compte” : prénom + nom requis.
                can_create = bool(email_signup and password_signup and is_email_ok and is_phone_ok) and (
                    can_activate or (first_name_su.strip() and last_name_su.strip())
                )
                label_create = "Activer mon compte" if can_activate else "Créer un compte"
                if st.button(
                    label_create,
                    type="secondary",
                    disabled=not can_create,
                    use_container_width=True,
                    key="acct_signup_btn",
                ):
                    ov = loading_overlay("LumenVia enregistre ton compte…")
                    try:
                        salt_b64, hash_b64 = hash_password(password_signup)
                        new_uid = sha256(email_signup.encode("utf-8")).hexdigest()[:24]
                        rec0 = _latest_user_record(users, email_signup)
                        if rec0:
                            has_pwd0 = bool(
                                str(rec0.get("password_hash_b64") or "").strip()
                                and str(rec0.get("password_salt_b64") or "").strip()
                            )
                            if has_pwd0:
                                st.error("Un compte existe déjà pour cet e-mail. Utilise l’onglet « Se connecter ».")
                                return
                        _supersede_users_by_email(email_signup)
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="users",
                            values_by_col={
                                "entity_id": new_uid,
                                "email": email_signup,
                                "source": "compte",
                                "first_name": first_name_su.strip(),
                                "last_name": last_name_su.strip(),
                                "phone_e164": phone_e164_su.strip(),
                                "country": "FR",
                                "password_salt_b64": salt_b64,
                                "password_hash_b64": hash_b64,
                            },
                        )
                        if want_opt_in_su:
                            latest_before = latest_subscription_record(subs, new_uid, "weekly_friday")
                            if not subscription_is_active(latest_before):
                                sub_entity = sha256(f"sub|{new_uid}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                                append_immutable_row(
                                    gspread_client=gs,
                                    spreadsheet_id=cfg.gsheet_id,
                                    table="subscriptions",
                                    values_by_col={
                                        "entity_id": sub_entity,
                                        "user_entity_id": new_uid,
                                        "type": "weekly_friday",
                                        "zone": "france",
                                        "length_pref": "250",
                                        "opt_in": "true",
                                        "active": "true",
                                    },
                                )
                        st.session_state.auth_user_entity_id = new_uid
                        st.session_state.auth_email_lc = email_signup
                        st.success("Compte créé et connecté.")
                        st.rerun()
                    finally:
                        ov.empty()

        st.divider()
        # Important : en vue "Mon compte", ne pas afficher le formulaire newsletter en dessous.
        return

    # --- Newsletter : inscription / opt-out ---
    auth_email_lc = str(st.session_state.get("auth_email_lc") or "").strip().lower()
    auth_uid = sha256(auth_email_lc.encode("utf-8")).hexdigest()[:24] if auth_email_lc else ""
    auth_latest_sub = latest_subscription_record(subs, auth_uid, "weekly_friday") if auth_uid else None
    auth_is_in = bool(auth_uid) and subscription_is_active(auth_latest_sub)

    if auth_email_lc:
        st.caption(
            "Tu peux gérer ici ton opt-in. ET tu peux aussi inscrire quelqu’un d’autre (un ami, un proche) "
            "en renseignant ses informations plus bas."
        )
        with st.expander("Mes préférences newsletter", expanded=False):
            cur_opt_in = str((auth_latest_sub or {}).get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes")
            want_opt_in = st.checkbox(
                "Je souhaite recevoir les e-mails du vendredi (opt-in)",
                value=bool(cur_opt_in),
                key="join_me_optin",
            )
            if st.button("Enregistrer", type="primary", key="join_me_optin_save"):
                ov = loading_overlay("Enregistrement…")
                try:
                    # Append-only : nouvelle ligne subscriptions si changement
                    cur_active = str((auth_latest_sub or {}).get("active") or "").strip().lower() in ("true", "1", "oui", "yes", "active")
                    target_opt_in = bool(want_opt_in)
                    target_active = bool(want_opt_in)
                    if (target_opt_in != cur_opt_in) or (target_active != cur_active):
                        sub_entity = sha256(f"sub|{auth_uid}|prefs|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="subscriptions",
                            values_by_col={
                                "entity_id": sub_entity,
                                "user_entity_id": auth_uid,
                                "type": "weekly_friday",
                                "zone": "france",
                                "length_pref": str((auth_latest_sub or {}).get("length_pref") or "250"),
                                "opt_in": "true" if target_opt_in else "false",
                                "active": "true" if target_active else "false",
                            },
                        )
                    st.success("Préférences enregistrées.")
                    st.rerun()
                finally:
                    ov.empty()

    if "join_email" not in st.session_state:
        st.session_state.join_email = ""
    # Pré-remplissage via lien e-mailing : /?route=join&email=...
    if qp_email and not str(st.session_state.join_email).strip():
        st.session_state.join_email = qp_email

    def _latest_user_by_email(email_lc: str) -> dict | None:
        rows = [u for u in users if str(u.get("email", "")).strip().lower() == email_lc]
        if not rows:
            return None
        return sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]

    col_n1, col_n2 = st.columns([1, 1], gap="small")
    with col_n1:
        first_name = st.text_input("Prénom", key="join_first_name").strip()
    with col_n2:
        last_name = st.text_input("Nom", key="join_last_name").strip()

    country = st.selectbox("Pays", options=["FR"], index=0, key="join_country")
    phone_e164 = st.text_input(
        "Téléphone (optionnel, format international)",
        key="join_phone_e164",
        placeholder="+33612345678",
    ).strip()

    email_in = st.text_input("Email", key="join_email")
    email_lc = email_in.strip().lower()
    is_email_ok = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_lc)) if email_lc else False
    if email_in.strip() and not is_email_ok:
        st.error("Merci d’indiquer une adresse e-mail valide (ex. nom@domaine.fr).")
    is_phone_ok = True
    if phone_e164:
        is_phone_ok = bool(re.match(r"^\+\d{8,15}$", phone_e164))
        if not is_phone_ok:
            st.error("Téléphone invalide. Utilise le format E.164, ex. +33612345678.")

    uid = sha256(email_lc.encode("utf-8")).hexdigest()[:24] if email_lc else ""
    latest_sub = latest_subscription_record(subs, uid, "weekly_friday") if uid else None
    already_in = bool(uid) and subscription_is_active(latest_sub)

    if auth_email_lc:
        st.caption("Astuce : si tu es déjà connecté, ce formulaire sert surtout à inscrire quelqu’un d’autre.")

    if already_in:
        st.success(f"Tu es déjà inscrit à la newsletter pour **{email_lc}**.")
        st.caption("Tu peux te désinscrire à tout moment.")
        if st.button("Se désinscrire", type="secondary", key="join_opt_out_btn"):
            ov = loading_overlay("Désinscription…")
            try:
                sub_entity = sha256(f"sub|{uid}|optout|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="subscriptions",
                    values_by_col={
                        "entity_id": sub_entity,
                        "user_entity_id": uid,
                        "type": "weekly_friday",
                        "zone": "france",
                        "length_pref": str((latest_sub or {}).get("length_pref") or "250"),
                        "opt_in": "false",
                        "active": "false",
                    },
                )
                st.success("Désinscription enregistrée.")
                st.rerun()
            finally:
                ov.empty()
        return

    consent = st.checkbox("J’accepte de recevoir ces e-mails (désinscription possible à tout moment).", key="join_consent")
    if st.button("S’abonner", type="primary", disabled=not (is_email_ok and first_name and last_name and is_phone_ok and consent), key="join_subscribe_btn"):
        ov = loading_overlay("LumenVia enregistre ton inscription…")
        should_refresh = False
        try:
            user_entity_id = sha256(email_lc.encode("utf-8")).hexdigest()[:24]
            rec_u = _latest_user_by_email(email_lc)
            if not rec_u:
                _supersede_users_by_email(email_lc)
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="users",
                    values_by_col={
                        "entity_id": user_entity_id,
                        "email": email_lc,
                        "first_name": first_name.strip(),
                        "last_name": last_name.strip(),
                        "phone_e164": phone_e164.strip(),
                        "country": str(country or "").strip(),
                        "source": "newsletter",
                    },
                )
            latest_before = latest_subscription_record(subs, user_entity_id, "weekly_friday")
            if subscription_is_active(latest_before):
                st.info("Tu étais déjà inscrit — aucune nouvelle ligne nécessaire.")
            else:
                sub_entity = sha256(f"sub|{user_entity_id}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="subscriptions",
                    values_by_col={
                        "entity_id": sub_entity,
                        "user_entity_id": user_entity_id,
                        "type": "weekly_friday",
                        "zone": "france",
                        "length_pref": "250",
                        "opt_in": "true",
                        "active": "true",
                    },
                )
                should_refresh = True
        finally:
            ov.empty()
        if should_refresh:
            st.rerun()


def render_reset_password() -> None:
    st.markdown(
        """
<style>
@media (max-width: 768px) {
  section[data-testid="stMain"] .block-container {
    padding-bottom: max(10rem, calc(env(safe-area-inset-bottom, 0px) + 8rem)) !important;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )
    st.title("Réinitialiser le mot de passe")
    st.caption("Saisis un nouveau mot de passe. Le lien est valide pendant une durée limitée.")

    try:
        em = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        em = ""
    try:
        tok = str(st.query_params.get("token") or "").strip()
    except Exception:
        tok = ""

    if not em or not tok:
        st.error("Lien invalide (paramètres manquants).")
        return

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configuration Google Sheets manquante.")
        return
    gs = build_gspread_client(cfg.gcp_service_account)
    # Même logique que l'inscription : on supersède l'ancienne ligne `users` (status Inactif) avant d'écrire la nouvelle.
    def _supersede_users_by_email(email_lc: str) -> None:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return
        try:
            from core.sheets_db import _resolve_table_name, compute_concat, SHEETS_ROW_STATUS_INACTIVE
        except Exception:
            return
        try:
            sh0 = gs.open_by_key(cfg.gsheet_id)
            ws0 = sh0.worksheet(_resolve_table_name(sh=sh0, table="users"))
            header0 = ws0.row_values(1)
            if not header0 or "status" not in header0:
                return
            col_status = header0.index("status") + 1
            col_concat = header0.index("concat") + 1 if "concat" in header0 else 0
            recs = ws0.get_all_records(numericise_ignore=["all"])
        except Exception:
            return
        for ix, r in enumerate(recs):
            if str(r.get("email") or "").strip().lower() != em0:
                continue
            if not sheet_row_status_is_live(r.get("status")):
                continue
            merged = dict(r)
            merged["status"] = SHEETS_ROW_STATUS_INACTIVE
            row_num = ix + 2
            try:
                ws0.update_cell(row_num, col_status, SHEETS_ROW_STATUS_INACTIVE)
                if col_concat:
                    ws0.update_cell(row_num, col_concat, compute_concat(merged, header=header0))
            except Exception:
                continue

    new_pwd = st.text_input("Nouveau mot de passe", type="password", key="pwd_reset_new")
    new_pwd2 = st.text_input("Confirmer le mot de passe", type="password", key="pwd_reset_new2")
    if st.button("Mettre à jour mon mot de passe", type="primary", disabled=not (new_pwd and new_pwd2)):
        if new_pwd != new_pwd2:
            st.error("Les deux mots de passe ne correspondent pas.")
            return
        ov = loading_overlay("Mise à jour du mot de passe…")
        try:
            from core.sheets_db import fetch_records, append_immutable_row
            from datetime import datetime, timezone

            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=get_table_spec("password_resets"),
            )
            resets = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="password_resets", limit=8000)
            tok_h = sha256(tok.encode("utf-8")).hexdigest()
            # Dernière demande pour ce token
            cand = [r for r in resets if str(r.get("token_hash") or "").strip() == tok_h and str(r.get("email") or "").strip().lower() == em]
            cand.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            rec = cand[0] if cand else {}
            if not rec:
                st.error("Lien invalide ou expiré.")
                return
            if str(rec.get("used") or "").strip().lower() in ("true", "1", "oui", "yes"):
                st.error("Ce lien a déjà été utilisé.")
                return
            exp = str(rec.get("expires_at") or "").strip()
            try:
                dt_exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if dt_exp.tzinfo is None:
                    dt_exp = dt_exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > dt_exp:
                    st.error("Ce lien a expiré.")
                    return
            except Exception:
                st.error("Ce lien a expiré.")
                return

            # Met à jour le mot de passe via append-only dans `users`
            users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            rows_u = [u for u in users if str(u.get("email") or "").strip().lower() == em]
            rows_u.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            u0 = rows_u[0] if rows_u else {}
            if not u0:
                st.error("Utilisateur introuvable.")
                return
            salt_b64, hash_b64 = hash_password(new_pwd)
            uid = sha256(em.encode("utf-8")).hexdigest()[:24]
            _supersede_users_by_email(em)
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="users",
                values_by_col={
                    "entity_id": uid,
                    "email": em,
                    # Dès qu'un mot de passe est défini, on considère l'utilisateur comme "compte"
                    # (même s'il a commencé par une inscription newsletter).
                    "source": "compte",
                    "first_name": str(u0.get("first_name") or "").strip(),
                    "last_name": str(u0.get("last_name") or "").strip(),
                    "phone_e164": str(u0.get("phone_e164") or "").strip(),
                    "country": str(u0.get("country") or "FR").strip() or "FR",
                    "password_salt_b64": salt_b64,
                    "password_hash_b64": hash_b64,
                },
            )
            # Marque le token comme utilisé (append-only)
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="password_resets",
                values_by_col={
                    "entity_id": sha256(f"pwdreset|used|{em}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "email": em,
                    "token_hash": tok_h,
                    "expires_at": exp,
                    "used": "true",
                },
            )
            st.success("Mot de passe mis à jour. Tu peux maintenant te connecter.")
        finally:
            ov.empty()

