"""Admin — Planificateur de campagnes (CMPG / RUNS)."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape as html_escape

import streamlit as st

from core.config import load_config
from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE
from core.outbound import SmtpConfig
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_row,
    append_immutable_rows_bulk,
    build_gspread_client,
    ensure_table,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.subscriptions_util import subscription_is_active
from core.weekly_email_urls import weekly_email_signed_urls
from ui.admin.broadcast_recipients import lumenvia_manual_broadcast_users
from ui.components import loading_overlay
from ui.navigation import lumenvia_app_origin_url as _lumenvia_app_origin_url


def render_admin_scheduler() -> None:
    st.title("Planificateur d'envoi")
    st.markdown(
        """
<div style="text-align:center;margin-top:0.85rem;margin-bottom:12px;padding-top:4px;color:#0b2745;opacity:0.92;line-height:1.45;">
<em>Planifie et déclenche des envois (structure générique pour hebdo/quotidien).</em>
</div>
        """.strip(),
        unsafe_allow_html=True,
    )
    st.caption(
        "Tant que le **déclenchement automatique dans le cloud** n’est pas câblé, rien ne part au créneau tout seul. "
        "Pour **tester** : tuiles **Emailing** (envoi manuel, jeu d’options plus simple) ou **Exécuter maintenant** dans "
        "**Déclencher une campagne (manuel)** ci-dessous."
    )
    with st.expander("Précision sur les statuts des campagnes", expanded=False):
        st.markdown(
            """
