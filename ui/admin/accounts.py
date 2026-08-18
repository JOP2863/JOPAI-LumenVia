"""Admin — Comptes et abonnés (Sheets `users` / `subscriptions`, append-only)."""

from __future__ import annotations

import re
from hashlib import sha256
from html import escape as html_escape

import streamlit as st

from core.auth import hash_password
from core.config import load_config
from core.locale_codes import (
    DEFAULT_COUNTRY,
    DEFAULT_PREF_LANGUE,
    DOMAINE_LANGUE,
    DOMAINE_PAYS,
    fallback_langue_options,
    fallback_pays_options,
    normalize_country,
    normalize_pref_langue,
    options_from_langues_pays_rows,
    user_country,
    user_pref_langue,
)
from core.sheets_db import (
    SHEETS_ROW_STATUS_INACTIVE,
    append_immutable_row,
    append_immutable_rows_bulk,
    build_gspread_client,
    compute_concat,
    fetch_records,
    invalidate_fetch_records_cache,
    sheet_row_status_is_live,
    utc_now_iso,
    _resolve_table_name,
)
from core.subscriptions_util import (
    latest_subscription_record,
    subscription_is_active,
    weekly_subscription_langs,
)
from core.sunday_view_locale import lang_flag_html
from ui.admin_secrets import admin_login_and_password
from ui.components import loading_overlay
from ui.streamlit_caches import (
    adm_sheets_fetch_cached,
    invalidate_adm_sheets_fetch_cache,
    service_account_json_fingerprint,
)


