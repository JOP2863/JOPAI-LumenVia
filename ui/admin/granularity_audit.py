"""Admin — Radar de granularité (Index gaussien), Constitution JOPAI V16.10."""

from __future__ import annotations

import streamlit as st

from core.system_audit import (
    bin_histogram,
    corps_line_counts,
    expected_bin_counts,
    run_granularity_audit,
)


def render_admin_granularity_audit() -> None:
    st.title("Radar — granularité")
    st.caption(
        "Index gaussien sur le « Corps » du dépôt (`core/`, `ui/pages/`, `ui/admin/`) : "
        "distribution des LOC par fichier, loi normale de référence (μ et σ empiriques), "
        "alertes au-delà de μ + 2σ — risque de navigation cognitive."
    )

    result = run_granularity_audit()

    st.markdown(
        f"""
<div style="font-size:0.95rem;color:#342E29;margin-bottom:0.75rem;">
<strong>Racine analysée :</strong> <code>{result.repo_root}</code>
</div>
""",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Fichiers corps", str(result.corps_n))
    c2.metric("μ (LOC)", f"{result.corps_mean:.1f}")
    c3.metric("σ (LOC)", f"{result.corps_pstdev:.1f}")
    c4.metric("Seuil μ + 2σ", f"{result.threshold_lines:.1f}")

    sommet_rows = [r for r in result.rows if r.zone == "sommet"]
    if sommet_rows:
        st.subheader("Sommet (léger)")
        lines_md = "| Fichier | LOC |\n| --- | ---: |\n"
        for r in sorted(sommet_rows, key=lambda x: (-x.line_count, x.rel_path)):
            lines_md += f"| `{r.rel_path}` | {r.line_count} |\n"
        st.markdown(lines_md)

    vals = corps_line_counts(result)
    if not vals:
        st.warning("Aucun fichier « corps » trouvé — vérifiez la racine du dépôt.")
        return

    centers, counts, edges = bin_histogram(vals, num_bins=18)
    mu, sigma = result.corps_mean, result.corps_pstdev
    expected = expected_bin_counts(edges, mu, sigma, result.corps_n)

    if sigma <= 0.0 or len(centers) != len(expected):
        st.info(
            "Écart-type nul ou échantillon dégénéré : la courbe gaussienne de référence n’est pas affichée. "
            "L’histogramme empirique reste disponible."
        )
        try:
            import pandas as pd

            st.bar_chart(
                pd.DataFrame({"LOC (centre de bin)": centers, "Fichiers": counts}).set_index(
                    "LOC (centre de bin)"
                )
            )
        except Exception:
            st.bar_chart({"Fichiers": counts})

    else:
        try:
            import altair as alt
            import pandas as pd

            df = pd.DataFrame(
                {
                    "loc_center": centers,
                    "observed": counts,
                    "expected_gauss": [round(x, 3) for x in expected],
                }
            )

            bar = (
                alt.Chart(df)
                .mark_bar(opacity=0.62, color="#0d9488")
                .encode(
                    x=alt.X("loc_center:Q", title="Lignes effectives (LOC) — centre de bin"),
                    y=alt.Y("observed:Q", title="Nombre de fichiers", axis=alt.Axis(titleColor="#0b2745")),
                    tooltip=[
                        alt.Tooltip("loc_center:Q", title="Bin (centre LOC)", format=".1f"),
                        alt.Tooltip("observed:Q", title="Observé"),
                    ],
                )
            )
            line = (
                alt.Chart(df)
                .mark_line(point={"filled": True, "size": 45}, color="#0b2745", strokeWidth=2)
                .encode(
                    x="loc_center:Q",
                    y=alt.Y("expected_gauss:Q", title=""),
                    tooltip=[
                        alt.Tooltip("loc_center:Q", title="Bin (centre LOC)", format=".1f"),
                        alt.Tooltip("expected_gauss:Q", title="Attendu (Gauss)", format=".2f"),
                    ],
                )
            )
            chart = (
                (bar + line)
                .resolve_scale(y="shared")
                .properties(height=380)
                .configure_axis(labelFont="Lora", titleFont="Lora")
            )
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            import pandas as pd

            st.bar_chart(
                pd.DataFrame({"Observé": counts, "Gauss (attendu)": expected}, index=centers)
            )

    st.subheader("Alertes — hors nuage (corps)")
    if not result.alert_paths:
        st.success(
            "Aucun fichier du corps ne dépasse le seuil μ + 2σ : granularité dans l’enveloppe de référence."
        )
    else:
        st.warning(
            "Les modules ci-dessous dépassent le seuil : **risque de navigation cognitive** "
            "(prioriser découpage ou façades)."
        )
        alert_md = "| Fichier | LOC | Dépassement vs seuil |\n| --- | ---: | ---: |\n"
        for rel, nlines, excess in result.alert_paths:
            alert_md += f"| `{rel}` | {nlines} | +{excess:.1f} |\n"
        st.markdown(alert_md)

    with st.expander("Périmètre périphérique (`tools/`, etc.)", expanded=False):
        peri = [r for r in result.rows if r.zone == "peripherie"]
        if not peri:
            st.caption("Aucun fichier Python dans les dossiers périphériques scannés.")
        else:
            peri.sort(key=lambda x: (-x.line_count, x.rel_path))
            t = "| Fichier | LOC |\n| --- | ---: |\n"
            for r in peri:
                t += f"| `{r.rel_path}` | {r.line_count} |\n"
            st.markdown(t)
