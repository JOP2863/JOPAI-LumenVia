"""Panneau admin Dimanche — tableau médias (produire / régénérer par case)."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st

from core.liturgy_day import coerce_liturgy_pref_langue, supported_liturgy_langs
from core.sunday_media_status import LangMediaStatus
from core.sunday_view_locale import lang_flag_html
from ui.components import loading_overlay
from ui.streamlit_caches import (
    invalidate_sunday_media_status_cache,
    service_account_json_fingerprint,
    sunday_media_status_matrix_cached,
)
from ui.sunday_admin_flows import _run_multilang_from_cell_selection

_MEDIA_KINDS: tuple[tuple[str, str, str], ...] = (
    ("synth_text", "synth_text", "📝"),
    ("synth_audio", "synth_audio", "🎧"),
    ("readings_audio", "readings_audio", "📖"),
    ("pdf", "pdf", "📄"),
)
_KIND_LABELS = {
    "synth_text": "Synthèse texte",
    "synth_audio": "Audio synthèse",
    "readings_audio": "Audio lectures",
    "pdf": "Fascicule PDF",
}
# Métiers qui exigent le pivot FR (localisation de synthèse).
_KINDS_NEED_FR_PIVOT = frozenset({"synth_text", "synth_audio", "pdf"})


def _cell_key(date_str: str, lang: str, kind: str) -> str:
    return f"adm_ml_cell_{date_str}_{lang}_{kind}"


def _status_ready(row: LangMediaStatus, kind: str) -> bool:
    return bool(getattr(row, kind, False))


def _status_url(row: LangMediaStatus, kind: str) -> str | None:
    return getattr(row, f"{kind}_url", None)


def _ready_cell_html(
    *,
    kind: str,
    url: str | None,
    icon: str,
    voice_info: str | None = None,
    has_ambiance: bool | None = None,
) -> str:
    """Case prête : ✅ + lien, « ? » voix, et 🎶 si bande-son (inline, nowrap)."""
    if url:
        main = (
            f'<a href="{html_escape(url)}" target="_blank" rel="noopener" '
            f'title="Ouvrir">{icon}</a>'
        )
    else:
        main = icon
    # Tout sur une seule ligne (nowrap) pour ne pas gonfler la hauteur des rangées.
    parts = [
        '<span style="display:inline-flex;align-items:center;justify-content:center;'
        'gap:0.15rem;white-space:nowrap;line-height:1.2;">',
        f"<span>✅ {main}</span>",
    ]
    if has_ambiance is True and kind in ("synth_audio", "readings_audio"):
        parts.append(
            '<span class="lv-amb" title="Bande-son / ambiance (intro · nappe · outro)" '
            'aria-label="Ambiance audio">🎶</span>'
        )
    if voice_info and kind in ("synth_audio", "readings_audio"):
        tip = html_escape(voice_info)
        parts.append(
            '<span class="lv-vq" tabindex="0" role="button" '
            'aria-label="Info voix" title="Info voix">?'
            f'<span class="lv-vq-tip">{tip}</span>'
            "</span>"
        )
    parts.append("</span>")
    return "".join(parts)


_MEDIA_STATUS_TABLE_CSS = """
<style>
.lv-vq {
  position: relative;
  display: inline-block;
  cursor: pointer;
  color: #6b5918;
  font-weight: 700;
  font-size: 0.85rem;
  line-height: 1;
  padding: 0 0.1rem;
  outline: none;
  user-select: none;
}
.lv-vq-tip {
  display: none;
  position: absolute;
  left: 50%;
  bottom: calc(100% + 0.35rem);
  transform: translateX(-50%);
  z-index: 1000;
  box-sizing: border-box;
  width: max(12rem, min(16rem, 70vw));
  padding: 0.45rem 0.55rem;
  border-radius: 8px;
  border: 1px solid rgba(212, 175, 55, 0.45);
  background: #fffdf6;
  color: #5f4f3a;
  font-size: 0.72rem;
  font-weight: 500;
  line-height: 1.35;
  white-space: normal;
  text-align: left;
  box-shadow: 0 4px 14px rgba(52, 46, 41, 0.14);
}
.lv-vq:focus .lv-vq-tip,
.lv-vq:focus-within .lv-vq-tip {
  display: block;
}
.lv-amb {
  display: inline-block;
  font-size: 0.78rem;
  line-height: 1;
  padding: 0;
  cursor: help;
  opacity: 0.92;
}
.lv-ml-miss {
  color: #9a8b6e;
  font-size: 0.75rem;
}
/* Matrice widgets : pleine largeur, sans scroller (ni X ni Y). */
div[class*="st-key-lv_ml_matrix"] {
  overflow: visible !important;
  max-width: 100%;
  margin: 0 0 0.55rem 0;
  padding: 0.35rem 0.35rem 0.65rem 0.35rem;
  border: 1px solid rgba(212, 175, 55, 0.35);
  border-radius: 10px;
  background: rgba(255, 253, 246, 0.65);
}
div[class*="st-key-lv_ml_matrix"] > div {
  min-width: 0 !important;
  width: 100% !important;
  max-height: none !important;
  overflow: visible !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stVerticalBlock"],
div[class*="st-key-lv_ml_matrix"] [data-testid="stVerticalBlockBorderWrapper"] {
  overflow: visible !important;
  max-height: none !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stHorizontalBlock"] {
  flex-wrap: nowrap !important;
  gap: 0.2rem !important;
  align-items: center !important;
  width: 100% !important;
  min-width: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="column"] {
  min-width: 0 !important;
  overflow: visible !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stMarkdown"] {
  margin-bottom: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-head {
  font-weight: 700;
  color: #6b5918;
  font-size: 0.68rem;
  text-align: center;
  line-height: 1.15;
  white-space: nowrap;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-head-lang {
  text-align: left;
  padding-left: 0.1rem;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-cell {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: flex-end;
  gap: 0.1rem;
  min-height: 1.25rem;
  white-space: nowrap;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] {
  min-height: 0 !important;
  margin: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] label {
  gap: 0 !important;
  justify-content: center !important;
  min-height: 1.1rem !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] p {
  display: none !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCaptionContainer"] {
  margin-top: 0.15rem !important;
}
</style>
"""


def _matrix_cell_html(
    *,
    row: LangMediaStatus,
    kind: str,
    icon: str,
) -> str:
    if _status_ready(row, kind):
        return _ready_cell_html(
            kind=kind,
            url=_status_url(row, kind),
            icon=icon,
            voice_info=_voice_info_for_cell(row, kind),
            has_ambiance=_ambiance_for_cell(row, kind),
        )
    return '<span class="lv-ml-miss">—</span>'


def _render_media_status_matrix(
    *,
    rows: list[LangMediaStatus],
    current_lang: str,
    date_str: str,
    fr_has_text: bool,
) -> dict[str, bool]:
    """Matrice médias : statut + case à cocher dans chaque cellule.

    Retourne ``{cell_key: cochée}`` (valeur widget, fiable dans un fragment).
    """
    headers = ("Langue", "Synthèse", "Audio synthèse", "Audio lectures", "PDF")
    col_weights = [1.05, 1.0, 1.15, 1.15, 0.95]
    checked: dict[str, bool] = {}

    with st.container(key="lv_ml_matrix"):
        head = st.columns(col_weights, gap="small")
        for i, title in enumerate(headers):
            cls = "lv-ml-head lv-ml-head-lang" if i == 0 else "lv-ml-head"
            with head[i]:
                st.markdown(
                    f'<div class="{cls}">{html_escape(title)}</div>',
                    unsafe_allow_html=True,
                )

        for r in rows:
            cols = st.columns(col_weights, gap="small")
            mark = " ←" if r.lang == current_lang else ""
            with cols[0]:
                st.markdown(
                    f'{lang_flag_html(r.lang, height=14)}'
                    f"<strong>{html_escape(r.lang)}</strong>{html_escape(mark)}",
                    unsafe_allow_html=True,
                )
            for i, (kind, _attr, icon) in enumerate(_MEDIA_KINDS):
                ready = _status_ready(r, kind)
                help_txt = _KIND_LABELS.get(kind, kind)
                if ready:
                    help_txt += " — cocher pour régénérer"
                else:
                    help_txt += " — cocher pour produire"
                    if (
                        not fr_has_text
                        and r.lang != "FR"
                        and kind in _KINDS_NEED_FR_PIVOT
                    ):
                        help_txt += " (nécessitera la synthèse FR pivot)"
                ck = _cell_key(date_str, r.lang, kind)
                if ck not in st.session_state:
                    st.session_state[ck] = False
                with cols[i + 1]:
                    status_col, check_col = st.columns([4, 1], gap="small")
                    with status_col:
                        st.markdown(
                            f'<div class="lv-ml-cell">'
                            f"{_matrix_cell_html(row=r, kind=kind, icon=icon)}"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    with check_col:
                        # Pas de value= (sinon reset à False à chaque rerun du fragment).
                        checked[ck] = bool(
                            st.checkbox(
                                "↺" if ready else "+",
                                key=ck,
                                help=help_txt,
                                label_visibility="collapsed",
                            )
                        )
    return checked


def _voice_info_for_cell(row: LangMediaStatus, kind: str) -> str | None:
    if kind == "synth_audio":
        return (row.synth_audio_voice_info or "").strip() or None
    if kind == "readings_audio":
        return (row.readings_audio_voice_info or "").strip() or None
    return None


def _ambiance_for_cell(row: LangMediaStatus, kind: str) -> bool | None:
    if kind == "synth_audio":
        return row.synth_audio_ambiance
    if kind == "readings_audio":
        return row.readings_audio_ambiance
    return None


def _load_media_rows(*, cfg: object, date_str: str) -> list[LangMediaStatus]:
    """Statut médias via cache Streamlit (évite GCS à chaque clic)."""
    sa_json = service_account_json_fingerprint(getattr(cfg, "gcp_service_account", None))
    langs = supported_liturgy_langs()
    langs_key = ",".join(langs)
    raw = sunday_media_status_matrix_cached(
        str(getattr(cfg, "gsheet_id", "") or "").strip(),
        str(date_str or "").strip(),
        str(getattr(cfg, "gcs_bucket_name", "") or "").strip(),
        sa_json,
        langs_key,
    )
    rows: list[LangMediaStatus] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            rows.append(
                LangMediaStatus(
                    lang=str(d.get("lang") or ""),
                    zone=str(d.get("zone") or ""),
                    synth_text=bool(d.get("synth_text")),
                    synth_audio=bool(d.get("synth_audio")),
                    readings_audio=bool(d.get("readings_audio")),
                    pdf=bool(d.get("pdf")),
                    gen_entity_id=str(d.get("gen_entity_id") or ""),
                    synth_text_url=d.get("synth_text_url"),
                    synth_audio_url=d.get("synth_audio_url"),
                    readings_audio_url=d.get("readings_audio_url"),
                    pdf_url=d.get("pdf_url"),
                    synth_audio_voice=d.get("synth_audio_voice"),
                    readings_audio_voice=d.get("readings_audio_voice"),
                    synth_audio_voice_info=d.get("synth_audio_voice_info"),
                    readings_audio_voice_info=d.get("readings_audio_voice_info"),
                    synth_audio_ambiance=d.get("synth_audio_ambiance"),
                    readings_audio_ambiance=d.get("readings_audio_ambiance"),
                )
            )
        except Exception:
            continue
    return rows


def _clear_sunday_preview_bundle_cache() -> None:
    for k in list(st.session_state.keys()):
        if str(k).startswith("_sunday_bundle_cache_") or str(k).startswith(
            "_sunday_readings_cache_"
        ):
            st.session_state.pop(k, None)


def collect_table_selection(
    *,
    rows: list[LangMediaStatus],
    date_str: str,
    checked: dict[str, bool] | None = None,
) -> tuple[list[tuple[str, str]], set[tuple[str, str]], list[str], bool]:
    """
    Lit les cases cochées du tableau (manquants + régénérations).

    Retourne ``(sélection, force_kinds, notes, auto_fr_synth)``.
    ``force_kinds`` = paires déjà publiées cochées pour régénération.
    """
    by_lang = {r.lang: r for r in rows}
    selected: list[tuple[str, str]] = []
    force_kinds: set[tuple[str, str]] = set()
    for r in rows:
        for kind, _attr, _icon in _MEDIA_KINDS:
            ck = _cell_key(date_str, r.lang, kind)
            if checked is not None:
                is_on = bool(checked.get(ck, False))
            else:
                is_on = bool(st.session_state.get(ck, False))
            if not is_on:
                continue
            selected.append((r.lang, kind))
            if _status_ready(r, kind):
                force_kinds.add((r.lang, kind))

    notes: list[str] = []
    auto_fr = False
    fr = by_lang.get("FR")
    fr_has_text = bool(fr and fr.synth_text)

    needs_fr_pivot = any(
        (lg != "FR" and kind in _KINDS_NEED_FR_PIVOT) for lg, kind in selected
    )
    if needs_fr_pivot and not fr_has_text:
        auto_fr = True
        if ("FR", "synth_text") not in selected:
            selected.insert(0, ("FR", "synth_text"))
            notes.append(
                "Synthèse **FR** (pivot) ajoutée automatiquement — requise pour localiser les autres langues."
            )

    # Si audio/PDF d’une langue non-FR est coché sans texte local : le batch localisera le texte.
    for lg, kind in list(selected):
        if lg == "FR" or kind == "readings_audio":
            continue
        if kind in ("synth_audio", "pdf"):
            row = by_lang.get(lg)
            if row and not row.synth_text and (lg, "synth_text") not in selected:
                selected.append((lg, "synth_text"))
                notes.append(
                    f"Synthèse texte **{lg}** ajoutée (préalable à { _KIND_LABELS.get(kind, kind) })."
                )

    # Déduplique en conservant l’ordre
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for item in selected:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered, force_kinds, notes, auto_fr


def plan_from_table_selection(
    *,
    rows: list[LangMediaStatus],
    selected: list[tuple[str, str]],
    force_kinds: set[tuple[str, str]] | None = None,
) -> list[str]:
    by_lang = {r.lang: r for r in rows}
    force = force_kinds or set()
    lines: list[str] = []
    for lg, kind in selected:
        label = _KIND_LABELS.get(kind, kind)
        row = by_lang.get(lg)
        extra = ""
        if (lg, kind) in force:
            extra = " — **régénération**"
        elif lg == "FR" and kind == "synth_text" and row and not row.synth_text:
            extra = " — rédaction Vertex (pivot)"
        elif lg != "FR" and kind == "synth_text":
            extra = " — localisation depuis FR"
        lines.append(f"- **{lg}** · {label}{extra}")
    return lines


def selection_to_batch_args(
    selected: list[tuple[str, str]],
) -> tuple[list[str], bool, bool, bool]:
    """Derive langs + flags (API de compat)."""
    langs: list[str] = []
    seen: set[str] = set()
    want_readings = False
    want_synth_audio = False
    want_pdf = False
    for lg, kind in selected:
        if lg not in seen:
            seen.add(lg)
            langs.append(lg)
        if kind == "readings_audio":
            want_readings = True
        elif kind == "synth_audio":
            want_synth_audio = True
        elif kind == "pdf":
            want_pdf = True
    if "FR" in seen:
        langs = ["FR"] + [x for x in langs if x != "FR"]
    return langs, want_readings, want_synth_audio, want_pdf


@st.fragment
def _sunday_multilang_admin_fragment(
    *,
    cfg: object,
    gs: object,
    gcs: object | None,
    identity: object,
    texts: object,
    date_str: str,
    current_lang: str,
    pct: int = 20,
    include_takeaways: bool = True,
    include_catechese_bridge: bool = True,
    include_catechese_pdf: bool = True,
) -> None:
    """Fragment isolé : cases / boutons ne relancent pas toute la page Dimanche."""
    _run_key = f"_adm_ml_table_run_{date_str}"
    _plan_key = f"_adm_ml_table_plan_{date_str}"
    _snap_key = f"_adm_ml_table_sel_snap_{date_str}"
    _flash_key = f"_adm_ml_table_flash_{date_str}"

    # 1) Génération en tête de run (avant les widgets) → overlay visible + pas de conflit session_state
    if st.session_state.pop(_run_key, False):
        sel_now = list(st.session_state.pop(_snap_key, None) or [])
        # tuples may have been stored as lists via session
        sel_now = [
            (str(a), str(b))
            for a, b in (tuple(x) if not isinstance(x, tuple) else x for x in sel_now)
        ]
        force_raw = list(
            st.session_state.pop(f"_adm_ml_table_force_{date_str}", None) or []
        )
        force_now = {
            (str(a), str(b))
            for a, b in (
                tuple(x) if not isinstance(x, tuple) else x for x in force_raw
            )
        }
        if gcs is None:
            st.session_state[_flash_key] = ("error", "GCS indisponible.")
        elif not sel_now:
            st.session_state[_flash_key] = ("warning", "Sélection vide.")
        else:
            pct_run = int(st.session_state.get(f"adm_sunday_pct_{date_str}", pct) or 20)
            take_run = bool(
                st.session_state.get(f"adm_sunday_takeaways_{date_str}", include_takeaways)
            )
            cate_run = bool(
                st.session_state.get(f"adm_sunday_catech_{date_str}", include_catechese_bridge)
            )
            cate_pdf_run = bool(
                st.session_state.get(f"pdf_catechese_{date_str}", include_catechese_pdf)
            )
            n = len(sel_now)
            ov = loading_overlay(
                f"Génération — 0/{n} · démarrage…",
                flush=True,
            )
            try:
                result = _run_multilang_from_cell_selection(
                    cfg=cfg,
                    gs=gs,
                    gcs=gcs,
                    identity=identity,
                    texts=texts,
                    selected=sel_now,
                    include_catechese_pdf=cate_pdf_run,
                    pct=pct_run,
                    include_takeaways=take_run,
                    include_catechese_bridge=cate_run,
                    force_kinds=force_now,
                    _overlay=ov,
                )
                level = str(result.get("level") or "info")
                msg = str(result.get("message") or "")
                st.session_state[_flash_key] = (level, msg)
                if level in ("success", "warning", "info"):
                    st.session_state[f"_adm_ml_deselect_all_{date_str}"] = True
                    invalidate_sunday_media_status_cache()
                    _clear_sunday_preview_bundle_cache()
                st.session_state.pop(_plan_key, None)
            finally:
                ov.empty()
            # Rafraîchir aussi l’aperçu public sous l’atelier.
            st.rerun(scope="app")

    flash = st.session_state.pop(_flash_key, None)
    if flash:
        level, msg = flash[0], flash[1]
        if level == "success":
            st.success(msg)
        elif level == "warning":
            st.warning(msg)
        elif level == "error":
            st.error(msg)
        else:
            st.info(msg)

    st.markdown("##### Médias par langue")
    st.caption(
        "Dans chaque case : ✅ / picto = déjà publié (lien) · cocher = **produire** ou **régénérer**. "
        "**?** = voix TTS · **🎶** = bande-son. "
        "Règle d’or : synthèse **FR** puis localisation · lectures audio = lectionnaire natif."
    )
    st.markdown(_MEDIA_STATUS_TABLE_CSS, unsafe_allow_html=True)
    if gs is None or not getattr(cfg, "gsheet_id", None):
        st.warning("Sheets indisponible — statut multi-langues impossible.")
        return

    rows = _load_media_rows(cfg=cfg, date_str=date_str)
    by_lang = {r.lang: r for r in rows}
    fr_row = by_lang.get("FR")
    fr_has_text = bool(fr_row and fr_row.synth_text)

    all_keys = [
        _cell_key(date_str, r.lang, kind)
        for r in rows
        for kind, _attr, _icon in _MEDIA_KINDS
    ]
    missing_keys = [
        _cell_key(date_str, r.lang, kind)
        for r in rows
        for kind, _attr, _icon in _MEDIA_KINDS
        if not _status_ready(r, kind)
    ]
    if st.session_state.pop(f"_adm_ml_select_all_{date_str}", False):
        for k in all_keys:
            st.session_state[k] = True
    if st.session_state.pop(f"_adm_ml_deselect_all_{date_str}", False):
        for k in all_keys:
            st.session_state[k] = False

    checked = _render_media_status_matrix(
        rows=rows,
        current_lang=current_lang,
        date_str=date_str,
        fr_has_text=fr_has_text,
    )

    selected, force_kinds, auto_notes, _auto_fr = collect_table_selection(
        rows=rows, date_str=date_str, checked=checked
    )
    if auto_notes:
        for note in auto_notes:
            st.info(note, icon="ℹ️")

    n_sel = len(selected)
    n_force = len(force_kinds)
    n_missing = len(missing_keys)
    if n_sel:
        parts = [f"**{n_sel}** média(s) sélectionné(s)"]
        if n_force:
            parts.append(f"dont **{n_force}** en régénération")
        if n_missing:
            parts.append(f"· {n_missing} encore manquant(s) au total")
        st.caption(" — ".join(parts) + ".")
    elif n_missing:
        st.caption(f"Aucun média sélectionné · **{n_missing}** manquant(s).")
    else:
        st.caption("Tous publiés — coche une case du tableau pour régénérer.")

    @st.dialog("Confirmer la génération")
    def _confirm_dialog() -> None:
        plan = list(st.session_state.get(_plan_key) or [])
        st.markdown(f"Dimanche **{date_str}** — éléments à produire / régénérer :")
        if not plan:
            st.info("Aucune sélection.")
        else:
            st.markdown("\n".join(plan))
        c_ok, c_no = st.columns(2)
        with c_ok:
            if st.button(
                "Lancer",
                type="primary",
                key=f"adm_ml_table_dlg_ok_{date_str}",
                disabled=not plan,
            ):
                st.session_state[_snap_key] = list(
                    st.session_state.get(_snap_key) or selected
                )
                st.session_state[_run_key] = True
                # scope=app : ferme le st.dialog (un rerun fragment le laisse ouvert).
                st.rerun(scope="app")
        with c_no:
            if st.button("Annuler", key=f"adm_ml_table_dlg_no_{date_str}"):
                st.session_state.pop(_plan_key, None)
                st.session_state.pop(_snap_key, None)
                st.session_state.pop(f"_adm_ml_table_force_{date_str}", None)
                st.rerun(scope="app")

    b_sel, b_desel, b_gen, b_refresh = st.columns([1, 1, 1.2, 0.85])
    with b_sel:
        if st.button(
            "Tout sélectionner",
            key=f"adm_ml_table_sel_all_{date_str}",
            disabled=not all_keys,
            use_container_width=True,
        ):
            st.session_state[f"_adm_ml_select_all_{date_str}"] = True
            st.rerun(scope="fragment")
    with b_desel:
        if st.button(
            "Tout désélectionner",
            key=f"adm_ml_table_desel_all_{date_str}",
            disabled=not all_keys,
            use_container_width=True,
        ):
            st.session_state[f"_adm_ml_deselect_all_{date_str}"] = True
            st.rerun(scope="fragment")
    with b_gen:
        gen_disabled = gcs is None or not selected
        if st.button(
            "Lancer la sélection",
            type="primary",
            key=f"adm_ml_table_gen_{date_str}",
            disabled=gen_disabled,
            use_container_width=True,
            help=(
                "GCS indisponible."
                if gcs is None
                else (
                    "Coche au moins un média dans le tableau."
                    if not selected
                    else "Produire ou régénérer les cases cochées."
                )
            ),
        ):
            if gcs is None:
                st.error("GCS indisponible.")
            else:
                st.session_state[_plan_key] = plan_from_table_selection(
                    rows=rows, selected=selected, force_kinds=force_kinds
                )
                st.session_state[_snap_key] = list(selected)
                st.session_state[f"_adm_ml_table_force_{date_str}"] = list(force_kinds)
                _confirm_dialog()
    with b_refresh:
        if st.button(
            "↻ Statut",
            key=f"adm_ml_table_refresh_{date_str}",
            help="Relire GCS/Sheets (ignore le cache ~75 s)",
            use_container_width=True,
        ):
            invalidate_sunday_media_status_cache()
            st.rerun(scope="fragment")

    with st.expander("Options de rédaction FR (si pivot à créer)", expanded=False):
        st.caption("Utilisées uniquement lorsque la synthèse française doit être rédigée (Vertex).")
        st.segmented_control(
            "Longueur (en % des lectures)",
            options=[10, 15, 20, 25, 30, 35, 40, 45, 50],
            default=int(pct or 20),
            format_func=lambda x: f"{x}%",
            key=f"adm_sunday_pct_{date_str}",
        )
        st.checkbox(
            "Inclure “À retenir”",
            value=bool(include_takeaways),
            key=f"adm_sunday_takeaways_{date_str}",
        )
        st.checkbox(
            "Inclure « Passerelle catéchèse »",
            value=bool(include_catechese_bridge),
            key=f"adm_sunday_catech_{date_str}",
        )
        st.checkbox(
            "Passerelle catéchèse dans le PDF",
            value=bool(include_catechese_pdf),
            key=f"pdf_catechese_{date_str}",
        )


def render_sunday_multilang_admin(
    *,
    cfg: object,
    gs: object,
    gcs: object | None,
    identity: object,
    texts: object,
    date_str: str,
    current_lang: str,
    pct: int = 20,
    include_takeaways: bool = True,
    include_catechese_bridge: bool = True,
    include_catechese_pdf: bool = True,
) -> None:
    """Tableau des livrables : ✅ + picto + case (produire / régénérer) dans chaque cellule."""
    _sunday_multilang_admin_fragment(
        cfg=cfg,
        gs=gs,
        gcs=gcs,
        identity=identity,
        texts=texts,
        date_str=date_str,
        current_lang=current_lang,
        pct=pct,
        include_takeaways=include_takeaways,
        include_catechese_bridge=include_catechese_bridge,
        include_catechese_pdf=include_catechese_pdf,
    )


# Compat : anciens appels éventuels
def plan_multilang_missing_items(
    *,
    rows: list[LangMediaStatus],
    selected_langs: list[str],
    want_readings: bool,
    want_synth_audio: bool,
    want_pdf: bool,
    force: bool = False,
) -> tuple[list[str], list[str], str]:
    """Ancienne API (sélection par langue) — conservée pour imports résiduels."""
    by_lang = {r.lang: r for r in rows}
    plan: list[str] = []
    skips: list[str] = []
    langs = [coerce_liturgy_pref_langue(x) for x in selected_langs if str(x or "").strip()]
    if not langs:
        return [], [], "Aucune langue sélectionnée."
    if not (want_readings or want_synth_audio or want_pdf or force):
        return [], [], "Aucune option cochée."
    selected_pairs: list[tuple[str, str]] = []
    for lg in langs:
        r = by_lang.get(lg)
        if r is None:
            continue
        if force or not r.synth_text:
            selected_pairs.append((lg, "synth_text"))
        if want_synth_audio and (force or not r.synth_audio):
            selected_pairs.append((lg, "synth_audio"))
        if want_readings and (force or not r.readings_audio):
            selected_pairs.append((lg, "readings_audio"))
        if want_pdf and (force or not r.pdf):
            selected_pairs.append((lg, "pdf"))
        if r.synth_text and not force:
            skips.append(f"Synthèse texte · {lg} — déjà publié")
    # Simulate FR auto
    fr = by_lang.get("FR")
    if any(lg != "FR" and k in _KINDS_NEED_FR_PIVOT for lg, k in selected_pairs):
        if not (fr and fr.synth_text) and ("FR", "synth_text") not in selected_pairs:
            selected_pairs.insert(0, ("FR", "synth_text"))
    return plan_from_table_selection(rows=rows, selected=selected_pairs), skips, ""
