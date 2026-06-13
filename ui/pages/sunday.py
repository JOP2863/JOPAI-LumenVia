"""Page publique « La Lumière du Dimanche » + flux admin génération."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from hashlib import sha256
from html import escape as html_escape
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.pdf_liturgy_sunday import build_liturgy_sunday_pdf_bytes
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_row,
    build_gspread_client,
    ensure_table,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.storage import download_bytes, upload_bytes, upload_text
from core.local_aelf_cache import load_aelf_snapshot, persist_aelf_snapshot
from core.local_bundle_cache import load_sunday_bundle, persist_sunday_bundle
from core.liturgy_theme import inject_liturgical_accent_style, liturgical_accent_hex
from core.gcs_signed_urls import gcs_signed_url
from core.sunday_existing_outputs import pdf_synthesis_listen_url
from core.sunday_calendar_status import compute_month_content_status
from core.weekly_email_urls import _latest_illustration_description_from_ilus
from ui.components import loading_overlay
from ui.liturgy_render import render_liturgy_block
from ui.pages.about import _ABOUT_MARKDOWN
from ui.sunday_admin_flows import _run_generate_sunday_flow, _run_incremental_sunday_outputs

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


def render_sunday() -> None:
    import app as ap
    st.title("La Lumière du Dimanche")
    zone = "france"
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

    # UX: l’utilisateur peut choisir n’importe quel jour ; on affiche le DIMANCHE de la semaine.
    default = date.today()
    if "_lumenvia_sunday_qs" in st.session_state:
        try:
            default = date.fromisoformat(str(st.session_state.pop("_lumenvia_sunday_qs"))[:10])
        except Exception:
            pass
    chosen_any = st.date_input(
        "Sélectionnez une date au calendrier pour préparer ou revivre la synthèse illustrée du dimanche correspondant.",
        value=default,
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

    # Mini-calendrier HTML : dimanches encerclés si contenu déjà présent
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
    Dimanches déjà générés — {mois_fr} {chosen_any.year}
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
                f"Voir les contenus déjà disponibles — {mois_fr} {chosen_any.year}",
                expanded=bool(qp_open_cal),
            ):
                st.markdown(html, unsafe_allow_html=True)
        except Exception:
            pass
    chosen = _sunday_of_week(chosen_any)
    if chosen_any != chosen:
        d_fr = html_escape(ap._french_day_month_year(chosen.isoformat()))
        st.caption(f"Le dimanche **{d_fr}**")
    date_str = chosen.isoformat()

    gcs_top: object | None = None
    if cfg.gcp_service_account and cfg.gcs_bucket_name:
        try:
            gcs_top = build_gcs_client(cfg.gcp_service_account)
        except Exception:
            gcs_top = None

    pdf_key = f"liturgy_sunday_pdf_{date_str}"
    pdf_bytes_for_user: bytes | None = st.session_state.get(pdf_key)
    if pdf_bytes_for_user is None and gcs_top and cfg.gcs_bucket_name:
        try:
            pdf_bytes_for_user = ap._fetch_existing_fascicule_pdf_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
        except Exception:
            pdf_bytes_for_user = None

    # Lectures : on utilise d'abord un cache Sheets (si configuré), sinon AELF, sinon cache local disque.
    offline = False
    cached_at = ""
    with st.spinner("Récupération des lectures…"):
        identity = None
        texts = None
        # 1) Cache Sheets (si disponible)
        if cfg.gcp_service_account and cfg.gsheet_id:
            try:
                from core.sheets_db import TableSpec, ensure_table

                gs = build_gspread_client(cfg.gcp_service_account)
                ensure_table(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table=TableSpec(
                        name="readings_cache",
                        columns=with_concat(
                            [
                                *BASE_COLUMNS,
                                "date",
                                "zone",
                                "periode",
                                "semaine",
                                "annee",
                                "couleur",
                                "fete",
                                "jour_liturgique_nom",
                                "premiere_lecture",
                                "psaume",
                                "deuxieme_lecture",
                                "evangile",
                                "source",
                                "error",
                            ]
                        ),
                    ),
                )
                rc = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=6000)
                hits = [
                    r
                    for r in rc
                    if _readings_cache_date_key(r.get("date")) == date_str[:10]
                    and str(r.get("zone") or "").strip() == zone
                    and sheet_row_status_is_live(r.get("status"))
                    and not str(r.get("error") or "").strip()
                ]
                if hits:
                    best = sorted(hits, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
                    from core.aelf import AelfDayIdentity, AelfTexts

                    p1 = _normalize_aelf_text_for_cache(str(best.get("premiere_lecture") or "")) or None
                    ps = _normalize_aelf_text_for_cache(str(best.get("psaume") or "")) or None
                    p2 = _normalize_aelf_text_for_cache(str(best.get("deuxieme_lecture") or "")) or None
                    ev = _normalize_aelf_text_for_cache(str(best.get("evangile") or "")) or None
                    if _readings_have_body(p1, ps, p2, ev):
                        identity = AelfDayIdentity(
                            date=str(best.get("date") or date_str[:10]),
                            zone=str(best.get("zone") or zone),
                            periode=str(best.get("periode") or "") or None,
                            semaine=str(best.get("semaine") or "") or None,
                            annee=str(best.get("annee") or "") or None,
                            couleur=str(best.get("couleur") or "") or None,
                            fete=str(best.get("fete") or "") or None,
                            jour_liturgique_nom=str(best.get("jour_liturgique_nom") or "") or None,
                        )
                        texts = AelfTexts(
                            premiere_lecture=p1,
                            psaume=ps,
                            deuxieme_lecture=p2,
                            evangile=ev,
                        )
            except Exception:
                pass

        # 2) AELF API (cache streamlit) + snapshot disque — sauté si lectures déjà fournies par RDC (Sheets).
        if identity is None or texts is None:
            try:
                identity, texts = ap.cached_aelf(date_str, zone=zone, _identity_schema=4)
                persist_aelf_snapshot(date_str, zone, identity, texts)
                # Écrit dans Sheets (sans champs chiffrés) pour éviter les appels futurs.
                if cfg.gcp_service_account and cfg.gsheet_id:
                    try:
                        gs2 = build_gspread_client(cfg.gcp_service_account)
                        append_immutable_row(
                            gspread_client=gs2,
                            spreadsheet_id=cfg.gsheet_id,
                            table="readings_cache",
                            values_by_col={
                                "entity_id": sha256(f"read|{date_str[:10]}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "date": date_str[:10],
                                "zone": zone,
                                "periode": getattr(identity, "periode", None) or "",
                                "semaine": getattr(identity, "semaine", None) or "",
                                "annee": getattr(identity, "annee", None) or "",
                                "couleur": getattr(identity, "couleur", None) or "",
                                "fete": getattr(identity, "fete", None) or "",
                                "jour_liturgique_nom": getattr(identity, "jour_liturgique_nom", None) or "",
                                "premiere_lecture": _normalize_aelf_text_for_cache(texts.premiere_lecture),
                                "psaume": _normalize_aelf_text_for_cache(texts.psaume),
                                "deuxieme_lecture": _normalize_aelf_text_for_cache(texts.deuxieme_lecture),
                                "evangile": _normalize_aelf_text_for_cache(texts.evangile),
                                "source": "aelf_api",
                                "error": "",
                            },
                        )
                    except Exception:
                        pass
            except Exception as aelf_err:
                snap = load_aelf_snapshot(date_str, zone)
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
                    msg = (
                        "Impossible de joindre l’API AELF pour ce jour, et aucune copie locale n’est encore disponible. "
                        "Réessaie avec du réseau, ou choisis une date déjà consultée récemment sur cet appareil."
                    )
                    if has_published_bundle:
                        msg += (
                            "\n\n**Note :** le calendrier peut indiquer une synthèse, un audio ou un PDF déjà publiés "
                            "pour ce dimanche — cela ne remplace pas les lectures liturgiques AELF. "
                            "En administration, ouvre **Cache lectures** et précharge le mois concerné "
                            "(table `readings_cache` / **RDC**)."
                        )
                    st.error(msg)
                    if st.session_state.get("admin_authenticated"):
                        st.caption(f"Détail technique (admin) : {type(aelf_err).__name__} — {aelf_err}")
                    return
                identity, texts, cached_at = snap
                offline = True

    inject_liturgical_accent_style(getattr(identity, "couleur", None))
    if offline:
        st.caption(ap._offline_cache_caption(cached_at))

    bundle_audio: tuple[bytes, str] | None = None
    bundle_synth_text: str | None = None
    bundle_audio_gcs_path: str | None = None
    bundle_readings_audio: tuple[bytes, str] | None = None
    bundle_readings_gcs_path: str | None = None
    bundle_from_disk = False
    if cfg.gcp_service_account and cfg.gsheet_id and cfg.gcs_bucket_name:
        try:
            gs_top = build_gspread_client(cfg.gcp_service_account)
            if gcs_top is None:
                gcs_top = build_gcs_client(cfg.gcp_service_account)
            bundle_audio, bundle_synth_text, bundle_audio_gcs_path = ap._fetch_existing_sunday_bundle(
                gs=gs_top, gcs=gcs_top, cfg=cfg, date_str=date_str, zone=zone
            )
            bundle_readings_audio, bundle_readings_gcs_path = ap._fetch_existing_readings_audio(
                gs=gs_top, gcs=gcs_top, cfg=cfg, date_str=date_str, zone=zone
            )
            if bundle_audio or (bundle_synth_text or "").strip():
                persist_sunday_bundle(
                    date_str=date_str,
                    zone=zone,
                    synth_text=bundle_synth_text,
                    audio_bytes=bundle_audio[0] if bundle_audio else None,
                    audio_mime=bundle_audio[1] if bundle_audio else None,
                )
        except Exception:
            bundle_audio, bundle_synth_text, bundle_audio_gcs_path = None, None, None
            bundle_readings_audio, bundle_readings_gcs_path = None, None

    if not bundle_audio and not (bundle_synth_text or "").strip():
        disk_bundle = load_sunday_bundle(date_str, zone)
        if disk_bundle:
            bundle_synth_text, aud_b, aud_mime, _disk_at = disk_bundle
            bundle_from_disk = True
            if aud_b and aud_mime:
                bundle_audio = (aud_b, aud_mime)

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

    st.subheader("Identité du jour")
    with st.container():
        # Formats publiés : intro selon le nombre réel, puis Pdf / Audio synthèse / Texte (colonnes) + bloc lectures audio.
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
                f" — les lectures textuelles figurent plus bas.</span>"
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
            st.audio(bundle_readings_audio[0], format=bundle_readings_audio[1])

        col_pdf, col_audio, col_texte = st.columns([1, 1.25, 1], gap="medium")
        with col_pdf:
            if has_pdf_fmt:
                st.download_button(
                    label="Télécharger le PDF du dimanche",
                    data=pdf_bytes_for_user,
                    file_name=f"lumenvia_dimanche_{date_str}.pdf",
                    mime="application/pdf",
                    key=f"dl_sunday_top_{date_str}",
                    type="secondary",
                    use_container_width=True,
                )
            else:
                st.caption("Indisponible pour cette date.")
        with col_audio:
            if has_audio_fmt:
                st.markdown(
                    "<p style=\"text-align:center;margin:0 0 0.3rem;line-height:1.35;color:#5f4f3a;"
                    "font-size:0.95rem;\"><strong>Audio de la synthèse</strong></p>",
                    unsafe_allow_html=True,
                )
                if bundle_from_disk:
                    st.markdown(
                        "<p style=\"text-align:center;margin:0 0 0.35rem;line-height:1.35;"
                        "color:#5f4f3a;font-size:0.78rem;opacity:0.88;\">En cache sur cet appareil</p>",
                        unsafe_allow_html=True,
                    )
                st.audio(bundle_audio[0], format=bundle_audio[1])
            else:
                st.markdown(
                    "<p style=\"text-align:center;margin:0 0 0.25rem;line-height:1.4;color:#5f4f3a;"
                    "font-size:0.85rem;\">Pas encore publié. Les lectures sont affichées plus bas.</p>",
                    unsafe_allow_html=True,
                )
        with col_texte:
            with st.expander("Lire le texte de cette synthèse", expanded=False):
                if has_text_fmt:
                    st.markdown(bundle_synth_text)
                elif has_audio_fmt:
                    st.info(
                        "Le texte de la synthèse n’est pas disponible (Cloud ou cache local). "
                        "Vérifie `text_gcs_path` dans la table generations si tu utilises le cloud."
                    )
                else:
                    st.caption("Le texte de la synthèse n’est pas encore disponible pour cette date.")

        if not has_pdf_fmt and not has_audio_fmt and not has_text_fmt and not has_readings_fmt:
            _synth_na_msg = (
                "Pour le moment, **seules les lectures** du dimanche sont disponibles sur cette page : "
                "la synthèse (texte et audio) réalisée avec l’aide de l’IA n’a pas encore été publiée.\n\n"
                "Si vous vous êtes **inscrit au service** depuis la rubrique **« Nous rejoindre »**, "
                "vous recevrez une **notification automatique** lorsqu’elle sera prête — en général "
                "**quelques jours avant** la célébration."
            )
            if is_admin_sunday:
                _synth_na_msg += (
                    "\n\n**Administrateur —** C’est le message vu par tous les visiteurs tant qu’il n’y a ni synthèse "
                    "ni PDF. Tu peux **générer la synthèse et l’audio**, puis **préparer le fascicule PDF**, "
                    "dans les blocs **Administration** affichés juste ci‑dessous."
                )
            st.info(_synth_na_msg, icon="📖")
        if is_admin_sunday:
            st.divider()
            if gcs_top and cfg.gcs_bucket_name:
                prep_key = f"prep_liturgy_pdf_{date_str}"
                st.caption("Administration — fascicule PDF")
                has_any_synthesis = bool((bundle_synth_text or "").strip()) or (bundle_audio is not None)
                if not has_any_synthesis:
                    st.info(
                        "Le fascicule PDF « complet » (avec synthèse) n’a pas encore de contenu : "
                        "génère d’abord **la synthèse et l’audio** ci‑dessous. "
                        "Sinon, le PDF contiendrait essentiellement les lectures.",
                        icon="ℹ️",
                    )
                include_catechese_pdf = st.checkbox(
                    "Inclure la « Passerelle catéchèse — L’écho des paraboles » dans le PDF",
                    value=True,
                    key=f"pdf_catechese_{date_str}",
                    help="Si la synthèse contient cette section, elle sera incluse dans le PDF (coché par défaut).",
                )
                force_regen_pdf = st.checkbox(
                    "Régénérer le PDF (ignorer le PDF déjà stocké sur Cloud)",
                    value=False,
                    key=f"pdf_force_regen_{date_str}",
                )
                can_build_pdf = bool(has_any_synthesis)
                if st.button("Préparer le PDF du dimanche (complet)", key=prep_key, disabled=not can_build_pdf):
                    ov_pdf = loading_overlay("Préparation du PDF (couverture + lectures + synthèse)…")
                    try:
                        if not force_regen_pdf:
                            cached_pdf = ap._fetch_existing_fascicule_pdf_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
                            if cached_pdf:
                                st.session_state[pdf_key] = cached_pdf
                                st.info("PDF déjà généré — réutilisation depuis Cloud.")
                                cached_pdf = None
                        img_b = ap._fetch_liturgy_illustration_full_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
                        _base_pub = ""
                        try:
                            s = st.secrets
                            _base_pub = str(
                                s.get("PUBLIC_APP_URL") or s.get("public_app_url") or ""
                            ).strip()
                        except Exception:
                            pass
                        _gen_eid = ""
                        if gs_top:
                            try:
                                _gr = ap._latest_generation_row_for_sunday(
                                    gs=gs_top,
                                    cfg=cfg,
                                    date_str=date_str,
                                    zone=zone,
                                )
                                if _gr:
                                    _gen_eid = str(_gr.get("entity_id") or "").strip()
                            except Exception:
                                _gen_eid = ""
                        aud_url, aud_note = pdf_synthesis_listen_url(
                            date_str=date_str,
                            public_app_url=_base_pub or None,
                            gcs=gcs_top,
                            bucket_name=str(cfg.gcs_bucket_name).strip(),
                            gcs_audio_path=bundle_audio_gcs_path,
                            gs=gs_top,
                            cfg=cfg,
                            gen_entity_id=_gen_eid or None,
                        )
                        readings_pdf_cover = None
                        if bundle_readings_gcs_path:
                            try:
                                readings_pdf_cover = gcs_signed_url(
                                    gcs=gcs_top,
                                    bucket_name=str(cfg.gcs_bucket_name).strip(),
                                    path=bundle_readings_gcs_path,
                                ) or None
                            except Exception:
                                readings_pdf_cover = None
                        ilus_desc_pdf = ""
                        if str(cfg.gsheet_id or "").strip():
                            try:
                                gs_pdf = build_gspread_client(cfg.gcp_service_account)
                                ilus_desc_pdf = _latest_illustration_description_from_ilus(
                                    gspread_client=gs_pdf,
                                    spreadsheet_id=str(cfg.gsheet_id).strip(),
                                    date_str=date_str,
                                    zone=zone,
                                )
                            except Exception:
                                ilus_desc_pdf = ""
                        synth_for_pdf = bundle_synth_text if has_any_synthesis else ""
                        if not include_catechese_pdf:
                            synth_for_pdf = ap._strip_catechese_bridge(synth_for_pdf)
                        back_cover_b = None
                        try:
                            y = str(date_str)[:4]
                            back_cover_b = download_bytes(
                                gcs=gcs_top,
                                bucket_name=str(cfg.gcs_bucket_name).strip(),
                                path=f"Images/thumbs/montage_{y}.png",
                            )
                        except Exception:
                            back_cover_b = None

                        # Titre PDF sur 2 lignes : fête puis (semaine du Psautier uniquement)
                        semaine_psautier = (getattr(identity, "semaine", None) or "").strip()
                        line1 = ap._liturgy_display_label(
                            (getattr(identity, "fete", None) or "").strip()
                            or (ap._jour_liturgique(identity) or "").strip()
                            or ap._liturgy_cover_pdf_title(identity)
                        )
                        line2 = ""
                        if semaine_psautier and ("psautier" in semaine_psautier.lower()):
                            lbl = ap._liturgy_display_label(semaine_psautier).strip()
                            line2 = f"({lbl})" if lbl else ""
                        week_title_pdf = (line1 + ("\n" + line2 if line2 else "")).strip()

                        # Index de la vignette du dimanche dans le montage annuel (pour encadrer la semaine correspondante)
                        highlight_idx = None
                        try:
                            manifest = json.loads(
                                Path("data/manifests/illustration_pipeline.json").read_text(encoding="utf-8")
                            )
                            targets = manifest.get("targets") or []
                            year = str(date_str)[:4]
                            year_targets = [t for t in targets if str(t.get("date") or "").startswith(year)]
                            year_dates = [str(t.get("date") or "")[:10] for t in year_targets]
                            if str(date_str)[:10] in year_dates:
                                highlight_idx = int(year_dates.index(str(date_str)[:10]))
                        except Exception:
                            highlight_idx = None

                        pdf_b = build_liturgy_sunday_pdf_bytes(
                            image_bytes=img_b,
                            week_title=week_title_pdf,
                            date_line=ap._french_long_date_label(date_str),
                            meta_line=(
                                f"{ap._liturgy_display_label(getattr(identity, 'periode', None))} · "
                                f"Cycle {ap._cycle_year_display(getattr(identity, 'annee', None))} · "
                                f"{ap._liturgy_display_label(getattr(identity, 'couleur', None))}"
                            ),
                            premiere_lecture=texts.premiere_lecture,
                            psaume=texts.psaume,
                            deuxieme_lecture=texts.deuxieme_lecture,
                            evangile=texts.evangile,
                            synthesis_text=synth_for_pdf,
                            audio_listen_url=aud_url,
                            audio_listen_note=aud_note,
                            audio_readings_listen_url=readings_pdf_cover,
                            illustration_description=ilus_desc_pdf or None,
                            about_markdown=_ABOUT_MARKDOWN,
                            back_cover_image_bytes=back_cover_b,
                            accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                            back_cover_highlight_cell_index=highlight_idx,
                        )
                        st.session_state[pdf_key] = pdf_b
                        try:
                            fasc_path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
                            upload_bytes(
                                gcs=gcs_top,
                                bucket_name=str(cfg.gcs_bucket_name).strip(),
                                path=fasc_path,
                                data=pdf_b,
                                content_type="application/pdf",
                            )
                            st.success("PDF enregistré.")
                        except Exception as ex:
                            st.warning(f"Impossible d’enregistrer le PDF sur Cloud (Fascicules/) : {ex}")
                    finally:
                        ov_pdf.empty()
                st.divider()
            st.caption("Administration — synthèse (texte + audio)")
            _show_sunday_admin_flash(date_str)
            already_has_bundle = bool((bundle_synth_text or "").strip()) or (bundle_audio is not None)
            if already_has_bundle:
                _tail = (
                    "Les supports **PDF**, **audio synthèse** et **texte** disponibles sont regroupés en haut de la page."
                )
                if not bundle_readings_audio:
                    _tail += (
                        " **L’audio des lectures** (bloc « Écouter les lectures (intégrales) ») n’apparaît que si une ligne "
                        "existe dans la table `audio` avec un chemin `AudioLectures/…` lié à la génération du jour ; "
                        "sinon utilise **Compléter les manquants** avec la case « Audio des lectures » cochée."
                    )
                else:
                    _tail += " **L’audio des lectures** figure au-dessus des trois colonnes."
                st.info(
                    "Une synthèse existe déjà pour ce dimanche (texte et/ou audio). " + _tail + " Tu peux régénérer ci-dessous si besoin.",
                    icon="ℹ️",
                )
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
                "Inclure « Passerelle catéchèse — L’écho des paraboles »",
                value=True,
                help=(
                    "Ajoute la passerelle catéchèse (5 sous-parties) en fin de synthèse. "
                    "Sa longueur (~275 mots) est fixe et indépendante du pourcentage ci-dessus."
                ),
                key=f"adm_sunday_catech_{date_str}",
            )
            auto_pdf = st.checkbox(
                "Inclure aussi le fascicule du dimanche au format PDF",
                value=False,
                key=f"adm_sunday_auto_pdf_{date_str}",
                help="À la fin d’une régénération complète, produit aussi le PDF et l’envoie sur Cloud.",
            )
            audio_readings_gen = st.checkbox(
                "Audio des lectures",
                value=True,
                key=f"adm_sunday_audio_readings_{date_str}",
                help="Fichier distinct AudioLectures/… rattaché à la même génération que la synthèse.",
            )
            debug = st.toggle("Mode debug", value=False, key=f"adm_sunday_debug_{date_str}")
            st.caption(
                "« Compléter les manquants » ajoute seulement ce qui manque encore sur Cloud, selon les cases "
                "**Audio des lectures** et **fascicule PDF** — sans refaire la synthèse IA. "
                "« Tout régénérer (long) » relance Vertex + audios ; prévoir plusieurs minutes. "
                "L’audio (synthèse et lectures) passe par **Vertex TTS** en priorité ; si le projet n’est pas "
                "allowlisté pour l’audio, configure `GEMINI_API_KEY` dans les secrets pour le repli automatique."
            )
            if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
                st.warning("Configuration incomplète (service account / gsheet_id / bucket). Synthèse indisponible.")
            else:
                col_inc, col_full = st.columns(2)
                with col_inc:
                    inc_clicked = st.button(
                        "Compléter les manquants",
                        type="primary",
                        key=f"adm_sunday_incremental_{date_str}",
                        help="Audio des lectures (si case cochée) et/ou fascicule PDF (si case fascicule cochée), "
                        "uniquement si absents sur Cloud — synthèse déjà enregistrée.",
                    )
                with col_full:
                    full_clicked = st.button(
                        "Tout régénérer (long)",
                        type="secondary",
                        key=f"adm_sunday_full_{date_str}",
                        help="Nouvelle synthèse Vertex, audio synthèse, options ci-dessous — plusieurs minutes.",
                    )
                if inc_clicked:
                    gcs_inc = gcs_top
                    if gcs_inc is None:
                        try:
                            gcs_inc = build_gcs_client(cfg.gcp_service_account)
                        except Exception as ex:
                            st.error(f"Connexion GCS impossible : {ex}")
                            gcs_inc = None
                    if gcs_inc:
                        overlay_inc = loading_overlay("Complément des contenus manquants…")
                        try:
                            gs_inc = build_gspread_client(cfg.gcp_service_account)
                            include_cat_state = bool(
                                st.session_state.get(f"pdf_catechese_{date_str}", True)
                            )
                            flash = _run_incremental_sunday_outputs(
                                cfg=cfg,
                                gs=gs_inc,
                                gcs=gcs_inc,
                                identity=identity,
                                texts=texts,
                                zone=zone,
                                bundle_synth_text=bundle_synth_text,
                                bundle_audio_gcs_path=bundle_audio_gcs_path,
                                bundle_readings_gcs_path=bundle_readings_gcs_path,
                                include_catechese_pdf=include_cat_state,
                                also_pdf_if_missing=bool(auto_pdf),
                                also_readings_if_missing=bool(audio_readings_gen),
                                pdf_key=pdf_key,
                            )
                            _set_sunday_admin_flash(
                                date_str=date_str,
                                level=str(flash.get("level") or "info"),
                                message=str(flash.get("message") or ""),
                            )
                            if flash.get("level") == "success":
                                _month_content_status.clear()
                            st.rerun()
                        finally:
                            overlay_inc.empty()
                if full_clicked:
                    overlay = loading_overlay("LumenVia régénère la synthèse et les audios (long)…")
                    try:
                        _run_generate_sunday_flow(
                            _overlay=overlay,
                            identity=identity,
                            texts=texts,
                            zone=zone,
                            total_words=total_words,
                            pct=int(pct or 20),
                            include_takeaways=bool(include_takeaways),
                            include_catechese_bridge=bool(include_catechese_bridge_gen),
                            generate_pdf=bool(auto_pdf),
                            generate_readings_audio=bool(audio_readings_gen),
                            debug=bool(debug),
                            cfg=cfg,
                        )
                        st.rerun()
                    finally:
                        overlay.empty()

    fete_raw = (identity.fete or "").strip() or (ap._jour_liturgique(identity) or "").strip()
    fete_line = ap._liturgy_display_label(fete_raw) if fete_raw else "—"
    st.markdown(
        f"<div style='font-size:0.95rem;line-height:1.45;color:var(--liturgie-text);'>"
        f"<strong>{identity.date}</strong> · {ap._liturgy_display_label(identity.periode)} · "
        f"Cycle {ap._cycle_year_display(identity.annee)} · {ap._liturgy_display_label(identity.couleur)}"
        f"<br/><span style='opacity:0.9'>Fête / mémoire : {html_escape(fete_line)}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Détails sur le temps liturgique", expanded=True):
        st.markdown(f"**Temps** : {ap._explain_liturgical_time(identity.periode)}")
        st.markdown(f"**Cycle** : {ap._explain_liturgical_cycle(identity.annee)}")
        couleur_nom = ap._liturgy_display_label(identity.couleur)
        st.markdown(
            f"**Couleur** : **{couleur_nom}** — {ap._explain_liturgical_color(identity.couleur)}"
        )

    if gcs_top and cfg.gcs_bucket_name:
        ap._try_show_liturgy_illustration(gcs=gcs_top, cfg=cfg, date_str=date_str)

    st.subheader("Lectures")
    # (supprimé) Total lectures : non affiché
    render_liturgy_block("Première lecture", texts.premiere_lecture)
    render_liturgy_block("Psaume", texts.psaume)
    render_liturgy_block("Deuxième lecture", texts.deuxieme_lecture)
    render_liturgy_block("Évangile", texts.evangile)


