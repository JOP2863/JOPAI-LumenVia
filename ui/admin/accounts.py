"""Admin — Comptes et abonnés (Sheets `users` / `subscriptions`, MARPA append-only)."""

from __future__ import annotations

import re
from hashlib import sha256
from html import escape as html_escape

import streamlit as st

from core.config import load_config
from core.sheets_db import (
    append_immutable_row,
    append_immutable_rows_bulk,
    build_gspread_client,
    fetch_records,
    utc_now_iso,
)
from core.subscriptions_util import subscription_is_active
from ui.admin_secrets import admin_login_and_password
from ui.components import loading_overlay


def render_admin_accounts() -> None:
    st.title("Comptes inscrits")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=6000)
    except Exception as e:
        st.error(f"Lecture `users` impossible : {e}")
        return
    try:
        subs = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=6000)
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

        # Formulaire en lot : 5 lignes
        with st.form("adm_add_subscribers_5"):
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

            country = st.selectbox("Pays", options=["FR"], index=0, key=f"adm_addsub_country_{nonce}")
            length_pref = st.selectbox(
                "Préférence de longueur",
                options=["150", "250", "400"],
                index=1,
                key=f"adm_addsub_lenpref_{nonce}",
            )
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
                                to_add_users.append(
                                    {
                                        "entity_id": uid0,
                                        "email": em_lc,
                                        "first_name": r["first_name"],
                                        "last_name": r["last_name"],
                                        "phone_e164": r.get("phone_e164") or "",
                                        "country": str(country or "").strip(),
                                        # Aligné avec “Nous rejoindre”
                                        "source": "newsletter",
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

    # Filtre simple (côté UI) : sous-chaîne e-mail
    q = st.text_input("Filtrer (e-mail contient)", value="", key="adm_accounts_filter").strip().lower()

    # Admin canonical : login secret (si présent)
    try:
        adm_login, _adm_pwd = admin_login_and_password()
    except Exception:
        adm_login = ""

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
            opt_txt = "—"
            if uid and title.lower().startswith("nous rejoindre"):
                rec = latest_sub.get(uid)
                # Colonne dédiée si présente, sinon fallback sur `active`
                if rec and str(rec.get("opt_in") or "").strip():
                    opt_txt = "Oui" if str(rec.get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes") else "Non"
                else:
                    opt_txt = "Oui" if subscription_is_active(rec) else "Non"
            body_rows.append(
                "<tr>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);'>{html_escape(em)}</td>"
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
      <th style="text-align:left;padding:9px 10px;">Source</th>
      <th style="text-align:left;padding:9px 10px;">Opt-in</th>
      <th style="text-align:left;padding:9px 10px;">Créé le</th>
    </tr>
  </thead>
  <tbody>
    {''.join(body_rows) if body_rows else '<tr><td colspan="4" style="padding:10px;opacity:0.75;">Aucun.</td></tr>'}
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