**`enabled`** — la campagne est en service ou non pour la prévision et les interrupteurs dans la liste.  
**`status`** — statut **de cette ligne** dans Sheets (historique immutable) ; **l’app affiche toujours la ligne la plus récente**
par identifiant de campagne.
            """.strip()
        )

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante.")
        return

    from core.sheets_db import TableSpec, ensure_table, with_concat, BASE_COLUMNS

    gs = build_gspread_client(cfg.gcp_service_account)

    # Assure les tables
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="AliasTables",
            columns=["#ID", "Statut", "Version", "Nom Complet Table", "Acronyme Table", "Description"],
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="scheduler_campaigns",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "name",
                    "enabled",
                    "timezone",
                    "schedule_kind",
                    "schedule_spec",
                    "audience_kind",
                    "audience_spec",
                    "send_email",
                    "send_sms",
                    "email_template_key",
                    "sms_template_key",
                    "content_pdf",
                    "content_audio",
                    "content_illustration",
                    "content_app_link",
                ]
            ),
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="scheduler_runs",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "run_kind",
                    "status_detail",
                    "started_at",
                    "finished_at",
                    "recipients_ok",
                    "recipients_err",
                    "error",
                ]
            ),
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="audiences",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "audience_key",
                    "libelle",
                    "description",
                    "spec_aide",
                ]
            ),
        ),
    )

    from core.emailing import pick_latest_live_email_template

    # Seed audiences si table vide
    try:
        aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audiences", limit=2000)
    except Exception:
        aud_rows = []
    if not aud_rows:
        seed = [
            {
                "entity_id": sha256(f"audc|dry_run|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "dry_run",
                "libelle": "Test (dry-run)",
                "description": "Envoi uniquement au compte de test (source=dry_run), ou au destinataire de test des secrets si disponible.",
                "spec_aide": "",
            },
            {
                "entity_id": sha256(f"audc|weekly_friday_optin|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "weekly_friday_optin",
                "libelle": "Tous les inscrits opt-in",
                "description": "Envoi à tous les inscrits ayant opt-in=true et active=true (lettre du vendredi).",
                "spec_aide": "",
            },
            {
                "entity_id": sha256(f"audc|by_country|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_country",
                "libelle": "Filtrer par pays",
                "description": "Envoi uniquement aux utilisateurs dont country correspond.",
                "spec_aide": "Ex: FR",
            },
            {
                "entity_id": sha256(f"audc|by_source|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_source",
                "libelle": "Filtrer par source",
                "description": "Envoi aux utilisateurs dont source est dans la liste.",
                "spec_aide": "Ex: newsletter,admin,dry_run",
            },
            {
                "entity_id": sha256(f"audc|by_email_list|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_email_list",
                "libelle": "Liste d’e-mails",
                "description": "Envoi aux e-mails listés (1 par ligne).",
                "spec_aide": "Ex:\nnom@domaine.fr\nprenom@domaine.fr",
            },
        ]
        append_immutable_rows_bulk(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audiences", values_by_col_list=seed)
        aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audiences", limit=2000)

    st.subheader("Campagnes")
    try:
        rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="scheduler_campaigns", limit=2000)
    except Exception:
        rows = []

    default_key = "weekly_friday_lumenvia"
    with st.expander("Campagnes (activer / désactiver)", expanded=False):
        # Dernière version par campaign_key
        latest_by_key: dict[str, dict] = {}
        for r in rows:
            k = str(r.get("campaign_key") or "").strip()
            if not k:
                continue
            prev = latest_by_key.get(k)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                latest_by_key[k] = r

        keys = sorted(latest_by_key.keys())
        def _is_true(v: object) -> bool:
            return str(v or "").strip().lower() in ("true", "1", "yes", "oui", "active")

        def _next_trigger_label(camp: dict) -> str:
            try:
                from datetime import datetime, time as dtime, timedelta
                try:
                    from zoneinfo import ZoneInfo  # py3.9+
                except Exception:
                    ZoneInfo = None  # type: ignore[assignment]

                tz = str(camp.get("timezone") or "Europe/Paris").strip() or "Europe/Paris"
                tzinfo = ZoneInfo(tz) if ZoneInfo else None
                now = datetime.now(tzinfo) if tzinfo else datetime.now()

                kind = str(camp.get("schedule_kind") or "").strip().lower()
                spec = str(camp.get("schedule_spec") or "").strip().lower()
                if kind == "weekly":
                    # ex: "ven 19:00"
                    day = (spec.split(" ", 1)[0] if " " in spec else "").strip() or "ven"
                    hm = (spec.split(" ", 1)[1] if " " in spec else "19:00").strip()
                    hh, mm = (hm.split(":", 1) + ["0"])[:2]
                    target_t = dtime(int(hh), int(mm))
                    day_map = {"lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6}
                    target_wd = day_map.get(day, 4)
                    # prochain jour cible
                    delta_days = (target_wd - now.weekday()) % 7
                    cand = now.replace(hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0) + timedelta(days=delta_days)
                    if cand <= now:
                        cand = cand + timedelta(days=7)
                    return cand.strftime("%a %d/%m %H:%M")
                if kind == "daily":
                    # ex: "19:00"
                    hm = spec or "19:00"
                    hh, mm = (hm.split(":", 1) + ["0"])[:2]
                    target_t = dtime(int(hh), int(mm))
                    cand = now.replace(hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0)
                    if cand <= now:
                        cand = cand + timedelta(days=1)
                    return cand.strftime("%d/%m %H:%M")
                return "—"
            except Exception:
                return "—"

        def _clone_campaign(*, base: dict, enabled_value: bool) -> None:
            k0 = str(base.get("campaign_key") or "").strip()
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="scheduler_campaigns",
                values_by_col={
                    "entity_id": sha256(f"cmpg|{k0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "campaign_key": k0,
                    "name": str(base.get("name") or k0).strip(),
                    "enabled": "true" if enabled_value else "false",
                    "timezone": str(base.get("timezone") or "Europe/Paris").strip(),
                    "schedule_kind": str(base.get("schedule_kind") or "manual").strip(),
                    "schedule_spec": str(base.get("schedule_spec") or "").strip(),
                    "audience_kind": str(base.get("audience_kind") or "dry_run").strip(),
                    "audience_spec": str(base.get("audience_spec") or "").strip(),
                    "send_email": str(base.get("send_email") or "true").strip(),
                    "send_sms": str(base.get("send_sms") or "false").strip(),
                    "email_template_key": str(base.get("email_template_key") or "weekly_friday_lumenvia").strip(),
                    "sms_template_key": str(base.get("sms_template_key") or "").strip(),
                    "content_pdf": str(base.get("content_pdf") or "true").strip(),
                    "content_audio": str(base.get("content_audio") or "true").strip(),
                    "content_illustration": str(base.get("content_illustration") or "true").strip(),
                    "content_app_link": str(base.get("content_app_link") or "true").strip(),
                },
            )

        st.caption(
            "Note : le déclenchement automatique n’est pas encore branché côté cloud. "
            "La “prochaine fois” affichée est une prévision basée sur la règle."
        )

        for k in keys[:80]:
            camp = latest_by_key.get(k) or {}
            name = str(camp.get("name") or k).strip() or k
            enabled0 = _is_true(camp.get("enabled") or "false")
            nxt = _next_trigger_label(camp) if enabled0 else "—"

            c_left, c_right = st.columns([5, 1], gap="small")
            with c_left:
                st.markdown(
                    f"**{name}**  \n<small style='color:#475569'>Prochain déclenchement (prévision) : <strong>{nxt}</strong></small>",
                    unsafe_allow_html=True,
                )
            with c_right:
                # Toggle: si changement, on écrit une nouvelle version CMPG.
                cur = st.toggle("On/Off", value=enabled0, key=f"adm_sched_onoff_{k}", label_visibility="collapsed")
                if cur != enabled0:
                    _clone_campaign(base=camp, enabled_value=bool(cur))
                    st.rerun()

    # Dernière version par campaign_key (reutilisé ci-dessous)
    latest_by_key: dict[str, dict] = {}
    for r in rows:
        k = str(r.get("campaign_key") or "").strip()
        if not k:
            continue
        prev = latest_by_key.get(k)
        if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
            latest_by_key[k] = r
    keys = sorted(latest_by_key.keys()) or [default_key]

    with st.expander("Créer une nouvelle campagne", expanded=False):
        st.caption("Crée une campagne si elle n’existe pas encore. Ensuite, tu peux la paramétrer et la déclencher.")
        new_key = st.text_input(
            "Identifiant campagne",
            value="",
            key="adm_sched_new_key",
            placeholder="ex. hebdom_vendredi_2026",
        ).strip()
        new_name = st.text_input(
            "Nom (affiché)",
            value="",
            key="adm_sched_new_name",
            placeholder="ex. Hebdo — préparation dominicale",
        ).strip()
        if st.button("Créer la campagne", type="secondary"):
            if not new_key:
                st.error("Saisis un identifiant campagne (champ obligatoire).")
            else:
                exists = str(new_key) in latest_by_key
                if exists:
                    st.info("Cette campagne existe déjà.")
                else:
                    append_immutable_row(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="scheduler_campaigns",
                        values_by_col={
                            "entity_id": sha256(f"cmpg|{new_key}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                            "campaign_key": new_key,
                            "name": new_name or new_key,
                            "enabled": "true",
                            "timezone": "Europe/Paris",
                            "schedule_kind": "weekly",
                            "schedule_spec": "ven 19:00",
                            "audience_kind": "weekly_friday_optin",
                            "audience_spec": "",
                            "send_email": "true",
                            "send_sms": "true",
                            "email_template_key": "weekly_friday_lumenvia",
                            "sms_template_key": "weekly_friday_lumenvia_sms",
                            "content_pdf": "true",
                            "content_audio": "true",
                            "content_illustration": "true",
                            "content_app_link": "true",
                        },
                    )
                    st.success("Campagne créée.")
            st.rerun()

    with st.expander("Paramétrer une campagne", expanded=False):
        camp_sel = st.selectbox("Choisir une campagne", options=keys, index=0, key="adm_sched_pick")
        camp = latest_by_key.get(camp_sel) or {}

        st.markdown("**Réglages**")
        c1, c2, c3 = st.columns([1, 1, 1], gap="small")
        with c1:
            enabled = st.checkbox("Activée", value=str(camp.get("enabled") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_enabled")
            timezone = st.text_input("Fuseau horaire", value=str(camp.get("timezone") or "Europe/Paris"), key="adm_sched_tz").strip()
        with c2:
            kind_code = str(camp.get("schedule_kind") or "weekly").strip() or "weekly"
            kind_labels = {"manual": "Manuel", "weekly": "Hebdomadaire", "daily": "Quotidien"}
            kind_order = ["manual", "weekly", "daily"]
            kind_pick = st.selectbox(
                "Fréquence",
                options=kind_order,
                format_func=lambda k: kind_labels.get(k, k),
                index=kind_order.index(kind_code) if kind_code in kind_order else 1,
                key="adm_sched_kind",
            )
            schedule_kind = kind_pick
            # Règle: guidée (pas de saisie libre)
            _days = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
            _day_labels = {
                "lun": "Lundi",
                "mar": "Mardi",
                "mer": "Mercredi",
                "jeu": "Jeudi",
                "ven": "Vendredi",
                "sam": "Samedi",
                "dim": "Dimanche",
            }
            spec0 = str(camp.get("schedule_spec") or "ven 19:00").strip().lower()
            day0 = spec0.split(" ", 1)[0] if spec0 else "ven"
            if day0 not in _days:
                day0 = "ven"
            import datetime as _dt

            t0 = _dt.time(19, 0)
            try:
                if ":" in spec0:
                    hhmm = spec0.split(" ", 1)[1].strip() if " " in spec0 else "19:00"
                    hh, mm = hhmm.split(":", 1)
                    t0 = _dt.time(int(hh), int(mm))
            except Exception:
                t0 = _dt.time(19, 0)

            if schedule_kind == "weekly":
                d_pick = st.selectbox("Jour d’envoi", options=_days, index=_days.index(day0), format_func=lambda d: _day_labels.get(d, d), key="adm_sched_day")
                t_pick = st.time_input("Heure d’envoi", value=t0, key="adm_sched_time")
                try:
                    from datetime import datetime
                    import time as _time

                    now_loc = datetime.now().strftime("%H:%M:%S")
                    tz_name = _time.tzname[0] if _time.tzname else "—"
                    st.caption(f"Actuellement : {now_loc} ({tz_name})")
                except Exception:
                    st.caption("Actuellement : —")
                schedule_spec = f"{d_pick} {t_pick.hour:02d}:{t_pick.minute:02d}"
            elif schedule_kind == "daily":
                t_pick = st.time_input("Heure d’envoi", value=t0, key="adm_sched_time_d")
                try:
                    from datetime import datetime
                    import time as _time

                    now_loc = datetime.now().strftime("%H:%M:%S")
                    tz_name = _time.tzname[0] if _time.tzname else "—"
                    st.caption(f"Actuellement : {now_loc} ({tz_name})")
                except Exception:
                    st.caption("Actuellement : —")
                schedule_spec = f"{t_pick.hour:02d}:{t_pick.minute:02d}"
            else:
                schedule_spec = ""
        with c3:
            send_email = st.checkbox("Envoyer e‑mail", value=str(camp.get("send_email") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_send_email")
            send_sms = st.checkbox("Envoyer SMS", value=str(camp.get("send_sms") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_send_sms")

    with st.expander("Audience (qui reçoit ?)", expanded=False):
        aud_active = [r for r in aud_rows if sheet_row_status_is_live(r.get("status"))]
        aud_latest: dict[str, dict] = {}
        for r in aud_active:
            k = str(r.get("audience_key") or "").strip()
            if not k:
                continue
            prev = aud_latest.get(k)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                aud_latest[k] = r
        aud_keys = sorted(aud_latest.keys())
        default_aud = str(camp.get("audience_kind") or "dry_run").strip() or "dry_run"
        aud_kind = st.selectbox(
            "Critère principal",
            options=aud_keys or ["dry_run"],
            index=(aud_keys.index(default_aud) if default_aud in aud_keys else 0),
            key="adm_sched_aud_kind",
            format_func=lambda k: str((aud_latest.get(k) or {}).get("libelle") or k),
        )
        aud_meta = aud_latest.get(aud_kind) or {}
        desc_txt = str(aud_meta.get("description") or "").strip()
        if desc_txt:
            st.markdown(
                f"""
