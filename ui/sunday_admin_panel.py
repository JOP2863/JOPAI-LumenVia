"""Panneau admin Dimanche — tableau médias interactif (cases à cocher sur les manquants)."""

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
) -> str:
    """Case prête : ✅ + lien, et éventuellement « ? » inline (popup CSS au clic)."""
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
        'gap:0.2rem;white-space:nowrap;line-height:1.2;">',
        f"<span>✅ {main}</span>",
    ]
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


_VOICE_POPUP_CSS = """
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
</style>
"""


def _voice_info_for_cell(row: LangMediaStatus, kind: str) -> str | None:
    if kind == "synth_audio":
        return (row.synth_audio_voice_info or "").strip() or None
    if kind == "readings_audio":
        return (row.readings_audio_voice_info or "").strip() or None
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
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """
    Lit les cases cochées du tableau.

    Retourne ``(sélection [(lang, kind)], notes, auto_fr_synth)``.
    """
    by_lang = {r.lang: r for r in rows}
    selected: list[tuple[str, str]] = []
    for r in rows:
        for kind, _attr, _icon in _MEDIA_KINDS:
            if _status_ready(r, kind):
                continue
            if bool(st.session_state.get(_cell_key(date_str, r.lang, kind), False)):
                selected.append((r.lang, kind))

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
    return ordered, notes, auto_fr


def plan_from_table_selection(
    *,
    rows: list[LangMediaStatus],
    selected: list[tuple[str, str]],
) -> list[str]:
    by_lang = {r.lang: r for r in rows}
    lines: list[str] = []
    for lg, kind in selected:
        label = _KIND_LABELS.get(kind, kind)
        row = by_lang.get(lg)
        extra = ""
        if lg == "FR" and kind == "synth_text" and row and not row.synth_text:
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
        "Coche les cases des médias à produire, puis **Générer la sélection**. "
        "Les ✅ ouvrent le fichier déjà publié ; clique **?** à côté d’un audio pour la voix TTS. "
        "Règle d’or : synthèse **FR** puis localisation · lectures audio = lectionnaire natif."
    )
    st.markdown(_VOICE_POPUP_CSS, unsafe_allow_html=True)
    if gs is None or not getattr(cfg, "gsheet_id", None):
        st.warning("Sheets indisponible — statut multi-langues impossible.")
        return

    rows = _load_media_rows(cfg=cfg, date_str=date_str)
    by_lang = {r.lang: r for r in rows}
    fr_row = by_lang.get("FR")
    fr_has_text = bool(fr_row and fr_row.synth_text)

    missing_keys = [
        _cell_key(date_str, r.lang, kind)
        for r in rows
        for kind, _attr, _icon in _MEDIA_KINDS
        if not _status_ready(r, kind)
    ]
    if st.session_state.pop(f"_adm_ml_select_all_{date_str}", False):
        for k in missing_keys:
            st.session_state[k] = True
    if st.session_state.pop(f"_adm_ml_deselect_all_{date_str}", False):
        for k in missing_keys:
            st.session_state[k] = False

    # En-tête
    h = st.columns([1.35, 1, 1, 1, 1])
    headers = ("Langue", "Synthèse", "Audio synthèse", "Audio lectures", "PDF")
    for col, title in zip(h, headers):
        with col:
            st.markdown(
                f"<div style='font-size:0.78rem;font-weight:700;color:#6b5918;"
                f"text-align:center;'>{html_escape(title)}</div>",
                unsafe_allow_html=True,
            )

    for r in rows:
        cols = st.columns([1.35, 1, 1, 1, 1])
        mark = " ←" if r.lang == current_lang else ""
        with cols[0]:
            st.markdown(
                f"<div style='padding-top:0.35rem;'>{lang_flag_html(r.lang, height=14)}"
                f"<strong>{html_escape(r.lang)}</strong>{html_escape(mark)}</div>",
                unsafe_allow_html=True,
            )
        for col, (kind, _attr, icon) in zip(cols[1:], _MEDIA_KINDS):
            with col:
                if _status_ready(r, kind):
                    st.markdown(
                        _ready_cell_html(
                            kind=kind,
                            url=_status_url(r, kind),
                            icon=icon,
                            voice_info=_voice_info_for_cell(r, kind),
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    help_txt = _KIND_LABELS.get(kind, kind)
                    if (
                        not fr_has_text
                        and r.lang != "FR"
                        and kind in _KINDS_NEED_FR_PIVOT
                    ):
                        help_txt += " (nécessitera la synthèse FR pivot)"
                    st.checkbox(
                        icon,
                        value=False,
                        key=_cell_key(date_str, r.lang, kind),
                        help=help_txt,
                    )

    selected, auto_notes, _auto_fr = collect_table_selection(rows=rows, date_str=date_str)
    if auto_notes:
        for note in auto_notes:
            st.info(note, icon="ℹ️")

    n_sel = len(selected)
    n_missing = len(missing_keys)
    st.caption(
        f"**{n_sel}** / {n_missing} média(s) manquant(s) sélectionné(s)."
        if n_missing
        else "Tous les médias sont déjà publiés."
    )

    b_sel, b_desel, b_gen, b_refresh = st.columns([1, 1, 1.2, 0.85])
    with b_sel:
        if st.button(
            "Tout sélectionner",
            key=f"adm_ml_table_sel_all_{date_str}",
            disabled=not missing_keys,
            use_container_width=True,
        ):
            st.session_state[f"_adm_ml_select_all_{date_str}"] = True
            st.rerun(scope="fragment")
    with b_desel:
        if st.button(
            "Tout désélectionner",
            key=f"adm_ml_table_desel_all_{date_str}",
            disabled=not missing_keys,
            use_container_width=True,
        ):
            st.session_state[f"_adm_ml_deselect_all_{date_str}"] = True
            st.rerun(scope="fragment")
    with b_refresh:
        if st.button(
            "↻ Statut",
            key=f"adm_ml_table_refresh_{date_str}",
            help="Relire GCS/Sheets (ignore le cache ~75 s)",
            use_container_width=True,
        ):
            invalidate_sunday_media_status_cache()
            st.rerun(scope="fragment")

    @st.dialog("Confirmer la génération")
    def _confirm_dialog() -> None:
        plan = list(st.session_state.get(_plan_key) or [])
        st.markdown(f"Dimanche **{date_str}** — éléments à produire :")
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
                st.rerun(scope="app")

    gen_disabled = gcs is None or not selected
    with b_gen:
        if st.button(
            "Générer la sélection",
            type="primary",
            key=f"adm_ml_table_gen_{date_str}",
            disabled=gen_disabled,
            use_container_width=True,
        ):
            if gcs is None:
                st.error("GCS indisponible.")
            else:
                st.session_state[_plan_key] = plan_from_table_selection(
                    rows=rows, selected=selected
                )
                st.session_state[_snap_key] = list(selected)
                _confirm_dialog()

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
    """Tableau des livrables : ✅ + lien si prêt, case à cocher si manquant."""
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
