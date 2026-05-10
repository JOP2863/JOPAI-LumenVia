"""Page questionnaire « Donner votre avis » + helpers SMTP notification."""

from __future__ import annotations

import random
import re
import threading
from hashlib import sha256

import streamlit as st

from core.config import load_config
from core.outbound import SmtpConfig
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_row,
    build_gspread_client,
    ensure_table,
    utc_now_iso,
    with_concat,
)
from ui.components import loading_overlay


def _lv_read_streamlit_secret(*keys: str) -> str:
    """Lit une clé dans ``st.secrets`` (racine ou sections ``smtp`` / ``twilio`` / ``dry_run``)."""
    try:
        s = st.secrets
    except Exception:
        return ""
    for k in keys:
        try:
            v = s.get(k)  # type: ignore[attr-defined]
        except Exception:
            v = None
        if v is not None and str(v).strip():
            return str(v).strip()
    for sec in ("smtp", "twilio", "dry_run"):
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


def _lv_smtp_config_from_secrets_optional() -> SmtpConfig | None:
    """Retourne une config SMTP si ``SMTP_HOST`` est défini, sinon ``None``."""
    try:
        host = _lv_read_streamlit_secret("SMTP_HOST")
        if not host:
            return None
        fe = _lv_read_streamlit_secret("SMTP_FROM")
        if not fe:
            return None
        return SmtpConfig(
            host=host,
            port=int(_lv_read_streamlit_secret("SMTP_PORT") or 587),
            username=_lv_read_streamlit_secret("SMTP_USER"),
            password=_lv_read_streamlit_secret("SMTP_PASSWORD"),
            from_email=fe,
            use_tls=str(_lv_read_streamlit_secret("SMTP_USE_TLS") or "true").strip().lower()
            not in ("0", "false", "no", "off"),
        )
    except Exception:
        return None


def _schedule_feedback_survey_notify_email(
    *,
    smtp_cfg: SmtpConfig,
    to_email: str,
    lines: list[str],
) -> None:
    """Envoi SMTP en arrière-plan ; aucune erreur ne remonte à l’interface utilisateur."""
    subject = "[LumenVia] Nouveau retour — questionnaire"
    body = (
        "Réponse enregistrée sur le formulaire « Donner votre avis ».\n\n"
        + "\n".join(lines)
    )

    def _run() -> None:
        try:
            from core.outbound import send_smtp_email

            send_smtp_email(
                cfg=smtp_cfg,
                to_email=to_email,
                subject=subject,
                body_text=body,
                body_html=None,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="lumenvia_feedback_notify").start()


