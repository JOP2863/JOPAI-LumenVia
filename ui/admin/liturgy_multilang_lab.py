"""Admin — Lab lectures multi-langues : sonde une semaine sur les sources API déclarées."""

from __future__ import annotations

import base64
import html
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from core.liturgy_source_probes import LiturgyProbeResult, probe_liturgy_source
from core.liturgy_sources_registry import (
    LANG_PRIORITY,
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
        "blocks": dict(blocks),
    }


def _html_table(headers: list[str], rows: list[list[str]], *, table_id: str) -> str:
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_parts: list[str] = []
    for cells in rows:
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


def _plain_cell(c: object) -> str:
    if isinstance(c, str) and c.startswith("\x00html\x00"):
        raw = c[len("\x00html\x00") :]
        return re_strip_tags(raw)
    return "" if c is None else str(c)


def re_strip_tags(fragment: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", fragment or "").strip()


def _tsv(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["\t".join(headers)]
    for row in rows:
        lines.append("\t".join(_plain_cell(c).replace("\t", " ").replace("\n", " ") for c in row))
    return "\n".join(lines) + "\n"


def _copy_button(text: str, *, label: str, key: str) -> None:
    """Bouton HTML qui copie ``text`` dans le presse-papiers (UTF-8)."""
    b64 = base64.b64encode((text or "").encode("utf-8")).decode("ascii")
    safe_label = html.escape(label)
    components.html(
        f"""
<div style="font-family: Lora, Georgia, serif;">
  <button id="btn_{key}" style="
    background: rgba(212,175,55,0.22);
    color: #342E29;
    border: 1px solid rgba(212,175,55,0.65);
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    font-weight: 600;
  ">{safe_label}</button>
  <span id="ok_{key}" style="margin-left:0.5rem;color:#1b5e20;font-size:0.85rem;"></span>
</div>
<script>
(() => {{
  const btn = document.getElementById("btn_{key}");
  const ok = document.getElementById("ok_{key}");
  if (!btn) return;
  btn.addEventListener("click", async () => {{
    try {{
      const bin = atob("{b64}");
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const text = new TextDecoder("utf-8").decode(bytes);
      await navigator.clipboard.writeText(text);
      ok.textContent = "copié";
      setTimeout(() => {{ ok.textContent = ""; }}, 1600);
    }} catch (e) {{
      ok.textContent = "échec copie — utilise la zone JSON";
      ok.style.color = "#bf360c";
    }}
  }});
}})();
</script>
        """.strip(),
        height=46,
    )


def _lab_css() -> str:
    return """
<style>
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
  margin: 0.35rem 0 0.9rem 0;
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
.lv-lab-block-title {
  font-family: Lora, Georgia, serif;
  font-weight: 600;
  margin: 0.75rem 0 0.25rem 0;
  color: #342E29;
}
</style>
""".strip()


def _render_copyable_table(
    *,
    title: str,
    headers: list[str],
    rows: list[list[Any]],
    table_id: str,
    copy_key: str,
) -> None:
    st.markdown(f'<p class="lv-lab-block-title">{_esc(title)}</p>', unsafe_allow_html=True)
    _copy_button(_tsv(headers, rows), label="Copier ce tableau (TSV)", key=copy_key)
    st.markdown(
        '<div class="lv-lab-wrap">' + _html_table(headers, rows, table_id=table_id) + "</div>",
        unsafe_allow_html=True,
    )


def _active_lab_sources():
    """Sources sondables : production + candidats (jamais les exclus / morts)."""
    return [
        s
        for s in sources_by_priority(include_excluded=False)
        if s.status in ("production", "candidate", "unproven")
    ]


def _build_export_payload(
    *,
    meta: dict[str, Any],
    results: list[dict[str, Any]],
    synth_rows: list[list[Any]],
    url_rows: list[list[str]],
    registry_sources: list[Any],
) -> dict[str, Any]:
    return {
        "schema": "lumenvia.liturgy_lab.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lang_priority": list(LANG_PRIORITY),
        "rule": "no_house_translation_full_mass_texts_only",
        "meta": meta,
        "registry": [
            {
                "id": s.id,
                "label": s.label,
                "lang": s.lang,
                "status": s.status,
                "provides_full_mass_texts": s.provides_full_mass_texts,
                "endpoint_template": s.endpoint_template,
                "notes": s.notes,
                "license_note": s.license_note,
            }
            for s in registry_sources
        ],
        "results": results,
        "synthesis": [
            {
                "langue": r[0],
                "source_id": r[1],
                "source": r[2],
                "jours": r[3],
                "http_ok": r[4],
                "messe_ok": r[5],
                "chars_median": r[6],
                "ms_median": r[7],
            }
            for r in synth_rows
        ],
        "example_urls": [
            {"source_id": r[0], "langue": r[1], "statut": r[2], "url": r[3]} for r in url_rows
        ],
    }


def _render_adapter_smoke_test() -> None:
    """Test rapide d’un adapter qui marche (Universalis ou Evangelizo)."""
    with st.expander("Test adapter (optionnel)", expanded=False):
        kind = st.radio(
            "Adapter",
            options=["universalis", "evangelizo"],
            format_func=lambda k: {
                "universalis": "Universalis EN (JSONP)",
                "evangelizo": "Evangelizo DE / ES(SP) / IT / EN(AM)",
            }[k],
            horizontal=True,
            key="lab_adapter_kind",
        )
        t_date = st.date_input("Date", value=date.today(), key="lab_adapter_date")

        if kind == "evangelizo":
            e_lang = st.selectbox(
                "Langue Reader",
                options=["DE", "SP", "IT", "AM"],
                format_func=lambda x: {
                    "DE": "DE — allemand",
                    "SP": "SP — espagnol (produit ES)",
                    "IT": "IT — italien",
                    "AM": "AM — anglais US (produit EN)",
                }.get(x, x),
                key="lab_ev_lang",
            )
        else:
            e_lang = None
            st.caption("Horizon JSONP souvent ~4 jours ; au-delà → erreur d’horizon.")

        if not st.button("Tester", key="lab_adapter_run"):
            return

        overlay = loading_overlay("Adapter…")
        try:
            if kind == "universalis":
                from core.universalis import copyright_notice, fetch_universalis_mass, is_full_mass

                ident, texts, payload = fetch_universalis_mass(t_date.isoformat())
                full = is_full_mass(texts)
                extra = f"- Copyright : {copyright_notice(payload)[:280]}"
                sample_extra = {"copyright": copyright_notice(payload), "top_keys": sorted(payload.keys())}
            else:
                from core.evangelizo import fetch_evangelizo_mass, is_full_mass as ev_full

                ident, texts, payload = fetch_evangelizo_mass(
                    t_date.isoformat(),
                    evangelizo_lang=str(e_lang),
                )
                full = ev_full(texts)
                extra = f"- Reader lang : `{e_lang}` · zone `{ident.zone}`"
                sample_extra = {"evangelizo_lang": e_lang, "payload_keys": sorted(payload.keys())}

            st.success(
                f"{ident.jour_liturgique_nom or ident.date} · "
                f"messe complète = {'oui' if full else 'non'}"
            )
            st.markdown(
                f"- L1 : {len(texts.premiere_lecture or '')} car. · ref `{texts.premiere_lecture_ref or '—'}`\n"
                f"- Ps : {len(texts.psaume or '')} car. · ref `{texts.psaume_ref or '—'}`\n"
                f"- L2 : {len(texts.deuxieme_lecture or '')} car. · ref `{texts.deuxieme_lecture_ref or '—'}`\n"
                f"- Év : {len(texts.evangile or '')} car. · ref `{texts.evangile_ref or '—'}`\n"
                f"{extra}"
            )
            sample = {
                "date": ident.date,
                "fete": ident.fete,
                "premiere_lecture_ref": texts.premiere_lecture_ref,
                "psaume_ref": texts.psaume_ref,
                "deuxieme_lecture_ref": texts.deuxieme_lecture_ref,
                "evangile_ref": texts.evangile_ref,
                "excerpt_gospel": (texts.evangile or "")[:400],
                **sample_extra,
            }
            st.text_area(
                "JSON (copier)",
                value=json.dumps(sample, ensure_ascii=False, indent=2),
                height=160,
                key="lab_adapter_json",
            )
        except Exception as ex:
            st.error(f"{type(ex).__name__}: {ex}")
        finally:
            overlay.empty()


def _purge_stale_lab_session(active_ids: set[str]) -> bool:
    """Efface résultats / export session s’ils citent des sources hors catalogue actif.

    Évite d’afficher un vieux JSON (katholisch, USCCB…) après mise à jour du Lab.
    Retourne True si une purge a eu lieu.
    """
    meta = st.session_state.get("lab_ml_last_meta")
    results = st.session_state.get("lab_ml_last_results")
    stale = False
    if isinstance(meta, dict):
        srcs = meta.get("sources") or []
        if any(str(s) not in active_ids for s in srcs):
            stale = True
    if isinstance(results, list):
        for row in results:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("source_id") or "")
            if sid and sid not in active_ids:
                stale = True
                break
    # Ancien schéma d’export / résultats sans les champs adapter
    if not stale and isinstance(results, list) and results:
        # Universalis via ancien probe HTTP brut (kind=jsonp) = run obsolète
        for row in results:
            if not isinstance(row, dict):
                continue
            if row.get("source_id") == "universalis_mass" and row.get("kind") in ("jsonp", "html"):
                stale = True
                break
    if not stale:
        return False
    for k in ("lab_ml_last_results", "lab_ml_last_meta", "lab_ml_last_export_json"):
        st.session_state.pop(k, None)
    return True


def render_admin_liturgy_multilang_lab() -> None:
    st.title("Lab — lectures multi-langues")
    st.markdown(_lab_css(), unsafe_allow_html=True)
    st.caption(
        "Uniquement les sources qui marchent : AELF (FR), Universalis (EN), Evangelizo (DE/ES/IT/EN-AM). "
        f"Priorité : {' · '.join(LANG_PRIORITY)}. Pas de traduction maison."
    )

    catalogue = _active_lab_sources()
    by_id = {s.id: s for s in catalogue}
    active_ids = set(by_id)

    if _purge_stale_lab_session(active_ids):
        st.warning(
            "Anciens résultats Lab (sources mortes ou probe obsolète) effacés de la session. "
            "Relance **Lancer la sonde** pour un export à jour — Evangelizo doit apparaître dans `meta.sources`."
        )

    with st.expander("Sources actives", expanded=False):
        _render_copyable_table(
            title="Registre (actifs seulement)",
            headers=["id", "label", "langue", "statut", "endpoint"],
            rows=[
                [s.id, s.label, s.lang, s.status, s.endpoint_template]
                for s in catalogue
            ],
            table_id="lab_reg",
            copy_key="copy_reg",
        )

    def _fmt_source(sid: str) -> str:
        s = by_id[sid]
        return f"[{s.lang}] {s.label} — {s.status}"

    # Nettoie une éventuelle sélection session qui contenait encore des sources mortes.
    prev = st.session_state.get("lab_ml_sources")
    if isinstance(prev, list):
        cleaned = [i for i in prev if i in by_id]
        if cleaned != prev:
            st.session_state["lab_ml_sources"] = cleaned or [s.id for s in catalogue]

    selected_ids = st.multiselect(
        "Sources à sonder",
        options=[s.id for s in catalogue],
        default=[s.id for s in catalogue],
        format_func=_fmt_source,
        key="lab_ml_sources",
    )

    lang_filter = st.multiselect(
        "Langues",
        options=list(LANG_PRIORITY),
        default=list(LANG_PRIORITY),
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
        st.caption("Dimanche → semaine dim–sam · sinon semaine ISO lun–dim.")

    if not isinstance(anchor, date):
        st.warning("Date invalide.")
        return

    week = _week_dates(anchor)
    st.info("Semaine : " + " · ".join(d.isoformat() for d in week))

    _render_adapter_smoke_test()

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
                "anchor": anchor.isoformat(),
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
    st.subheader("Résultats")
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
    detail_rows: list[list[Any]] = []
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

    _render_copyable_table(
        title="Détail des sondes",
        headers=detail_headers,
        rows=detail_rows,
        table_id="lab_detail",
        copy_key="copy_detail",
    )

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

    synth_rows: list[list[Any]] = []
    for (lg, sid, label), a in sorted(agg.items()):
        chars = sorted(a["chars"])
        ms = sorted(a["ms"])
        med_chars = chars[len(chars) // 2] if chars else ""
        med_ms = ms[len(ms) // 2] if ms else ""
        synth_rows.append([lg, sid, label, a["jours"], a["http_ok"], a["messe_ok"], med_chars, med_ms])

    _render_copyable_table(
        title="Synthèse par source (semaine)",
        headers=["langue", "source_id", "source", "jours", "http_ok", "messe_ok", "chars_médian", "ms_médian"],
        rows=synth_rows,
        table_id="lab_synth",
        copy_key="copy_synth",
    )

    only_full = [
        s for s in catalogue if s.id in {r.get("source_id") for r in results} and s.provides_full_mass_texts
    ]
    if only_full:
        st.success("Messe complète déjà prouvée pour : " + ", ".join(s.label for s in only_full))

    d0 = (meta.get("week") or [week[0].isoformat()])[0]
    url_rows: list[list[str]] = []
    for sid in meta.get("sources") or []:
        spec = by_id.get(sid)
        if not spec:
            continue
        url_rows.append([spec.id, spec.lang, spec.status, format_endpoint(spec, date_iso=d0)])

    _render_copyable_table(
        title="URLs d’exemple (1er jour)",
        headers=["source_id", "langue", "statut", "url"],
        rows=url_rows,
        table_id="lab_urls",
        copy_key="copy_urls",
    )

    export = _build_export_payload(
        meta=meta,
        results=results,
        synth_rows=synth_rows,
        url_rows=url_rows,
        registry_sources=catalogue,
    )
    export_json = json.dumps(export, ensure_ascii=False, indent=2)
    st.session_state["lab_ml_last_export_json"] = export_json

    st.subheader("Export JSON (pour coller dans le chat)")
    st.caption(
        "Contient registre + meta + résultats détaillés + synthèse + URLs. "
        "C’est le format préféré pour l’analyse."
    )
    b1, b2 = st.columns([1, 1])
    with b1:
        _copy_button(export_json, label="Copier le JSON complet", key="copy_json_full")
    with b2:
        st.download_button(
            "Télécharger lab_probe.json",
            data=export_json.encode("utf-8"),
            file_name=f"lumenvia_liturgy_lab_{d0}.json",
            mime="application/json",
            key="lab_ml_dl_json",
            use_container_width=True,
        )
    st.text_area(
        "JSON (Ctrl+A puis Ctrl+C si le bouton presse-papiers est bloqué)",
        value=export_json,
        height=220,
        key="lab_ml_json_area",
    )
