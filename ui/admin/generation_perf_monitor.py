"""Tableau de bord admin — temps de génération des artefacts dominicaux."""

from __future__ import annotations

import streamlit as st

from core.config import load_config
from core.sunday_generation_perf import (
    join_perf_by_date,
    live_audio_with_perf,
    live_generations_with_perf,
    live_pdf_with_perf,
    mean_metric,
)
from ui.streamlit_caches import adm_sheets_fetch_cached, service_account_json_fingerprint


def _load_perf_tables(*, gsheet_id: str, sa_fp: str) -> tuple[list[dict], list[dict], list[dict]]:
    gen = adm_sheets_fetch_cached(gsheet_id, "generations", 0, sa_fp)
    aud = adm_sheets_fetch_cached(gsheet_id, "audio", 0, sa_fp)
    pdf = adm_sheets_fetch_cached(gsheet_id, "pdf_exports", 0, sa_fp)
    return (
        live_generations_with_perf(gen),
        live_audio_with_perf(aud),
        live_pdf_with_perf(pdf),
    )


def render_admin_generation_perf_monitor() -> None:
    st.subheader("Performance génération — artefacts dominicaux")
    st.caption(
        "Durées enregistrées dans **GEN** (texte), **AUD** (audios) et **PDFX** (fascicules) "
        "à chaque « Tout régénérer » ou « Compléter les manquants ». "
        "Seules les lignes **Actif** avec au moins une métrique de durée sont prises en compte."
    )

    cfg = load_config()
    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
    if not gsheet_id or not getattr(cfg, "gcp_service_account", None):
        st.info("Configure `gsheet_id` et le compte de service pour afficher les métriques.")
        return

    sa_fp = service_account_json_fingerprint(cfg.gcp_service_account)
    if st.button("Actualiser les métriques", key="adm_gen_perf_refresh"):
        adm_sheets_fetch_cached.clear()

    try:
        gen_rows, aud_rows, pdf_rows = _load_perf_tables(gsheet_id=gsheet_id, sa_fp=sa_fp)
    except Exception as ex:
        st.error(f"Lecture Sheets impossible : {ex}")
        return

    if not gen_rows and not aud_rows and not pdf_rows:
        st.info(
            "Aucune métrique enregistrée pour l’instant. "
            "Lance une régénération après déploiement du code — les colonnes de durée doivent être remplies."
        )
        return

    joined = join_perf_by_date(generations=gen_rows, audios=aud_rows, pdfs=pdf_rows)
    n_gen = len(gen_rows)
    n_aud = len(aud_rows)
    n_pdf = len(pdf_rows)

    c1, c2, c3, c4 = st.columns(4)
    m_text = mean_metric(gen_rows, "duration_text_s")
    m_words = mean_metric(gen_rows, "text_words")
    m_syn = mean_metric(
        [a for a in aud_rows if a.get("kind") == "synthese"],
        "duration_tts_s",
    )
    m_lect = mean_metric(
        [a for a in aud_rows if a.get("kind") == "lectures"],
        "duration_tts_s",
    )
    c1.metric("Synthèses suivies", n_gen)
    c2.metric("Temps texte moyen", f"{m_text:.0f} s" if m_text is not None else "—")
    c3.metric("Mots moyens", f"{int(round(m_words))}" if m_words is not None else "—")
    c4.metric("TTS lectures moy.", f"{m_lect:.0f} s" if m_lect is not None else "—")

    c5, c6, c7 = st.columns(3)
    m_retry = mean_metric(gen_rows, "duration_text_retry_s")
    m_pdf = mean_metric(pdf_rows, "duration_build_s")
    c5.metric("TTS synthèse moy.", f"{m_syn:.0f} s" if m_syn is not None else "—")
    c6.metric("Relance texte moy.", f"{m_retry:.0f} s" if m_retry and m_retry > 0 else "—")
    c7.metric("PDF moy.", f"{m_pdf:.0f} s" if m_pdf is not None else "—")

    if not joined:
        st.warning("Données partielles — pas assez de lignes GEN datées pour construire l’historique.")
        return

    import pandas as pd

    df = pd.DataFrame(joined)
    chart_cols = {
        "duration_text_s": "Texte Vertex (s)",
        "duration_tts_synthese_s": "TTS synthèse (s)",
        "duration_tts_lectures_s": "TTS lectures (s)",
        "duration_pdf_s": "PDF (s)",
        "text_words": "Mots synthèse",
    }
    present = [c for c in chart_cols if c in df.columns and df[c].notna().any()]
    if not present:
        st.info("Historique sans colonnes graphiques exploitables.")
        return

    st.markdown("#### Évolution par dimanche")
    plot_df = df.set_index("date")[present].rename(columns={k: chart_cols[k] for k in present})
    st.line_chart(plot_df, height=320)

    st.markdown("#### Moyennes par artefact (période affichée)")
    bar_rows = []
    for col, label in chart_cols.items():
        if col not in df.columns:
            continue
        avg = mean_metric(df.to_dict("records"), col)
        if avg is not None:
            bar_rows.append({"Artefact": label, "Secondes (ou mots)": avg})
    if bar_rows:
        bar_df = pd.DataFrame(bar_rows).set_index("Artefact")
        st.bar_chart(bar_df, height=280)

    st.markdown("#### Dérives récentes (derniers 8 dimanches vs moyenne globale)")
    recent = df.tail(8)
    drift_lines: list[str] = []
    for col, label in chart_cols.items():
        if col not in df.columns:
            continue
        global_avg = mean_metric(df.to_dict("records"), col)
        recent_avg = mean_metric(recent.to_dict("records"), col)
        if global_avg is None or recent_avg is None or global_avg <= 0:
            continue
        delta_pct = ((recent_avg - global_avg) / global_avg) * 100.0
        if abs(delta_pct) >= 15:
            drift_lines.append(
                f"- **{label}** : {recent_avg:.0f} récent vs {global_avg:.0f} moy. ({delta_pct:+.0f} %)"
            )
    if drift_lines:
        st.warning("Écart notable détecté :\n" + "\n".join(drift_lines))
    else:
        st.success("Aucune dérive > 15 % sur les 8 derniers dimanches (par rapport à la moyenne globale).")

    with st.expander("Détail des lignes GEN (texte)", expanded=False):
        if gen_rows:
            show_gen = pd.DataFrame(gen_rows).sort_values("date", ascending=False)
            st.dataframe(show_gen, use_container_width=True, hide_index=True)
        else:
            st.caption("Aucune ligne GEN avec métriques.")

    with st.expander("Détail AUD (audios)", expanded=False):
        if aud_rows:
            show_aud = pd.DataFrame(aud_rows)
            st.dataframe(show_aud, use_container_width=True, hide_index=True)
        else:
            st.caption("Aucune ligne AUD avec métriques.")

    with st.expander("Détail PDFX (fascicules)", expanded=False):
        if pdf_rows:
            show_pdf = pd.DataFrame(pdf_rows).sort_values("date", ascending=False)
            st.dataframe(show_pdf, use_container_width=True, hide_index=True)
        else:
            st.caption("Aucune ligne PDFX avec métriques.")

    st.caption(
        "Astuce : filtre les lignes **Inactif** dans Sheets si tu veux exclure des générations obsolètes du calcul."
    )
