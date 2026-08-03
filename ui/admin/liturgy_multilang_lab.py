"""Admin — Lab lectures multi-langues : sonde une semaine sur les sources API déclarées."""

from __future__ import annotations

import html
from datetime import date, timedelta
from typing import Any

import streamlit as st

from core.liturgy_source_probes import LiturgyProbeResult, probe_liturgy_source
from core.liturgy_sources_registry import (
    LANG_PRIORITY,
    LITURGY_SOURCES,
    format_endpoint,
    sources_by_priority,
)
from ui.components import loading_overlay, update_loading_overlay

_LANG_LABELS: dict[str, str] = {
    "FR": "français",
    "DE": "allemand",
    "EN": "anglais",
    "ES": "espagnol",
    "IT": "italien",
}


def _next_sunday(today: date | None = None) -> date:
    d = today or date.today()
    delta = (6 - d.weekday()) % 7
    return d + timedelta(days=delta)


def _week_dates(anchor: date) -> list[date]:
    if anchor.weekday() == 6:
        return [anchor + timedelta(days=i) for i in range(7)]
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def _esc(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _yn(v: object) -> str:
    return "oui" if bool(v) else "non"


def _yn_cell(v: object) -> str:
    ok = bool(v)
    cls = "lv-lab-ok" if ok else "lv-lab-no"
    return f'<td class="{cls}">{_yn(ok)}</td>'


def _result_row(r: LiturgyProbeResult, *, label: str, status: str) -> dict[str, Any]:
    blocks = r.blocks_found or {}
    return {
        "langue": r.lang,
        "source": label,
        "source_id": r.source_id,
        "statut_registre": status,
        "date": r.date,
        "url": r.url,
        "final_url": r.final_url or r.url,
        "http_ok": r.ok,
        "http": r.http_status if r.http_status is not None else "",
        "elapsed_ms": r.elapsed_ms if r.elapsed_ms is not None else "",
        "messe_complete": r.full_mass_heuristic,
        "L1": bool(blocks.get("lecture_1")),
        "Ps": bool(blocks.get("psaume")),
        "L2": bool(blocks.get("lecture_2")),
        "Ev": bool(blocks.get("evangile")),
        "chars": r.chars_total,
        "kind": r.raw_kind,
        "content_type": r.content_type,
        "content_length": r.content_length if r.content_length is not None else "",
        "redirects": r.redirect_count,
        "encoding": r.encoding,
        "server": r.server,
        "cache_control": r.cache_control,
        "top_keys": r.top_keys,
        "headers_debug": r.headers_debug,
        "body_fp": r.body_sha_prefix,
        "erreur": (r.error or "")[:240],
        "extrait": (r.excerpt or "")[:280],
    }


def _html_table(headers: list[str], rows: list[list[str]], *, table_id: str) -> str:
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_parts: list[str] = []
    for cells in rows:
        # cells may already contain HTML (e.g. yn_cell) — marked with \x00html\x00 prefix
        tds: list[str] = []
        for c in cells:
            if isinstance(c, str) and c.startswith("\x00html\x00"):
                tds.append(c[len("\x00html\x00") :])
            else:
                tds.append(f"<td>{_esc(c)}</td>")
        body_parts.append("<tr>" + "".join(tds) + "</tr>")
    return f"""
<div class="lv-lab-scroll" id="{_esc(table_id)}">
<table class="lv-lab-table">
<thead><tr>{thead}</tr></thead>
<tbody>
{"".join(body_parts)}
</tbody>
</table>
</div>
""".strip()


def _lab_css() -> str:
    return """
<style>
/* Pastilles multiselect : éviter F blanc hors fond or (letter-spacing / primary leak) */
div[class*="st-key-lab_ml_"] [data-baseweb="tag"],
div[class*="st-key-lab_ml_"] span[kind],
div[data-baseweb="select"] [data-baseweb="tag"] {
  background-color: rgba(212, 175, 55, 0.22) !important;
  color: #342E29 !important;
  border: 1px solid rgba(212, 175, 55, 0.65) !important;
  letter-spacing: normal !important;
  padding-left: 0.55rem !important;
  padding-right: 0.35rem !important;
}
div[class*="st-key-lab_ml_"] [data-baseweb="tag"] span,
div[data-baseweb="select"] [data-baseweb="tag"] span {
  color: #342E29 !important;
  letter-spacing: normal !important;
}
.lv-lab-wrap { font-family: Lora, Georgia, serif; color: #342E29; font-size: 0.88rem; }
.lv-lab-scroll {
  overflow-x: auto;
  max-width: 100%;
  margin: 0.6rem 0 1.1rem 0;
  border: 1px solid rgba(212, 175, 55, 0.35);
  background: rgba(255,255,255,0.72);
}
.lv-lab-table {
  border-collapse: collapse;
  width: max-content;
  min-width: 100%;
}
.lv-lab-table th {
  position: sticky; top: 0;
  text-align: left;
  padding: 8px 10px;
  background: rgba(212, 175, 55, 0.22);
  border: 1px solid rgba(212, 175, 55, 0.4);
  white-space: nowrap;
  font-weight: 600;
}
.lv-lab-table td {
  vertical-align: top;
  padding: 7px 10px;
  border: 1px solid rgba(52, 46, 41, 0.12);
  max-width: 28rem;
  word-break: break-word;
}
.lv-lab-table tr:nth-child(even) td { background: rgba(253, 251, 247, 0.95); }
.lv-lab-ok { color: #1b5e20; font-weight: 600; }
.lv-lab-no { color: #6d4c41; }
.lv-lab-prog-label {
  font-family: Lora, Georgia, serif;
  color: #342E29;
  margin: 0.35rem 0 0.15rem 0;
  font-size: 0.92rem;
}
</style>
""".strip()


def render_admin_liturgy_multilang_lab() -> None:
    st.title("Lab — lectures multi-langues")
    st.markdown(_lab_css(), unsafe_allow_html=True)
    st.caption(
        "Règle : **pas de traduction maison**. Uniquement des API à textes complets. "
        f"Priorité : {' · '.join(LANG_PRIORITY)}. "
        "« messe complète » = heuristique L1+Ps+Év + payload substantiel."
    )

    with st.expander("Registre des sources", expanded=False):
        reg_rows: list[list[str]] = []
        for s in LITURGY_SOURCES:
            reg_rows.append(
                [
                    s.id,
                    s.label,
                    s.lang,
                    s.status,
                    _yn(s.provides_full_mass_texts),
                    s.endpoint_template,
                    s.notes,
                    s.license_note,
                ]
            )
        st.markdown(
            '<div class="lv-lab-wrap">'
            + _html_table(
                ["id", "label", "langue", "statut", "full_mass prouvé", "endpoint", "notes", "licence"],
                reg_rows,
                table_id="lab_reg",
            )
            + "</div>",
            unsafe_allow_html=True,
        )

    include_excluded = st.checkbox(
        "Inclure les sources exclues (Romcal, Evangeli.net…) pour documentation",
        value=False,
        key="lab_ml_include_excluded",
    )
    catalogue = sources_by_priority(include_excluded=include_excluded)
    by_id = {s.id: s for s in catalogue}

    default_ids = [s.id for s in catalogue if s.status in ("production", "unproven", "candidate")]

    def _fmt_source(sid: str) -> str:
        s = by_id[sid]
        # Préfixe « langue » évite le bug pastille Streamlit (1ʳᵉ lettre blanche hors fond or)
        return f"[{s.lang}] {s.label} — {s.status}"

    selected_ids = st.multiselect(
        "Sources à sonder",
        options=[s.id for s in catalogue],
        default=default_ids,
        format_func=_fmt_source,
        key="lab_ml_sources",
    )

    lang_options = list(LANG_PRIORITY)
    lang_filter = st.multiselect(
        "Filtrer les langues (vide = toutes dans la sélection)",
        options=lang_options,
        default=lang_options,
        format_func=lambda lg: f"[{lg}] {_LANG_LABELS.get(lg, lg)}",
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

    prog_slot = st.empty()
    prog_label = st.empty()

    if run:
        specs = [by_id[i] for i in selected_ids if i in by_id]
        if lang_filter:
            specs = [s for s in specs if s.lang in set(lang_filter)]
        if not specs:
            st.warning("Aucune source sélectionnée après filtre langues.")
            return

        results: list[dict[str, Any]] = []
        total = len(specs) * len(week)
        overlay = loading_overlay("Sonde multi-langues — démarrage…")
        try:
            done = 0
            bar = prog_slot.progress(0.0, text=f"0 / {total}")
            for spec in specs:
                for d in week:
                    msg = f"{done + 1}/{total} — {spec.id} · {d.isoformat()}"
                    prog_label.markdown(
                        f'<p class="lv-lab-prog-label">{_esc(msg)}</p>',
                        unsafe_allow_html=True,
                    )
                    update_loading_overlay(overlay, f"Sonde multi-langues — {msg}", flush=False)
                    r = probe_liturgy_source(spec, date_iso=d.isoformat())
                    results.append(_result_row(r, label=spec.label, status=spec.status))
                    done += 1
                    bar.progress(done / max(total, 1), text=f"{done} / {total} — {spec.id} {d.isoformat()}")
            st.session_state["lab_ml_last_results"] = results
            st.session_state["lab_ml_last_meta"] = {
                "week": [d.isoformat() for d in week],
                "sources": [s.id for s in specs],
            }
            prog_label.markdown(
                f'<p class="lv-lab-prog-label">Terminé : {done} / {total} appels.</p>',
                unsafe_allow_html=True,
            )
        finally:
            overlay.empty()

    results = st.session_state.get("lab_ml_last_results")
    if not results:
        st.caption("Aucun résultat en session — lance une sonde.")
        return

    meta = st.session_state.get("lab_ml_last_meta") or {}
    st.subheader("Résultats (détail — HTML copiable)")
    st.caption(
        f"Semaine : {', '.join(meta.get('week') or [])} · "
        f"Sources : {', '.join(meta.get('sources') or [])}"
    )

    detail_headers = [
        "langue",
        "source_id",
        "source",
        "statut",
        "date",
        "http_ok",
        "http",
        "ms",
        "messe*",
        "L1",
        "Ps",
        "L2",
        "Év",
        "chars",
        "kind",
        "content-type",
        "content-length",
        "redirects",
        "encoding",
        "server",
        "cache-control",
        "url",
        "final_url",
        "top_keys",
        "headers",
        "body_fp",
        "erreur",
        "extrait",
    ]
    detail_rows: list[list[str]] = []
    for row in results:
        detail_rows.append(
            [
                row.get("langue", ""),
                row.get("source_id", ""),
                row.get("source", ""),
                row.get("statut_registre", ""),
                row.get("date", ""),
                "\x00html\x00" + _yn_cell(row.get("http_ok")),
                row.get("http", ""),
                row.get("elapsed_ms", ""),
                "\x00html\x00" + _yn_cell(row.get("messe_complete")),
                "\x00html\x00" + _yn_cell(row.get("L1")),
                "\x00html\x00" + _yn_cell(row.get("Ps")),
                "\x00html\x00" + _yn_cell(row.get("L2")),
                "\x00html\x00" + _yn_cell(row.get("Ev")),
                row.get("chars", ""),
                row.get("kind", ""),
                row.get("content_type", ""),
                row.get("content_length", ""),
                row.get("redirects", ""),
                row.get("encoding", ""),
                row.get("server", ""),
                row.get("cache_control", ""),
                row.get("url", ""),
                row.get("final_url", ""),
                row.get("top_keys", ""),
                row.get("headers_debug", ""),
                row.get("body_fp", ""),
                row.get("erreur", ""),
                row.get("extrait", ""),
            ]
        )

    st.markdown(
        '<div class="lv-lab-wrap">' + _html_table(detail_headers, detail_rows, table_id="lab_detail") + "</div>",
        unsafe_allow_html=True,
    )

    # Synthèse par source
    from collections import defaultdict

    agg: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"jours": 0, "http_ok": 0, "messe_ok": 0, "chars": [], "ms": []}
    )
    for row in results:
        key = (str(row.get("langue") or ""), str(row.get("source_id") or ""), str(row.get("source") or ""))
        a = agg[key]
        a["jours"] += 1
        a["http_ok"] += 1 if row.get("http_ok") else 0
        a["messe_ok"] += 1 if row.get("messe_complete") else 0
        try:
            a["chars"].append(int(row.get("chars") or 0))
        except Exception:
            pass
        try:
            if row.get("elapsed_ms") != "":
                a["ms"].append(int(row.get("elapsed_ms") or 0))
        except Exception:
            pass

    st.subheader("Synthèse par source (semaine)")
    synth_rows: list[list[str]] = []
    for (lg, sid, label), a in sorted(agg.items()):
        chars = sorted(a["chars"])
        ms = sorted(a["ms"])
        med_chars = chars[len(chars) // 2] if chars else ""
        med_ms = ms[len(ms) // 2] if ms else ""
        synth_rows.append(
            [
                lg,
                sid,
                label,
                a["jours"],
                a["http_ok"],
                a["messe_ok"],
                med_chars,
                med_ms,
            ]
        )
    st.markdown(
        '<div class="lv-lab-wrap">'
        + _html_table(
            ["langue", "source_id", "source", "jours", "http_ok", "messe_ok", "chars_médian", "ms_médian"],
            [[str(c) for c in r] for r in synth_rows],
            table_id="lab_synth",
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    only_prod = [s for s in LITURGY_SOURCES if s.id in {r.get("source_id") for r in results} and s.provides_full_mass_texts]
    if only_prod:
        st.success(
            "Sources déjà marquées « textes complets prouvés » : " + ", ".join(s.label for s in only_prod)
        )
    else:
        st.warning(
            "Aucune source de la sélection n’est encore `provides_full_mass_texts=True` "
            "hors AELF — valider manuellement avant adapters."
        )

    with st.expander("URLs d’exemple (1er jour de la semaine)", expanded=True):
        d0 = (meta.get("week") or [week[0].isoformat()])[0]
        url_rows: list[list[str]] = []
        for sid in meta.get("sources") or []:
            spec = by_id.get(sid) or next((s for s in LITURGY_SOURCES if s.id == sid), None)
            if not spec:
                continue
            url_rows.append([spec.id, spec.lang, spec.status, format_endpoint(spec, date_iso=d0)])
        st.markdown(
            '<div class="lv-lab-wrap">'
            + _html_table(["source_id", "langue", "statut", "url"], url_rows, table_id="lab_urls")
            + "</div>",
            unsafe_allow_html=True,
        )