def render_feedback() -> None:
    """Questionnaire flash post-envoi (Sheets RSTN `experience_feedback`)."""
    st.title("Donner votre avis")
    st.markdown(
        """
**« LumenVia est un chemin que nous construisons ensemble.**  
En tant que premier passager de cette aventure, votre retour nous est précieux pour ajuster nos pas. Pourriez-vous nous accorder environ **une minute** ? »
        """.strip()
    )

    try:
        q_campaign = str(st.query_params.get("campaign") or "").strip()
    except Exception:
        q_campaign = ""
    try:
        q_dim = str(st.query_params.get("date_dimanche") or "").strip()
    except Exception:
        q_dim = ""
    try:
        qp_email_fb = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        qp_email_fb = ""

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Enregistrement indisponible : configuration Google Sheets manquante.")
        return

    from core.sheets_db import TableSpec, ensure_table

    gs = build_gspread_client(cfg.gcp_service_account)
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="experience_feedback",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "submitter_email",
                    "emotion_global",
                    "rating_illustration",
                    "rating_synthesis",
                    "rating_audio",
                    "utility_liturgy",
                    "touch_memorable",
                    "wish_improve_one",
                    "campaign_hint",
                    "date_dimanche_hint",
                    "source_route",
                ]
            ),
        ),
    )

    auth_em = str(st.session_state.get("auth_email_lc") or "").strip().lower()

    def _em_ok_feedback(e: str) -> bool:
        em = (e or "").strip().lower()
        return bool(em and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em))

    logged_in_fb = bool(str(st.session_state.get("auth_user_entity_id") or "").strip())
    allow_feedback = logged_in_fb or _em_ok_feedback(qp_email_fb)
    if not allow_feedback:
        st.warning(
            "Pour répondre au questionnaire, connecte-toi (**Mon compte**) ou ouvre le lien reçu dans ton e-mail "
            "LumenVia — il préremplit ton adresse et permet de participer sans compte.",
            icon="🔒",
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Aller à Mon compte", type="primary", key="fb_need_login_account"):
                st.session_state.route = "account"
                st.rerun()
        with b2:
            if st.button("S'inscrire à la newsletter", type="secondary", key="fb_need_login_join"):
                st.session_state.route = "join"
                st.rerun()
        return

    # Préremplissage : lien e-mail (?email=) prioritaire ; sinon session connectée.
    if _em_ok_feedback(qp_email_fb):
        st.session_state.fb_email_in = qp_email_fb
    else:
        cur = str(st.session_state.get("fb_email_in") or "").strip()
        if not _em_ok_feedback(cur) and _em_ok_feedback(auth_em):
            st.session_state.fb_email_in = auth_em
        elif not st.session_state.get("fb_email_in"):
            st.session_state.fb_email_in = ""

    st.subheader("Vos premiers pas avec LumenVia")
    st.caption(
        "Les trois évaluations suivantes utilisent une **échelle de 1 à 5** "
        "(**1** = note la plus basse · **5** = note la plus haute)."
    )

    with st.form("experience_feedback_form", clear_on_submit=True):
        em_in = st.text_input(
            "E-mail (optionnel)",
            placeholder="toi@domaine.fr",
            help="Souvent prérempli depuis le lien reçu par e-mail ou depuis ta connexion au site ; tu peux modifier ou effacer.",
            key="fb_email_in",
        )
        emotion = st.radio(
            "Comment décririez-vous votre état d'esprit après avoir consulté cette synthèse ?",
            options=("Apaisé", "Éclairé", "Curieux", "Indifférent"),
            horizontal=True,
            key="fb_emotion",
        )
        st.caption("Échelle 1–5 pour les trois critères ci-dessous (1 = plus faible · 5 = meilleure note).")
        c1, c2, c3 = st.columns(3)
        with c1:
            r_illus = int(
                st.slider(
                    "L'illustration",
                    1,
                    5,
                    4,
                    key="fb_r_illus",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )
        with c2:
            r_synth = int(
                st.slider(
                    "Le pdf de synthèse",
                    1,
                    5,
                    4,
                    key="fb_r_synth",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )
        with c3:
            r_audio = int(
                st.slider(
                    "L'audio",
                    1,
                    5,
                    4,
                    key="fb_r_audio",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )

        utility = st.select_slider(
            "Ce contenu vous aide-t-il réellement à vous préparer pour la célébration de dimanche ?",
            options=("Pas vraiment", "Un peu", "Oui, beaucoup"),
            value="Un peu",
            key="fb_utility",
        )
        standout = st.text_area(
            "Qu'est-ce qui vous a le plus touché ou semblé le plus utile dans cet envoi ?",
            max_chars=4000,
            height=180,
            key="fb_standout",
        )
        wish = st.text_area(
            "Une seule chose à améliorer ou à ajouter (musique d'ambiance, texte plus court, …) ?",
            max_chars=4000,
            height=180,
            key="fb_wish",
        )
        submitted = st.form_submit_button("Envoyer mon avis", use_container_width=True)

    if submitted:
        em_clean = str(em_in or "").strip().lower()
        overlay = loading_overlay("Enregistrement de votre retour…")
        try:
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="experience_feedback",
                values_by_col={
                    "entity_id": sha256(f"fb|{utc_now_iso()}|{random.random()}".encode("utf-8")).hexdigest()[:28],
                    "submitter_email": em_clean,
                    "emotion_global": str(emotion),
                    "rating_illustration": str(r_illus),
                    "rating_synthesis": str(r_synth),
                    "rating_audio": str(r_audio),
                    "utility_liturgy": str(utility),
                    "touch_memorable": (standout or "").strip(),
                    "wish_improve_one": (wish or "").strip(),
                    "campaign_hint": q_campaign or "",
                    "date_dimanche_hint": q_dim[:10] if len(q_dim) >= 10 else q_dim,
                    "source_route": "feedback",
                },
            )
            st.success("Merci infiniment : ton avis nous aide à faire grandir LumenVia.")
            try:
                notify_to = str(st.secrets.get("EMAIL_TEST_RECIPIENT_1") or "").strip().lower()
            except Exception:
                notify_to = ""
            if _em_ok_feedback(notify_to):
                smtp_n = _lv_smtp_config_from_secrets_optional()
                if smtp_n:
                    lines_fb = [
                        f"E-mail (champ formulaire) : {em_clean or '—'}",
                        f"État d'esprit : {emotion}",
                        f"Illustration (1–5) : {r_illus}",
                        f"Synthèse PDF (1–5) : {r_synth}",
                        f"Audio (1–5) : {r_audio}",
                        f"Préparation dimanche : {utility}",
                        f"Ce qui a marqué : {(standout or '').strip() or '—'}",
                        f"À améliorer : {(wish or '').strip() or '—'}",
                        f"Campagne (hint) : {q_campaign or '—'}",
                        f"Date dimanche (hint) : "
                        f"{q_dim[:10] if len(q_dim) >= 10 else (q_dim or '—')}",
                    ]
                    _schedule_feedback_survey_notify_email(
                        smtp_cfg=smtp_n,
                        to_email=notify_to,
                        lines=lines_fb,
                    )
        except Exception as ex:
            st.exception(ex)
        finally:
            overlay.empty()

