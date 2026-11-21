"""Panneau admin Dimanche — tableau médias (produire / régénérer par case)."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st

from core.liturgy_day import coerce_liturgy_pref_langue, supported_liturgy_langs
from core.sunday_media_status import LangMediaStatus
from core.sunday_view_locale import lang_flag_html
from ui.components import loading_overlay
from ui.navigation import _use_compact_top_nav
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


def _row_all_key(date_str: str, lang: str) -> str:
    return f"adm_ml_row_all_{date_str}_{lang}"


def _row_toggle_pending_key(date_str: str, lang: str) -> str:
    """Valeur bool cible pour les 4 cases (run submit → appliquer avant form au rerun)."""
    return f"_adm_ml_row_toggle_{date_str}_{lang}"


def _status_ready(row: LangMediaStatus, kind: str) -> bool:
    return bool(getattr(row, kind, False))


def _row_has_any_media(row: LangMediaStatus) -> bool:
    return any(_status_ready(row, kind) for kind, _attr, _icon in _MEDIA_KINDS)


def _week_has_any_media(rows: list[LangMediaStatus]) -> bool:
    return any(_row_has_any_media(r) for r in rows)


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


_KIND_SHORT_LABELS = {
    "synth_text": "Synthèse",
    "synth_audio": "Aud. synth.",
    "readings_audio": "Aud. lect.",
    "pdf": "PDF",
}

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
/* Cartes langue : pleine largeur, pas de scroll horizontal. */
div[class*="st-key-lv_ml_matrix"] {
  overflow-x: hidden !important;
  overflow-y: visible !important;
  max-width: 100%;
  margin: 0 0 0.55rem 0;
  padding: 0.25rem 0.2rem 0.45rem 0.2rem;
  border: 1px solid rgba(212, 175, 55, 0.35);
  border-radius: 10px;
  background: rgba(255, 253, 246, 0.65);
  box-sizing: border-box;
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
  max-width: 100% !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stHorizontalBlock"] {
  gap: 0.2rem !important;
  width: 100% !important;
  min-width: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] [class*="st-key-lv_ml_row_"] > [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"] {
  flex-wrap: nowrap !important;
  align-items: stretch !important;
}
div[class*="st-key-lv_ml_kinds"] [data-testid="stHorizontalBlock"] {
  flex-wrap: wrap !important;
  align-items: stretch !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="column"] {
  min-width: 0 !important;
  overflow: visible !important;
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stMarkdown"] {
  margin-bottom: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-lang {
  font-weight: 700;
  color: #6b5918;
  font-size: 0.95rem;
  line-height: 1.3;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-kind-lab {
  font-weight: 700;
  color: #6b5918;
  font-size: 0.62rem;
  text-align: center;
  line-height: 1.15;
  margin: 0 0 0.15rem 0;
}
.lv-ml-h {
  font-weight: 700;
  color: #8a7340;
  font-size: 0.62rem;
  text-align: center;
  line-height: 1.2;
  margin: 0 0 0.35rem 0;
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
.lv-ml-kind-slot {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  width: 100%;
  box-sizing: border-box;
  min-height: 3.35rem;
  padding: 0.05rem 0.1rem 0.1rem 0.1rem;
}
.lv-ml-kind-slot .lv-ml-kind-lab {
  margin-bottom: 0.12rem;
}
.lv-ml-kind-body {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 1.35rem;
}
div[class*="st-key-lv_ml_matrix"] .lv-ml-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.05rem;
  min-height: 1.1rem;
  white-space: nowrap;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] > label {
  justify-content: center !important;
  width: 100%;
}
/* Mobile réel : hint posé par Python (UA), pas seulement media-query viewport. */
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) {
  overflow-x: hidden !important;
  max-width: 100% !important;
  box-sizing: border-box !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) [data-testid="column"] {
  display: block !important;
  min-width: 0 !important;
  max-width: 100% !important;
  flex: 1 1 auto !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) .lv-ml-mobile-kind {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem 0.55rem;
  line-height: 1.35;
  max-width: 100%;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) .lv-ml-mobile-kind .lv-ml-kind-lab {
  font-size: 0.8rem;
  font-weight: 700;
  flex: 0 0 auto;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) .lv-ml-mobile-kind .lv-ml-cell {
  flex: 1 1 auto;
  justify-content: flex-start;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) .lv-ml-mobile-stack {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  width: 100%;
  max-width: 100%;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) .lv-ml-mobile-kind-row {
  width: 100% !important;
  max-width: 100% !important;
  margin: 0.12rem 0 !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint)
  div[class*="st-key-lv_ml_mkind_"] [data-testid="stHorizontalBlock"] {
  flex-wrap: nowrap !important;
  align-items: center !important;
  width: 100% !important;
  gap: 0.35rem !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint)
  div[class*="st-key-lv_ml_mkind_"] [data-testid="column"]:first-child {
  flex: 1 1 auto !important;
  min-width: 0 !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint)
  div[class*="st-key-lv_ml_mkind_"] [data-testid="column"]:last-child {
  flex: 0 0 2.75rem !important;
  max-width: 2.75rem !important;
  display: flex !important;
  justify-content: center !important;
}
div[class*="st-key-lv_ml_matrix"]:has(.lv-ml-matrix-compact-hint) [data-testid="stCheckbox"] label {
  justify-content: center !important;
  margin: 0 auto !important;
  min-height: 2rem !important;
}
.lv-ml-form-actions-compact [data-testid="stFormSubmitButton"] button {
  min-height: 2.65rem !important;
}
span.lv-ml-matrix-compact-hint {
  display: none !important;
  height: 0 !important;
  width: 0 !important;
  overflow: hidden !important;
}
div[class*="st-key-lv_ml_row"] {
  margin: 0.2rem 0.1rem 0.45rem 0.1rem !important;
  padding: 0.35rem 0.4rem 0.45rem 0.4rem !important;
  border: 1px solid rgba(212, 175, 55, 0.28) !important;
  border-radius: 8px !important;
  background: rgba(255, 255, 255, 0.45) !important;
  box-sizing: border-box !important;
  max-width: 100% !important;
  overflow-x: hidden !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] {
  min-height: 0 !important;
  margin: 0 !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCheckbox"] label {
  gap: 0.25rem !important;
  justify-content: flex-start !important;
  min-height: 1.35rem !important;
}
div[class*="st-key-lv_ml_matrix"] [data-testid="stCaptionContainer"] {
  margin-top: 0.15rem !important;
}
/* Viewport étroit (complément du hint UA). */
@media (max-width: 720px) {
  div[class*="st-key-lv_ml_matrix"] {
    overflow-x: hidden !important;
  }
  div[class*="st-key-lv_ml_row_"] {
    padding: 0.4rem 0.35rem 0.5rem 0.35rem !important;
  }
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


def _row_keys(date_str: str, lang: str) -> list[str]:
    return [_cell_key(date_str, lang, kind) for kind, _attr, _icon in _MEDIA_KINDS]


def _render_kind_cell(
    *,
    r: LangMediaStatus,
    kind: str,
    icon: str,
    date_str: str,
    fr_has_text: bool,
    show_label: bool = True,
) -> None:
    """Case média dans un ``st.form`` : pas de rerun à chaque clic."""
    ready = _status_ready(r, kind)
    help_txt = _KIND_LABELS.get(kind, kind)
    if ready:
        help_txt += " - cocher pour régénérer"
    else:
        help_txt += " - cocher pour produire"
        if not fr_has_text and r.lang != "FR" and kind in _KINDS_NEED_FR_PIVOT:
            help_txt += " (nécessitera la synthèse FR pivot)"
    ck = _cell_key(date_str, r.lang, kind)
    if ck not in st.session_state:
        st.session_state[ck] = False
    short = _KIND_SHORT_LABELS.get(kind, kind)
    label_html = (
        f'<div class="lv-ml-kind-lab">{html_escape(short)}</div>' if show_label else ""
    )
    st.markdown(
        f'<div class="lv-ml-kind-slot">{label_html}'
        f'<div class="lv-ml-kind-body lv-ml-cell">{_matrix_cell_html(row=r, kind=kind, icon=icon)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    st.checkbox(
        "↺" if ready else "+",
        key=ck,
        help=help_txt,
        label_visibility="collapsed",
    )


def _render_kind_row_mobile(
    *,
    r: LangMediaStatus,
    kind: str,
    icon: str,
    date_str: str,
    fr_has_text: bool,
) -> None:
    """Une ligne par média : libellé + statut à gauche, case à droite (pile verticale sur téléphone)."""
    ready = _status_ready(r, kind)
    help_txt = _KIND_LABELS.get(kind, kind)
    if ready:
        help_txt += " - cocher pour régénérer"
    else:
        help_txt += " - cocher pour produire"
        if not fr_has_text and r.lang != "FR" and kind in _KINDS_NEED_FR_PIVOT:
            help_txt += " (nécessitera la synthèse FR pivot)"
    ck = _cell_key(date_str, r.lang, kind)
    if ck not in st.session_state:
        st.session_state[ck] = False
    short = _KIND_SHORT_LABELS.get(kind, kind)
    cell_html = _matrix_cell_html(row=r, kind=kind, icon=icon)
    with st.container(key=f"lv_ml_mkind_{r.lang}_{kind}"):
        left, right = st.columns(
            [6.5, 1],
            gap="small",
            vertical_alignment="center",
        )
        with left:
            st.markdown(
                f'<div class="lv-ml-mobile-kind">'
                f'<span class="lv-ml-kind-lab">{html_escape(short)}</span>'
                f'<span class="lv-ml-cell">{cell_html}</span>'
                f"</div>",
                unsafe_allow_html=True,
            )
        with right:
            st.checkbox(
                "↺" if ready else "+",
                key=ck,
                help=help_txt,
                label_visibility="collapsed",
            )


def _media_matrix_compact_mode() -> bool:
    """Layout pile (téléphone) : UA, menu dépliant, simulateur admin, Client Hint mobile."""
    if _use_compact_top_nav():
        st.session_state["lv_ml_force_compact"] = True
        return True
    if bool(st.session_state.get("admin_phone_preview")):
        return True
    try:
        hdrs = getattr(st.context, "headers", None) or {}
        ch = str(
            hdrs.get("sec-ch-ua-mobile")
            or hdrs.get("Sec-CH-UA-Mobile")
            or hdrs.get("sec-ch-ua-mobile")
            or ""
        ).strip()
        if ch in ("?1", "?1;"):
            return True
    except Exception:
        pass
    return bool(st.session_state.get("lv_ml_force_compact"))


def _render_media_status_matrix(
    *,
    rows: list[LangMediaStatus],
    current_lang: str,
    date_str: str,
    fr_has_text: bool,
) -> dict[str, bool]:
    """Cartes par langue dans le form courant.

    Retourne ``{lang: True}`` si le bouton « Toute la ligne » de cette langue a été soumis.
    """
    row_clicked: dict[str, bool] = {}
    compact = _media_matrix_compact_mode()
    _row_layout = [1.05, 1, 1, 1, 1, 1.15]

    with st.container(key="lv_ml_matrix"):
        if compact:
            st.markdown(
                '<span class="lv-ml-matrix-compact-hint" aria-hidden="true"></span>',
                unsafe_allow_html=True,
            )
        if not compact:
            hcols = st.columns(_row_layout, gap="small")
            with hcols[0]:
                st.markdown('<p class="lv-ml-h">Langue</p>', unsafe_allow_html=True)
            for i, (kind, _attr, _icon) in enumerate(_MEDIA_KINDS):
                with hcols[i + 1]:
                    short = _KIND_SHORT_LABELS.get(kind, kind)
                    st.markdown(
                        f'<p class="lv-ml-h">{html_escape(short)}</p>',
                        unsafe_allow_html=True,
                    )
            with hcols[5]:
                st.markdown('<p class="lv-ml-h">Ligne</p>', unsafe_allow_html=True)

        for r in rows:
            mark = " ←" if r.lang == current_lang else ""
            with st.container(key=f"lv_ml_row_{r.lang}"):
                if compact:
                    head_l, head_r = st.columns(
                        [1.35, 1],
                        gap="small",
                        vertical_alignment="center",
                    )
                    with head_l:
                        st.markdown(
                            f'<div class="lv-ml-lang lv-ml-mobile-head">'
                            f"{lang_flag_html(r.lang, height=14)}"
                            f"<strong>{html_escape(r.lang)}</strong>{html_escape(mark)}</div>",
                            unsafe_allow_html=True,
                        )
                    with head_r:
                        row_clicked[r.lang] = bool(
                            st.form_submit_button(
                                "Toute la ligne",
                                key=_row_all_key(date_str, r.lang),
                                use_container_width=True,
                                help=f"Basculer les 4 médias pour {r.lang}",
                            )
                        )
                    for kind, _attr, icon in _MEDIA_KINDS:
                        _render_kind_row_mobile(
                            r=r,
                            kind=kind,
                            icon=icon,
                            date_str=date_str,
                            fr_has_text=fr_has_text,
                        )
                else:
                    cols = st.columns(_row_layout, gap="small")
                    with cols[0]:
                        st.markdown(
                            f'<div class="lv-ml-lang" style="margin-top:0.65rem;">'
                            f"{lang_flag_html(r.lang, height=14)}"
                            f"<strong>{html_escape(r.lang)}</strong>{html_escape(mark)}</div>",
                            unsafe_allow_html=True,
                        )
                    for i, (kind, _attr, icon) in enumerate(_MEDIA_KINDS):
                        with cols[i + 1]:
                            _render_kind_cell(
                                r=r,
                                kind=kind,
                                icon=icon,
                                date_str=date_str,
                                fr_has_text=fr_has_text,
                                show_label=False,
                            )
                    with cols[5]:
                        row_clicked[r.lang] = bool(
                            st.form_submit_button(
                                "Toute la ligne",
                                key=_row_all_key(date_str, r.lang),
                                use_container_width=True,
                                help=f"Basculer les 4 médias pour {r.lang}",
                            )
                        )
    return row_clicked


def _media_rows_session_key(date_str: str) -> str:
    return f"_adm_ml_rows_snap_{date_str}"


def _get_media_rows(*, cfg: object, date_str: str, force: bool = False) -> list[LangMediaStatus]:
    """Statut médias : snapshot session (évite fingerprint/cache lookup à chaque interaction)."""
    sk = _media_rows_session_key(date_str)
    if force or sk not in st.session_state:
        st.session_state[sk] = _load_media_rows(cfg=cfg, date_str=date_str)
    return list(st.session_state[sk])


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
                    st.session_state.pop(_media_rows_session_key(date_str), None)
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
        "Les cases se cochent **sans recharger** (formulaire Streamlit). "
        "**Toute la ligne** bascule les 4 médias d’une langue. "
        "✅ / picto = déjà publié · cocher = produire ou régénérer. "
        "Semaine vide : ligne **FR** cochée par défaut."
    )
    st.markdown(_MEDIA_STATUS_TABLE_CSS, unsafe_allow_html=True)

    if gs is None or not getattr(cfg, "gsheet_id", None):
        st.warning("Sheets indisponible - statut multi-langues impossible.")
        return

    rows = _get_media_rows(cfg=cfg, date_str=date_str)
    by_lang = {r.lang: r for r in rows}
    fr_row = by_lang.get("FR")
    fr_has_text = bool(fr_row and fr_row.synth_text)

    all_keys = [
        _cell_key(date_str, r.lang, kind)
        for r in rows
        for kind, _attr, _icon in _MEDIA_KINDS
    ]
    n_missing = sum(
        1
        for r in rows
        for kind, _attr, _icon in _MEDIA_KINDS
        if not _status_ready(r, kind)
    )
    _fr_default_key = f"_adm_ml_fr_defaulted_{date_str}"
    # Semaine vide : cocher toute la ligne FR une seule fois (session / date).
    if (
        not _week_has_any_media(rows)
        and not st.session_state.get(_fr_default_key)
        and not st.session_state.get(f"_adm_ml_deselect_all_{date_str}")
    ):
        for kind, _attr, _icon in _MEDIA_KINDS:
            st.session_state[_cell_key(date_str, "FR", kind)] = True
        st.session_state[_fr_default_key] = True

    if st.session_state.pop(f"_adm_ml_select_all_{date_str}", False):
        for k in all_keys:
            st.session_state[k] = True
    if st.session_state.pop(f"_adm_ml_deselect_all_{date_str}", False):
        for k in all_keys:
            st.session_state[k] = False
        st.session_state[_fr_default_key] = True

    for r in rows:
        pk = _row_toggle_pending_key(date_str, r.lang)
        if pk in st.session_state:
            want = bool(st.session_state.pop(pk))
            for k in _row_keys(date_str, r.lang):
                st.session_state[k] = want

    plan_pending = list(st.session_state.get(_plan_key) or [])
    form_disabled = bool(plan_pending)
    compact_ml = _media_matrix_compact_mode()

    with st.form(key=f"adm_ml_form_{date_str}", border=False, clear_on_submit=False):
        row_clicked = _render_media_status_matrix(
            rows=rows,
            current_lang=current_lang,
            date_str=date_str,
            fr_has_text=fr_has_text,
        )
        if n_missing:
            st.caption(
                f"**{n_missing}** média(s) manquant(s) · coche puis **Lancer la sélection** "
                "(pas de rechargement à chaque case)."
            )
        else:
            st.caption("Tous publiés - coche pour régénérer, puis lance.")

        do_sel = do_desel = do_gen = do_refresh = False
        if compact_ml:
            st.markdown(
                '<div class="lv-ml-form-actions-compact">',
                unsafe_allow_html=True,
            )
            do_gen = bool(
                st.form_submit_button(
                    "Lancer la sélection",
                    type="primary",
                    disabled=gcs is None or form_disabled,
                    use_container_width=True,
                    help=(
                        "GCS indisponible."
                        if gcs is None
                        else (
                            "Confirme ou annule ci-dessous."
                            if plan_pending
                            else "Envoie la sélection cochée (confirmation ensuite)."
                        )
                    ),
                )
            )
            b_sel, b_desel = st.columns(2, gap="small")
            with b_sel:
                do_sel = bool(
                    st.form_submit_button(
                        "Tout sélectionner",
                        disabled=not all_keys or form_disabled,
                        use_container_width=True,
                    )
                )
            with b_desel:
                do_desel = bool(
                    st.form_submit_button(
                        "Tout désélectionner",
                        disabled=not all_keys or form_disabled,
                        use_container_width=True,
                    )
                )
            do_refresh = bool(
                st.form_submit_button(
                    "↻ Statut",
                    disabled=form_disabled,
                    use_container_width=True,
                    help="Relire GCS/Sheets (ignore le cache)",
                )
            )
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            b_sel, b_desel, b_gen, b_refresh = st.columns([1, 1, 1.2, 0.85])
            with b_sel:
                do_sel = st.form_submit_button(
                    "Tout sélectionner",
                    disabled=not all_keys or form_disabled,
                    use_container_width=True,
                )
            with b_desel:
                do_desel = st.form_submit_button(
                    "Tout désélectionner",
                    disabled=not all_keys or form_disabled,
                    use_container_width=True,
                )
            with b_gen:
                do_gen = st.form_submit_button(
                    "Lancer la sélection",
                    type="primary",
                    disabled=gcs is None or form_disabled,
                    use_container_width=True,
                    help=(
                        "GCS indisponible."
                        if gcs is None
                        else (
                            "Confirme ou annule ci-dessous."
                            if plan_pending
                            else "Envoie la sélection cochée (confirmation ensuite)."
                        )
                    ),
                )
            with b_refresh:
                do_refresh = st.form_submit_button(
                    "↻ Statut",
                    disabled=form_disabled,
                    use_container_width=True,
                    help="Relire GCS/Sheets (ignore le cache)",
                )

    # « Toute la ligne » : cible calculée au submit (valeurs des cases déjà en session).
    for lang, clicked in row_clicked.items():
        if clicked:
            keys = _row_keys(date_str, lang)
            want = not all(bool(st.session_state.get(k, False)) for k in keys)
            st.session_state[_row_toggle_pending_key(date_str, lang)] = want
            st.rerun(scope="fragment")
    if do_sel:
        st.session_state[f"_adm_ml_select_all_{date_str}"] = True
        st.rerun(scope="fragment")
    if do_desel:
        st.session_state[f"_adm_ml_deselect_all_{date_str}"] = True
        st.rerun(scope="fragment")
    if do_refresh:
        invalidate_sunday_media_status_cache()
        st.session_state.pop(_media_rows_session_key(date_str), None)
        st.rerun(scope="fragment")
    if do_gen:
        if gcs is None:
            st.error("GCS indisponible.")
        else:
            selected, force_kinds, auto_notes, _auto_fr = collect_table_selection(
                rows=rows, date_str=date_str
            )
            if not selected:
                st.warning("Coche au moins un média avant de lancer.")
            else:
                for note in auto_notes:
                    st.info(note, icon="ℹ️")
                st.session_state[_plan_key] = plan_from_table_selection(
                    rows=rows, selected=selected, force_kinds=force_kinds
                )
                st.session_state[_snap_key] = list(selected)
                st.session_state[f"_adm_ml_table_force_{date_str}"] = list(force_kinds)
                st.rerun(scope="fragment")

    # Confirmation inline sous le formulaire (fiable sur téléphone).
    if plan_pending:
        st.markdown(f"**Confirmer** · dimanche **{date_str}**")
        st.markdown("\n".join(str(x) for x in plan_pending))
        c_ok, c_no = st.columns(2)
        with c_ok:
            if st.button(
                "Confirmer la génération",
                type="primary",
                key=f"adm_ml_table_confirm_ok_{date_str}",
                use_container_width=True,
            ):
                st.session_state[_run_key] = True
                st.rerun(scope="app")
        with c_no:
            if st.button(
                "Annuler",
                key=f"adm_ml_table_confirm_no_{date_str}",
                use_container_width=True,
            ):
                st.session_state.pop(_plan_key, None)
                st.session_state.pop(_snap_key, None)
                st.session_state.pop(f"_adm_ml_table_force_{date_str}", None)
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
