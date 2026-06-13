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

# Colonnes du graphique d'évolution — secondes vs mots (deux ordonnées distinctes).
_SECONDS_SERIES: dict[str, str] = {
    "duration_text_s": "Texte Vertex — 1re passe (s)",
    "duration_text_retry_s": "Texte Vertex — relance (s)",
    "duration_tts_synthese_s": "TTS synthèse (s)",
    "duration_tts_lectures_s": "TTS lectures (s)",
    "duration_pdf_s": "PDF (s)",
}
_WORDS_SERIES: dict[str, str] = {
    "text_words": "Mots synthèse (axe droit)",
}

# Tuiles KPI — fonds pastels (charte LumenVia).
_KPI_TILES: tuple[tuple[str, str, str], ...] = (
    ("Synthèses suivies", "#eef6fb", "#0b2745"),
    ("Vertex — 1re passe (moy.)", "#e6f2ef", "#0b2745"),
    ("Mots moyens", "#fdf3e7", "#92400e"),
    ("TTS lectures (moy.)", "#ede9fe", "#5b21b6"),
    ("TTS synthèse (moy.)", "#ecfdf5", "#047857"),
    ("Relance Vertex (moy.)", "#fef9c3", "#854d0e"),
    ("PDF (moy.)", "#f1f5f9", "#334155"),
)


