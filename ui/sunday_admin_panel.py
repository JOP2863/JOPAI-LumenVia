"""Panneau admin Dimanche — statut multi-langues + génération lot."""

from __future__ import annotations

import streamlit as st

from core.liturgy_day import coerce_liturgy_pref_langue, supported_liturgy_langs
from core.sunday_media_status import LangMediaStatus, media_status_matrix, status_cell
from core.sunday_view_locale import lang_flag
from ui.components import loading_overlay
from ui.sunday_admin_flows import _run_multilang_sunday_batch


def plan_multilang_missing_items(
    *,
    rows: list[LangMediaStatus],
    selected_langs: list[str],
    want_readings: bool,
    want_synth_audio: bool,
    want_pdf: bool,
    force: bool = False,
) -> tuple[list[str], list[str], str]:
    """
    Liste ce qui sera produit pour les langues sélectionnées.

    Retourne ``(à_générer, déjà_présents, message_bloquant)``.
    """
    by_lang = {r.lang: r for r in rows}
    plan: list[str] = []
    skips: list[str] = []
    langs = [coerce_liturgy_pref_langue(x) for x in selected_langs if str(x or "").strip()]
    if not langs:
        return [], [], "Aucune langue sélectionnée dans « Langues à publier / compléter »."
    if not (want_readings or want_synth_audio or want_pdf or force):
        return [], [], "Aucune option cochée (Audio lectures / Audio synthèse / PDF)."

    for lg in langs:
        flag = lang_flag(lg)
        r = by_lang.get(lg)
        if r is None:
            plan.append(f"Synthèse texte · {flag} {lg} (statut inconnu — tentative de publication)")
            if want_synth_audio:
                plan.append(f"Audio synthèse · {flag} {lg}")
            if want_readings:
                plan.append(f"Audio lectures · {flag} {lg}")
            if want_pdf:
                plan.append(f"Fascicule PDF · {flag} {lg}")
            continue

        need_text = force or not r.synth_text
        # Texte pivot localisé si un livrable dépendant manque et que le texte n’existe pas.
        need_dependent = (
            (want_synth_audio and (force or not r.synth_audio))
            or (want_readings and (force or not r.readings_audio))
            or (want_pdf and (force or not r.pdf))
        )
        if need_text and (need_dependent or force or not r.synth_text):
            if force or not r.synth_text:
                plan.append(f"Synthèse texte · {flag} {lg}")
            else:
                skips.append(f"Synthèse texte · {flag} {lg} — déjà publié")
        elif r.synth_text and not need_dependent and not force:
            skips.append(f"Synthèse texte · {flag} {lg} — déjà publié")

        if want_synth_audio:
            if force or not r.synth_audio:
                plan.append(f"Audio synthèse · {flag} {lg}")
            else:
                skips.append(f"Audio synthèse · {flag} {lg} — déjà publié")
        if want_readings:
            if force or not r.readings_audio:
                plan.append(f"Audio lectures · {flag} {lg}")
            else:
                skips.append(f"Audio lectures · {flag} {lg} — déjà publié")
        if want_pdf:
            if force or not r.pdf:
                plan.append(f"Fascicule PDF · {flag} {lg}")
            else:
                skips.append(f"Fascicule PDF · {flag} {lg} — déjà publié")

    return plan, skips, ""


