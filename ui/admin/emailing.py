"""Admin — E-mailing (templates, envois manuels, journaux)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.config import load_config
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
from core.weekly_email_urls import weekly_email_signed_urls
from ui.admin.emailing_manual_broadcast import render_emailing_manual_broadcast
from ui.components import loading_overlay
from ui.navigation import lumenvia_app_origin_url as _lumenvia_app_origin_url


def render_admin_emailing() -> None:
    import app as ap  # lazy: évite import circulaire avec app.py

    st.title("Emailing — templates & automatisation")
    st.caption("Édite le contenu de l’e-mail hebdomadaire (vendredi soir).")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        return

    try:
        gs = build_gspread_client(cfg.gcp_service_account)
        sa_email = str(cfg.gcp_service_account.get("client_email") or "").strip()
    except ValueError as ex:
        st.error(str(ex))
        return

    try:
        ensure_table(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table=TableSpec(
                name="email_templates",
                columns=with_concat(
                    [
                        *BASE_COLUMNS,
                        "template_key",
                        "channel",
                        "language",
                        "subject",
                        "body",
                        "active",  # colonne facultative sur la feuille ; pas utilisée pour filtrer le template (seul `status` compte).
                        "status_note",
                    ]
                ),
            ),
        )
    except Exception as ex:
        from core.sheets_db import describe_gspread_api_error

        msg = str(ex)
        if not msg or "HTTP" not in msg:
            msg = describe_gspread_api_error(
                ex,
                spreadsheet_id=cfg.gsheet_id,
                service_account_email=sa_email or None,
            )
        st.error(msg)
        if sa_email:
            st.caption(f"Compte de service à ajouter comme **Éditeur** sur le Google Sheet : `{sa_email}`")
        return

    from core.sheets_db import _resolve_table_name, open_spreadsheet

    try:
        sh_etpl_hint = open_spreadsheet(gs, cfg.gsheet_id, service_account_email=sa_email or None)
        etpl_tab = _resolve_table_name(sh=sh_etpl_hint, table="email_templates")
    except Exception:
        etpl_tab = "ETPL"

    template_key = "weekly_friday_lumenvia"
    st.caption(
        f"**Onglet Sheets :** `{etpl_tab}` (alias logique `email_templates` → acronyme via **AliasTables**). "
        "**Templates e-mail :** seule la colonne **`status`** (**Actif** / **Inactif**) détermine quelle ligne est la version "
        "courante (aperçu, enregistrement, envoi manuel, **et** choix du modèle côté campagne / scheduler). "
        "La colonne **`active`** sur cette table n’est **pas** utilisée par l’app pour ce choix (elle peut rester pour du "
        "pilotage manuel ou un usage futur lié au planning, mais si **`status`** est **Inactif**, la ligne est ignorée "
        "**sans** lire **`active`**)."
    )
    with st.expander("Paramètres du template (clé/canal/langue)", expanded=False):
        st.caption(f"Template : `{template_key}` (canal: email, langue: fr)")

    from core.emailing import (
        EmailTemplate,
        render_template,
        supported_tags,
        french_day_month_year,
        pick_latest_live_email_template,
        email_template_row_is_live,
        resolve_email_nom_du_dimanche,
    )

    try:
        rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
    except Exception:
        rows = []

    lang_fr = ("fr", "fr-fr", "france", "")
    current = pick_latest_live_email_template(rows, template_key=template_key, channel="email", language_in=lang_fr) or {}

    if current:
        _ver = str(current.get("version") or "?").strip()
        _subj = str(current.get("subject") or "").strip()
        st.success(
            f"Connexion Sheets OK — onglet **`{etpl_tab}`**, template **Actif** v{_ver} "
            f"(`{template_key}`). Objet : {_subj[:72]}{'…' if len(_subj) > 72 else ''}"
        )
        st.caption(
            "La colonne **`concat`** est un historique figé à l’enregistrement : seules **`subject`** et **`body`** "
            "font foi pour l’aperçu et l’envoi."
        )
    elif rows:
        st.warning(
            f"Connexion Sheets OK — onglet **`{etpl_tab}`** ({len(rows)} ligne(s)), "
            f"mais aucune ligne **`status` = Actif** pour `{template_key}` (e-mail, FR)."
        )
    else:
        st.warning(
            f"Onglet **`{etpl_tab}`** accessible mais vide — enregistrez un premier template ci-dessous."
        )

    default_subject = "🕯️ Votre pause LumenVia : Préparez la célébration du dimanche {{date_dimanche}}"
    default_body = ""
    try:
        p = Path("data/emailing_template_raw.txt")
        if p.is_file():
            default_body = p.read_text(encoding="utf-8").strip()
    except Exception:
        default_body = ""

    with st.expander("Paramètres du template (clé/canal/langue)", expanded=False):
        subject = st.text_input(
            "Objet",
            value=str(current.get("subject") or default_subject).strip(),
            key="adm_email_tpl_subject",
        )
        body = st.text_area(
            "Corps (texte, avec balises {{...}})",
            value=str(current.get("body") or default_body).strip(),
            height=320,
            key="adm_email_tpl_body",
        )
        note = st.text_input("Note (optionnel)", value="", key="adm_email_tpl_note")

    with st.expander("Balises supportées", expanded=False):
        st.code("\n".join([f"{{{{{t}}}}}" for t in supported_tags()]), language="text")

    st.divider()
    with st.expander("Dimanche de référence (aperçu + envoi manuel)", expanded=False):
        # Dimanche cible (par défaut : prochain dimanche)
        try:
            today = date.today()
            next_sun = today + timedelta(days=(6 - today.weekday()) % 7)
        except Exception:
            next_sun = date.today()
        d_pick = st.date_input("Dimanche ciblé", value=next_sun, key="adm_email_sunday_pick")
        date_str = d_pick.isoformat()[:10]

        # Identité AELF
        try:
            ident0, _texts0 = ap.cached_aelf(date_str, zone="france", _identity_schema=4)
        except Exception:
            ident0 = None

        # Liens signés (si objets présents)
        origin = _lumenvia_app_origin_url() or ""
        url_app = (origin.rstrip("/") + "/?sunday=" + date_str) if origin else ""
        _urls = weekly_email_signed_urls(cfg=cfg, gs=gs, date_str=date_str, zone="france")
        url_pdf = _urls["url_pdf"]
        url_audio = _urls["url_audio"]
        url_audio_readings = _urls["url_audio_readings"]
        url_illu = _urls["url_illustration"]

        # Valeurs exemple (issues d'un dimanche réel)
        values = {
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
            "url_pdf": url_pdf,
            "url_audio": url_audio,
            "url_audio_readings": url_audio_readings,
            "url_illustration": url_illu,
            "illustration_description": (_urls.get("illustration_description") or "").strip(),
            "url_app": url_app,
            "optout_url": (origin.rstrip("/") + "/?route=join") if origin else "",
        }
        rendered = render_template(EmailTemplate(subject=subject, body=body), values=values)
        st.markdown(f"**Objet :** {rendered.subject}")
        st.code((rendered.body or "")[:4000] or "—")

    st.caption(
        "Astuce : pour rendre les CTA cliquables, mets directement des URLs dans le corps, par ex. "
        "`{{url_pdf}}`, `{{url_audio}}`, `{{url_audio_readings}}`, `{{url_illustration}}`, `{{url_app}}`. "
        "Pour l’illustration dominicale : gardez `{{affichage de l’illustration de la semaine}}` et "
        "`{{illustration_description}}` **en fin de template** (marqueurs) — le gabarit HTML les place "
        "automatiquement **sous le bouton « Donner mon avis »**, avant le bandeau légal."
    )

    st.divider()
    if st.button("Enregistrer le template", type="primary", disabled=not (subject.strip() and body.strip())):
        ov = loading_overlay("Enregistrement du template emailing…")
        try:
            # Inactivation (historique) de la version précédente active (si elle existe)
            try:
                rows2 = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
            except Exception:
                rows2 = []
            prev = pick_latest_live_email_template(rows2, template_key=template_key, channel="email", language_in=lang_fr)
            body_n = body.strip()
            subj_n = subject.strip()

            # Immuabilité : si le contenu n’a pas bougé, on n’écrit pas une nouvelle version.
            unchanged = prev and str(prev.get("subject") or "").strip() == subj_n and str(prev.get("body") or "").strip() == body_n
            if unchanged:
                st.info("Aucune modification détectée (objet + corps inchangés) — pas de nouvelle ligne.")
            else:
                # 1) Mettre les lignes actuellement **Actives** (même clé / canal / langue) en **Inactif** dans la feuille
                # (comme MARPA pour Paramètres_IA : append seul laisse l’historique encore « Actif »).
                from core.sheets_db import _resolve_table_name, open_spreadsheet

                sh_etpl = open_spreadsheet(
                    gs, cfg.gsheet_id, service_account_email=sa_email or None
                )
                ws_etpl = sh_etpl.worksheet(_resolve_table_name(sh=sh_etpl, table="email_templates"))
                header_etpl = ws_etpl.row_values(1)
                if not header_etpl:
                    raise RuntimeError("Onglet templates e-mail sans en-tête — relance init_sheets_db ou vérifie l’alias ETPL.")

                try:
                    rec_etpl = ws_etpl.get_all_records(numericise_ignore=["all"])
                except Exception:
                    rec_etpl = []

                def _tpl_row_lang_ok(lang_raw: object) -> bool:
                    lg = str(lang_raw or "").strip().lower()
                    return lg in lang_fr

                col_status_etpl = header_etpl.index("status") + 1 if "status" in header_etpl else 0
                col_concat_etpl = header_etpl.index("concat") + 1 if "concat" in header_etpl else 0
                if not col_status_etpl:
                    raise RuntimeError("Colonne `status` absente sur l’onglet templates e-mail.")

                for ix, rr in enumerate(rec_etpl):
                    if str(rr.get("template_key") or "").strip() != template_key:
                        continue
                    if str(rr.get("channel") or "").strip().lower() != "email":
                        continue
                    if not _tpl_row_lang_ok(rr.get("language")):
                        continue
                    if not email_template_row_is_live(rr):
                        continue

                    merged = dict(rr)
                    merged["status"] = SHEETS_ROW_STATUS_INACTIVE
                    row_num = ix + 2
                    ws_etpl.update_cell(row_num, col_status_etpl, SHEETS_ROW_STATUS_INACTIVE)
                    if col_concat_etpl:
                        ws_etpl.update_cell(row_num, col_concat_etpl, compute_concat(merged, header=header_etpl))

                # 2) Version suivante à partir de l’historique (toutes lignes série, pas seulement actives)
                max_tpl_ver = 0
                for r0 in rows2:
                    if str(r0.get("template_key") or "").strip() != template_key:
                        continue
                    if str(r0.get("channel") or "").strip().lower() != "email":
                        continue
                    if not _tpl_row_lang_ok(r0.get("language")):
                        continue
                    vtxt = str(r0.get("version") or "").strip()
                    if vtxt.isdigit():
                        max_tpl_ver = max(max_tpl_ver, int(vtxt))
                next_tpl_ver = int(max_tpl_ver + 1)

                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="email_templates",
                    values_by_col={
                        "entity_id": sha256(f"tpl|{template_key}|{next_tpl_ver}|{subj_n}|{body_n}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                        "template_key": template_key,
                        "channel": "email",
                        "language": "fr",
                        "subject": subj_n,
                        "body": body_n,
                        "version": next_tpl_ver,
                        "status": SHEETS_ROW_STATUS_ACTIVE,
                        "status_note": note.strip(),
                    },
                    version=next_tpl_ver,
                )
                st.success("Template enregistré.")
                st.rerun()
        finally:
            ov.empty()

    render_emailing_manual_broadcast(
        gs=gs,
        cfg=cfg,
        template_key=template_key,
        lang_fr=lang_fr,
        subject=subject,
        body=body,
    )