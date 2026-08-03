"""Admin — Lab lectures multi-langues : sonde une semaine sur les sources API déclarées."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from core.liturgy_source_probes import LiturgyProbeResult, probe_liturgy_source
from core.liturgy_sources_registry import (
    LANG_PRIORITY,
    LITURGY_SOURCES,
    format_endpoint,
    sources_by_priority,
)
from ui.components import loading_overlay


def _next_sunday(today: date | None = None) -> date:
    d = today or date.today()
    # dimanche = 6 en weekday() Python (lun=0)
    delta = (6 - d.weekday()) % 7
    return d + timedelta(days=delta)


def _week_dates(anchor: date) -> list[date]:
    """Semaine civil : lundi → dimanche contenant ``anchor`` (ou ancrée dimanche → +6 j)."""
    # Si l’utilisateur choisit un dimanche, on prend dimanche → samedi suivant (semaine liturgique simple).
    if anchor.weekday() == 6:
        return [anchor + timedelta(days=i) for i in range(7)]
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def _result_row(r: LiturgyProbeResult, *, label: str, status: str) -> dict[str, Any]:
    blocks = r.blocks_found or {}
    return {
        "langue": r.lang,
        "source": label,
        "source_id": r.source_id,
        "statut_registre": status,
        "date": r.date,
        "http_ok": r.ok,
        "http": r.http_status if r.http_status is not None else "",
        "messe_complète*": r.full_mass_heuristic,
        "L1": bool(blocks.get("lecture_1")),
        "Ps": bool(blocks.get("psaume")),
        "L2": bool(blocks.get("lecture_2")),
        "Év": bool(blocks.get("evangile")),
        "chars": r.chars_total,
        "kind": r.raw_kind,
        "erreur": (r.error or "")[:120],
        "extrait": (r.excerpt or "")[:160],
    }


def render_admin_liturgy_multilang_lab() -> None:
    st.title("Lab — lectures multi-langues")
    st.caption(
        "Règle : **pas de traduction maison**. On ne retient que les API qui renvoient déjà "
        "les textes complets de la messe dans la langue. "
        f"Priorité : {' → '.join(LANG_PRIORITY)}. "
        "L’astérisque « messe complète* » est une heuristique (L1+Ps+Év + payload substantiel)."
    )

    with st.expander("Registre des sources", expanded=False):
        rows = []
        for s in LITURGY_SOURCES:
            rows.append(
                {
                    "id": s.id,
                    "label": s.label,
                    "lang": s.lang,
                    "status": s.status,
                    "full_mass_prouvé": s.provides_full_mass_texts,
                    "endpoint": s.endpoint_template,
                    "notes": s.notes,
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    include_excluded = st.checkbox(
        "Inclure les sources exclues (Romcal, Evangeli.net…) pour documentation",
        value=False,
        key="lab_ml_include_excluded",
    )
    catalogue = sources_by_priority(include_excluded=include_excluded)
    by_id = {s.id: s for s in catalogue}

    default_ids = [s.id for s in catalogue if s.status in ("production", "unproven", "candidate")]
    selected_ids = st.multiselect(
        "Sources à sonder",
        options=[s.id for s in catalogue],
        default=default_ids,
        format_func=lambda sid: f"{by_id[sid].lang} · {by_id[sid].label} [{by_id[sid].status}]",
        key="lab_ml_sources",
    )

    lang_filter = st.multiselect(
        "Filtrer les langues (vide = toutes dans la sélection)",
        options=list(LANG_PRIORITY),
        default=list(LANG_PRIORITY),
        key="lab_ml_langs",
    )

    c1, c2 = st.columns(2)
    with c1:
        anchor = st.date_input(
            "Date d’ancrage de la semaine",
            value=_next_sunday(),
            key="lab_ml_anchor",
        )
    with c2:
        st.markdown(
            "- Si **dimanche** : 7 jours dimanche → samedi.\n"
            "- Sinon : semaine ISO **lundi → dimanche** contenant la date."
        )

    if not isinstance(anchor, date):
        st.warning("Date invalide.")
        return

    week = _week_dates(anchor)
    st.info("Semaine sondée : " + " · ".join(d.isoformat() for d in week))

    run = st.button("Lancer la sonde (semaine × sources)", type="primary", key="lab_ml_run")

    if run:
        specs = [by_id[i] for i in selected_ids if i in by_id]
        if lang_filter:
            specs = [s for s in specs if s.lang in set(lang_filter)]
        if not specs:
            st.warning("Aucune source sélectionnée après filtre langues.")
            return

        results: list[dict[str, Any]] = []
        overlay = loading_overlay("Sonde multi-langues en cours…")
        try:
            total = len(specs) * len(week)
            done = 0
            prog = st.progress(0.0, text="0 / " + str(total))
            for spec in specs:
                for d in week:
                    r = probe_liturgy_source(spec, date_iso=d.isoformat())
                    results.append(_result_row(r, label=spec.label, status=spec.status))
                    done += 1
                    prog.progress(done / max(total, 1), text=f"{done} / {total} — {spec.id} {d.isoformat()}")
            st.session_state["lab_ml_last_results"] = results
            st.session_state["lab_ml_last_meta"] = {
                "week": [d.isoformat() for d in week],
                "sources": [s.id for s in specs],
            }
        finally:
            overlay.empty()

    results = st.session_state.get("lab_ml_last_results")
    if not results:
        st.caption("Aucun résultat en session — lance une sonde.")
        return

    meta = st.session_state.get("lab_ml_last_meta") or {}
    st.subheader("Résultats")
    st.caption(
        f"Semaine : {', '.join(meta.get('week') or [])} · "
        f"Sources : {', '.join(meta.get('sources') or [])}"
    )

    df = pd.DataFrame(results)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Synthèse par source : taux « messe complète* » sur la semaine
    if not df.empty:
        synth = (
            df.groupby(["langue", "source_id", "source"], dropna=False)
            .agg(
                jours=("date", "count"),
                http_ok=("http_ok", "sum"),
                messe_ok=("messe_complète*", "sum"),
                chars_med=("chars", "median"),
            )
            .reset_index()
        )
        st.subheader("Synthèse par source (semaine)")
        st.dataframe(synth, use_container_width=True, hide_index=True)

        only_prod = [s for s in LITURGY_SOURCES if s.id in set(df["source_id"]) and s.provides_full_mass_texts]
        if only_prod:
            st.success(
                "Sources déjà marquées « textes complets prouvés » dans le registre : "
                + ", ".join(s.label for s in only_prod)
            )
        else:
            st.warning(
                "Aucune source de la sélection n’est encore `provides_full_mass_texts=True` "
                "hors AELF — valider manuellement avant adapters."
            )

    with st.expander("URLs d’exemple (1er jour de la semaine)", expanded=False):
        d0 = (meta.get("week") or [week[0].isoformat()])[0]
        for sid in meta.get("sources") or []:
            spec = by_id.get(sid) or next((s for s in LITURGY_SOURCES if s.id == sid), None)
            if not spec:
                continue
            st.code(f"{spec.id}\n{format_endpoint(spec, date_iso=d0)}", language="text")