<div style="border-left:4px solid #0d9488;background:#f0fdfa;color:#0b2745;
padding:10px 12px;border-radius:10px;margin:6px 0 10px 0;">
{desc_txt}
</div>
                """.strip(),
                unsafe_allow_html=True,
            )

        # Valeurs du critère : assistées (pas de saisie libre)
        aud_spec = ""
        if aud_kind in ("by_country", "by_source", "by_email_list"):
            try:
                users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            except Exception:
                users_rows = []
            if aud_kind == "by_country":
                countries = sorted({str(u.get("country") or "").strip() for u in users_rows if str(u.get("country") or "").strip()})
                pick = st.selectbox("Pays", options=countries or ["FR"], index=0, key="adm_sched_country")
                aud_spec = pick
            elif aud_kind == "by_source":
                sources = sorted({str(u.get("source") or "").strip() for u in users_rows if str(u.get("source") or "").strip()})
                picks = st.multiselect("Source(s)", options=sources, default=[], key="adm_sched_sources")
                aud_spec = ",".join(picks)
            elif aud_kind == "by_email_list":
                emails = sorted({str(u.get("email") or "").strip().lower() for u in users_rows if str(u.get("email") or "").strip()})
                picks = st.multiselect("E‑mails (multi‑sélection)", options=emails[:100], default=[], key="adm_sched_emails")
                aud_spec = "\n".join(picks)

        # Affichage explicite du destinataire dry-run (comme la page emailing)
        if aud_kind == "dry_run":
            try:
                users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            except Exception:
                users_rows = []

            def _is_email_ok(email: str) -> bool:
                em = (email or "").strip().lower()
                return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)) if em else False

            dry_users = [
                u
                for u in users_rows
                if str(u.get("source") or "").strip().lower() in ("dry_run", "test_emailing", "test")
                and _is_email_ok(str(u.get("email") or "").strip())
            ]
            dry_users.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            u0 = dry_users[0] if dry_users else {}
            dry_email = str(u0.get("email") or "").strip()
            dry_phone = str(u0.get("phone_e164") or "").strip()
            if not dry_email:
                try:
                    s = st.secrets
                    dry_email = str(s.get("EMAIL_DRY_RUN_TO") or "").strip()
                    dry_phone = str(s.get("SMS_DRY_RUN_TO") or "").strip()
                except Exception:
                    pass
            st.markdown("**Destinataire de test (dry-run)**")
            st.code(f"email: {dry_email or '—'}\nphone_e164: {dry_phone or '—'}")

    with st.expander("Contenu (quoi envoyer ?)", expanded=False):
        st.caption(
            "Une seule case **Audios** couvre **les deux** pistes de l’e-mail hebdo : "
            "synthèse (`{{url_audio}}`) et lectures — AudioLectures (`{{url_audio_readings}}`)."
        )
        k1, k2, k3, k4 = st.columns([1, 1, 1, 1], gap="small")
        with k1:
            content_pdf = st.checkbox("PDF", value=str(camp.get("content_pdf") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_pdf")
        with k2:
            content_audio = st.checkbox(
                "Audios (synthèse + lectures)",
                value=str(camp.get("content_audio") or "true").strip().lower() in ("true", "1", "yes", "oui"),
                key="adm_sched_c_audio",
                help="Contrôle l’intention « inclure les audios » dans la campagne : les deux URLs signées sont fournies au modèle (synthèse et lectures).",
            )
        with k3:
            content_illustration = st.checkbox("Illustration", value=str(camp.get("content_illustration") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_illu")
        with k4:
            content_app = st.checkbox("Lien app", value=str(camp.get("content_app_link") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_app")

        st.markdown("**Templates (formats)**")
        # Templates e-mail: sélection parmi les clés existantes
        try:
            tpl_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
        except Exception:
            tpl_rows = []
        email_keys = sorted({str(r.get("template_key") or "").strip() for r in tpl_rows if str(r.get("channel") or "").strip().lower() == "email" and str(r.get("template_key") or "").strip()})
        sms_keys = sorted({str(r.get("template_key") or "").strip() for r in tpl_rows if str(r.get("channel") or "").strip().lower() == "sms" and str(r.get("template_key") or "").strip()})
        cur_email_key = str(camp.get("email_template_key") or "weekly_friday_lumenvia").strip()
        cur_sms_key = str(camp.get("sms_template_key") or "weekly_friday_lumenvia_sms").strip()
        email_tpl_key = st.selectbox(
            "Template e‑mail",
            options=email_keys or [cur_email_key],
            index=(email_keys.index(cur_email_key) if cur_email_key in email_keys else 0),
            key="adm_sched_tpl_email",
        ).strip()
        sms_tpl_key = st.selectbox(
            "Template SMS",
            options=sms_keys or [cur_sms_key],
            index=(sms_keys.index(cur_sms_key) if cur_sms_key in sms_keys else 0),
            key="adm_sched_tpl_sms",
            help="Si aucun template SMS n’existe encore, on garde la clé actuelle.",
        ).strip()

        if st.button("Enregistrer les réglages", type="primary"):
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="scheduler_campaigns",
                values_by_col={
                    "entity_id": sha256(f"cmpg|{camp_sel}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "campaign_key": camp_sel,
                    "name": str(camp.get("name") or camp_sel),
                    "enabled": "true" if enabled else "false",
                    "timezone": timezone or "Europe/Paris",
                    "schedule_kind": schedule_kind,
                    "schedule_spec": schedule_spec,
                    "audience_kind": aud_kind,
                    "audience_spec": aud_spec,
                    "send_email": "true" if send_email else "false",
                    "send_sms": "true" if send_sms else "false",
                    "email_template_key": email_tpl_key,
                    "sms_template_key": sms_tpl_key,
                    "content_pdf": "true" if content_pdf else "false",
                    "content_audio": "true" if content_audio else "false",
                    "content_illustration": "true" if content_illustration else "false",
                    "content_app_link": "true" if content_app else "false",
                },
            )
            st.success("Réglages enregistrés.")
            st.rerun()

    def _manual_campaign_snapshot_fr(cp: dict) -> str:
        """Une ligne lisible pour l’admin (sans empiler tous les champs en colonne)."""
        if not cp or not str(cp.get("campaign_key") or "").strip():
            return "Aucune version en Sheets pour cet identifiant — crée ou paramètre une campagne d’abord."
        ck = str(cp.get("campaign_key") or "").strip()
        nm = str(cp.get("name") or ck).strip()
        tf = lambda v: str(v or "").strip().lower() in ("true", "1", "oui", "yes")
        en = tf(cp.get("enabled"))
        tz = str(cp.get("timezone") or "Europe/Paris").strip()
        kind = str(cp.get("schedule_kind") or "manual").strip().lower()
        kind_fr = {"manual": "manuel", "weekly": "hebdomadaire", "daily": "quotidien"}.get(kind, kind)
        spec = str(cp.get("schedule_spec") or "").strip()
        aud_k = str(cp.get("audience_kind") or "").strip() or "—"
        raw_aud = str(cp.get("audience_spec") or "").strip().replace("\n", ", ")
        if len(raw_aud) > 140:
            raw_aud = raw_aud[:137].rstrip() + "…"
        row_st = str(cp.get("status") or "").strip()
        chunks = [
            f"nom « {nm} »",
            f"identifiant `{ck}`",
            "en service (`enabled`=oui)" if en else "hors service (`enabled`=non)",
            f"fuseau {tz}",
            f"cadence {kind_fr}",
        ]
        if spec:
            chunks.append(f"créneau `{spec}`")
        chunks.append(f"audience `{aud_k}`")
        if raw_aud:
            chunks.append(f"paramètre audience « {raw_aud} »")
        chunks.append(f"envoi mail {'oui' if tf(cp.get('send_email')) else 'non'}")
        chunks.append(f"envoi SMS {'oui' if tf(cp.get('send_sms')) else 'non'}")
        ek = str(cp.get("email_template_key") or "").strip()
        sk = str(cp.get("sms_template_key") or "").strip()
        if ek:
            chunks.append(f"modèle mail `{ek}`")
        if sk:
            chunks.append(f"modèle SMS `{sk}`")
        media: list[str] = []
        if tf(cp.get("content_pdf")):
            media.append("PDF")
        if tf(cp.get("content_audio")):
            media.append("audios (synthèse + lectures)")
        if tf(cp.get("content_illustration")):
            media.append("illustration")
        if tf(cp.get("content_app_link")):
            media.append("lien app")
        chunks.append("contenus : " + (", ".join(media) if media else "aucun coché en base"))
        if row_st:
            chunks.append(f"statut de cette ligne Sheets `{row_st}` (historique immutable)")
        return ", ".join(chunks)

    with st.expander("Déclencher une campagne (manuel)", expanded=False):
        camp_key = st.selectbox(
            "Campagne",
            options=keys or [default_key],
            index=(keys.index(camp_sel) if camp_sel in keys else 0),
            key="adm_sched_key_sel",
        )
        camp_snap = latest_by_key.get(str(camp_key).strip()) or {}

        mode = st.selectbox(
            "Mode d’envoi",
            options=["test_dry_run", "tous_opt_in"],
            index=0,
            format_func=lambda x: "Test (dry-run)" if x == "test_dry_run" else "Tous les inscrits opt-in",
            key="adm_sched_mode",
        )
        send_to_all = mode == "tous_opt_in"
        if send_to_all:
            st.warning(
                "Mode **Tous les inscrits opt-in** : tu vas cibler l’ensemble des abonnés actifs (selon `subscriptions`). "
                "Vérifie la liste ci-dessous avant d’envoyer.",
                icon="⚠️",
            )

        # Dimanche ciblé : force un dimanche
        today = date.today()
        next_sun = today + timedelta(days=(6 - today.weekday()) % 7)
        sunday = st.date_input("Dimanche ciblé", value=next_sun, key="adm_sched_sunday")
        if sunday.weekday() != 6:
            fixed = sunday + timedelta(days=(6 - sunday.weekday()) % 7)
            st.warning(f"Date ajustée au dimanche suivant : {fixed.isoformat()}")
            sunday = fixed
        date_str = sunday.isoformat()[:10]

        _tf_mail = lambda v: str(v or "").strip().lower() in ("true", "1", "oui", "yes")
        chan_em = _tf_mail(camp_snap.get("send_email"))
        chan_sm = _tf_mail(camp_snap.get("send_sms"))
        users_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
        subs_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=8000)
        rec_preview = lumenvia_manual_broadcast_users(
            users_rows=users_preview,
            subs_rows=subs_preview,
            send_to_all=send_to_all,
            for_email=chan_em,
            for_sms=chan_sm,
        )
        n_em = (
            sum(1 for u in rec_preview if str(u.get("email") or "").strip())
            if chan_em
            else 0
        )
        n_sm = (
            sum(1 for u in rec_preview if str(u.get("phone_e164") or "").strip())
            if chan_sm
            else 0
        )
        n_touch = len(rec_preview)
        mode_lbl = "tous les inscrits opt-in actifs (abonnement hebdo vendredi)" if send_to_all else "dry-run (compte source=dry_run)"
        recap = (
            _manual_campaign_snapshot_fr(camp_snap)
            + f"\n\n**Portée (mode actuel · {mode_lbl}) : {n_touch} destinataire(s) visé(s), "
            f"**{n_em}** envoi(s) e-mail prévu(x) et **{n_sm}** envoi(s) SMS prévu(x) "
            f"**(chaînes « envoi » de la campagne + coordonnée renseignée).**"
        )
        st.info(recap)

        # Sécurité / ajustement liste : uniquement pour l’envoi “tous opt-in”
        filtered_preview = list(rec_preview)
        excluded_emails: set[str] = set()
        confirm_all_ok = True
        confirm_phrase_ok = True
        if send_to_all:
            with st.expander("Aperçu des destinataires (avant envoi)", expanded=True):
                em_list = sorted(
                    {
                        str(u.get("email") or "").strip().lower()
                        for u in rec_preview
                        if str(u.get("email") or "").strip()
                    }
                )
                st.caption(f"E-mails détectés : **{len(em_list)}**")

                # Exclusions : multi-select + copier/coller
                excl_pick = st.multiselect(
                    "Exclure des e-mails (optionnel)",
                    options=em_list,
                    default=[],
                    key="adm_sched_excl_pick",
                )
                excl_paste = st.text_area(
                    "Ou coller une liste d’e-mails à exclure (un par ligne, optionnel)",
                    value="",
                    height=80,
                    key="adm_sched_excl_paste",
                )
                excluded_emails = {str(e or "").strip().lower() for e in (excl_pick or []) if str(e or "").strip()}
                for ln in (excl_paste or "").splitlines():
                    s = str(ln or "").strip().lower()
                    if s and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
                        excluded_emails.add(s)

                if excluded_emails:
                    filtered_preview = [
                        u
                        for u in filtered_preview
                        if str(u.get("email") or "").strip().lower() not in excluded_emails
                    ]

                # Mini table (limite) : qui va recevoir
                show_n = st.slider(
                    "Afficher les N premiers destinataires (aperçu)",
                    min_value=10,
                    max_value=300,
                    value=60,
                    step=10,
                    key="adm_sched_preview_n",
                )
                lines = []
                for u in filtered_preview[: int(show_n)]:
                    em0 = str(u.get("email") or "").strip().lower()
                    fn0 = str(u.get("first_name") or "").strip()
                    ln0 = str(u.get("last_name") or "").strip()
                    lines.append(f"{em0}\t{fn0}\t{ln0}")
                st.code(("\n".join(lines) if lines else "—")[:9000])

                # Confirmations
                final_em = sum(1 for u in filtered_preview if str(u.get("email") or "").strip()) if chan_em else 0
                final_sm = sum(1 for u in filtered_preview if str(u.get("phone_e164") or "").strip()) if chan_sm else 0
                final_touch = len(filtered_preview)
                st.markdown(
                    f"**Après exclusions :** {final_touch} destinataire(s), "
                    f"**{final_em}** e-mail(s) et **{final_sm}** SMS."
                )

                confirm_all_ok = st.checkbox(
                    "J’ai vérifié la liste et je confirme vouloir envoyer à ces destinataires.",
                    value=False,
                    key="adm_sched_confirm_checked",
                )
                phrase = st.text_input(
                    "Pour activer l’envoi, tape exactement : ENVOYER",
                    value="",
                    key="adm_sched_confirm_phrase",
                ).strip()
                confirm_phrase_ok = phrase == "ENVOYER"

        can_execute = (confirm_all_ok and confirm_phrase_ok) if send_to_all else True

        if st.button("Exécuter maintenant", type="primary", disabled=not can_execute):
            # Exécution : réutilise la logique d’envoi (hebdo opt-in)
            started = utc_now_iso()
            ok0 = 0
            err0 = 0
            run_id = sha256(f"run|{camp_key}|{date_str}|{started}".encode("utf-8")).hexdigest()[:24]
            ov = loading_overlay("Envoi en cours…")
            try:
                # Récupère campagne (dernière)
                camp_rows = [r for r in rows if str(r.get("campaign_key") or "").strip() == camp_key]
                camp_rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
                camp = camp_rows[0] if camp_rows else {}
                do_email = str(camp.get("send_email") or "true").strip().lower() in ("true", "1", "oui", "yes")
                do_sms = str(camp.get("send_sms") or "true").strip().lower() in ("true", "1", "oui", "yes")
                email_tpl_key = str(camp.get("email_template_key") or "weekly_friday_lumenvia").strip()
    
                users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
                subs_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=8000)
                recipients = lumenvia_manual_broadcast_users(
                    users_rows=users_rows,
                    subs_rows=subs_rows,
                    send_to_all=send_to_all,
                    for_email=do_email,
                    for_sms=do_sms,
                )
                # Applique les exclusions choisies dans l’aperçu (sécurité)
                if send_to_all and excluded_emails:
                    recipients = [u for u in recipients if str(u.get("email") or "").strip().lower() not in excluded_emails]
    
                # template actif — même filtres vivants/langue FR que la page Emailing
                tpl_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
    
                tpl0 = pick_latest_live_email_template(
                    tpl_rows,
                    template_key=email_tpl_key,
                    channel="email",
                    language_in=("fr", "fr-fr", "france", ""),
                )
                tpl = tpl0 if tpl0 is not None else {}
                subj = str(tpl.get("subject") or "").strip()
                body = str(tpl.get("body") or "").strip()
    
                # Liens (signés) — PDF, audios, illustration
                origin = _lumenvia_app_origin_url() or ""
                url_app = (origin.rstrip("/") + "/?route=about") if origin else ""
                _sched_urls = weekly_email_signed_urls(cfg=cfg, gs=gs, date_str=date_str, zone="france")
                url_pdf = _sched_urls["url_pdf"]
                url_audio = _sched_urls["url_audio"]
                url_audio_readings = _sched_urls["url_audio_readings"]
                url_illu = _sched_urls["url_illustration"]
    
                # SMTP/Twilio config réutilise _secret_get de la page Emailing (simple)
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
    
                from core.outbound import SmtpConfig, TwilioConfig, send_smtp_email, send_twilio_sms
                smtp_cfg = SmtpConfig(
                    host=_secret_get("SMTP_HOST"),
                    port=int(_secret_get("SMTP_PORT") or 587),
                    username=_secret_get("SMTP_USER"),
                    password=_secret_get("SMTP_PASSWORD"),
                    from_email=_secret_get("SMTP_FROM"),
                    use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower() not in ("0", "false", "no", "off"),
                )
                tw_cfg = TwilioConfig(
                    account_sid=_secret_get("TWILIO_ACCOUNT_SID"),
                    auth_token=_secret_get("TWILIO_AUTH_TOKEN"),
                    from_phone_e164=_secret_get("TWILIO_FROM", "TWILIO_FROM_NUMBER"),
                )
    
                from core.emailing import (
                    EmailTemplate,
                    render_weekly_email_template,
                    french_day_month_year,
                    resolve_email_nom_du_dimanche,
                )

                import app as ap

                try:
                    ident_sched, _ = ap.cached_aelf(date_str, zone="france", _identity_schema=4)
                except Exception:
                    ident_sched = None

                vals_base = {
                    "origin": origin,
                    "date_dimanche": french_day_month_year(sunday),
                    "nom_du_dimanche": resolve_email_nom_du_dimanche(
                        identity=ident_sched,
                        date_str=date_str,
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                    ),
                    "url_pdf": url_pdf,
                    "url_audio": url_audio,
                    "url_audio_readings": url_audio_readings,
                    "url_illustration": url_illu,
                    "illustration_description": (_sched_urls.get("illustration_description") or "").strip(),
                    "url_app": url_app,
                }
    
                for urec in recipients[:2000]:
                    uid0 = str(urec.get("entity_id") or "").strip() or "recipient"
                    em = str(urec.get("email") or "").strip()
                    ph = str(urec.get("phone_e164") or "").strip()
                    vals = dict(vals_base)
                    vals["prenom"] = str(urec.get("first_name") or "—").strip() or "—"
                    vals["nom"] = str(urec.get("last_name") or "—").strip() or "—"
                    vals["email"] = em
                    rendered = render_weekly_email_template(
                        EmailTemplate(subject=subj, body=body), values={k: str(v) for k, v in vals.items()}
                    )
    
                    # Envoi email (HTML gabarit)
                    if do_email and em and smtp_cfg.host and smtp_cfg.from_email:
                        try:
                            html2 = ""  # le gabarit est généré dans render_admin_emailing; ici simple fallback texte
                            bt = rendered.body.strip()
                            bt = (bt + "\n\n—\n") if bt else ""
                            bt += LUMENVIA_DEVELOPMENT_NOTICE
                            send_smtp_email(cfg=smtp_cfg, to_email=em, subject=rendered.subject, body_text=bt, body_html=html2 or None)
                            ok0 += 1
                        except Exception:
                            err0 += 1
    
                    # Envoi SMS (minimaliste)
                    if do_sms and ph and tw_cfg.account_sid and tw_cfg.from_phone_e164:
                        try:
                            send_twilio_sms(cfg=tw_cfg, to_phone_e164=ph, body_text="Message de JOPAI LumenVia")
                            ok0 += 1
                        except Exception:
                            err0 += 1
    
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="scheduler_runs",
                    values_by_col={
                        "entity_id": run_id,
                        "campaign_key": camp_key,
                        "run_kind": "manual",
                        "status_detail": "done",
                        "started_at": started,
                        "finished_at": utc_now_iso(),
                        "recipients_ok": str(ok0),
                        "recipients_err": str(err0),
                        "error": "",
                    },
                )
                if ok0 == 0 and err0 == 0:
                    st.info("Aucun envoi effectué.")
                elif err0 == 0 and ok0 > 0:
                    st.success(f"Terminé : {ok0} envoi(s) OK.")
                elif ok0 == 0 and err0 > 0:
                    st.error(f"Échec : {err0} erreur(s), aucun envoi réussi.")
                else:
                    st.warning(f"Terminé partiellement : {ok0} OK, {err0} erreur(s).")
            finally:
                ov.empty()


