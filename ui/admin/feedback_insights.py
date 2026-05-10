"""Admin — Synthèse IA des retours questionnaire (`feedback_insights`)."""

from __future__ import annotations

import json
from hashlib import sha256

import streamlit as st

from core.config import load_config
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_row,
    build_gspread_client,
    ensure_table,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.vertex_gemini import VertexGeminiClient
from ui.components import loading_overlay


def render_admin_feedback_insights() -> None:
    """Synthèse IA des réponses au questionnaire (`experience_feedback`)."""
    import app as ap  # lazy: évite import circulaire avec app.py

    st.title("Synthèse des retours (questionnaire)")
    st.caption(
        "À partir des lignes **Actif** de la table `experience_feedback`, génère une synthèse et des pistes d’action "
        "via **Gemini (Vertex)** — sans afficher d’e-mails dans le prompt agrégé."
    )
    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configuration `gcp_service_account` / `gsheet_id` manquante.")
        return
    gs = build_gspread_client(cfg.gcp_service_account)
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="feedback_insights",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "n_sample",
                    "bundle_sha256",
                    "model_used",
                    "synthesis_text",
                ]
            ),
        ),
    )
    sid = str(cfg.gsheet_id).strip()
    sa_json = json.dumps(cfg.gcp_service_account, sort_keys=True)
    try:
        rows = ap._adm_feedback_sheet_fetch_cached(sid, "experience_feedback", 1200, sa_json)
    except Exception as e:
        st.error(f"Lecture `experience_feedback` impossible : {e}")
        return
    live_fb = [r for r in rows if sheet_row_status_is_live(r.get("status"))]
    live_fb.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    st.metric("Réponses (lignes actives)", len(live_fb))
    if not live_fb:
        st.info("Aucun retour à analyser pour l’instant.")
        return

    # Export Excel des réponses brutes
    try:
        from openpyxl import Workbook
        import io as _io

        _cols = [
            "created_at",
            "status",
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
            "row_id",
            "entity_id",
        ]
        wb = Workbook()
        ws = wb.active
        ws.title = "retours"
        ws.append(_cols)
        for r in live_fb:
            ws.append([str(r.get(c) or "") for c in _cols])
        buf = _io.BytesIO()
        wb.save(buf)
        st.download_button(
            label="Télécharger les réponses brutes (.xlsx)",
            data=buf.getvalue(),
            file_name="lumenvia_retours_questionnaire.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="adm_fb_dl_xlsx",
        )
    except Exception as ex:
        st.caption(f"Export Excel indisponible ({ex}).")

    ln_fb = len(live_fb)
    cap_n = min(200, ln_fb)
    if cap_n <= 10:
        n_sample = cap_n
        st.caption(f"Toutes les **{n_sample}** réponses seront incluses.")
    else:
        n_sample = int(
            st.slider(
                "Nombre de réponses récentes à inclure dans l’analyse",
                min_value=10,
                max_value=cap_n,
                value=min(80, cap_n),
                step=5,
                key="adm_fb_insights_n",
            )
        )
    sample = live_fb[:n_sample]
    lines: list[str] = []
    for i, r in enumerate(sample):
        lines.append(
            f"- {i + 1} | date={str(r.get('created_at') or '')[:16]} | "
            f"humeur={r.get('emotion_global', '')} | illus={r.get('rating_illustration', '')} | "
            f"pdf={r.get('rating_synthesis', '')} | audio={r.get('rating_audio', '')} | utile={r.get('utility_liturgy', '')} | "
            f"souvenir={str(r.get('touch_memorable') or '')[:140]} | idée={str(r.get('wish_improve_one') or '')[:140]}"
        )
    bundle = "\n".join(lines)
    bundle_sig = sha256(f"{n_sample}\n{bundle}".encode("utf-8")).hexdigest()

    try:
        ins_rows_all = ap._adm_feedback_sheet_fetch_cached(sid, "feedback_insights", 800, sa_json)
    except Exception:
        ins_rows_all = []

    def _latest_saved_insight(ins_rows: list[dict], sig: str) -> dict | None:
        cand = [
            r
            for r in ins_rows
            if sheet_row_status_is_live(r.get("status"))
            and str(r.get("bundle_sha256") or "").strip() == sig
        ]
        if not cand:
            return None
        cand.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return cand[0]

    prev_ins = _latest_saved_insight(ins_rows_all, bundle_sig)
    if prev_ins:
        st.warning(
            "Une synthèse **identique** (même agrégat et même nombre de réponses) est déjà enregistrée. "
            "Tu peux la relire ci‑dessous sans refaire d’appel IA — coche **Forcer** uniquement si tu veux une nouvelle analyse.",
            icon="📎",
        )
        with st.expander("Synthèse déjà enregistrée", expanded=False):
            st.caption(
                f"Enregistrée le **{str(prev_ins.get('created_at') or '')[:19]}** · modèle **{prev_ins.get('model_used') or '—'}**"
            )
            st.markdown(str(prev_ins.get("synthesis_text") or "") or "—")

    prompt_fb = (
        "Tu es consultant pour LumenVia (préparation dominicale, textes AELF, ton bienveillant).\n\n"
        "Données : réponses utilisateurs au questionnaire flash (sans adresses e-mail).\n"
        f"{bundle}\n\n"
        "Tâche :\n"
        "1) Synthèse en français (8 à 12 phrases) : tendances, forces, points de friction.\n"
        "2) Liste **5 à 8 actions concrètes** numérotées (priorité décroissante), titre court + une phrase utile.\n"
        "3) Trois questions ouvertes pour approfondir ensuite.\n\n"
        "Contraintes : pas de jargon SMTP/API ; pas inventer de citations ; rester factuel par rapport aux données."
    )

    # Formulaire : case « Forcer » + bouton dans la même soumission (aligné tactile / mobile ; évite deux reruns désynchronisés).
    with st.form("adm_fb_insights_generate_form", clear_on_submit=False):
        force_regen = st.checkbox(
            "Forcer un nouvel appel IA (refaire la synthèse même si une version existe pour ce périmètre)",
            value=False,
            key="adm_fb_insights_force",
            disabled=not bool(prev_ins),
        )
        gen_clicked = st.form_submit_button(
            "Générer la synthèse IA", type="primary", use_container_width=True
        )

    if gen_clicked:
        force_ok = bool(force_regen)
        if prev_ins and not force_ok:
            st.info(
                "Aucun appel IA lancé : ouvre l’expander « Synthèse déjà enregistrée » ou coche **Forcer** pour refaire une analyse."
            )
            return
        ov = loading_overlay("Analyse des retours…")
        try:
            vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
            out = vx.generate_text_auto(
                preferred_models=["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"],
                prompt=prompt_fb,
                max_output_tokens=4096,
            )
            txt = out.text or ""
            st.markdown(txt)
            entity_ins = sha256(f"fbins|{bundle_sig}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:28]
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="feedback_insights",
                values_by_col={
                    "entity_id": entity_ins,
                    "n_sample": str(int(n_sample)),
                    "bundle_sha256": bundle_sig,
                    "model_used": str(out.model or ""),
                    "synthesis_text": txt[:47000],
                },
            )
            st.success("Synthèse enregistrée dans la table `feedback_insights`.")
            try:
                ap._adm_feedback_sheet_fetch_cached.clear()
            except Exception:
                pass
        except Exception as e:
            st.exception(e)
        finally:
            ov.empty()