def _admin_locale_options(*, cfg: object, gs: object, sa_json: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    rows: list[dict] = []
    try:
        gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
        if sa_json and gsheet_id:
            rows = list(adm_sheets_fetch_cached(gsheet_id, "langues_pays", 0, sa_json) or [])
    except Exception:
        rows = []
    if not rows:
        try:
            rows = list(
                fetch_records(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="langues_pays",
                    limit=0,
                    use_cache=True,
                )
                or []
            )
        except Exception:
            rows = []
    langues = options_from_langues_pays_rows(rows, domaine=DOMAINE_LANGUE) or fallback_langue_options()
    pays = options_from_langues_pays_rows(rows, domaine=DOMAINE_PAYS) or fallback_pays_options()
    return langues, pays


def render_admin_accounts() -> None:
    st.title("Comptes inscrits")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    sa_json = service_account_json_fingerprint(cfg.gcp_service_account)
    try:
        users = list(adm_sheets_fetch_cached(cfg.gsheet_id, "users", 0, sa_json) or [])
    except Exception as e:
        st.error(f"Lecture `users` impossible : {e}")
        return
    try:
        subs = list(adm_sheets_fetch_cached(cfg.gsheet_id, "subscriptions", 0, sa_json) or [])
    except Exception:
        subs = []

    # "Flash message" persistant (après rerun)
    flash = str(st.session_state.get("adm_addsub_flash") or "").strip()
    if flash:
        st.success(flash)
        st.session_state.pop("adm_addsub_flash", None)

    # Nonce pour forcer un "reset" visuel fiable des champs Streamlit après succès
    # (en changeant les keys des widgets plutôt que de dépendre d'un pop()).
    nonce = int(st.session_state.get("adm_addsub_nonce") or 0)

    langues_opts, pays_opts = _admin_locale_options(cfg=cfg, gs=gs, sa_json=sa_json)

    with st.expander(
        "Ajouter des abonnés (lot de 5)",
        expanded=bool(st.session_state.get("adm_addsub_open") or False),
    ):
        def _norm_email(s: object) -> str:
            return str(s or "").strip().lower()

        def _email_ok(em: str) -> bool:
            return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)) if em else False

        def _phone_ok(ph: str) -> bool:
            if not ph:
                return True
            return bool(re.match(r"^\+\d{8,15}$", ph))

        lang_codes = [c for c, _ in langues_opts]
        pays_codes = [c for c, _ in pays_opts]
        if DEFAULT_PREF_LANGUE not in lang_codes:
            lang_codes = [DEFAULT_PREF_LANGUE] + lang_codes
            langues_opts_local = [(DEFAULT_PREF_LANGUE, f"Français ({DEFAULT_PREF_LANGUE})")] + list(langues_opts)
        else:
            langues_opts_local = list(langues_opts)
        if DEFAULT_COUNTRY not in pays_codes:
            pays_codes = [DEFAULT_COUNTRY] + pays_codes
            pays_opts_local = [(DEFAULT_COUNTRY, f"France ({DEFAULT_COUNTRY})")] + list(pays_opts)
        else:
            pays_opts_local = list(pays_opts)

        # Formulaire en lot : 5 lignes
        with st.form("adm_add_subscribers_5"):
            c_pay, c_lang, c_len = st.columns([1, 1, 1], gap="small")
            with c_pay:
                country = st.selectbox(
                    "Pays / nationalité",
                    options=pays_codes,
                    index=pays_codes.index(DEFAULT_COUNTRY) if DEFAULT_COUNTRY in pays_codes else 0,
                    format_func=lambda c: next((lab for code, lab in pays_opts_local if code == c), c),
                    key=f"adm_addsub_country_{nonce}",
                )
            with c_lang:
                pref_langue = st.selectbox(
                    "Préférence langue",
                    options=lang_codes,
                    index=lang_codes.index(DEFAULT_PREF_LANGUE) if DEFAULT_PREF_LANGUE in lang_codes else 0,
                    format_func=lambda c: next((lab for code, lab in langues_opts_local if code == c), c),
                    key=f"adm_addsub_pref_langue_{nonce}",
                    help="Langue de consultation LumenVia (ISO 639-1 majuscules) — e-mails et contenus.",
                )
            with c_len:
                length_pref = st.selectbox(
                    "Préférence de longueur",
                    options=["150", "250", "400"],
                    index=1,
                    key=f"adm_addsub_lenpref_{nonce}",
                )

            col_a, col_b, col_c, col_d = st.columns([1.3, 1, 1, 1], gap="small")
            with col_a:
                st.markdown("**E-mail**")
            with col_b:
                st.markdown("**Prénom**")
            with col_c:
                st.markdown("**Nom**")
            with col_d:
                st.markdown("**Téléphone (optionnel)**")

            rows_in: list[dict[str, str]] = []
            for i in range(5):
                c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1], gap="small")
                with c1:
                    em = st.text_input("E-mail", label_visibility="collapsed", key=f"adm_addsub_em_{nonce}_{i}").strip()
                with c2:
                    fn = st.text_input("Prénom", label_visibility="collapsed", key=f"adm_addsub_fn_{nonce}_{i}").strip()
                with c3:
                    ln = st.text_input("Nom", label_visibility="collapsed", key=f"adm_addsub_ln_{nonce}_{i}").strip()
                with c4:
                    ph = st.text_input(
                        "Téléphone",
                        label_visibility="collapsed",
                        key=f"adm_addsub_ph_{nonce}_{i}",
                        placeholder="+33612345678",
                    ).strip()
                rows_in.append({"email": em, "first_name": fn, "last_name": ln, "phone_e164": ph})

            do_submit = st.form_submit_button(
                "Créer ces abonnés", type="primary", use_container_width=True
            )

        if do_submit:
            # En cas d'erreur, on garde l'expander ouvert au rerun.
            st.session_state["adm_addsub_open"] = True
            # Nettoyage + validation
            cleaned: list[dict[str, str]] = []
            for r in rows_in:
                em_lc = _norm_email(r.get("email"))
                fn = str(r.get("first_name") or "").strip()
                ln = str(r.get("last_name") or "").strip()
                ph = str(r.get("phone_e164") or "").strip()
                if not (em_lc or fn or ln or ph):
                    continue  # ligne vide
                cleaned.append({"email": em_lc, "first_name": fn, "last_name": ln, "phone_e164": ph})

            if not cleaned:
                st.warning("Aucune ligne renseignée.")
            else:
                bad_lines: list[str] = []
                for idx, r in enumerate(cleaned, start=1):
                    em_lc = r["email"]
                    if not _email_ok(em_lc):
                        bad_lines.append(f"Ligne {idx} : e-mail invalide.")
                    if not r["first_name"] or not r["last_name"]:
                        bad_lines.append(f"Ligne {idx} : prénom/nom requis.")
                    if not _phone_ok(r.get("phone_e164") or ""):
                        bad_lines.append(f"Ligne {idx} : téléphone invalide (format +336...).")
                if bad_lines:
                    for m in bad_lines[:12]:
                        st.error(m)
                    if len(bad_lines) > 12:
                        st.error(f"... et {len(bad_lines) - 12} autre(s) erreur(s).")
                else:
                    ov = loading_overlay("Création des abonnés…")
                    try:
                        from core.sheets_db import append_immutable_rows_bulk

                        # Index existants (dernier état par e-mail / par user_entity_id)
                        by_email: dict[str, dict] = {}
                        for u in users:
                            em = _norm_email(u.get("email"))
                            if not em:
                                continue
                            prev = by_email.get(em)
                            if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
                                by_email[em] = u

                        latest_sub_by_uid: dict[str, dict] = {}
                        for s in subs:
                            if str(s.get("type") or "").strip() != "weekly_friday":
                                continue
                            uid0 = str(s.get("user_entity_id") or "").strip()
                            if not uid0:
                                continue
                            prev = latest_sub_by_uid.get(uid0)
                            if not prev or str(s.get("created_at") or "") > str(prev.get("created_at") or ""):
                                latest_sub_by_uid[uid0] = s

                        to_add_users: list[dict[str, str]] = []
                        to_add_subs: list[dict[str, str]] = []
                        seen_batch: set[str] = set()
                        already_users: list[str] = []
                        already_optin: list[str] = []

                        for r in cleaned:
                            em_lc = r["email"]
                            if em_lc in seen_batch:
                                continue
                            seen_batch.add(em_lc)
                            uid0 = sha256(em_lc.encode("utf-8")).hexdigest()[:24]

                            # User (si absent)
                            if em_lc not in by_email:
                                country_n = normalize_country(country)
                                lang_n = normalize_pref_langue(pref_langue)
                                to_add_users.append(
                                    {
                                        "entity_id": uid0,
                                        "email": em_lc,
                                        "first_name": r["first_name"],
                                        "last_name": r["last_name"],
                                        "phone_e164": r.get("phone_e164") or "",
                                        "country": country_n,
                                        "pref_langue": lang_n,
                                        # Aligné avec “Nous rejoindre”
                                        "source": "newsletter",
                                        "password_salt_b64": "",
                                        "password_hash_b64": "",
                                    }
                                )
                                by_email[em_lc] = {"entity_id": uid0, "email": em_lc, "created_at": utc_now_iso()}
                            else:
                                already_users.append(em_lc)

                            # Subscription (si pas active)
                            last = latest_sub_by_uid.get(uid0)
                            if subscription_is_active(last):
                                already_optin.append(em_lc)
                                continue
                            sub_entity = sha256(f"sub|{uid0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                            to_add_subs.append(
                                {
                                    "entity_id": sub_entity,
                                    "user_entity_id": uid0,
                                    "type": "weekly_friday",
                                    "zone": "france",
                                    "pref_langue": normalize_pref_langue(pref_langue),
                                    "length_pref": str(length_pref or "250").strip(),
                                    "opt_in": "true",
                                    "active": "true",
                                }
                            )

                        added_u = append_immutable_rows_bulk(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="users",
                            values_by_col_list=to_add_users,
                            chunk_size=120,
                        )
                        added_s = append_immutable_rows_bulk(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="subscriptions",
                            values_by_col_list=to_add_subs,
                            chunk_size=120,
                        )
                        invalidate_fetch_records_cache(spreadsheet_id=cfg.gsheet_id, table="users")
                        invalidate_fetch_records_cache(
                            spreadsheet_id=cfg.gsheet_id, table="subscriptions"
                        )
                        invalidate_adm_sheets_fetch_cache()
                        # Message + reset UI (champs vidés + expander replié)
                        msg = f"Abonnés ajoutés : {added_u} utilisateur(s) créé(s), {added_s} abonnement(s) ajouté(s)."
                        if already_users:
                            uniq = sorted(set(already_users))
                            msg += f"\nDéjà existants (non recréés) : {', '.join(uniq[:12])}" + ("…" if len(uniq) > 12 else "")
                        if already_optin:
                            uniq2 = sorted(set(already_optin))
                            msg += f"\nDéjà abonnés (opt-in actif) : {', '.join(uniq2[:12])}" + ("…" if len(uniq2) > 12 else "")
                        st.session_state["adm_addsub_flash"] = msg
                        st.session_state["adm_addsub_open"] = False
                        st.session_state["adm_addsub_nonce"] = nonce + 1
                        st.rerun()
                    finally:
                        ov.empty()

    def _latest_by_email(rows: list[dict]) -> list[dict]:
        by: dict[str, dict] = {}
        for r in rows:
            em = str(r.get("email") or "").strip().lower()
            if not em:
                continue
            prev = by.get(em)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[em] = r
        return sorted(by.values(), key=lambda x: str(x.get("created_at") or ""), reverse=True)

    def _supersede_users_by_email(email_lc: str) -> None:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
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
                pass

    latest_for_edit = _latest_by_email(users)
    edit_emails = [
        str(u.get("email") or "").strip().lower()
        for u in latest_for_edit
        if str(u.get("email") or "").strip()
    ]

    with st.expander("Éditer un utilisateur / initialiser le mot de passe", expanded=False):
        st.caption(
            "Corrige la fiche après création (prénom, pays, langue…) ou **définit un mot de passe** "
            "pour transformer un abonné newsletter en compte connectable."
        )
        if not edit_emails:
            st.info("Aucun utilisateur à éditer.")
        else:
            em_pick = st.selectbox(
                "Utilisateur (e-mail)",
                options=edit_emails,
                key="adm_edit_user_email",
            )
            rp = next(
                (u for u in latest_for_edit if str(u.get("email") or "").strip().lower() == em_pick),
                {},
            )
            has_pwd = bool(str(rp.get("password_hash_b64") or "").strip())
            st.caption(
                f"Source actuelle : **{str(rp.get('source') or '—')}** · "
                f"Mot de passe : **{'déjà défini' if has_pwd else 'absent (newsletter / non activé)'}**"
            )

            lang_codes_ed = [c for c, _ in langues_opts]
            pays_codes_ed = [c for c, _ in pays_opts]
            langues_opts_ed = list(langues_opts)
            pays_opts_ed = list(pays_opts)
            if DEFAULT_PREF_LANGUE not in lang_codes_ed:
                lang_codes_ed = [DEFAULT_PREF_LANGUE] + lang_codes_ed
                langues_opts_ed = [(DEFAULT_PREF_LANGUE, f"Français ({DEFAULT_PREF_LANGUE})")] + langues_opts_ed
            if DEFAULT_COUNTRY not in pays_codes_ed:
                pays_codes_ed = [DEFAULT_COUNTRY] + pays_codes_ed
                pays_opts_ed = [(DEFAULT_COUNTRY, f"France ({DEFAULT_COUNTRY})")] + pays_opts_ed

            cur_lang = user_pref_langue(rp)
            cur_country = user_country(rp)
            if cur_lang not in lang_codes_ed:
                lang_codes_ed = [cur_lang] + lang_codes_ed
                langues_opts_ed = [(cur_lang, cur_lang)] + langues_opts_ed
            if cur_country not in pays_codes_ed:
                pays_codes_ed = [cur_country] + pays_codes_ed
                pays_opts_ed = [(cur_country, cur_country)] + pays_opts_ed

            auth_uid = str(rp.get("entity_id") or "").strip() or sha256(em_pick.encode("utf-8")).hexdigest()[:24]
            latest_sub_ed = latest_subscription_record(subs, auth_uid, "weekly_friday")
            cur_weekly_langs = weekly_subscription_langs(
                subs,
                auth_uid,
                default_lang=cur_lang,
            )
            cur_opt = bool(cur_weekly_langs)

            # Streamlit ignore `value=` / `index=` si la clé existe déjà en session :
            # resynchroniser les champs quand l’e-mail sélectionné change.
            if st.session_state.get("_adm_edit_sync_email") != em_pick:
                st.session_state["adm_edit_fn"] = str(rp.get("first_name") or "").strip()
                st.session_state["adm_edit_ln"] = str(rp.get("last_name") or "").strip()
                st.session_state["adm_edit_ph"] = str(rp.get("phone_e164") or "").strip()
                st.session_state["adm_edit_country"] = cur_country
                st.session_state["adm_edit_pref_langue"] = cur_lang
                st.session_state["adm_edit_optin"] = bool(cur_opt)
                st.session_state["adm_edit_weekly_langs"] = list(cur_weekly_langs)
                st.session_state["adm_edit_set_pwd"] = False
                st.session_state["adm_edit_pwd"] = ""
                st.session_state["adm_edit_pwd2"] = ""
                st.session_state["adm_edit_send_welcome"] = True
                st.session_state["_adm_edit_sync_email"] = em_pick

            with st.form("adm_edit_user_form"):
                e_fn = st.text_input(
                    "Prénom",
                    key="adm_edit_fn",
                )
                e_ln = st.text_input(
                    "Nom",
                    key="adm_edit_ln",
                )
                e_ph = st.text_input(
                    "Téléphone (optionnel, E.164)",
                    key="adm_edit_ph",
                    placeholder="+33612345678",
                )
                c1, c2 = st.columns(2)
                with c1:
                    e_country = st.selectbox(
                        "Pays / nationalité",
                        options=pays_codes_ed,
                        format_func=lambda c: next((lab for code, lab in pays_opts_ed if code == c), c),
                        key="adm_edit_country",
                    )
                with c2:
                    e_lang = st.selectbox(
                        "Préférence langue",
                        options=lang_codes_ed,
                        format_func=lambda c: next((lab for code, lab in langues_opts_ed if code == c), c),
                        key="adm_edit_pref_langue",
                    )
                e_opt = st.checkbox(
                    "Opt-in newsletter (vendredi)",
                    key="adm_edit_optin",
                )
                e_weekly_langs = st.multiselect(
                    "Langues de la newsletter du vendredi",
                    options=lang_codes_ed,
                    default=list(cur_weekly_langs),
                    format_func=lambda c: next((lab for code, lab in langues_opts_ed if code == c), c),
                    key="adm_edit_weekly_langs",
                    help="Jusqu’à 2 langues : un e-mail distinct sera envoyé pour chaque langue cochée.",
                    max_selections=2,
                    disabled=not bool(e_opt),
                )
                st.markdown("**Mot de passe**")
                set_pwd = st.checkbox(
                    "Définir / réinitialiser le mot de passe",
                    key="adm_edit_set_pwd",
                )
                e_pwd = st.text_input(
                    "Nouveau mot de passe (si case cochée)",
                    type="password",
                    key="adm_edit_pwd",
                    autocomplete="new-password",
                )
                e_pwd2 = st.text_input(
                    "Confirmer le mot de passe",
                    type="password",
                    key="adm_edit_pwd2",
                    autocomplete="new-password",
                )
                send_welcome = st.checkbox(
                    "Envoyer un e-mail d’instructions (connexion, mot de passe…)",
                    key="adm_edit_send_welcome",
                    help=(
                        "Si un nouveau mot de passe est défini, il est communiqué une fois "
                        "(provisoire). Sinon un lien de réinitialisation est proposé."
                    ),
                )
                save_ed = st.form_submit_button(
                    "Enregistrer les modifications",
                    type="primary",
                    use_container_width=True,
                )

            if save_ed:
                errs: list[str] = []
                if e_ph.strip() and not re.match(r"^\+\d{8,15}$", e_ph.strip()):
                    errs.append("Téléphone invalide (format +41… / +33…).")
                weekly_langs_n = [normalize_pref_langue(x) for x in (e_weekly_langs or [])]
                if e_opt and not weekly_langs_n:
                    weekly_langs_n = [normalize_pref_langue(e_lang)]
                weekly_langs_n = list(dict.fromkeys(weekly_langs_n))[:2]
                if set_pwd:
                    if len(e_pwd or "") < 8:
                        errs.append("Mot de passe : 8 caractères minimum.")
                    if (e_pwd or "") != (e_pwd2 or ""):
                        errs.append("Les deux mots de passe ne correspondent pas.")
                if send_welcome and not set_pwd and not has_pwd:
                    errs.append(
                        "Pour envoyer l’e-mail d’instructions, définis d’abord un mot de passe "
                        "(case « Définir / réinitialiser »)."
                    )
                if errs:
                    for m in errs:
                        st.error(m)
                else:
                    ov_ed = loading_overlay("Enregistrement de la fiche utilisateur…")
                    try:
                        try:
                            next_ver = int(str(rp.get("version") or "1")) + 1
                        except ValueError:
                            next_ver = 2
                        country_n = normalize_country(e_country)
                        lang_n = normalize_pref_langue(e_lang)
                        if set_pwd:
                            salt_b64, hash_b64 = hash_password(e_pwd)
                            source_n = "compte"
                        else:
                            salt_b64 = str(rp.get("password_salt_b64") or "")
                            hash_b64 = str(rp.get("password_hash_b64") or "")
                            source_n = str(rp.get("source") or "newsletter").strip() or "newsletter"
                            if hash_b64.strip():
                                source_n = "compte"

                        _supersede_users_by_email(em_pick)
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="users",
                            values_by_col={
                                "entity_id": auth_uid,
                                "email": em_pick,
                                "first_name": e_fn.strip(),
                                "last_name": e_ln.strip(),
                                "phone_e164": e_ph.strip(),
                                "country": country_n,
                                "pref_langue": lang_n,
                                "source": source_n,
                                "password_salt_b64": salt_b64,
                                "password_hash_b64": hash_b64,
                                "version": next_ver,
                            },
                            version=next_ver,
                        )

                        target_weekly_langs = set(weekly_langs_n if e_opt else [])
                        current_weekly_langs = set(cur_weekly_langs if cur_opt else [])
                        if target_weekly_langs != current_weekly_langs:
                            length_pref_ed = str((latest_sub_ed or {}).get("length_pref") or "250")
                            for lg in sorted(target_weekly_langs - current_weekly_langs):
                                append_immutable_row(
                                    gspread_client=gs,
                                    spreadsheet_id=cfg.gsheet_id,
                                    table="subscriptions",
                                    values_by_col={
                                        "entity_id": sha256(
                                            f"sub|{auth_uid}|adm_edit|{lg}|{utc_now_iso()}".encode("utf-8")
                                        ).hexdigest()[:24],
                                        "user_entity_id": auth_uid,
                                        "type": "weekly_friday",
                                        "zone": "france",
                                        "pref_langue": lg,
                                        "length_pref": length_pref_ed,
                                        "opt_in": "true",
                                        "active": "true",
                                    },
                                )
                            for lg in sorted(current_weekly_langs - target_weekly_langs):
                                append_immutable_row(
                                    gspread_client=gs,
                                    spreadsheet_id=cfg.gsheet_id,
                                    table="subscriptions",
                                    values_by_col={
                                        "entity_id": sha256(
                                            f"sub|{auth_uid}|adm_edit_off|{lg}|{utc_now_iso()}".encode("utf-8")
                                        ).hexdigest()[:24],
                                        "user_entity_id": auth_uid,
                                        "type": "weekly_friday",
                                        "zone": "france",
                                        "pref_langue": lg,
                                        "length_pref": length_pref_ed,
                                        "opt_in": "false",
                                        "active": "false",
                                    },
                                )

                        mail_note = ""
                        if send_welcome:
                            try:
                                from datetime import datetime, timedelta, timezone
                                from secrets import token_urlsafe

                                from core.account_welcome_email import send_account_welcome_email
                                from core.sheets_db import ensure_table, get_table_spec
                                from ui.navigation import lumenvia_app_origin_url

                                origin = (lumenvia_app_origin_url() or "").rstrip("/")
                                login_url = (origin + "/?route=account") if origin else ""
                                temp_pwd: str | None = e_pwd if set_pwd else None
                                reset_url: str | None = None
                                if not temp_pwd:
                                    ensure_table(
                                        gspread_client=gs,
                                        spreadsheet_id=cfg.gsheet_id,
                                        table=get_table_spec("password_resets"),
                                    )
                                    tok = token_urlsafe(32)
                                    tok_h = sha256(tok.encode("utf-8")).hexdigest()
                                    exp = (
                                        datetime.now(timezone.utc) + timedelta(hours=2)
                                    ).isoformat(timespec="seconds")
                                    append_immutable_row(
                                        gspread_client=gs,
                                        spreadsheet_id=cfg.gsheet_id,
                                        table="password_resets",
                                        values_by_col={
                                            "entity_id": sha256(
                                                f"pwdreset|{em_pick}|{utc_now_iso()}".encode("utf-8")
                                            ).hexdigest()[:24],
                                            "email": em_pick,
                                            "token_hash": tok_h,
                                            "expires_at": exp,
                                            "used": "false",
                                        },
                                    )
                                    if not origin:
                                        raise RuntimeError(
                                            "URL publique introuvable (PUBLIC_APP_URL) pour le lien."
                                        )
                                    reset_url = (
                                        origin
                                        + "/?route=reset_password&email="
                                        + em_pick
                                        + "&token="
                                        + tok
                                    )
                                send_account_welcome_email(
                                    to_email=em_pick,
                                    first_name=e_fn.strip(),
                                    login_url=login_url or origin,
                                    temp_password=temp_pwd,
                                    reset_password_url=reset_url,
                                )
                                mail_note = f" E-mail d’instructions envoyé à {em_pick}."
                            except Exception as ex_mail:
                                mail_note = f" Fiche OK, mais e-mail non envoyé : {ex_mail}"

                        invalidate_fetch_records_cache(spreadsheet_id=cfg.gsheet_id, table="users")
                        invalidate_fetch_records_cache(
                            spreadsheet_id=cfg.gsheet_id, table="subscriptions"
                        )
                        invalidate_adm_sheets_fetch_cache()
                        msg_ok = "Fiche enregistrée."
                        if set_pwd:
                            msg_ok += " Mot de passe initialisé — l’utilisateur peut se connecter."
                        msg_ok += mail_note
                        st.session_state["adm_addsub_flash"] = msg_ok
                        st.rerun()
                    finally:
                        ov_ed.empty()

            # Renvoi d’instructions sans modifier la fiche (compte déjà avec mot de passe)
            if has_pwd:
                if st.button(
                    "Renvoyer uniquement l’e-mail d’instructions (lien de réinit.)",
                    key="adm_edit_resend_welcome",
                    use_container_width=True,
                ):
                    ov_m = loading_overlay("Envoi de l’e-mail d’instructions…")
                    try:
                        from datetime import datetime, timedelta, timezone
                        from secrets import token_urlsafe

                        from core.account_welcome_email import send_account_welcome_email
                        from core.sheets_db import ensure_table, get_table_spec
                        from ui.navigation import lumenvia_app_origin_url

                        origin = (lumenvia_app_origin_url() or "").rstrip("/")
                        if not origin:
                            raise RuntimeError("PUBLIC_APP_URL manquant pour générer le lien.")
                        ensure_table(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table=get_table_spec("password_resets"),
                        )
                        tok = token_urlsafe(32)
                        tok_h = sha256(tok.encode("utf-8")).hexdigest()
                        exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(
                            timespec="seconds"
                        )
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="password_resets",
                            values_by_col={
                                "entity_id": sha256(
                                    f"pwdreset|{em_pick}|{utc_now_iso()}".encode("utf-8")
                                ).hexdigest()[:24],
                                "email": em_pick,
                                "token_hash": tok_h,
                                "expires_at": exp,
                                "used": "false",
                            },
                        )
                        reset_url = (
                            origin
                            + "/?route=reset_password&email="
                            + em_pick
                            + "&token="
                            + tok
                        )
                        send_account_welcome_email(
                            to_email=em_pick,
                            first_name=str(rp.get("first_name") or "").strip(),
                            login_url=origin + "/?route=account",
                            temp_password=None,
                            reset_password_url=reset_url,
                        )
                        st.success(f"E-mail d’instructions envoyé à **{em_pick}**.")
                    except Exception as ex:
                        st.error(str(ex))
                    finally:
                        ov_m.empty()

    # Filtre simple (côté UI) : sous-chaîne e-mail
    q = st.text_input("Filtrer (e-mail contient)", value="", key="adm_accounts_filter").strip().lower()

    # Admin canonical : login secret (si présent)
    try:
        adm_login, _adm_pwd = admin_login_and_password()
    except Exception:
        adm_login = ""

    latest = _latest_by_email(users)
    if q:
        latest = [u for u in latest if q in str(u.get("email") or "").strip().lower()]

    def _latest_sub_by_user_entity_id(sub_rows: list[dict]) -> dict[str, dict]:
        by: dict[str, dict] = {}
        for r in sub_rows:
            if str(r.get("type") or "").strip() != "weekly_friday":
                continue
            uid = str(r.get("user_entity_id") or "").strip()
            if not uid:
                continue
            prev = by.get(uid)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[uid] = r
        return by

    latest_sub = _latest_sub_by_user_entity_id(subs)

    def _kind(u: dict) -> str:
        em = str(u.get("email") or "").strip().lower()
        src = str(u.get("source") or "").strip().lower()
        has_pwd = bool(str(u.get("password_hash_b64") or "").strip())
        if adm_login and em == adm_login:
            return "ADMIN"
        if src in ("dry_run", "test_emailing", "test"):
            return "TEST (DRY-RUN)"
        if src == "newsletter":
            return "NOUS REJOINDRE"
        if has_pwd:
            return "COMPTE"
        return "AUTRE"

    buckets: dict[str, list[dict]] = {"NOUS REJOINDRE": [], "ADMIN": [], "TEST (DRY-RUN)": [], "COMPTE": [], "AUTRE": []}
    for u in latest:
        buckets[_kind(u)].append(u)

    st.markdown(
        f"""
<div style="display:flex;gap:0.75rem;flex-wrap:wrap;justify-content:center;margin:0.5rem 0 0.75rem;">
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Nous rejoindre</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['NOUS REJOINDRE'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Admin</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['ADMIN'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Test (dry-run)</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['TEST (DRY-RUN)'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Comptes</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['COMPTE'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Total</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(latest)}</div>
  </div>
</div>
        """.strip(),
        unsafe_allow_html=True,
    )

    def _render_table(title: str, rows: list[dict]) -> None:
        body_rows = []
        for u in rows[:400]:
            em = str(u.get("email") or "").strip().lower()
            created = str(u.get("created_at") or "").strip()
            src = str(u.get("source") or "").strip()
            uid = str(u.get("entity_id") or "").strip()
            lg = user_pref_langue(u)
            weekly_langs = weekly_subscription_langs(subs, uid, default_lang=lg) if uid else []
            ordered_langs = [lg] + [x for x in weekly_langs if x != lg]
            ordered_langs = list(dict.fromkeys(ordered_langs))
            lang_cell = (
                f"<span style='display:inline-flex;align-items:center;gap:0.35rem;white-space:nowrap;flex-wrap:wrap;'>"
                + "".join(
                    f"<span style='display:inline-flex;align-items:center;gap:0.2rem;'>"
                    f"{lang_flag_html(code, height=14)}"
                    f"<span style='opacity:0.9;'>{html_escape(code)}</span>"
                    f"</span>"
                    for code in ordered_langs
                )
                + "</span>"
            )
            opt_txt = "—"
            if uid and title.lower().startswith("nous rejoindre"):
                opt_txt = "Oui" if weekly_langs else "Non"
            body_rows.append(
                "<tr>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);'>{html_escape(em)}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);'>{lang_cell}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{html_escape(src or '—')}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{html_escape(opt_txt)}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{html_escape(created or '—')}</td>"
                "</tr>"
            )
        html = f"""
<div style="margin:0.75rem 0 0.25rem;font-weight:700;color:#6b5918;text-align:center;">{html_escape(title)}</div>
<div style="overflow:auto;border:1px solid rgba(212,175,55,0.35);background:rgba(255,255,255,0.72);">
<table style="width:100%;border-collapse:collapse;font-size:0.95rem;">
  <thead>
    <tr style="background:rgba(212,175,55,0.10);">
      <th style="text-align:left;padding:9px 10px;">E-mail</th>
      <th style="text-align:left;padding:9px 10px;">Langue</th>
      <th style="text-align:left;padding:9px 10px;">Source</th>
      <th style="text-align:left;padding:9px 10px;">Opt-in</th>
      <th style="text-align:left;padding:9px 10px;">Créé le</th>
    </tr>
  </thead>
  <tbody>
    {''.join(body_rows) if body_rows else '<tr><td colspan="5" style="padding:10px;opacity:0.75;">Aucun.</td></tr>'}
  </tbody>
</table>
</div>
        """.strip()
        st.markdown(html, unsafe_allow_html=True)

    _render_table("Nous rejoindre", buckets["NOUS REJOINDRE"])
    _render_table("Admin", buckets["ADMIN"])
    _render_table("Test (dry-run)", buckets["TEST (DRY-RUN)"])
    _render_table("Comptes (mot de passe)", buckets["COMPTE"])
    if buckets["AUTRE"]:
        _render_table("Autres", buckets["AUTRE"])


