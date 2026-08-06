"""Page publique « La Lumière du Dimanche » + flux admin génération."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape as html_escape

import streamlit as st

from core.liturgy_day import coerce_liturgy_pref_langue, supported_liturgy_langs
from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.readings_cache_loader import (
    load_liturgy_from_readings_cache,
    rdc_zone_aliases_for_pref_langue,
    rdc_zone_for_pref_langue,
    readings_cache_row_from_texts,
)
from core.sheets_db import append_immutable_row, build_gspread_client, utc_now_iso
from core.local_aelf_cache import (
    load_aelf_snapshot,
    load_aelf_snapshot_for_zones,
    persist_aelf_snapshot,
)
from core.local_bundle_cache import load_sunday_bundle, persist_sunday_bundle
from core.liturgy_theme import inject_liturgical_accent_style
from core.sunday_calendar_status import compute_month_content_status
from core.weekly_email_urls import _latest_illustration_description_from_ilus
from ui.components import loading_overlay
from ui.liturgy_render import render_liturgy_block
from ui.sunday_admin_flows import (
    _run_generate_sunday_flow,
)

_SUNDAY_FLASH_KEY = "_lumenvia_sunday_flash"


def _set_sunday_admin_flash(*, date_str: str, level: str, message: str) -> None:
    st.session_state[f"{_SUNDAY_FLASH_KEY}_{date_str}"] = {
        "level": level,
        "message": message,
    }


def _pop_sunday_admin_flash(date_str: str) -> dict[str, str] | None:
    return st.session_state.pop(f"{_SUNDAY_FLASH_KEY}_{date_str}", None)


def _show_sunday_admin_flash(date_str: str) -> None:
    payload = _pop_sunday_admin_flash(date_str)
    if not payload:
        return
    level = str(payload.get("level") or "info")
    message = str(payload.get("message") or "").strip()
    if not message:
        return
    if level == "success":
        st.success(message)
    elif level == "error":
        st.error(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.info(message)


def _sunday_identity_audio(
    data: bytes,
    mime: str,
    *,
    voice_label: str | None = None,
    accent_label: str | None = None,
) -> None:
    """Lecteur audio pleine largeur (Streamlit ≥ 1.56 : ``width='stretch'``)."""
    try:
        st.audio(data, format=mime, width="stretch")
    except TypeError:
        st.audio(data, format=mime)
    lab = (voice_label or "").strip()
    acc = (accent_label or "").strip()
    if lab or acc:
        parts: list[str] = []
        if lab:
            parts.append(f"Voix : {html_escape(lab)}")
        if acc:
            parts.append(f"Accent : {html_escape(acc)}")
        st.markdown(
            f"<p style=\"text-align:center;margin:0.2rem 0 0.55rem;line-height:1.35;"
            f"color:#5f4f3a;font-size:0.78rem;opacity:0.9;\">{' · '.join(parts)}</p>",
            unsafe_allow_html=True,
        )


def _tts_voice_display_label(voice_name: str | None) -> str | None:
    raw = (voice_name or "").strip()
    if not raw:
        return None
    try:
        from core.gemini_tts_catalog import load_gemini_tts_voice_catalog

        mapping, _ = load_gemini_tts_voice_catalog()
        return str(mapping.get(raw) or raw)
    except Exception:
        return raw


def _tts_accent_display_label(
    *,
    date_str: str,
    cible: str,
    voice_name: str | None,
) -> str | None:
    if not (voice_name or "").strip():
        return None
    try:
        from core.sunday_readings_tts import tts_french_accent_label_fr

        d = date.fromisoformat(str(date_str)[:10])
        return tts_french_accent_label_fr(
            sunday_date=d, cible=cible, voice_name=voice_name
        )
    except Exception:
        return None


def _lookup_sunday_audio_voices(
    *,
    gs: object,
    cfg: object,
    date_str: str,
    zone: str,
) -> tuple[str | None, str | None]:
    """Voix synthèse / lectures depuis la table ``audio`` (sans télécharger les fichiers)."""
    try:
        from core.sunday_existing_outputs import latest_generation_row_for_sunday, sheet_day_key
        from core.weekly_email_urls import is_readings_audio_gcs_path
        from core.sheets_db import fetch_records

        latest = latest_generation_row_for_sunday(
            gs=gs, cfg=cfg, date_str=date_str, zone=zone
        )
        if not latest:
            return None, None
        gen_eid = str(latest.get("entity_id") or "").strip()
        if not gen_eid:
            return None, None
        audios = fetch_records(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="audio",
            limit=0,
            use_cache=True,
        )
        syn_v: str | None = None
        read_v: str | None = None
        day = sheet_day_key(date_str)
        for a in audios or []:
            if str(a.get("gen_entity_id") or "").strip() != gen_eid:
                continue
            path = str(a.get("gcs_path") or "").strip()
            voice = str(a.get("voice") or "").strip() or None
            if not voice:
                continue
            if is_readings_audio_gcs_path(path):
                if read_v is None:
                    read_v = voice
            else:
                if syn_v is None:
                    syn_v = voice
        if read_v is None:
            prefix = f"AudioLectures/{day}/"
            for a in audios or []:
                path = str(a.get("gcs_path") or "").replace("\\", "/")
                if prefix in path:
                    voice = str(a.get("voice") or "").strip() or None
                    if voice:
                        read_v = voice
                        break
        return syn_v, read_v
    except Exception:
        return None, None


def render_sunday() -> None:
    import app as ap
    from core.sunday_view_locale import lang_flag

    st.title("La Lumière du Dimanche")
    cfg = load_config()

    def _normalize_aelf_text_for_cache(s: str | None) -> str:
        """
        Normalise les textes AELF pour le stockage en Sheets.

        Mode “extrême” : on supprime TOUS les retours chariot et on stocke un seul bloc.
        Le rendu (PDF / UI) se chargera ensuite du wrap et de la mise en forme.
        """
        raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        # Remplace tout whitespace (incluant \n) par des espaces, puis compacte.
        return re.sub(r"\s+", " ", raw).strip()

    def _sunday_of_week(d: date) -> date:
        """Retourne le dimanche de la semaine ISO contenant d (dimanche inclus)."""
        return d + timedelta(days=(6 - d.weekday()) % 7)

    def _readings_cache_date_key(raw: object) -> str:
        """Normalise une date Sheets vers ISO (YYYY-MM-DD) pour la recherche dans RDC."""
        s = str(raw or "").strip()
        if not s:
            return ""
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        for sep in ("/", "."):
            if sep in s[:10]:
                parts = s.replace(".", "/").split("/")
                if len(parts) == 3:
                    try:
                        if len(parts[0]) == 4:
                            return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
                        return date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
                    except Exception:
                        pass
        return s[:10]

    def _readings_have_body(prem: str | None, ps: str | None, deux: str | None, ev: str | None) -> bool:
        """True si au moins une lecture textuelle est présente (cache Sheets exploitable sans API)."""
        for x in (prem, ps, deux, ev):
            if (x or "").strip():
                return True
        return False

    # UX: l’utilisateur peut choisir n’importe quel jour ; on affiche / charge le DIMANCHE
    # de la semaine. La date est pilotée par une clé session stable (évite le décalage
    # Streamlit où le widget affiche ``value=`` mais renvoie encore l’ancienne date).
    _SUNDAY_DATE_KEY = "sunday_view_date"

    if "_lumenvia_sunday_qs" in st.session_state:
        try:
            st.session_state[_SUNDAY_DATE_KEY] = _sunday_of_week(
                date.fromisoformat(str(st.session_state.pop("_lumenvia_sunday_qs"))[:10])
            )
        except Exception:
            st.session_state.pop("_lumenvia_sunday_qs", None)

    if _SUNDAY_DATE_KEY not in st.session_state:
        st.session_state[_SUNDAY_DATE_KEY] = _sunday_of_week(date.today())
    else:
        raw_d = st.session_state.get(_SUNDAY_DATE_KEY)
        try:
            if isinstance(raw_d, datetime):
                raw_d = raw_d.date()
            elif not isinstance(raw_d, date):
                raw_d = date.fromisoformat(str(raw_d)[:10])
            st.session_state[_SUNDAY_DATE_KEY] = _sunday_of_week(raw_d)
        except Exception:
            st.session_state[_SUNDAY_DATE_KEY] = _sunday_of_week(date.today())

    account_pref = coerce_liturgy_pref_langue(st.session_state.get("pref_langue"))
    _lang_opts = list(supported_liturgy_langs())
    _lang_labels = {
        "FR": f"{lang_flag('FR')} FR — français (AELF)",
        "DE": f"{lang_flag('DE')} DE — Deutsch (Evangelizo)",
        "EN": f"{lang_flag('EN')} EN — English (Evangelizo)",
        "ES": f"{lang_flag('ES')} ES — español (Evangelizo)",
        "IT": f"{lang_flag('IT')} IT — italiano (Evangelizo)",
    }
    if "sunday_view_pref_langue" not in st.session_state:
        st.session_state["sunday_view_pref_langue"] = account_pref
    elif st.session_state.get("sunday_view_pref_langue") not in _lang_opts:
        st.session_state["sunday_view_pref_langue"] = account_pref

    col_date, col_lang = st.columns([1.2, 1])
    with col_date:
        chosen_any = st.date_input(
            "Date (dimanche de la semaine)",
            key=_SUNDAY_DATE_KEY,
        )
    with col_lang:
        pref_langue = st.selectbox(
            "Langue",
            options=_lang_opts,
            format_func=lambda lg: _lang_labels.get(lg, lg),
            key="sunday_view_pref_langue",
        )
    chosen = _sunday_of_week(chosen_any if isinstance(chosen_any, date) else date.today())
    if chosen != chosen_any:
        st.session_state[_SUNDAY_DATE_KEY] = chosen
        st.rerun()
    date_str = chosen.isoformat()

    pref_langue = coerce_liturgy_pref_langue(pref_langue)
    zone = rdc_zone_for_pref_langue(pref_langue)
    with st.expander("Source des lectures", expanded=False):
        st.caption(f"{lang_flag(pref_langue)} Zone : **{zone}** · langue **{pref_langue}**")
        if pref_langue != "FR":
            from core.evangelizo import (
                EVANGELIZO_HORIZON_DAYS,
                evangelizo_horizon_bounds,
                is_within_evangelizo_horizon,
            )

            if is_within_evangelizo_horizon(date_str):
                st.caption(
                    f"Evangelizo à la volée (fenêtre ±{EVANGELIZO_HORIZON_DAYS} j) "
                    "si absent du cache RDC — puis écriture RDC."
                )
            else:
                lo, hi = evangelizo_horizon_bounds()
                st.warning(
                    f"Date hors fenêtre Evangelizo (±{EVANGELIZO_HORIZON_DAYS} j : "
                    f"{lo.isoformat()} → {hi.isoformat()}). "
                    "Sans ligne RDC préchargée, les lectures peuvent manquer."
                )

    @st.cache_data(ttl=900, show_spinner=False, max_entries=48)
    def _month_content_status(
        *,
        gsheet_id: str,
        service_account_fp: str,
        year: int,
        month: int,
        zone: str,
        bucket_name: str | None,
    ) -> dict[str, dict[str, bool]]:
        """
        Retourne un mapping date_iso -> {text,audio,pdf,readings_audio} pour les dimanches du mois.
        Objectif : affichage indicatif (encerclage) sans empêcher la régénération.

        Optimisations : cache plus long (pas besoin temps réel), filtre rapide sur l’année affichée,
        audio rattaché uniquement aux `generations` du mois concerné.
        """
        return compute_month_content_status(
            gsheet_id=gsheet_id,
            service_account_fp=service_account_fp,
            year=year,
            month=month,
            zone=zone,
            bucket_name=bucket_name,
        )

    # Mini-calendrier HTML : dimanches encerclés si contenu déjà présent (zone = langue)
    if cfg.gcp_service_account and cfg.gsheet_id:
        try:
            try:
                qp_open_cal = str(st.query_params.get("open_cal") or "").strip().lower() in ("1", "true", "oui", "yes", "on")
            except Exception:
                qp_open_cal = False
            fp = ap._service_account_fingerprint(getattr(cfg, "gcp_service_account", {}) or {})
            bucket = str(cfg.gcs_bucket_name or "").strip() or None
            st_map = _month_content_status(
                gsheet_id=str(cfg.gsheet_id).strip(),
                service_account_fp=fp,
                year=int(chosen_any.year),
                month=int(chosen_any.month),
                zone=zone,
                bucket_name=bucket,
            )
            # Rendu HTML
            import calendar as _cal2

            cal2 = _cal2.Calendar(firstweekday=0)
            weeks = cal2.monthdatescalendar(int(chosen_any.year), int(chosen_any.month))
            mois_fr = (
                "janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"
            )[int(chosen_any.month) - 1]
            rows_html: list[str] = []
            for w in weeks:
                tds: list[str] = []
                for d in w:
                    in_month = (d.month == int(chosen_any.month))
                    ds = d.isoformat()
                    st0 = st_map.get(ds) or {}
                    is_sun = d.weekday() == 6
                    has_any = bool(
                        st0.get("text")
                        or st0.get("audio")
                        or st0.get("pdf")
                        or st0.get("readings_audio")
                    )
                    ring = "lv-ring" if (in_month and is_sun and has_any) else ("lv-sun" if (in_month and is_sun) else "")
                    muted = "lv-muted" if not in_month else ""
                    # Clique sur un dimanche avec contenu → charge ce dimanche (comme si sélectionné au date_input).
                    href = f"?sunday={ds}&open_cal=1" if (in_month and is_sun and has_any) else ""
                    inner = (
                        f"<a class='lv-daylink' href='{href}' target='_self'>{d.day}</a>"
                        if href
                        else str(d.day)
                    )
                    tds.append(
                        f"<td class='{muted}'><div class='lv-day {ring}'>{inner}</div></td>"
                    )
                rows_html.append("<tr>" + "".join(tds) + "</tr>")

            html = f"""