def _escape_html(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_perf_kpi_tiles(items: list[tuple[str, str]]) -> None:
    """Indicateurs en tuiles pastel (label + valeur)."""
    if not items:
        return
    cols_per_row = 4
    for start in range(0, len(items), cols_per_row):
        row_items = items[start : start + cols_per_row]
        cols = st.columns(cols_per_row)
        for j, (label, value) in enumerate(row_items):
            idx = start + j
            _, bg, fg = _KPI_TILES[idx] if idx < len(_KPI_TILES) else ("", "#f8fafc", "#0b2745")
            with cols[j]:
                cols[j].markdown(
                    f"""
<div style="
  background:{bg};
  border:1px solid rgba(11,39,69,0.09);
  border-radius:12px;
  padding:0.75rem 0.95rem;
  min-height:4.6rem;
  box-shadow:0 1px 2px rgba(11,39,69,0.04);
">
  <div style="font-size:0.78rem;line-height:1.25;color:#5c5348;margin-bottom:0.35rem;">
    {_escape_html(label)}
  </div>
  <div style="font-size:1.35rem;font-weight:600;line-height:1.2;color:{fg};font-family:Lora,Georgia,serif;">
    {_escape_html(value)}
  </div>
</div>
""",
                    unsafe_allow_html=True,
                )


def _altair_legend_config() -> dict[str, object]:
    return {
        "orient": "bottom",
        "direction": "horizontal",
        "labelLimit": 420,
        "titleLimit": 420,
        "symbolLimit": 420,
        "columns": 2,
        "labelFont": "Lora",
        "titleFont": "Lora",
        "padding": 8,
        "offset": 12,
    }


def _render_evolution_dual_axis_chart(df: object) -> None:
    """Courbes temporelles : ordonnée gauche = secondes, droite = mots (synthèse)."""
    import pandas as pd

    plot_df = df.copy()
    if "date" not in plot_df.columns:
        return
    plot_df["date"] = pd.to_datetime(plot_df["date"])

    sec_keys = [k for k in _SECONDS_SERIES if k in plot_df.columns and plot_df[k].notna().any()]
    has_words = "text_words" in plot_df.columns and plot_df["text_words"].notna().any()
    if not sec_keys and not has_words:
        st.info("Historique sans colonnes graphiques exploitables.")
        return

    try:
        import altair as alt

        layers: list[alt.Chart] = []

        if sec_keys:
            df_sec = plot_df[["date", *sec_keys]].melt(
                id_vars="date",
                value_vars=sec_keys,
                var_name="metric_key",
                value_name="seconds",
            )
            df_sec = df_sec.dropna(subset=["seconds"])
            df_sec["metric"] = df_sec["metric_key"].map(_SECONDS_SERIES)
            layers.append(
                alt.Chart(df_sec)
                .mark_line(point={"filled": True, "size": 40})
                .encode(
                    x=alt.X("date:T", title="Dimanche"),
                    y=alt.Y(
                        "seconds:Q",
                        title="Secondes",
                        axis=alt.Axis(titleColor="#0b2745"),
                    ),
                    color=alt.Color(
                        "metric:N",
                        title="Durées",
                        scale=alt.Scale(range=["#0b2745", "#0d9488", "#0369a1", "#7c3aed", "#64748b"]),
                    ),
                    tooltip=[
                        alt.Tooltip("date:T", title="Dimanche"),
                        "metric:N",
                        alt.Tooltip("seconds:Q", title="Valeur", format=".1f"),
                    ],
                )
            )

        if has_words:
            df_words = plot_df[["date", "text_words"]].dropna(subset=["text_words"])
            layers.append(
                alt.Chart(df_words)
                .mark_line(
                    point={"filled": True, "size": 46},
                    color="#b45309",
                    strokeWidth=2.5,
                    strokeDash=[6, 3],
                )
                .encode(
                    x=alt.X("date:T", title="Dimanche"),
                    y=alt.Y(
                        "text_words:Q",
                        title="Mots (synthèse)",
                        axis=alt.Axis(orient="right", titleColor="#b45309"),
                    ),
                    tooltip=[
                        alt.Tooltip("date:T", title="Dimanche"),
                        alt.Tooltip("text_words:Q", title="Mots synthèse", format="d"),
                    ],
                )
            )

        chart = (
            alt.layer(*layers)
            .resolve_scale(y="independent")
            .properties(height=400, padding={"bottom": 88})
            .configure_axis(labelFont="Lora", titleFont="Lora")
            .configure_legend(**_altair_legend_config())
        )
        st.altair_chart(chart, use_container_width=True)
        if has_words:
            st.caption(
                "Trait **orange pointillé** = mots de la synthèse (ordonnée de **droite**). "
                "Lignes pleines = durées en **secondes** (ordonnée de **gauche**)."
            )
    except Exception:
        sec_present = [k for k in _SECONDS_SERIES if k in plot_df.columns and plot_df[k].notna().any()]
        if sec_present:
            st.caption("Évolution — durées (secondes)")
            st.line_chart(
                plot_df.set_index("date")[sec_present].rename(columns=_SECONDS_SERIES),
                height=280,
            )
        if has_words:
            st.caption("Évolution — volume synthèse (mots)")
            st.line_chart(
                plot_df.set_index("date")[["text_words"]].rename(columns=_WORDS_SERIES),
                height=220,
            )


def _render_bar_seconds_only(df: object) -> None:
    import pandas as pd

    bar_rows: list[dict[str, str | float]] = []
    for col, label in _SECONDS_SERIES.items():
        if col not in df.columns:
            continue
        avg = mean_metric(df.to_dict("records"), col)
        if avg is not None and (col != "duration_text_retry_s" or avg > 0):
            bar_rows.append({"Artefact": label, "Secondes": avg})
    if not bar_rows:
        return
    bar_df = pd.DataFrame(bar_rows)
    try:
        import altair as alt

        chart_h = max(240, 52 * len(bar_rows))
        chart = (
            alt.Chart(bar_df)
            .mark_bar(color="#0d9488", cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                y=alt.Y(
                    "Artefact:N",
                    sort="-x",
                    title=None,
                    axis=alt.Axis(labelLimit=480, labelFont="Lora"),
                ),
                x=alt.X("Secondes:Q", title="Secondes (moyenne sur la période)"),
                tooltip=[
                    alt.Tooltip("Artefact:N", title="Étape"),
                    alt.Tooltip("Secondes:Q", title="Secondes", format=".1f"),
                ],
            )
            .properties(height=chart_h)
            .configure_axis(labelFont="Lora", titleFont="Lora")
        )
        st.altair_chart(chart, use_container_width=True)
    except Exception:
        st.bar_chart(bar_df.set_index("Artefact"), height=260)


def _load_perf_tables(*, gsheet_id: str, sa_fp: str) -> tuple[list[dict], list[dict], list[dict]]:
    _ACR = {"generations": "GEN", "audio": "AUD", "pdf_exports": "PDFX"}
    loaded: dict[str, list[dict]] = {}
    for logical in ("generations", "audio", "pdf_exports"):
        try:
            loaded[logical] = adm_sheets_fetch_cached(gsheet_id, logical, 0, sa_fp)
        except Exception as ex:
            acr = _ACR.get(logical, logical)
            raise RuntimeError(f"table {logical!r} (onglet {acr}) — {ex}") from ex
    return (
        live_generations_with_perf(loaded["generations"]),
        live_audio_with_perf(loaded["audio"]),
        live_pdf_with_perf(loaded["pdf_exports"]),
    )


def render_admin_generation_perf_monitor() -> None:
    st.subheader("Performance génération — artefacts dominicaux")
    st.caption(
        "Durées enregistrées dans **GEN** (texte), **AUD** (audios) et **PDFX** (fascicules) "
        "à chaque « Tout régénérer » ou « Compléter les manquants ». "
        "Seules les lignes **Actif** avec au moins une métrique de durée sont prises en compte. "
        "**Texte Vertex (s)** = temps d’appel API Gemini (1re passe ; relance séparée si besoin) ; "
        "**Mots synthèse** = longueur du texte produit (volume, pas une durée)."
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
        if "duplicates" in str(ex).lower() or "dupliqu" in str(ex).lower():
            st.info(
                "Cause fréquente : une colonne ajoutée à la main existe déjà dans l’en-tête "
                "(souvent **`zone`** sur **PDFX** ou **GEN**). "
                "Ouvre l’onglet concerné, repère les deux colonnes identiques en ligne 1, "
                "supprime celle qui est vide ou en double, puis actualise."
            )
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
    m_retry = mean_metric(gen_rows, "duration_text_retry_s")
    m_pdf = mean_metric(pdf_rows, "duration_build_s")

    _render_perf_kpi_tiles(
        [
            ("Synthèses suivies", str(n_gen)),
            ("Vertex — 1re passe (moy.)", f"{m_text:.0f} s" if m_text is not None else "—"),
            ("Mots moyens", f"{int(round(m_words))}" if m_words is not None else "—"),
            ("TTS lectures (moy.)", f"{m_lect:.0f} s" if m_lect is not None else "—"),
            ("TTS synthèse (moy.)", f"{m_syn:.0f} s" if m_syn is not None else "—"),
            ("Relance Vertex (moy.)", f"{m_retry:.0f} s" if m_retry and m_retry > 0 else "—"),
            ("PDF (moy.)", f"{m_pdf:.0f} s" if m_pdf is not None else "—"),
        ]
    )

    if not joined:
        st.warning("Données partielles — pas assez de lignes GEN datées pour construire l’historique.")
        return

    import pandas as pd

    df = pd.DataFrame(joined)
    drift_labels = {**_SECONDS_SERIES, **_WORDS_SERIES}

    st.markdown("#### Évolution par dimanche")
    st.caption(
        "Ordonnée **gauche** : durées (secondes). Ordonnée **droite** (trait orange) : "
        "nombre de mots de la synthèse."
    )
    _render_evolution_dual_axis_chart(df)

    st.markdown("#### Moyennes par artefact — durées (secondes)")
    _render_bar_seconds_only(df)

    st.markdown("#### Dérives récentes (derniers 8 dimanches vs moyenne globale)")
    recent = df.tail(8)
    drift_lines: list[str] = []
    for col, label in drift_labels.items():
        if col not in df.columns:
            continue
        global_avg = mean_metric(df.to_dict("records"), col)
        recent_avg = mean_metric(recent.to_dict("records"), col)
        if global_avg is None or recent_avg is None or global_avg <= 0:
            continue
        if col == "duration_text_retry_s" and global_avg <= 0:
            continue
        unit = " mots" if col == "text_words" else ""
        delta_pct = ((recent_avg - global_avg) / global_avg) * 100.0
        if abs(delta_pct) >= 15:
            drift_lines.append(
                f"- **{label}** : {recent_avg:.0f}{unit} récent vs {global_avg:.0f}{unit} moy. "
                f"({delta_pct:+.0f} %)"
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
