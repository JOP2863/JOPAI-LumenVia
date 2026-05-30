"""Admin — panneau d'envoi manuel e-mail / SMS (dry-run, tous opt-in)."""

from __future__ import annotations

import json
import re
import traceback
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape as html_escape
from pathlib import Path

import streamlit as st

from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE
from core.outbound import SmtpConfig
from core.sheets_db import (
    BASE_COLUMNS,
    SHEETS_ROW_STATUS_ACTIVE,
    SHEETS_ROW_STATUS_INACTIVE,
    TableSpec,
    append_immutable_row,
    build_gspread_client,
    compute_concat,
    ensure_table,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.emailing import (
    EmailTemplate,
    french_day_month_year,
    normalize_email_template_text,
    pick_latest_live_email_template,
    render_template,
    resolve_email_nom_du_dimanche,
)
from core.emailing_newsletter_html import (
    build_lv_newsletter_email_html,
    email_body_to_minimal_html,
)
from core.weekly_email_urls import weekly_email_signed_urls
from ui.admin.broadcast_recipients import (
    count_skipped_weekly_broadcast_recipients,
    is_broadcast_email_ok,
    lumenvia_manual_broadcast_recipient_pairs,
    lumenvia_manual_broadcast_users,
)
from ui.components import loading_overlay
from ui.navigation import lumenvia_app_origin_url as _lumenvia_app_origin_url


def render_emailing_manual_broadcast(
    *,
    gs: object,
    cfg: object,
    template_key: str,
    lang_fr: tuple[str, ...],
    subject: str,
    body: str,
    sa_json: str = "",
    tpl_rows: list[dict] | None = None,
) -> None:
    """Suite de `render_admin_emailing` : destinataires, confirmation, envoi."""
    import app as ap

    from ui.streamlit_caches import adm_sheets_fetch_cached

    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()

    st.divider()
    st.subheader("Déclencher un envoi (manuel)")
    st.caption(
        "Par défaut, c’est un **dry-run** : envoi uniquement vers les coordonnées de test (secrets). "
        "Coche l’option pour envoyer à tous les inscrits opt-in. "
        "L’envoi manuel reprend **exactement** l’objet et le corps du formulaire (comme l’aperçu) — "
        "pensez à **Enregistrer le template** pour que le scheduler utilise la même version."
    )

    # UI simplifiée : 3 cases uniquement
    send_email = st.checkbox("Envoyer e-mail", value=True, key="adm_email_send_email")
    send_sms = st.checkbox("Envoyer SMS", value=False, key="adm_email_send_sms")
    send_to_all = st.checkbox(
        "Envoyer à tous les inscrits opt-in (désactivé par défaut)",
        value=False,
        key="adm_email_send_to_all",
    )

    # Comportement implicite
    send_email_as_html = True
    use_lv_html_template = True
    send_email_html_only = False
    sms_short_mode = True
    def _is_email_ok(email: str) -> bool:
        email_lc = (email or "").strip().lower()
        return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_lc)) if email_lc else False

    # Dry-run : prioritaire depuis la table users (source = dry_run/test_emailing)
    # Cache 90 s : un rerun Streamlit ne relit pas 9000 lignes à chaque widget.
    users_rows_for_dry = (
        adm_sheets_fetch_cached(gsheet_id, "users", 9000, sa_json)
        if sa_json and gsheet_id
        else fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="users", limit=9000, use_cache=True)
    )
    dry_candidates = [
        u
        for u in users_rows_for_dry
        if str(u.get("source") or "").strip().lower() in ("dry_run", "test_emailing", "test")
        and _is_email_ok(str(u.get("email") or "").strip())
    ]
    dry_candidates.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    dry_user = dry_candidates[0] if dry_candidates else {}
    dry_email_in = str(dry_user.get("email") or "").strip()
    dry_phone_in = str(dry_user.get("phone_e164") or "").strip()
    # Fallback : secrets (option B à plat ou section [dry_run])
    dry_email_secret = ""
    dry_phone_secret = ""
    try:
        s = st.secrets
        dry_email_secret = str(s.get("EMAIL_DRY_RUN_TO") or "").strip()
        dry_phone_secret = str(s.get("SMS_DRY_RUN_TO") or "").strip()
        if not dry_email_secret and isinstance(s.get("dry_run"), dict):
            dry_email_secret = str((s.get("dry_run") or {}).get("EMAIL_DRY_RUN_TO") or "").strip()
        if not dry_phone_secret and isinstance(s.get("dry_run"), dict):
            dry_phone_secret = str((s.get("dry_run") or {}).get("SMS_DRY_RUN_TO") or "").strip()
    except Exception:
        pass

    if not dry_email_in and _is_email_ok(dry_email_secret):
        dry_email_in = dry_email_secret
    if not dry_phone_in and dry_phone_secret:
        dry_phone_in = dry_phone_secret

    # Destinataires de test (manuel) : adresses réelles uniquement via secrets
    # (`EMAIL_TEST_RECIPIENT_1` … `EMAIL_TEST_RECIPIENT_3` dans `.streamlit/secrets.toml`).
    # Libellés UI neutres : aucune adresse en clair dans le dépôt ni dans le HTML envoyé au client.
    test_recipient_slots: list[tuple[str, str]] = []
    try:
        ssec = st.secrets
        for i in range(1, 4):
            sk = f"EMAIL_TEST_RECIPIENT_{i}"
            em = str(ssec.get(sk) or "").strip().lower()
            if _is_email_ok(em):
                test_recipient_slots.append((em, str(i)))
    except Exception:
        pass

    selected_test_emails: list[str] = []
    for em_addr, slot_i in test_recipient_slots:
        if st.checkbox(
            f"Envoyer aussi au destinataire de test (profil {slot_i})",
            value=False,
            key=f"adm_email_test_opt_{slot_i}",
            disabled=bool(send_to_all),
        ):
            selected_test_emails.append(em_addr)
    manual_extra_raw = st.text_area(
        "Autres destinataires (saisie manuelle — une adresse par ligne, ou séparées par virgule / point-virgule)",
        value="",
        height=72,
        key="adm_email_manual_extra",
        disabled=bool(send_to_all),
        placeholder="ex. prenom.nom@laposte.net , autre@yahoo.fr",
    )

    def _parse_extra_emails(raw: object) -> list[str]:
        acc: list[str] = []
        for part in re.split(r"[\n,;]+", str(raw or "")):
            em = part.strip().lower()
            if _is_email_ok(em):
                acc.append(em)
        seen: set[str] = set()
        dedup: list[str] = []
        for em in acc:
            if em not in seen:
                seen.add(em)
                dedup.append(em)
        return dedup

    def _latest_user_by_email(email_lc: str) -> dict:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return {}
        best: dict = {}
        best_ts = ""
        for u in users_rows_for_dry:
            if str(u.get("email") or "").strip().lower() != em0:
                continue
            # En e-mailing, on veut la fiche "live" (immuabilité : une seule version Actif).
            if not sheet_row_status_is_live(u.get("status")):
                continue
            ts = str(u.get("created_at") or "")
            if not best or ts > best_ts:
                best = u
                best_ts = ts
        return best

    for em in _parse_extra_emails(manual_extra_raw):
        if em not in selected_test_emails:
            selected_test_emails.append(em)
    # Si rien n'est coché, on retombe sur le destinataire dry-run automatique (users/secrets)
    if not selected_test_emails and _is_email_ok(dry_email_in):
        selected_test_emails = [dry_email_in.strip().lower()]

    if not selected_test_emails and not dry_phone_in:
        st.warning(
            "Aucun destinataire de test trouvé. "
            "Coche au moins un destinataire ci-dessus, "
            "ou ajoute un e‑mail de test dans `users` (source=test), "
            "ou configure une adresse de test dans les secrets."
        )

    st.markdown("**Destinataires de test (aperçu)**")
    preview_lines: list[str] = []
    for em in selected_test_emails:
        u0 = _latest_user_by_email(em)
        fn0 = str(u0.get("first_name") or "Test").strip() or "Test"
        ln0 = str(u0.get("last_name") or "JOPAI").strip() or "JOPAI"
        src0 = str(u0.get("source") or "").strip() or "—"
        preview_lines.append(f"{em}\t{fn0}\t{ln0}\t{src0}")
    if dry_phone_in and not selected_test_emails:
        preview_lines.append(f"phone_e164:\t{dry_phone_in}")
    st.code(("\n".join(preview_lines) if preview_lines else "—")[:9000])
    debug_verbose = False

    # Garde-fous supplémentaires si envoi "tous opt-in"
    excluded_emails: set[str] = set()
    limit_to_n = 0
    confirm_all_ok = True
    confirm_phrase_ok = True
    if send_to_all:
        st.warning(
            "Tu es sur le point de cibler **tous les inscrits opt-in**. "
            "Prévisualise la liste, ajuste si besoin, puis confirme avant de pouvoir envoyer.",
            icon="⚠️",
        )
        with st.expander("Aperçu des destinataires (avant envoi)", expanded=True):
            users_preview = (
                users_rows_for_dry
                if sa_json and gsheet_id
                else fetch_records(
                    gspread_client=gs,
                    spreadsheet_id=gsheet_id,
                    table="users",
                    limit=8000,
                    use_cache=True,
                )
            )
            subs_preview = (
                adm_sheets_fetch_cached(gsheet_id, "subscriptions", 8000, sa_json)
                if sa_json and gsheet_id
                else fetch_records(
                    gspread_client=gs,
                    spreadsheet_id=gsheet_id,
                    table="subscriptions",
                    limit=8000,
                    use_cache=True,
                )
            )
            rec_preview = lumenvia_manual_broadcast_users(
                users_rows=users_preview,
                subs_rows=subs_preview,
                send_to_all=True,
                for_email=bool(send_email),
                for_sms=bool(send_sms),
            )
            skip_stats = count_skipped_weekly_broadcast_recipients(
                users_rows=users_preview,
                subs_rows=subs_preview,
                for_email=bool(send_email),
                for_sms=bool(send_sms),
            )
            em_list = sorted(
                {
                    str(u.get("email") or "").strip().lower()
                    for u in rec_preview
                    if is_broadcast_email_ok(str(u.get("email") or ""))
                }
            )
            st.caption(
                f"Destinataires retenus pour cet envoi : **{len(rec_preview)}** "
                f"(e-mails valides : **{len(em_list)}**)."
            )
            if skip_stats["no_user"] or skip_stats["no_email"] or skip_stats["no_phone"]:
                parts: list[str] = []
                if skip_stats["no_user"]:
                    parts.append(f"{skip_stats['no_user']} sans fiche `users`")
                if skip_stats["no_email"] and send_email:
                    parts.append(f"{skip_stats['no_email']} sans e-mail valide")
                if skip_stats["no_phone"] and send_sms:
                    parts.append(f"{skip_stats['no_phone']} sans téléphone E.164")
                st.info(
                    "Abonnements hebdo ignorés pour cet envoi : " + ", ".join(parts) + ".",
                    icon="ℹ️",
                )

            # Exclusions (optionnel)
            excl_pick = st.multiselect(
                "Exclure des e-mails (optionnel)",
                options=em_list,
                default=[],
                key="adm_email_excl_pick",
            )
            excl_paste = st.text_area(
                "Ou coller une liste d’e-mails à exclure (un par ligne, optionnel)",
                value="",
                height=80,
                key="adm_email_excl_paste",
            )
            excluded_emails = {str(e or "").strip().lower() for e in (excl_pick or []) if str(e or "").strip()}
            for ln in (excl_paste or "").splitlines():
                s0 = str(ln or "").strip().lower()
                if s0 and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s0):
                    excluded_emails.add(s0)

            filtered_preview = (
                [u for u in rec_preview if str(u.get("email") or "").strip().lower() not in excluded_emails]
                if excluded_emails
                else list(rec_preview)
            )

            limit_to_n = int(
                st.number_input(
                    "Limiter l’envoi aux N premiers (optionnel, utile pour un test)",
                    min_value=0,
                    max_value=max(0, len(filtered_preview)),
                    value=0,
                    step=1,
                    key="adm_email_limit_to_n",
                )
            )

            # Mini table (limite) : qui va recevoir
            show_n = st.slider(
                "Afficher les N premiers destinataires (aperçu)",
                min_value=10,
                max_value=300,
                value=60,
                step=10,
                key="adm_email_preview_n",
            )
            lines = []
            for u in filtered_preview[: int(show_n)]:
                em0 = str(u.get("email") or "").strip().lower()
                fn0 = str(u.get("first_name") or "").strip()
                ln0 = str(u.get("last_name") or "").strip()
                lines.append(f"{em0}\t{fn0}\t{ln0}")
            st.code(("\n".join(lines) if lines else "—")[:9000])

            final_list = filtered_preview[: int(limit_to_n)] if limit_to_n > 0 else filtered_preview
            st.markdown(f"**Après exclusions/limite :** {len(final_list)} destinataire(s).")

            confirm_all_ok = st.checkbox(
                "J’ai vérifié la liste et je confirme vouloir envoyer à ces destinataires.",
                value=False,
                key="adm_email_confirm_checked",
            )
            phrase = st.text_input(
                "Pour activer l’envoi, tape exactement : ENVOYER",
                value="",
                key="adm_email_confirm_phrase",
            ).strip()
            confirm_phrase_ok = phrase == "ENVOYER"

    can_execute = (confirm_all_ok and confirm_phrase_ok) if send_to_all else True
    can_execute = can_execute and (bool(send_to_all) or bool(selected_test_emails) or bool(dry_phone_in))

    if st.button(
        "Lancer l’envoi",
        type="primary",
        key="adm_email_send_run",
        disabled=(not (send_email or send_sms)) or (not can_execute),
    ):
        ov = loading_overlay("Préparation de l’envoi…")
        try:
            import traceback

            try:
                if tpl_rows is not None:
                    _tpl_mail_rows = tpl_rows
                elif sa_json and gsheet_id:
                    _tpl_mail_rows = adm_sheets_fetch_cached(gsheet_id, "email_templates", 0, sa_json)
                else:
                    _tpl_mail_rows = fetch_records(
                        gspread_client=gs,
                        spreadsheet_id=gsheet_id,
                        table="email_templates",
                        limit=0,
                        use_cache=True,
                    )
            except Exception:
                _tpl_mail_rows = []
            _tpl_live_mail = pick_latest_live_email_template(
                _tpl_mail_rows, template_key=template_key, channel="email", language_in=("fr", "fr-fr", "france", "")
            )
            _live_subj = (
                str(_tpl_live_mail.get("subject") or "").strip() if _tpl_live_mail else ""
            )
            _live_body = str(_tpl_live_mail.get("body") or "").strip() if _tpl_live_mail else ""
            # Aligné sur l’aperçu : le formulaire prime (Sheets sert de secours si le champ est vide).
            subject_rt = (subject or "").strip() or _live_subj
            body_rt = (body or "").strip() or _live_body
            if not subject_rt or not body_rt:
                st.error(
                    "Objet ou corps vide — complétez le formulaire ou enregistrez un template **Actif** sur Sheets."
                )
                ov.empty()
                st.stop()
            if not _tpl_live_mail:
                st.warning(
                    "Aucune ligne avec **`status` = Actif** pour ce template (clé `weekly_friday_lumenvia`, e-mail, FR). "
                    "L’envoi utilise le **texte du formulaire** — enregistrez-le pour le scheduler et l’historique."
                )
            elif _live_body and (body or "").strip():
                _form_norm = normalize_email_template_text(body)
                _live_norm = normalize_email_template_text(_live_body)
                if _form_norm != _live_norm or normalize_email_template_text(subject) != normalize_email_template_text(
                    _live_subj
                ):
                    _ver = str(_tpl_live_mail.get("version") or "?").strip()
                    st.warning(
                        f"Le formulaire diffère du template **Actif** en Sheets (version {_ver}). "
                        "Cet envoi reprend le **formulaire** (aperçu). Cliquez **Enregistrer le template** "
                        "pour aligner le scheduler."
                    )

            # recipients
            users_rows = (
                users_rows_for_dry
                if send_to_all and sa_json and gsheet_id
                else (
                    adm_sheets_fetch_cached(gsheet_id, "users", 8000, sa_json)
                    if sa_json and gsheet_id
                    else fetch_records(
                        gspread_client=gs,
                        spreadsheet_id=gsheet_id,
                        table="users",
                        limit=8000,
                        use_cache=True,
                    )
                )
            )
            subs_rows = (
                adm_sheets_fetch_cached(gsheet_id, "subscriptions", 8000, sa_json)
                if sa_json and gsheet_id
                else fetch_records(
                    gspread_client=gs,
                    spreadsheet_id=gsheet_id,
                    table="subscriptions",
                    limit=8000,
                    use_cache=True,
                )
            )

            recipients: list[tuple[str, dict]] = []
            if send_to_all:
                recipients = lumenvia_manual_broadcast_recipient_pairs(
                    users_rows=users_rows,
                    subs_rows=subs_rows,
                    send_to_all=True,
                    for_email=bool(send_email),
                    for_sms=bool(send_sms),
                )
                skip_send = count_skipped_weekly_broadcast_recipients(
                    users_rows=users_rows,
                    subs_rows=subs_rows,
                    for_email=bool(send_email),
                    for_sms=bool(send_sms),
                )
                if skip_send["no_user"] or skip_send["no_email"] or skip_send["no_phone"]:
                    st.caption(
                        "Ignorés : "
                        + ", ".join(
                            p
                            for p in (
                                f"{skip_send['no_user']} sans fiche users" if skip_send["no_user"] else "",
                                f"{skip_send['no_email']} sans e-mail" if skip_send["no_email"] and send_email else "",
                                f"{skip_send['no_phone']} sans téléphone" if skip_send["no_phone"] and send_sms else "",
                            )
                            if p
                        )
                    )
                # Applique exclusions / limite (si demandées dans l’aperçu)
                if excluded_emails:
                    recipients = [
                        (uid0, u)
                        for (uid0, u) in recipients
                        if str((u or {}).get("email") or "").strip().lower() not in excluded_emails
                    ]
                if limit_to_n > 0:
                    recipients = recipients[: int(limit_to_n)]
            else:
                # Destinataires de test sélectionnés (1 ou 2) + fallback dry-run
                recipients = []
                for em in selected_test_emails:
                    u0 = _latest_user_by_email(em)
                    recipients.append(
                        (
                            "dry_run",
                            {
                                "email": em.strip(),
                                "phone_e164": str(u0.get("phone_e164") or "").strip(),
                                "first_name": str(u0.get("first_name") or "Test").strip() or "Test",
                                "last_name": str(u0.get("last_name") or "JOPAI").strip() or "JOPAI",
                            },
                        )
                    )
                if not recipients:
                    recipients = [
                        (
                            "dry_run",
                            {
                                "email": dry_email_in.strip(),
                                "phone_e164": dry_phone_in.strip(),
                                "first_name": "Test",
                                "last_name": "JOPAI",
                            },
                        )
                    ]

            from core.outbound import SmtpConfig, TwilioConfig, send_smtp_email, send_twilio_sms
            from core.sheets_db import TableSpec, ensure_table

            def _secret_get(*keys: str) -> str:
                """
                Lit une valeur depuis st.secrets en supportant:
                - clés racine (ex: SMTP_HOST)
                - sous-sections (ex: [smtp] SMTP_HOST=...)
                """
                try:
                    s = st.secrets
                except Exception:
                    return ""
                # 1) racine
                for k in keys:
                    try:
                        v = s.get(k)  # type: ignore[attr-defined]
                    except Exception:
                        v = None
                    if v is not None and str(v).strip():
                        return str(v).strip()
                # 2) sous-sections connues
                sections = ("smtp", "twilio", "dry_run")
                for sec in sections:
                    try:
                        block = s.get(sec)  # type: ignore[attr-defined]
                    except Exception:
                        block = None
                    if not isinstance(block, dict):
                        continue
                    for k in keys:
                        v = block.get(k)
                        if v is not None and str(v).strip():
                            return str(v).strip()
                return ""

            # Outbound log
            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=TableSpec(
                    name="outbound_messages",
                    columns=with_concat(
                        [
                            *BASE_COLUMNS,
                            "channel",
                            "template_key",
                            "user_entity_id",
                            "email",
                            "phone_e164",
                            "date_dimanche",
                            "status_detail",
                            "scheduled_at",
                            "sent_at",
                            "error",
                        ]
                    ),
                ),
            )

            # SMTP config
            try:
                smtp_cfg = SmtpConfig(
                    host=_secret_get("SMTP_HOST"),
                    port=int(_secret_get("SMTP_PORT") or 587),
                    username=_secret_get("SMTP_USER"),
                    password=_secret_get("SMTP_PASSWORD"),
                    from_email=_secret_get("SMTP_FROM"),
                    use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower()
                    not in ("0", "false", "no", "off"),
                )
            except Exception:
                smtp_cfg = SmtpConfig(host="", port=587, username="", password="", from_email="")

            # Twilio config
            try:
                tw_cfg = TwilioConfig(
                    account_sid=_secret_get("TWILIO_ACCOUNT_SID"),
                    auth_token=_secret_get("TWILIO_AUTH_TOKEN"),
                    from_phone_e164=_secret_get("TWILIO_FROM", "TWILIO_FROM_NUMBER"),
                )
            except Exception:
                tw_cfg = TwilioConfig(account_sid="", auth_token="", from_phone_e164="")

            ok = 0
            err = 0
            debug_rows: list[dict[str, str]] = []


            def _inject_illustration_placeholder(*, text: str, url_illustration: str, as_html: bool) -> str:
                """
                Remplace le placeholder libre du template docx par un rendu concret.
                - HTML: injecte <img> + lien
                - texte: injecte l'URL si dispo
                """
                s = str(text or "")
                # Tolère plusieurs variantes observées (apostrophes différentes / espaces).
                variants = (
                    "{{affichage de l’illustration de la semaine}}",
                    "{{affichage de l'illustration de la semaine}}",
                    "{{affichage de l’illustration de la semaine }}",
                    "{{affichage de l'illustration de la semaine }}",
                )
                if not any(v in s for v in variants):
                    return s
                u = (url_illustration or "").strip()
                if not u:
                    rep = "" if as_html else ""
                else:
                    if as_html:
                        rep = (
                            f'<p><a href="{u}" target="_blank" rel="noopener noreferrer">Voir l’illustration de la semaine</a></p>'
                            f'<p><img src="{u}" alt="Illustration de la semaine"></p>'
                        )
                    else:
                        rep = f"Illustration : {u}"
                for v in variants:
                    s = s.replace(v, rep)
                return s

            # Balises communes (aligné sur l’expander « Dimanche ciblé » dans emailing.py)
            try:
                d_pick = st.session_state.get("adm_email_sunday_pick")
                if d_pick is None:
                    today = date.today()
                    d_pick = today + timedelta(days=(6 - today.weekday()) % 7)
            except Exception:
                d_pick = date.today()
            if not isinstance(d_pick, date):
                try:
                    d_pick = date.fromisoformat(str(d_pick)[:10])
                except Exception:
                    d_pick = date.today()
            date_str = d_pick.isoformat()[:10]
            try:
                ident0, _texts0 = ap.cached_aelf(date_str, zone="france", _identity_schema=4)
            except Exception:
                ident0 = None
            origin = _lumenvia_app_origin_url() or ""
            url_app = (origin.rstrip("/") + "/?sunday=" + date_str) if origin else ""
            _urls_send = weekly_email_signed_urls(cfg=cfg, gs=gs, date_str=date_str, zone="france")
            values: dict[str, str] = {
                "prenom": "Jean",
                "nom": "Dupont",
                "origin": origin,
                "date_dimanche": french_day_month_year(d_pick),
                "nom_du_dimanche": resolve_email_nom_du_dimanche(
                    identity=ident0,
                    date_str=date_str,
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                ),
                "url_pdf": _urls_send["url_pdf"],
                "url_audio": _urls_send["url_audio"],
                "url_audio_readings": _urls_send["url_audio_readings"],
                "url_illustration": _urls_send["url_illustration"],
                "illustration_description": (_urls_send.get("illustration_description") or "").strip(),
                "url_app": url_app,
                "optout_url": (origin.rstrip("/") + "/?route=join") if origin else "",
            }

            for uid0, urec in recipients[:500]:
                to_email = str(urec.get("email") or "").strip()
                to_phone = str(urec.get("phone_e164") or "").strip()
                values2 = dict(values)
                values2["prenom"] = str(urec.get("first_name") or "—").strip() or "—"
                values2["nom"] = str(urec.get("last_name") or "—").strip() or "—"
                values2["email"] = to_email
                # Lien préférences: pré-remplit l'email sur "Nous rejoindre"
                try:
                    from urllib.parse import quote_plus
                except Exception:  # pragma: no cover
                    quote_plus = None  # type: ignore[assignment]
                if values2.get("origin") and to_email:
                    enc = quote_plus(to_email) if quote_plus else to_email
                    values2["optout_url"] = values2["origin"].rstrip("/") + "/?route=join&email=" + enc
                rendered2 = render_template(EmailTemplate(subject=subject_rt, body=body_rt), values=values2)
                # Placeholder "docx" (non-tag) : illustration
                rendered2 = EmailTemplate(
                    subject=rendered2.subject,
                    body=_inject_illustration_placeholder(
                        text=rendered2.body,
                        url_illustration=str(values2.get("url_illustration") or ""),
                        as_html=False,
                    ),
                )
                # Nettoyage des artefacts du template (copier/coller mail) : on ne retire « Objet : … »
                # que si cette ligne redit essentiellement l’objet réel (sinon on garde variantes / mentions de test).
                body_clean = (rendered2.body or "").replace("\r\n", "\n").strip()

                def _maybe_strip_objet_preamble(*, body: str, subject: str) -> str:
                    b = body
                    m = re.match(r"(?im)^\s*Objet\s*:\s*(.+?)\s*\n+", b)
                    if not m:
                        return b
                    obj_line = str(m.group(1) or "").strip()
                    subj = str(subject or "").strip()

                    def _squash(s: str) -> str:
                        return re.sub(r"\s+", " ", s).replace("—", "-").strip().lower()

                    if _squash(obj_line) == _squash(subj) or (
                        subj and _squash(subj) in _squash(obj_line) and len(_squash(obj_line)) - len(_squash(subj)) <= 2
                    ):
                        return b[m.end() :].lstrip()
                    return b

                body_clean = _maybe_strip_objet_preamble(body=body_clean, subject=rendered2.subject)
                body_clean = re.sub(r"(?im)^\s*Corps du message\s*:\s*\n*", "", body_clean)
                rendered2 = EmailTemplate(subject=rendered2.subject, body=body_clean.strip())

                if send_email:
                    try:
                        if not smtp_cfg.host or not smtp_cfg.from_email:
                            raise RuntimeError("SMTP non configuré (SMTP_HOST/SMTP_FROM).")
                        if not to_email:
                            raise RuntimeError("E-mail destinataire manquant.")
                        html_src = _inject_illustration_placeholder(
                            text=rendered2.body,
                            url_illustration=str(values2.get("url_illustration") or ""),
                            as_html=True,
                        )
                        if send_email_as_html and use_lv_html_template:
                            html2 = build_lv_newsletter_email_html(
                                subject0=rendered2.subject,
                                values0={k: str(v) for k, v in values2.items()},
                                intro_text=rendered2.body,
                            )
                        else:
                            html2 = email_body_to_minimal_html(html_src) if send_email_as_html else None
                        _notice_txt_mail = rendered2.body.strip()
                        _notice_txt_mail = (_notice_txt_mail + "\n\n—\n") if _notice_txt_mail else ""
                        _notice_txt_mail += LUMENVIA_DEVELOPMENT_NOTICE
                        send_smtp_email(
                            cfg=smtp_cfg,
                            to_email=to_email,
                            subject=rendered2.subject,
                            body_text=_notice_txt_mail,
                            body_html=html2,
                            html_only=bool(send_email_html_only and send_email_as_html),
                        )
                        ok += 1
                        if debug_verbose:
                            debug_rows.append(
                                {
                                    "channel": "email",
                                    "status": "ok",
                                    "uid": str(uid0),
                                    "to": to_email,
                                    "detail": "sent",
                                }
                            )
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|email|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "email",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": to_email,
                                "phone_e164": "",
                                "date_dimanche": date_str,
                                "status_detail": "sent",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": utc_now_iso(),
                                "error": "",
                            },
                        )
                    except Exception as e:
                        err += 1
                        tb = traceback.format_exc()
                        debug_rows.append(
                            {
                                "channel": "email",
                                "status": "error",
                                "uid": str(uid0),
                                "to": (to_email or "—"),
                                "detail": f"{type(e).__name__}: {str(e)}",
                            }
                        )
                        if debug_verbose:
                            st.error(f"Erreur EMAIL → {to_email or '—'}")
                            st.code(tb[:8000] or (str(e)[:2000] or "—"))
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|email|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "email",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": to_email,
                                "phone_e164": "",
                                "date_dimanche": date_str,
                                "status_detail": "error",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": "",
                                "error": str(e)[:900],
                            },
                        )

                if send_sms:
                    try:
                        if not tw_cfg.account_sid or not tw_cfg.from_phone_e164:
                            raise RuntimeError("Twilio non configuré (TWILIO_*).")
                        if not to_phone:
                            raise RuntimeError("Téléphone destinataire manquant.")
                        if not re.match(r"^\+[1-9]\d{6,14}$", to_phone):
                            raise RuntimeError("Téléphone invalide (format E.164 attendu, ex: +33612345678).")
                        from core.outbound import fetch_twilio_message_status

                        sms_body = rendered2.body
                        if sms_short_mode:
                            # SMS ultra-minimaliste pour éviter tout filtrage opérateur (Twilio 30044).
                            # Aucun lien, pas d’emoji, pas de ponctuation exotique.
                            sms_body = "Message de JOPAI LumenVia"

                        sid = send_twilio_sms(cfg=tw_cfg, to_phone_e164=to_phone, body_text=sms_body)
                        st_tw = fetch_twilio_message_status(cfg=tw_cfg, sid=sid) if sid else {"status": "", "error_code": "", "error_message": ""}
                        if debug_verbose and st_tw.get("status") == "not_found":
                            st.warning(
                                "Twilio: SID introuvable via l'API. "
                                "Tu consultes probablement un autre projet/compte dans la console Twilio. "
                                f"Compte utilisé par l'app: …{str(tw_cfg.account_sid or '')[-6:]} "
                                f"(from: {tw_cfg.from_phone_e164 or '—'})."
                            )
                        ok += 1
                        if debug_verbose:
                            debug_rows.append(
                                {
                                    "channel": "sms",
                                    "status": "ok",
                                    "uid": str(uid0),
                                    "to": to_phone,
                                    "detail": (
                                        f"sid={sid} status={st_tw.get('status') or '—'}"
                                        + (f" err={st_tw.get('error_code')}" if st_tw.get("error_code") else "")
                                        + (f" msg={st_tw.get('error_message')}" if st_tw.get("error_message") else "")
                                    )
                                    if sid
                                    else "sent",
                                }
                            )
                            st.caption(f"SMS envoyé (contenu) : {sms_body}")
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|sms|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "sms",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": "",
                                "phone_e164": to_phone,
                                "date_dimanche": date_str,
                                "status_detail": (
                                    f"sid={sid} status={st_tw.get('status') or '—'}"
                                    + (f" err={st_tw.get('error_code')}" if st_tw.get("error_code") else "")
                                    + (f" msg={st_tw.get('error_message')}" if st_tw.get("error_message") else "")
                                )
                                if sid
                                else "sent",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": utc_now_iso(),
                                "error": "",
                            },
                        )
                    except Exception as e:
                        err += 1
                        tb = traceback.format_exc()
                        debug_rows.append(
                            {
                                "channel": "sms",
                                "status": "error",
                                "uid": str(uid0),
                                "to": (to_phone or "—"),
                                "detail": f"{type(e).__name__}: {str(e)}",
                            }
                        )
                        if debug_verbose:
                            st.error(f"Erreur SMS → {to_phone or '—'}")
                            st.code(tb[:8000] or (str(e)[:2000] or "—"))
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|sms|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "sms",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": "",
                                "phone_e164": to_phone,
                                "date_dimanche": date_str,
                                "status_detail": "error",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": "",
                                "error": str(e)[:900],
                            },
                        )

            if ok == 0 and err == 0:
                st.info("Aucun envoi effectué (aucun destinataire ou canaux désactivés).")
            elif err == 0 and ok > 0:
                st.success(f"Terminé : {ok} envoi(s) OK.")
            elif ok == 0 and err > 0:
                st.error(f"Échec : {err} erreur(s), aucun envoi réussi.")
            else:
                st.warning(f"Terminé partiellement : {ok} envoi(s) OK, {err} erreur(s).")
            if debug_verbose and debug_rows:
                st.markdown("**Debug (résumé)**")
                st.code(
                    ("\n".join([f"{r['channel']}\t{r['status']}\t{r['to']}\t{r['detail']}" for r in debug_rows])[:9000])
                    or "—"
                )
        finally:
            ov.empty()