def render_sunday_multilang_admin(
    *,
    cfg: object,
    gs: object,
    gcs: object | None,
    identity: object,
    texts: object,
    date_str: str,
    current_lang: str,
    pct: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    include_catechese_pdf: bool,
) -> None:
    """Tableau des livrables par langue + lancement de génération multi-langues."""
    st.markdown("##### Médias par langue")
    st.caption(
        "Règle d’or : **lectures audio** = lectionnaire natif de chaque langue · "
        "**synthèse** rédigée en **FR** puis **traduite** (pas de nouvelle rédaction IA) · "
        "**audio synthèse** = TTS du script traduit · **PDF** = lectures natives + synthèse localisée. "
        "Clique l’icône (📝 🎧 📖 📄) pour ouvrir le fichier quand il est disponible."
    )
    if gs is None or not getattr(cfg, "gsheet_id", None):
        st.warning("Sheets indisponible — statut multi-langues impossible.")
        return

    rows = media_status_matrix(
        gs=gs, gcs=gcs, cfg=cfg, date_str=date_str, langs=supported_liturgy_langs()
    )
    lines = [
        "| Langue | Synthèse | Audio synthèse | Audio lectures | PDF |",
        "|---|:---:|:---:|:---:|:---:|",
    ]
    for r in rows:
        mark = "←" if r.lang == current_lang else ""
        lines.append(
            f"| {lang_flag(r.lang)} **{r.lang}** {mark} | "
            f"{status_cell(r.synth_text, r.synth_text_url, icon='📝')} | "
            f"{status_cell(r.synth_audio, r.synth_audio_url, icon='🎧')} | "
            f"{status_cell(r.readings_audio, r.readings_audio_url, icon='📖')} | "
            f"{status_cell(r.pdf, r.pdf_url, icon='📄')} |"
        )
    st.markdown("\n".join(lines))

    langs = supported_liturgy_langs()
    default_sel = [lg for lg in langs if lg in ("FR", current_lang)]
    selected = st.multiselect(
        "Langues à publier / compléter",
        options=list(langs),
        default=default_sel or ["FR"],
        format_func=lambda lg: f"{lang_flag(lg)} {lg}",
        key=f"adm_sunday_ml_langs_{date_str}",
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        want_readings = st.checkbox(
            "Audio lectures", value=True, key=f"adm_sunday_ml_read_{date_str}"
        )
    with c2:
        want_synth_audio = st.checkbox(
            "Audio synthèse", value=True, key=f"adm_sunday_ml_syna_{date_str}"
        )
    with c3:
        want_pdf = st.checkbox("PDF", value=True, key=f"adm_sunday_ml_pdf_{date_str}")
    with c4:
        force = st.checkbox("Forcer", value=False, key=f"adm_sunday_ml_force_{date_str}")

    _ml_plan_key = f"_adm_sunday_ml_plan_{date_str}"
    _ml_skip_key = f"_adm_sunday_ml_skip_{date_str}"
    _ml_blocker_key = f"_adm_sunday_ml_blocker_{date_str}"
    _ml_run_key = f"_adm_sunday_ml_run_confirm_{date_str}"

    @st.dialog("Confirmer — publication multi-langues")
    def _confirm_multilang_dialog() -> None:
        plan = list(st.session_state.get(_ml_plan_key) or [])
        skips = list(st.session_state.get(_ml_skip_key) or [])
        blocker = str(st.session_state.get(_ml_blocker_key) or "").strip()
        sel = list(st.session_state.get(f"adm_sunday_ml_langs_{date_str}") or [])
        sel_lbl = ", ".join(f"{lang_flag(coerce_liturgy_pref_langue(x))} {coerce_liturgy_pref_langue(x)}" for x in sel) or "—"
        st.markdown(f"Dimanche **{date_str}** · langues : {sel_lbl}.")
        if blocker:
            st.warning(blocker)
        elif not plan:
            st.info("Aucun manquant à produire avec la sélection et les options cochées.")
        else:
            st.markdown("Éléments qui seront générés :")
            st.markdown("\n".join(f"- {line}" for line in plan))
        if skips:
            st.caption("Déjà présents (ignorés) :")
            st.markdown("\n".join(f"- {line}" for line in skips))
        c_ok, c_no = st.columns(2)
        with c_ok:
            can_run = bool(plan) and not blocker
            if st.button(
                "Lancer",
                type="primary",
                key=f"adm_sunday_ml_dlg_ok_{date_str}",
                disabled=not can_run,
            ):
                st.session_state[_ml_run_key] = True
                st.session_state.pop(_ml_plan_key, None)
                st.session_state.pop(_ml_skip_key, None)
                st.session_state.pop(_ml_blocker_key, None)
                st.rerun()
        with c_no:
            if st.button("Annuler", key=f"adm_sunday_ml_dlg_no_{date_str}"):
                st.session_state.pop(_ml_plan_key, None)
                st.session_state.pop(_ml_skip_key, None)
                st.session_state.pop(_ml_blocker_key, None)
                st.rerun()

    def _open_multilang_confirm() -> None:
        plan, skips, blocker = plan_multilang_missing_items(
            rows=rows,
            selected_langs=list(selected),
            want_readings=bool(want_readings),
            want_synth_audio=bool(want_synth_audio),
            want_pdf=bool(want_pdf),
            force=bool(force),
        )
        st.session_state[_ml_plan_key] = plan
        st.session_state[_ml_skip_key] = skips
        st.session_state[_ml_blocker_key] = blocker
        _confirm_multilang_dialog()

    if st.button(
        "Publier les langues sélectionnées",
        type="primary",
        key=f"adm_sunday_ml_run_{date_str}",
        disabled=not selected or gcs is None,
    ):
        if gcs is None:
            st.error("GCS indisponible.")
            return
        _open_multilang_confirm()

    if st.session_state.pop(_ml_run_key, False):
        if gcs is None:
            st.error("GCS indisponible.")
            return
        sel_run = list(st.session_state.get(f"adm_sunday_ml_langs_{date_str}") or [])
        ov = loading_overlay("Publication multi-langues…", flush=True)
        try:
            result = _run_multilang_sunday_batch(
                cfg=cfg,
                gs=gs,
                gcs=gcs,
                identity=identity,
                texts=texts,
                langs=list(sel_run),
                generate_readings_audio=bool(
                    st.session_state.get(f"adm_sunday_ml_read_{date_str}", True)
                ),
                generate_synth_audio=bool(
                    st.session_state.get(f"adm_sunday_ml_syna_{date_str}", True)
                ),
                generate_pdf=bool(st.session_state.get(f"adm_sunday_ml_pdf_{date_str}", True)),
                include_catechese_pdf=include_catechese_pdf,
                force=bool(st.session_state.get(f"adm_sunday_ml_force_{date_str}", False)),
                ensure_fr_first=True,
                pct=pct,
                include_takeaways=include_takeaways,
                include_catechese_bridge=include_catechese_bridge,
                _overlay=ov,
            )
            level = str(result.get("level") or "info")
            msg = str(result.get("message") or "")
            if level == "success":
                st.success(msg)
            elif level == "warning":
                st.warning(msg)
            elif level == "error":
                st.error(msg)
            else:
                st.info(msg)
            st.rerun()
        finally:
            ov.empty()