<div style="margin:0.35rem auto 0.15rem;max-width:min(420px,100%);width:100%;box-sizing:border-box;">
  <div style="text-align:center;color:#6b5918;font-weight:700;margin-bottom:0.25rem;font-size:0.95rem;">
    {lang_flag(pref_langue)} Dimanches déjà générés — {mois_fr} {chosen_any.year}
  </div>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid rgba(212,175,55,0.30);background:rgba(255,255,255,0.62);padding:0.25rem 0.25rem 0.35rem;">
    <table style="width:100%;min-width:260px;border-collapse:collapse;text-align:center;font-size:0.85rem;table-layout:fixed;">
      <thead>
        <tr style="opacity:0.85;">
          <th style="padding:3px 0;">L</th><th>M</th><th>M</th><th>J</th><th>V</th><th>S</th><th>D</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
    <div style="display:flex;gap:0.55rem;justify-content:center;margin-top:0.25rem;font-size:0.78rem;opacity:0.9;">
      <span><span class="lv-legend-ring"></span> Dimanche avec contenu</span>
    </div>
  </div>
</div>
<style>
.lv-day{{position:relative;display:inline-flex;align-items:center;justify-content:center;width:26px;height:22px;border-radius:9px;margin:1px auto;color:var(--liturgie-text);font-size:0.82rem;}}
@media (max-width:520px) {{
  .lv-day{{width:22px;height:20px;font-size:0.76rem;}}
}}
.lv-sun{{color:#6b5918;font-weight:600;}}
.lv-ring{{outline:1px solid var(--liturgie-accent);outline-offset:1px;border-radius:9px;}}
.lv-daylink{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:100%;color:inherit;text-decoration:none;}}
.lv-daylink:hover{{text-decoration:underline;}}
.lv-muted .lv-day{{opacity:0.35;}}
.lv-legend-ring{{display:inline-block;width:9px;height:9px;border-radius:3px;outline:1px solid var(--liturgie-accent);outline-offset:1px;margin-right:0.25rem;vertical-align:middle;}}
</style>
            """.strip()
            with st.expander(
                f"{lang_flag(pref_langue)} Voir les contenus déjà disponibles — {mois_fr} {chosen_any.year}",
                expanded=bool(qp_open_cal),
            ):
                st.markdown(html, unsafe_allow_html=True)
        except Exception:
            pass

    gcs_top: object | None = None
    if cfg.gcp_service_account and cfg.gcs_bucket_name:
        try:
            gcs_top = build_gcs_client(cfg.gcp_service_account)
        except Exception:
            gcs_top = None

    pdf_key = f"liturgy_sunday_pdf_{date_str}_{pref_langue}"
    pdf_bytes_for_user: bytes | None = st.session_state.get(pdf_key)
    if pdf_bytes_for_user is None and gcs_top and cfg.gcs_bucket_name:
        try:
            pdf_bytes_for_user = ap._fetch_existing_fascicule_pdf_bytes(
                gcs=gcs_top, cfg=cfg, date_str=date_str, pref_langue=pref_langue
            )
        except Exception:
            pdf_bytes_for_user = None

    # Lectures : RDC (zone = langue) → API (AELF / Evangelizo) → snapshot **même zone uniquement**.
    # Jamais de repli FR quand pref_langue ≠ FR (sinon les lectures restent en français).
    offline = False
    cached_at = ""
    liturgy_source_id = "aelf_france"
    rdc_zone = zone
    load_err: Exception | None = None

    def _texts_nonempty(t: object | None) -> bool:
        if t is None:
            return False
        for attr in ("premiere_lecture", "psaume", "evangile"):
            if str(getattr(t, attr, None) or "").strip():
                return True
        return False

    def _zone_matches_pref(ident: object | None, *, lg: str, want_zone: str) -> bool:
        if ident is None:
            return False

        z = str(getattr(ident, "zone", None) or "").strip().lower()
        aliases = {a.lower() for a in rdc_zone_aliases_for_pref_langue(lg)}
        aliases.add(want_zone.lower())
        if lg == "FR":
            return z in ("", "france") or z in aliases
        return z in aliases

    with st.spinner(f"{lang_flag(pref_langue)} Récupération des lectures…"):
        identity = None
        texts = None
        prev_lg_view = st.session_state.get("_sunday_liturgy_loaded_lang")
        lang_changed = bool(prev_lg_view and prev_lg_view != pref_langue)
        if lang_changed and cfg.gsheet_id:
            try:
                from core.sheets_db import invalidate_fetch_records_cache
                from ui.streamlit_caches import invalidate_adm_sheets_fetch_cache

                invalidate_fetch_records_cache(
                    spreadsheet_id=cfg.gsheet_id, table="readings_cache"
                )
                invalidate_adm_sheets_fetch_cache()
            except Exception:
                pass
        # 1) Cache Sheets RDC — zone pays (+ alias historiques evangelizo_*).
        if cfg.gcp_service_account and cfg.gsheet_id:
            try:
                gs = build_gspread_client(cfg.gcp_service_account)
                cached_rdc = load_liturgy_from_readings_cache(
                    gs=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    date_str=date_str,
                    pref_langue=pref_langue,
                )
                if cached_rdc:
                    cand_id, cand_tx = cached_rdc
                    if _texts_nonempty(cand_tx) and _zone_matches_pref(
                        cand_id, lg=pref_langue, want_zone=rdc_zone
                    ):
                        identity, texts = cand_id, cand_tx
                        liturgy_source_id = (
                            "aelf_france" if pref_langue == "FR" else f"evangelizo_rdc_{pref_langue}"
                        )
            except Exception:
                pass

        # 2) Facade multi-langues (AELF / Evangelizo) + snapshot disque + écriture RDC.
        if identity is None or texts is None or not _texts_nonempty(texts):
            identity, texts = None, None
            try:
                # Invalider l’entrée cache Streamlit pour (date, langue) si on vient de changer de langue.
                prev_lg = st.session_state.get("_sunday_liturgy_loaded_lang")
                if prev_lg and prev_lg != pref_langue:
                    try:
                        from ui.streamlit_caches import _cached_liturgy_day_raw

                        _cached_liturgy_day_raw.clear()
                    except Exception:
                        pass
                from ui.streamlit_caches import cached_liturgy_day

                identity, texts, liturgy_source_id = cached_liturgy_day(
                    date_str, pref_langue=pref_langue
                )
                st.session_state["_sunday_liturgy_loaded_lang"] = pref_langue
                if not _texts_nonempty(texts):
                    raise RuntimeError(f"Lectures vides pour {pref_langue} / {date_str}")
                from core.readings_cache_loader import rdc_source_for_pref_langue
                from dataclasses import replace as _dc_replace

                snap_zone = rdc_zone
                # Toujours écrire / exposer la zone pays canonique.
                if str(getattr(identity, "zone", None) or "") != rdc_zone:
                    try:
                        identity = _dc_replace(identity, zone=rdc_zone)
                    except Exception:
                        pass
                # Snapshot local : ne doit jamais faire échouer l’affichage des lectures.
                try:
                    persist_aelf_snapshot(date_str, snap_zone, identity, texts)
                except Exception:
                    pass
                if cfg.gcp_service_account and cfg.gsheet_id:
                    try:
                        gs2 = build_gspread_client(cfg.gcp_service_account)
                        z_write = rdc_zone
                        row = readings_cache_row_from_texts(
                            ds=date_str[:10],
                            zone=z_write,
                            identity=identity,
                            texts=texts,
                            source=rdc_source_for_pref_langue(pref_langue),
                        )
                        row["entity_id"] = sha256(
                            f"read|{date_str[:10]}|{z_write}|{utc_now_iso()}".encode("utf-8")
                        ).hexdigest()[:24]
                        append_immutable_row(
                            gspread_client=gs2,
                            spreadsheet_id=cfg.gsheet_id,
                            table="readings_cache",
                            values_by_col=row,
                        )
                        try:
                            from core.sheets_db import invalidate_fetch_records_cache
                            from ui.streamlit_caches import invalidate_adm_sheets_fetch_cache

                            invalidate_fetch_records_cache(
                                spreadsheet_id=cfg.gsheet_id, table="readings_cache"
                            )
                            invalidate_adm_sheets_fetch_cache()
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception as aelf_err:
                load_err = aelf_err
                # Snapshot local : zone canonique + alias (pas de repli france hors FR).
                snap_zones = [rdc_zone, *rdc_zone_aliases_for_pref_langue(pref_langue)]
                if pref_langue == "FR":
                    snap_zones.append("france")
                snap = load_aelf_snapshot_for_zones(date_str, snap_zones)
                if not snap:
                    has_published_bundle = False
                    if cfg.gcp_service_account and cfg.gsheet_id:
                        try:
                            gs_chk = build_gspread_client(cfg.gcp_service_account)
                            gen_row = ap._latest_generation_row_for_sunday(
                                gs=gs_chk, cfg=cfg, date_str=date_str, zone=zone
                            )
                            has_published_bundle = bool(
                                gen_row and str(gen_row.get("text_gcs_path") or "").strip()
                            )
                        except Exception:
                            has_published_bundle = False
                    from core.evangelizo import (
                        EVANGELIZO_HORIZON_DAYS,
                        evangelizo_horizon_bounds,
                        is_within_evangelizo_horizon,
                    )

                    msg = (
                        f"{lang_flag(pref_langue)} Impossible de récupérer les lectures en "
                        f"**{pref_langue}** pour le {date_str}."
                    )
                    if pref_langue != "FR" and not is_within_evangelizo_horizon(date_str):
                        lo, hi = evangelizo_horizon_bounds()
                        msg += (
                            f"\n\nEvangelizo ne livre que ±{EVANGELIZO_HORIZON_DAYS} j "
                            f"({lo.isoformat()} → {hi.isoformat()}). "
                            "Choisis un dimanche dans cette fenêtre, ou précharge le RDC quand la date y entre."
                        )
                    else:
                        msg += (
                            "\n\nRéessaie avec du réseau, ou choisis une date déjà consultée "
                            "récemment dans cette langue sur cet appareil."
                        )
                    if has_published_bundle:
                        msg += (
                            "\n\n**Note :** une synthèse / un PDF peut exister sans remplacer les lectures."
                        )
                    st.error(msg)
                    if load_err is not None:
                        st.caption(
                            f"Détail : {type(load_err).__name__} — {load_err}"
                        )
                    return
                identity, texts, cached_at = snap
                offline = True
                liturgy_source_id = (
                    "aelf_france" if pref_langue == "FR" else f"evangelizo_offline_{pref_langue}"
                )

    st.session_state["_sunday_liturgy_loaded_lang"] = pref_langue
    _preview = str(getattr(texts, "premiere_lecture", None) or "").strip().replace("\n", " ")
    if len(_preview) > 140:
        _preview = _preview[:140].rstrip() + "…"
    if _preview:
        from core.sunday_view_locale import lang_flag as _lang_flag

        st.info(
            f"{_lang_flag(pref_langue)} Lectures **{pref_langue}** (`{liturgy_source_id}`) — "
            f"aperçu : _{_preview}_",
            icon="📖",
        )
        if liturgy_source_id and "rdc" not in str(liturgy_source_id) and pref_langue != "FR":
            if "offline" in str(liturgy_source_id):
                st.caption("Source : snapshot local (hors-ligne).")
            else:
                st.caption(
                    "Source : Evangelizo à la volée (pas encore en RDC au chargement — "
                    "une ligne RDC a pu être écrite juste après)."
                )

    inject_liturgical_accent_style(getattr(identity, "couleur", None))
    if offline:
        st.caption(ap._offline_cache_caption(cached_at))

    bundle_audio: tuple[bytes, str] | None = None
    bundle_synth_text: str | None = None
    bundle_audio_gcs_path: str | None = None
    bundle_synth_voice: str | None = None
    bundle_readings_audio: tuple[bytes, str] | None = None
    bundle_readings_gcs_path: str | None = None
    bundle_readings_voice: str | None = None
    bundle_from_disk = False
    gs_top = None
    if cfg.gcp_service_account and cfg.gsheet_id and cfg.gcs_bucket_name:
        try:
            gs_top = build_gspread_client(cfg.gcp_service_account)
            if gcs_top is None:
                gcs_top = build_gcs_client(cfg.gcp_service_account)
        except Exception:
            gs_top = None

        # Synthèse et lectures : chargements indépendants (un échec ne doit pas effacer l’autre).
        if gs_top is not None and gcs_top is not None:
            try:
                syn_pack = ap._fetch_existing_sunday_bundle(
                    gs=gs_top,
                    gcs=gcs_top,
                    cfg=cfg,
                    date_str=date_str,
                    zone=zone,
                    pref_langue=pref_langue,
                )
                # Compat : anciens builds renvoyaient 3 valeurs (sans voix).
                if syn_pack and len(syn_pack) >= 4:
                    bundle_audio, bundle_synth_text, bundle_audio_gcs_path, bundle_synth_voice = syn_pack[:4]
                elif syn_pack and len(syn_pack) == 3:
                    bundle_audio, bundle_synth_text, bundle_audio_gcs_path = syn_pack
                    bundle_synth_voice = None
            except Exception:
                bundle_audio, bundle_synth_text, bundle_audio_gcs_path, bundle_synth_voice = (
                    None,
                    None,
                    None,
                    None,
                )
            try:
                read_pack = ap._fetch_existing_readings_audio(
                    gs=gs_top, gcs=gcs_top, cfg=cfg, date_str=date_str, zone=zone
                )
                if read_pack and len(read_pack) >= 3:
                    bundle_readings_audio, bundle_readings_gcs_path, bundle_readings_voice = read_pack[:3]
                elif read_pack and len(read_pack) == 2:
                    bundle_readings_audio, bundle_readings_gcs_path = read_pack
                    bundle_readings_voice = None
            except Exception:
                bundle_readings_audio, bundle_readings_gcs_path, bundle_readings_voice = (
                    None,
                    None,
                    None,
                )
            if bundle_audio or (bundle_synth_text or "").strip():
                try:
                    persist_sunday_bundle(
                        date_str=date_str,
                        zone=zone,
                        synth_text=bundle_synth_text,
                        audio_bytes=bundle_audio[0] if bundle_audio else None,
                        audio_mime=bundle_audio[1] if bundle_audio else None,
                    )
                except Exception:
                    pass

    if not bundle_audio and not (bundle_synth_text or "").strip():
        disk_bundle = load_sunday_bundle(date_str, zone)
        if disk_bundle:
            bundle_synth_text, aud_b, aud_mime, _disk_at = disk_bundle
            bundle_from_disk = True
            if aud_b and aud_mime:
                bundle_audio = (aud_b, aud_mime)

    # Voix : filet si absente (cache disque, ancien build, etc.).
    if gs_top is not None and (not bundle_synth_voice or not bundle_readings_voice):
        syn_v, read_v = _lookup_sunday_audio_voices(
            gs=gs_top, cfg=cfg, date_str=date_str, zone=zone
        )
        if not bundle_synth_voice:
            bundle_synth_voice = syn_v
        if not bundle_readings_voice:
            bundle_readings_voice = read_v

    is_admin_sunday = bool(st.session_state.get("admin_authenticated"))

    total_words = ap._count_words(
        (texts.premiere_lecture or "")
        + "\n"
        + (texts.psaume or "")
        + "\n"
        + (texts.deuxieme_lecture or "")
        + "\n"
        + (texts.evangile or "")
    )

    if is_admin_sunday:
        with st.expander(
            "Générer et Valider les contenus de la semaine sélectionnée",
            expanded=True,
        ):
            _show_sunday_admin_flash(date_str)
            st.caption(
                "Atelier de production — la page ci-dessous sert d’aperçu pour valider les contenus."
            )

            from ui.sunday_admin_panel import render_sunday_multilang_admin

            try:
                gs_ml = gs_top or build_gspread_client(cfg.gcp_service_account)
            except Exception:
                gs_ml = None
            render_sunday_multilang_admin(
                cfg=cfg,
                gs=gs_ml,
                gcs=gcs_top,
                identity=identity,
                texts=texts,
                date_str=date_str,
                current_lang=pref_langue,
                pct=int(st.session_state.get(f"adm_sunday_pct_{date_str}", 20) or 20),
                include_takeaways=bool(st.session_state.get(f"adm_sunday_takeaways_{date_str}", True)),
                include_catechese_bridge=bool(st.session_state.get(f"adm_sunday_catech_{date_str}", True)),
                include_catechese_pdf=bool(st.session_state.get(f"pdf_catechese_{date_str}", True)),
            )

            st.divider()
            pct = st.segmented_control(
                "Longueur (en % du total des lectures)",
                options=[10, 15, 20, 25, 30, 35, 40, 45, 50],
                default=20,
                format_func=lambda x: f"{x}%",
                key=f"adm_sunday_pct_{date_str}",
            )
            include_takeaways = st.checkbox(
                "Inclure “À retenir” (3–5 points)", value=True, key=f"adm_sunday_takeaways_{date_str}"
            )
            include_catechese_bridge_gen = st.checkbox(
                "Inclure « Passerelle catéchèse »",
                value=True,
                help=(
                    "Ajoute la passerelle catéchèse (5 sous-parties) en fin de synthèse. "
                    "Sa longueur (~275 mots) est fixe et indépendante du pourcentage ci-dessus."
                ),
                key=f"adm_sunday_catech_{date_str}",
            )
            st.checkbox(
                "Inclure la « Passerelle catéchèse » dans le PDF",
                value=True,
                key=f"pdf_catechese_{date_str}",
                help="Si la synthèse contient cette section, elle sera incluse dans le PDF.",
            )
            auto_pdf = st.checkbox(
                "Inclure aussi le fascicule du dimanche au format PDF",
                value=True,
                key=f"adm_sunday_auto_pdf_{date_str}",
                help=(
                    "Coché par défaut : à la fin d’une régénération / complément, "
                    "produit aussi le PDF et l’envoie sur Cloud."
                ),
            )
            audio_readings_gen = st.checkbox(
                "Audio des lectures",
                value=True,
                key=f"adm_sunday_audio_readings_{date_str}",
                help="Fichier distinct AudioLectures/… rattaché à la même génération que la synthèse.",
            )
            debug = st.toggle("Mode debug", value=False, key=f"adm_sunday_debug_{date_str}")
            admin_pref_langue = pref_langue

            if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
                st.warning("Configuration incomplète (service account / gsheet_id / bucket). Synthèse indisponible.")
            else:
                _inc_plan_key = f"_adm_sunday_inc_plan_{date_str}"
                _inc_run_key = f"_adm_sunday_inc_run_{date_str}"
                _inc_blocker_key = f"_adm_sunday_inc_blocker_{date_str}"
                _inc_skip_key = f"_adm_sunday_inc_skip_{date_str}"

                @st.dialog("Confirmer — Compléter les manquants")
                def _confirm_incremental_dialog() -> None:
                    plan = list(st.session_state.get(_inc_plan_key) or [])
                    skips = list(st.session_state.get(_inc_skip_key) or [])
                    blocker = str(st.session_state.get(_inc_blocker_key) or "").strip()
                    sel = list(st.session_state.get(f"adm_sunday_ml_langs_{date_str}") or [])
                    if not sel:
                        sel = [admin_pref_langue]
                    sel_lbl = ", ".join(
                        f"{lang_flag(coerce_liturgy_pref_langue(x))} {coerce_liturgy_pref_langue(x)}"
                        for x in sel
                    )
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
                            key=f"adm_sunday_inc_dlg_ok_{date_str}",
                            disabled=not can_run,
                        ):
                            st.session_state[_inc_run_key] = True
                            st.session_state.pop(_inc_plan_key, None)
                            st.session_state.pop(_inc_skip_key, None)
                            st.session_state.pop(_inc_blocker_key, None)
                            st.rerun()
                    with c_no:
                        if st.button("Annuler", key=f"adm_sunday_inc_dlg_no_{date_str}"):
                            st.session_state.pop(_inc_plan_key, None)
                            st.session_state.pop(_inc_skip_key, None)
                            st.session_state.pop(_inc_blocker_key, None)
                            st.rerun()

                col_inc, col_full = st.columns(2)
                with col_inc:
                    inc_clicked = st.button(
                        "Compléter les manquants",
                        type="primary",
                        key=f"adm_sunday_incremental_{date_str}",
                        help="Complète les médias manquants pour les langues du bloc "
                        "« Langues à publier / compléter » et les cases Audio/PDF de ce bloc.",
                    )
                with col_full:
                    full_clicked = st.button(
                        "Tout régénérer (long)",
                        type="secondary",
                        key=f"adm_sunday_full_{date_str}",
                        help="Nouvelle synthèse Vertex, audio synthèse, options ci-dessus — plusieurs minutes "
                        "(langue d’affichage uniquement).",
                    )
                if inc_clicked:
                    from core.sunday_media_status import media_status_matrix
                    from ui.sunday_admin_panel import plan_multilang_missing_items

                    sel_langs = list(st.session_state.get(f"adm_sunday_ml_langs_{date_str}") or [])
                    if not sel_langs:
                        sel_langs = [admin_pref_langue]
                    want_read_ml = bool(st.session_state.get(f"adm_sunday_ml_read_{date_str}", True))
                    want_syna_ml = bool(st.session_state.get(f"adm_sunday_ml_syna_{date_str}", True))
                    want_pdf_ml = bool(st.session_state.get(f"adm_sunday_ml_pdf_{date_str}", True))
                    force_ml = bool(st.session_state.get(f"adm_sunday_ml_force_{date_str}", False))
                    try:
                        status_rows = media_status_matrix(
                            gs=gs_top or build_gspread_client(cfg.gcp_service_account),
                            gcs=gcs_top,
                            cfg=cfg,
                            date_str=date_str,
                        )
                    except Exception:
                        status_rows = []
                    plan_lines, skip_lines, blocker_msg = plan_multilang_missing_items(
                        rows=status_rows,
                        selected_langs=sel_langs,
                        want_readings=want_read_ml,
                        want_synth_audio=want_syna_ml,
                        want_pdf=want_pdf_ml,
                        force=force_ml,
                    )
                    st.session_state[_inc_plan_key] = plan_lines
                    st.session_state[_inc_skip_key] = skip_lines
                    st.session_state[_inc_blocker_key] = blocker_msg
                    _confirm_incremental_dialog()

                if st.session_state.pop(_inc_run_key, False):
                    from ui.sunday_admin_flows import _run_multilang_sunday_batch

                    gcs_inc = gcs_top
                    if gcs_inc is None:
                        try:
                            gcs_inc = build_gcs_client(cfg.gcp_service_account)
                        except Exception as ex:
                            st.error(f"Connexion GCS impossible : {ex}")
                            gcs_inc = None
                    if gcs_inc:
                        sel_run = list(st.session_state.get(f"adm_sunday_ml_langs_{date_str}") or [])
                        if not sel_run:
                            sel_run = [admin_pref_langue]
                        overlay_inc = loading_overlay("Complément multi-langues…", flush=True)
                        try:
                            gs_inc = build_gspread_client(cfg.gcp_service_account)
                            include_cat_state = bool(
                                st.session_state.get(f"pdf_catechese_{date_str}", True)
                            )
                            flash = _run_multilang_sunday_batch(
                                cfg=cfg,
                                gs=gs_inc,
                                gcs=gcs_inc,
                                identity=identity,
                                texts=texts,
                                langs=list(sel_run),
                                generate_readings_audio=bool(
                                    st.session_state.get(f"adm_sunday_ml_read_{date_str}", True)
                                ),
                                generate_synth_audio=bool(
                                    st.session_state.get(f"adm_sunday_ml_syna_{date_str}", True)
                                ),
                                generate_pdf=bool(
                                    st.session_state.get(f"adm_sunday_ml_pdf_{date_str}", True)
                                ),
                                include_catechese_pdf=include_cat_state,
                                force=bool(
                                    st.session_state.get(f"adm_sunday_ml_force_{date_str}", False)
                                ),
                                ensure_fr_first=True,
                                pct=int(pct or 20),
                                include_takeaways=bool(include_takeaways),
                                include_catechese_bridge=bool(include_catechese_bridge_gen),
                                _overlay=overlay_inc,
                            )
                            _set_sunday_admin_flash(
                                date_str=date_str,
                                level=str(flash.get("level") or "info"),
                                message=str(flash.get("message") or ""),
                            )
                            if flash.get("level") in ("success", "warning"):
                                _month_content_status.clear()
                            st.rerun()
                        finally:
                            overlay_inc.empty()
                if full_clicked:
                    overlay = loading_overlay(
                        "1/4 — Préparation et synthèse écrite (Vertex)…",
                        flush=True,
                    )
                    try:
                        flow_result = _run_generate_sunday_flow(
                            _overlay=overlay,
                            identity=identity,
                            texts=texts,
                            zone=zone,
                            pref_langue=admin_pref_langue,
                            total_words=total_words,
                            pct=int(pct or 20),
                            include_takeaways=bool(include_takeaways),
                            include_catechese_bridge=bool(include_catechese_bridge_gen),
                            generate_pdf=bool(auto_pdf),
                            generate_readings_audio=bool(audio_readings_gen),
                            debug=bool(debug),
                            cfg=cfg,
                        )
                        if flow_result.get("message"):
                            _set_sunday_admin_flash(
                                date_str=date_str,
                                level=str(flow_result.get("level") or ("success" if flow_result.get("ok") else "error")),
                                message=str(flow_result.get("message") or ""),
                            )
                        if flow_result.get("ok"):
                            _month_content_status.clear()
                        st.rerun()
                    finally:
                        overlay.empty()

    from core.sunday_view_locale import sunday_ui as _sunday_ui_hero
    _hero_ui = _sunday_ui_hero(pref_langue)
    st.markdown(
        f'<h2 class="lv-sunday-identity-heading">{_hero_ui["identity"]}</h2>',
        unsafe_allow_html=True,
    )

    fete_raw = (identity.fete or "").strip() or (ap._jour_liturgique(identity) or "").strip()
    fete_line = ap._liturgy_display_label(fete_raw) if fete_raw else "—"

    from core.sunday_view_locale import (
        explain_color_localized,
        explain_cycle_localized,
        explain_time_localized,
        lang_panel_banner_html,
        lang_panel_css,
        reading_titles,
        sunday_ui,
    )

    _ui = sunday_ui(pref_langue)
    _rt = reading_titles(pref_langue)
    st.markdown(lang_panel_css(pref_langue, container_key="lv_sunday_lang_content"), unsafe_allow_html=True)
    with st.container(border=True, key="lv_sunday_lang_content"):
        st.markdown(
            lang_panel_banner_html(
                pref_langue=pref_langue,
                kind="content",
                source_id=liturgy_source_id,
            ),
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='font-size:0.95rem;line-height:1.45;color:var(--liturgie-text);'>"
            f"<strong>{identity.date}</strong> · {ap._liturgy_display_label(identity.periode)} · "
            f"Cycle {ap._cycle_year_display(identity.annee)} · {ap._liturgy_display_label(identity.couleur)}"
            f"<br/><span style='opacity:0.9'>{html_escape(_ui['feast'])} : {html_escape(fete_line)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        with st.expander(_ui["liturgy_details"], expanded=True):
            st.markdown(
                f"**{_ui['season']}** : {explain_time_localized(identity.periode, pref_langue)}"
            )
            st.markdown(
                f"**{_ui['cycle']}** : {explain_cycle_localized(identity.annee, pref_langue)}"
            )
            couleur_nom = ap._liturgy_display_label(identity.couleur)
            st.markdown(
                f"**{_ui['color']}** : **{couleur_nom}** — "
                f"{explain_color_localized(identity.couleur, pref_langue)}"
            )

        ilus_desc_view = ""
        if cfg.gcp_service_account and cfg.gsheet_id:
            try:
                gs_ilus = gs_top or build_gspread_client(cfg.gcp_service_account)
                ilus_desc_view = _latest_illustration_description_from_ilus(
                    gspread_client=gs_ilus,
                    spreadsheet_id=str(cfg.gsheet_id).strip(),
                    date_str=date_str,
                    zone=zone,
                )
            except Exception:
                ilus_desc_view = ""

        if gcs_top and cfg.gcs_bucket_name:
            try:
                from ui.sunday_liturgy_illustration import try_show_liturgy_illustration as _show_ilus

                _show_ilus(
                    gcs=gcs_top,
                    cfg=cfg,
                    date_str=date_str,
                    pref_langue=pref_langue,
                    illustration_description=ilus_desc_view or None,
                )
            except Exception:
                try:
                    ap._try_show_liturgy_illustration(
                        gcs=gcs_top, cfg=cfg, date_str=date_str
                    )
                except Exception:
                    pass
                if ilus_desc_view:
                    try:
                        from ui.sunday_liturgy_illustration import (
                            _translate_illustration_comment,
                        )

                        desc_show = _translate_illustration_comment(
                            text_fr=ilus_desc_view,
                            pref_langue=pref_langue,
                            date_str=date_str,
                            cfg=cfg,
                        )
                    except Exception:
                        desc_show = ilus_desc_view
                    st.markdown(f"**{_ui['image_comment']}**")
                    st.markdown(desc_show)

        from core.aelf_reading_meta import compose_psalm_text

        st.subheader(_ui["readings"])
        render_liturgy_block(
            _rt["premiere_lecture"],
            texts.premiere_lecture,
            intro_lue=getattr(texts, "premiere_lecture_intro", None),
            ref=getattr(texts, "premiere_lecture_ref", None),
        )
        render_liturgy_block(
            _rt["psaume"],
            compose_psalm_text(
                refrain=getattr(texts, "psaume_refrain", None),
                body=texts.psaume,
            ),
            ref=getattr(texts, "psaume_ref", None),
        )
        render_liturgy_block(
            _rt["deuxieme_lecture"],
            texts.deuxieme_lecture,
            intro_lue=getattr(texts, "deuxieme_lecture_intro", None),
            ref=getattr(texts, "deuxieme_lecture_ref", None),
        )
        render_liturgy_block(
            _rt["evangile"],
            texts.evangile,
            intro_lue=getattr(texts, "evangile_intro", None),
            ref=getattr(texts, "evangile_ref", None),
        )

    st.markdown(
        '<h2 class="lv-sunday-identity-heading">Supports du dimanche</h2>',
        unsafe_allow_html=True,
    )
    # Livrables numériques : cadre complet (filet liturgique) sous le titre.
    has_pdf_fmt = bool(pdf_bytes_for_user)
    has_audio_fmt = bundle_audio is not None
    has_text_fmt = bool((bundle_synth_text or "").strip())
    has_readings_fmt = bundle_readings_audio is not None
    n_formats = sum([has_pdf_fmt, has_audio_fmt, has_text_fmt, has_readings_fmt])
    date_prep = html_escape(ap._french_weekday_day_month_year(date_str))
    # Teintes tirées du couple or / sépia (charte liturgique) : lisibles sur fond crème, distinctes du corps #342E29.
    if n_formats <= 0:
        intro_inner = (
            f"<strong style=\"color:#6b5918;font-weight:600;\">Aucun support numérique</strong>"
            f"<span style=\"color:#5f4f3a;\"> publié pour l’instant par "
            f"<strong style=\"color:#6b5918;font-weight:600;\">{ap._jopai_mark_html()} LumenVia</strong>"
            f" pour vous préparer</span>"
            f"<span style=\"color:#5f4f3a;\"><br/>à la célébration du "
            f"<strong style=\"color:#584610;\">{date_prep}</strong>"
            f" — les lectures textuelles figurent ci-dessus.</span>"
        )
    else:
        cardinals = ("Un", "Deux", "Trois", "Quatre")
        c = cardinals[n_formats - 1]
        fmt_word = "format" if n_formats == 1 else "formats"
        disp = "disponible" if n_formats == 1 else "disponibles"
        prop = "proposé" if n_formats == 1 else "proposés"
        intro_inner = (
            f"<strong style=\"color:#6b5918;font-weight:600;\">{c} {fmt_word}</strong>"
            f"<span style=\"color:#5f4f3a;\"> {disp} {prop} par "
            f"<strong style=\"color:#6b5918;font-weight:600;\">{ap._jopai_mark_html()} LumenVia</strong>"
            f" pour vous préparer</span>"
            f"<span style=\"color:#5f4f3a;\"><br/>à la célébration du "
            f"<strong style=\"color:#584610;\">{date_prep}</strong>.</span>"
        )

    # Pastilles d’état des livrables
    def _pill(ok: bool, label: str) -> str:
        color = "#3d6b45" if ok else "#8a7a66"
        bg = "rgba(61,107,69,0.12)" if ok else "rgba(138,122,102,0.10)"
        mark = "●" if ok else "○"
        return (
            f"<span style=\"display:inline-block;margin:0.15rem 0.35rem;padding:0.2rem 0.55rem;"
            f"border-radius:999px;font-size:0.78rem;color:{color};background:{bg};\">"
            f"{mark} {html_escape(label)}</span>"
        )

    pills_html = (
        "<p style=\"text-align:center;margin:0 0 0.65rem;\">"
        + _pill(has_readings_fmt, "Lectures audio")
        + _pill(has_text_fmt, "Synthèse texte")
        + _pill(has_audio_fmt, "Synthèse audio")
        + _pill(has_pdf_fmt, "PDF")
        + "</p>"
    )

    with st.container(border=True, key="lv_sunday_deliverables_box"):
        st.markdown(pills_html, unsafe_allow_html=True)
        st.markdown(
            f"<p style=\"font-size:clamp(0.95rem, 0.35vw + 0.94rem, 1.06rem);line-height:1.52;"
            f"text-align:center;text-wrap:balance;max-width:min(42rem,calc(100% - 0.75rem));"
            f"margin:0 auto 0.85rem;color:#5f4f3a;\">{intro_inner}</p>",
            unsafe_allow_html=True,
        )

        if has_readings_fmt:
            st.markdown(
                "<p style=\"text-align:center;margin:0 0 0.35rem;line-height:1.4;color:#5f4f3a;"
                "font-size:0.95rem;\"><strong>Écouter les lectures (intégrales)</strong></p>",
                unsafe_allow_html=True,
            )
            _sunday_identity_audio(
                bundle_readings_audio[0],
                bundle_readings_audio[1],
                voice_label=_tts_voice_display_label(bundle_readings_voice),
                accent_label=_tts_accent_display_label(
                    date_str=date_str,
                    cible="lectures",
                    voice_name=bundle_readings_voice,
                ),
            )
            st.caption("Texte des lectures : section **Identité du jour** ci-dessus.")

        if has_pdf_fmt or has_text_fmt:
            col_pdf, col_texte = st.columns(2, gap="medium")
            with col_pdf:
                if has_pdf_fmt:
                    st.download_button(
                        label="Télécharger le PDF du dimanche",
                        data=pdf_bytes_for_user,
                        file_name=f"lumenvia_dimanche_{date_str}_{pref_langue}.pdf",
                        mime="application/pdf",
                        key=f"dl_sunday_top_{date_str}_{pref_langue}",
                        type="secondary",
                        use_container_width=True,
                    )
                else:
                    st.caption("PDF indisponible pour cette date.")
            with col_texte:
                with st.expander("Lire le texte de cette synthèse\n\u00a0", expanded=False):
                    if has_text_fmt:
                        st.markdown(bundle_synth_text)
                    elif has_audio_fmt:
                        st.info(
                            "Le texte de la synthèse n’est pas disponible (Cloud ou cache local). "
                            "Vérifie `text_gcs_path` dans la table generations si tu utilises le cloud."
                        )
                    else:
                        st.caption("Le texte de la synthèse n’est pas encore disponible pour cette date.")

        if has_audio_fmt:
            st.markdown(
                "<p style=\"text-align:center;margin:0.65rem 0 0.35rem;line-height:1.4;color:#5f4f3a;"
                "font-size:0.95rem;\"><strong>Audio de la synthèse</strong></p>",
                unsafe_allow_html=True,
            )
            if bundle_from_disk:
                st.markdown(
                    "<p style=\"text-align:center;margin:0 0 0.35rem;line-height:1.35;"
                    "color:#5f4f3a;font-size:0.78rem;opacity:0.88;\">En cache sur cet appareil</p>",
                    unsafe_allow_html=True,
                )
            _sunday_identity_audio(
                bundle_audio[0],
                bundle_audio[1],
                voice_label=_tts_voice_display_label(bundle_synth_voice),
                accent_label=_tts_accent_display_label(
                    date_str=date_str,
                    cible="synthese",
                    voice_name=bundle_synth_voice,
                ),
            )
        elif has_readings_fmt or has_pdf_fmt or has_text_fmt:
            st.markdown(
                "<p style=\"text-align:center;margin:0.65rem 0 0.25rem;line-height:1.4;color:#5f4f3a;"
                "font-size:0.85rem;\">Audio synthèse pas encore publié.</p>",
                unsafe_allow_html=True,
            )

        if not has_pdf_fmt and not has_audio_fmt and not has_text_fmt and not has_readings_fmt:
            _synth_na_msg = (
                "**Pas encore de supports numériques** pour ce dimanche (PDF / audio / synthèse). "
                "Les lectures textuelles sont affichées ci-dessus. "
                "Inscris-toi via **Nous rejoindre** pour être prévenu quand ils seront prêts."
            )
            if is_admin_sunday:
                _synth_na_msg += (
                    "\n\n**Admin —** utilise l’atelier **Générer et Valider** en haut de page pour publier."
                )
            st.info(_synth_na_msg, icon="📖")



