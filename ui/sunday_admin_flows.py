"""Flux admin Dimanche : complément incrémental et régénération Vertex/GCS/Sheets."""

from __future__ import annotations

import json
import time
from datetime import date
from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.aelf_reading_meta import pdf_liturgy_reading_kwargs
from core.audio_utils import normalize_audio_bytes
from core.content_locale_paths import (
    audio_readings_path,
    audio_synth_path,
    fascicule_pdf_path,
    synthesis_text_path,
)
from core.locale_codes import DEFAULT_PREF_LANGUE
from core.liturgy_day import coerce_liturgy_pref_langue
from core.gcp_clients import build_gcs_client
from core.pdf_liturgy_sunday import build_liturgy_sunday_pdf_bytes
from core.pdf_locale import about_markdown_for_lang, pdf_cover_date_line, pdf_cover_meta_line
from core.prompt_locale import coerce_aip_langue
from core.prompt_translate import translate_plain_fr_to
from core.synthesis_localize import strip_localized_from_banners
from core.synthesis_vertex_prompt import (
    CATECHESE_BRIDGE_TARGET_WORDS,
    build_sunday_vertex_synthesis_prompt,
)
from core.sheets_db import append_immutable_row, build_gspread_client
from core.storage import blob_exists, download_bytes, upload_bytes, upload_text
from core.local_bundle_cache import persist_sunday_bundle
from core.liturgy_theme import liturgical_accent_hex
from core.vertex_gemini import VertexGeminiClient
from core.voix_audio import pick_voice_name, resolve_voice
from ui.streamlit_caches import service_account_json_fingerprint
from core.gcs_signed_urls import gcs_signed_url
from core.config import resolve_gemini_api_key
from core.sunday_existing_outputs import has_readings_audio_for_gen, pdf_synthesis_listen_url
from core.readings_cache_loader import load_liturgy_from_readings_cache
from core.sunday_gemini_tts import (
    last_tts_ambiance,
    last_tts_route,
    mark_vertex_tts_allowlist_blocked,
    tts_readings_audio_bytes,
    tts_spoken_audio_bytes,
    vertex_tts_allowlist_blocked,
)
from core.sunday_readings_tts import compose_readings_tts_text, plain_readings_for_tts
from core.weekly_email_urls import _latest_illustration_description_from_ilus
from ui.components import update_loading_overlay


def _audio_ambiance_sheet_flag() -> str:
    """Colonne Sheets ``ambiance`` : 1 si mix bande-son, 0 sinon."""
    return "1" if last_tts_ambiance() else "0"


def _pdf_illustration_description_localized(
    *,
    text_fr: str,
    pref_langue: object | None,
    cfg: object,
) -> str | None:
    """Traduit le commentaire ILUS (FR) pour la légende PDF ; aucun appel si FR ou vide."""
    src = (text_fr or "").strip()
    if not src:
        return None
    lg = coerce_aip_langue(pref_langue)
    if lg == DEFAULT_PREF_LANGUE:
        return src
    vertex = None
    try:
        sa = getattr(cfg, "gcp_service_account", None)
        if sa:
            vertex = VertexGeminiClient(service_account_info=sa)
    except Exception:
        vertex = None
    try:
        out = translate_plain_fr_to(src, target_lang=lg, vertex_client=vertex)
        return (out or src).strip() or src
    except Exception:
        return src


def _flow_overlay_step(
    _overlay: object,
    message: str,
    *,
    hint: str | None = None,
    t0: float | None = None,
    flush: bool = True,
) -> None:
    elapsed = (time.perf_counter() - t0) if t0 is not None else None
    update_loading_overlay(_overlay, message, hint=hint, elapsed_s=elapsed, flush=flush)


_VERTEX_FINISH_TRUNCATED = frozenset({"MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH"})


def _vertex_finish_truncated(finish_reason: object) -> bool:
    return str(finish_reason or "").strip().upper() in _VERTEX_FINISH_TRUNCATED


def _synthesis_min_words(target_words: int) -> int:
    """Seuil minimal pour publier une synthèse (50 % de la cible principale, hors passerelle)."""
    return max(60, int(int(target_words) * 0.50))


def _synthesis_generation_usable(*, candidate: dict, text: str, min_words: int) -> bool:
    """
    Une synthèse est publiable dès qu'il y a assez de texte utile.

    Ne plus rejeter systématiquement ``MAX_TOKENS`` / ``citationMetadata`` : Gemini 2.5
    consomme souvent le budget en « thinking » et remonte MAX_TOKENS même avec un corps
    déjà exploitable ; les métadonnées de citation apparaissent aussi en faux positifs.
    """
    words = len((text or "").split())
    if words < 50:
        return False
    floor = max(50, int(int(min_words) * 0.55))
    return words >= floor


def _flow_result(*, ok: bool, level: str, message: str) -> dict[str, str | bool]:
    return {"ok": ok, "level": level, "message": message}


def _sheet_seconds(v: float | int | None) -> str:
    if v is None:
        return ""
    try:
        return str(round(float(v), 3))
    except (TypeError, ValueError):
        return ""


def _sunday_date_for_voice(identity: object, date_str: str | None = None) -> date:
    """Date du dimanche pour la rotation des pools Voix_Audio."""
    raw = str(getattr(identity, "date", None) or date_str or "").strip()[:10]
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return date.fromisoformat(raw)
        except Exception:
            pass
    return date.today()


def _resolve_voice_for_identity(
    voix_rows: list[dict],
    *,
    identity: object,
    cible: str,
    exclude_voices: list[str] | None = None,
) -> dict:
    """Résolution voix isolée (évite UnboundLocalError dans le flux long)."""
    sunday_d = _sunday_date_for_voice(identity)
    return resolve_voice(
        voix_rows,
        cible=cible,
        couleur=getattr(identity, "couleur", None),
        periode=getattr(identity, "periode", None),
        today=sunday_d,
        sunday_date=sunday_d,
        exclude_voices=exclude_voices,
    )

def _existing_synthese_voice_for_exclude(
    *,
    gs: object,
    spreadsheet_id: str,
    gen_entity_id: str | None,
) -> list[str]:
    """Si une synthèse existe déjà pour ce gen, exclure sa voix des lectures."""
    eid = str(gen_entity_id or "").strip()
    if not eid or not spreadsheet_id:
        return []
    try:
        from core.sheets_db import fetch_records

        rows = fetch_records(
            gspread_client=gs,
            spreadsheet_id=spreadsheet_id,
            table="audio",
            limit=0,
            use_cache=True,
        )
    except Exception:
        return []
    for r in rows or []:
        if str(r.get("gen_entity_id") or "").strip() != eid:
            continue
        if str(r.get("kind") or "").strip().lower() not in ("synthese", "synthèse", "synthesis"):
            continue
        v = str(r.get("voice") or "").strip()
        if v:
            return [v]
    return []

def _append_pdf_export_row(
    *,
    gs: object,
    cfg: object,
    date_str: str,
    zone: str,
    gen_entity_id: str,
    gcs_path: str,
    duration_build_s: float,
) -> None:
    append_immutable_row(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table="pdf_exports",
        values_by_col={
            "entity_id": sha256(f"pdf|{gen_entity_id}|{gcs_path}".encode("utf-8")).hexdigest()[:24],
            "range_start": date_str,
            "range_end": date_str,
            "zone": zone,
            "gcs_path": gcs_path,
            "date_semaine_liturgique": date_str,
            "gen_entity_id": gen_entity_id,
            "kind": "fascicule_dimanche",
            "duration_build_s": _sheet_seconds(duration_build_s),
        },
    )


def _incremental_flash_payload(*, done: list[str], issues: list[str], skipped: list[str]) -> dict[str, str]:
    """Message utilisateur après « Compléter les manquants » (survit au rerun Streamlit)."""
    if done:
        return {"level": "success", "message": "Complété : " + " · ".join(done) + "."}
    if issues:
        return {"level": "warning", "message": " ".join(issues)}
    if skipped:
        return {
            "level": "info",
            "message": "Rien à compléter : "
            + " · ".join(skipped)
            + " (selon les cases cochées et le stockage Cloud).",
        }
    return {
        "level": "warning",
        "message": (
            "Aucune action effectuée. Coche **Audio des lectures** et/ou **fascicule PDF**, "
            "ou vérifie que la synthèse du dimanche est bien enregistrée."
        ),
    }


def _resolve_texts_for_readings_tts(
    *,
    texts: object,
    identity: object,
    gs: object,
    cfg: object,
    zone: str,
    pref_langue: str = DEFAULT_PREF_LANGUE,
) -> object:
    """Recharge les textes si le corps TTS est vide (RDC toutes langues, puis facade)."""
    readings_plain = plain_readings_for_tts(texts, pref_langue=pref_langue)
    if readings_plain.strip():
        return texts
    lg = coerce_liturgy_pref_langue(pref_langue)
    date_str = str(getattr(identity, "date", "") or "")
    sid = str(getattr(cfg, "gsheet_id", "") or "").strip()
    if sid:
        loaded = load_liturgy_from_readings_cache(
            gs=gs,
            spreadsheet_id=sid,
            date_str=date_str,
            pref_langue=lg,
        )
        if loaded:
            _id, cache_texts = loaded
            if plain_readings_for_tts(cache_texts, pref_langue=lg).strip():
                return cache_texts
    if lg != "FR":
        try:
            from ui.streamlit_caches import cached_liturgy_day

            _id, cache_texts, _sid = cached_liturgy_day(date_str, pref_langue=lg)
            if plain_readings_for_tts(cache_texts, pref_langue=lg).strip():
                return cache_texts
        except Exception:
            pass
    return texts


def _ensure_texts_for_pref_langue(
    *,
    texts: object,
    identity: object,
    pref_langue: str,
) -> tuple[object, object]:
    """Aligne identity/texts sur la langue de génération (Evangelizo hors FR)."""
    lg = coerce_liturgy_pref_langue(pref_langue)
    date_str = str(getattr(identity, "date", "") or "")[:10]
    if lg == "FR":
        return identity, texts
    try:
        from ui.streamlit_caches import cached_liturgy_day

        ident2, texts2, _sid = cached_liturgy_day(date_str, pref_langue=lg)
        return ident2, texts2
    except Exception:
        return identity, texts


def _readings_tts_vertex_client(cfg: object) -> VertexGeminiClient | None:
    sa = getattr(cfg, "gcp_service_account", None)
    if not sa:
        return None
    try:
        return VertexGeminiClient(service_account_info=sa)
    except Exception:
        return None


def _run_incremental_sunday_outputs(
    *,
    cfg: object,
    gs: object,
    gcs: object,
    identity: object,
    texts: object,
    zone: str,
    bundle_synth_text: str | None,
    bundle_audio_gcs_path: str | None,
    bundle_readings_gcs_path: str | None,
    include_catechese_pdf: bool,
    also_pdf_if_missing: bool,
    also_readings_if_missing: bool,
    pdf_key: str,
    pref_langue: str = DEFAULT_PREF_LANGUE,
    _overlay: object | None = None,
) -> dict[str, str]:
    """Sans nouvelle synthèse Vertex : audio des lectures (TTS) et/ou fascicule PDF si absents sur Cloud."""
    import app as ap
    pref_langue = coerce_liturgy_pref_langue(pref_langue)
    identity, texts = _ensure_texts_for_pref_langue(
        texts=texts, identity=identity, pref_langue=pref_langue
    )
    date_str = str(identity.date)
    t_flow = time.perf_counter()
    done: list[str] = []
    issues: list[str] = []
    skipped: list[str] = []
    gen_row = ap._latest_generation_row_for_sunday(gs=gs, cfg=cfg, date_str=date_str, zone=zone)
    if not gen_row:
        return {
            "level": "error",
            "message": (
                "Aucune synthèse enregistrée pour cette date. Utilise d’abord « Tout régénérer (long) »."
            ),
        }
    gen_eid = str(gen_row.get("entity_id") or "").strip()
    if not gen_eid:
        return {
            "level": "error",
            "message": "Enregistrement de génération invalide (identifiant manquant).",
        }

    synth = (bundle_synth_text or "").strip()
    if not synth:
        tp = str(gen_row.get("text_gcs_path") or "").strip()
        if tp:
            try:
                synth = (
                    download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=tp)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            except Exception as ex:
                st.warning(f"Lecture du texte de synthèse sur Cloud impossible : {ex}")
                synth = ""

    readings_path_this_run: str | None = None
    has_readings_audio = has_readings_audio_for_gen(
        gs=gs, cfg=cfg, gen_entity_id=gen_eid, gcs=gcs
    )

    if also_readings_if_missing:
        if has_readings_audio:
            skipped.append("audio des lectures déjà publié")
        elif not resolve_gemini_api_key() and vertex_tts_allowlist_blocked():
            issues.append(
                "Vertex TTS refuse l’audio (projet non allowlisté). "
                "Ajoute `GEMINI_API_KEY` dans `.streamlit/secrets.toml` puis redémarre l’app."
            )
        elif not resolve_gemini_api_key() and not getattr(cfg, "gcp_service_account", None):
            issues.append(
                "Configure GEMINI_API_KEY ou le compte de service GCP — impossible de générer l’audio des lectures."
            )
        else:
            texts = _resolve_texts_for_readings_tts(
                texts=texts, identity=identity, gs=gs, cfg=cfg, zone=zone, pref_langue=pref_langue
            )
            readings_plain = plain_readings_for_tts(texts, pref_langue=pref_langue)
            if not readings_plain.strip():
                issues.append(
                    "Texte des lectures vide pour ce dimanche — impossible de produire l’audio. "
                    "Ouvre **Cache lectures** et précharge le mois, ou vérifie l’API AELF."
                )
            else:
                try:
                    _flow_overlay_step(
                        _overlay,
                        "Audio des lectures (TTS)…",
                        hint=(
                            "Plusieurs appels Vertex/Gemini — le fichier n’apparaît dans "
                            f"`AudioLectures/{date_str}/` qu’à la fin (3–8 min). "
                            "La synthèse existante reste dans `Syntheses/`."
                        ),
                        t0=t_flow,
                    )
                    with st.spinner("Audio des lectures (TTS)…"):
                        templates_ia: dict[str, str] = {}
                        voix_r: list[dict] = []
                        try:
                            templates_ia = ap._load_prompt_templates_cached(
                                gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
                                service_account_fingerprint=service_account_json_fingerprint(
                                    getattr(cfg, "gcp_service_account", {}) or {}
                                ),
                                pref_langue=pref_langue,
                            )
                            voix_r = ap._load_voix_rules_cached(
                                gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
                                service_account_fingerprint=service_account_json_fingerprint(
                                    getattr(cfg, "gcp_service_account", {}) or {}
                                ),
                            )
                        except Exception:
                            templates_ia = {}
                            voix_r = []
                        exclude_read = _existing_synthese_voice_for_exclude(
                            gs=gs,
                            spreadsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
                            gen_entity_id=gen_eid,
                        )
                        voice_read = pick_voice_name(
                            voix_r,
                            cible="lectures",
                            couleur=getattr(identity, "couleur", None),
                            periode=getattr(identity, "periode", None),
                            sunday_date=_sunday_date_for_voice(identity),
                            exclude_voices=exclude_read,
                        )
                        readings_tts = compose_readings_tts_text(
                            body=readings_plain, templates=templates_ia
                        )
                        vx_read = _readings_tts_vertex_client(cfg)
                        rt0 = time.perf_counter()
                        r_bytes, r_mime, r_ext = tts_readings_audio_bytes(
                            cfg=cfg,
                            text=readings_tts,
                            voice_name=voice_read,
                            vertex_client=vx_read,
                            gemini_api_key=resolve_gemini_api_key(),
                            sunday_date=_sunday_date_for_voice(identity),
                            pref_langue=pref_langue,
                        )
                        duration_readings_tts_s = round(time.perf_counter() - rt0, 3)
                        readings_tts_route = last_tts_route()
                    day_for_path_inc = str(getattr(identity, "date", "") or "").strip()[:10]
                    readings_path = audio_readings_path(
                        day_for_path_inc, gen_eid, r_ext, pref_langue=pref_langue
                    )
                    ru0 = time.perf_counter()
                    upload_bytes(
                        gcs=gcs,
                        bucket_name=cfg.gcs_bucket_name,
                        path=readings_path,
                        data=r_bytes,
                        content_type=r_mime,
                    )
                    duration_readings_upload_s = round(time.perf_counter() - ru0, 3)
                    append_immutable_row(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="audio",
                        values_by_col={
                            "entity_id": sha256(
                                f"audio_lect|{gen_eid}|{readings_path}".encode("utf-8")
                            ).hexdigest()[:24],
                            "gen_entity_id": gen_eid,
                            "voice": voice_read,
                            "format": r_ext,
                            "gcs_path": readings_path,
                            "kind": "lectures",
                            "duration_tts_s": _sheet_seconds(duration_readings_tts_s),
                            "duration_upload_s": _sheet_seconds(duration_readings_upload_s),
                            "tts_route": readings_tts_route or "",
                            "ambiance": _audio_ambiance_sheet_flag(),
                        },
                    )
                    readings_path_this_run = readings_path
                    done.append("audio des lectures")
                except Exception as ex:
                    issues.append(f"Audio des lectures non publié : {ex}")

    fasc_path = fascicule_pdf_path(date_str, pref_langue=pref_langue)
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    pdf_on_cloud = bool(bucket and blob_exists(gcs=gcs, bucket_name=bucket, path=fasc_path))
    need_pdf = bool(also_pdf_if_missing and synth and bucket and not pdf_on_cloud)
    if also_pdf_if_missing:
        if pdf_on_cloud:
            skipped.append("fascicule PDF déjà sur Cloud")
        elif not synth:
            issues.append("Synthèse introuvable — le fascicule PDF ne peut pas être produit.")
    if need_pdf:
        try:
            _flow_overlay_step(
                _overlay,
                "Fascicule PDF sur Cloud…",
                hint=f"Publication dans `Fascicules/{date_str}/` à la fin de l’assemblage.",
                t0=t_flow,
            )
            with st.spinner("Fascicule PDF sur Cloud…"):
                tpdf0 = time.perf_counter()
                img_b = ap._fetch_liturgy_illustration_full_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
                _base_pub = ""
                try:
                    s = st.secrets
                    _base_pub = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip()
                except Exception:
                    pass
                aud_url, aud_note = pdf_synthesis_listen_url(
                    date_str=date_str,
                    public_app_url=_base_pub or None,
                    gcs=gcs,
                    bucket_name=bucket,
                    gcs_audio_path=(bundle_audio_gcs_path or "").strip()
                    or ap._synthesis_audio_gcs_path_for_gen(gs=gs, cfg=cfg, gen_entity_id=gen_eid),
                    gs=gs,
                    cfg=cfg,
                    gen_entity_id=gen_eid,
                )
                synth_for_pdf = synth
                if not include_catechese_pdf:
                    synth_for_pdf = ap._strip_catechese_bridge(synth_for_pdf)
                back_cover_b = None
                try:
                    y = str(date_str)[:4]
                    back_cover_b = download_bytes(
                        gcs=gcs,
                        bucket_name=bucket,
                        path=f"Images/thumbs/montage_{y}.png",
                    )
                except Exception:
                    back_cover_b = None
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
                rp_for_cover = (readings_path_this_run or "").strip() or (
                    (bundle_readings_gcs_path or "").strip()
                )
                readings_pdf_signed = None
                if rp_for_cover:
                    try:
                        readings_pdf_signed = gcs_signed_url(
                            gcs=gcs, bucket_name=bucket, path=rp_for_cover
                        ) or None
                    except Exception:
                        readings_pdf_signed = None
                ilus_desc_pdf = ""
                if str(cfg.gsheet_id or "").strip():
                    try:
                        ilus_desc_pdf = _latest_illustration_description_from_ilus(
                            gspread_client=gs,
                            spreadsheet_id=str(cfg.gsheet_id).strip(),
                            date_str=date_str,
                            zone=zone,
                        )
                    except Exception:
                        ilus_desc_pdf = ""
                pdf_b = build_liturgy_sunday_pdf_bytes(
                    image_bytes=img_b,
                    week_title=week_title_pdf,
                    date_line=pdf_cover_date_line(date_str, pref_langue),
                    meta_line=pdf_cover_meta_line(
                        periode=getattr(identity, "periode", None),
                        annee=getattr(identity, "annee", None),
                        couleur=getattr(identity, "couleur", None),
                        pref_langue=pref_langue,
                    ),
                    **pdf_liturgy_reading_kwargs(texts),
                    synthesis_text=synth_for_pdf,
                    audio_listen_url=aud_url,
                    audio_listen_note=aud_note,
                    audio_readings_listen_url=readings_pdf_signed,
                    illustration_description=_pdf_illustration_description_localized(
                        text_fr=ilus_desc_pdf or "",
                        pref_langue=pref_langue,
                        cfg=cfg,
                    ),
                    about_markdown=about_markdown_for_lang(pref_langue),
                    back_cover_image_bytes=back_cover_b,
                    accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                    back_cover_highlight_cell_index=highlight_idx,
                    pref_langue=pref_langue,
                )
                upload_bytes(
                    gcs=gcs,
                    bucket_name=bucket,
                    path=fasc_path,
                    data=pdf_b,
                    content_type="application/pdf",
                )
                _append_pdf_export_row(
                    gs=gs,
                    cfg=cfg,
                    date_str=date_str,
                    zone=zone,
                    gen_entity_id=gen_eid,
                    gcs_path=fasc_path,
                    duration_build_s=round(time.perf_counter() - tpdf0, 3),
                )
                st.session_state[pdf_key] = pdf_b
                st.session_state.pop(f"liturgy_sunday_pdf_{date_str}", None)
                done.append("fascicule PDF")
        except Exception as ex:
            issues.append(f"Fascicule PDF non produit : {ex}")

    return _incremental_flash_payload(done=done, issues=issues, skipped=skipped)


def _run_generate_sunday_flow(
    *,
    _overlay: object,
    identity: object,
    texts: object,
    zone: str,
    total_words: int,
    pct: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    generate_pdf: bool,
    generate_readings_audio: bool,
    debug: bool,
    cfg: object,
    pref_langue: str = DEFAULT_PREF_LANGUE,
) -> dict[str, str | bool]:
    import app as ap
    t_flow = time.perf_counter()
    pref_langue = coerce_liturgy_pref_langue(pref_langue)
    identity, texts = _ensure_texts_for_pref_langue(
        texts=texts, identity=identity, pref_langue=pref_langue
    )
    # Copies locales immédiates (évite tout UnboundLocalError / ombre de paramètre).
    zone_key = str(zone or "france").strip() or "france"
    date_str = str(getattr(identity, "date", "") or "").strip()[:10]
    date_label = date_str
    retry_fallback_note = ""
    max_out = 8192
    gen = None
    audio = None
    audio_ext = "wav"
    audio_bytes_norm = b""
    audio_mime_norm = "audio/wav"
    _flow_overlay_step(
        _overlay,
        "1/4 — Préparation et synthèse écrite (Vertex)…",
        hint=(
            "Chargement des prompts (Sheets) puis génération du texte — 1 à 3 min. "
            f"Le fichier `.txt` apparaîtra dans `Syntheses/{date_label}/` à la fin de cette étape."
        ),
        t0=t_flow,
        flush=True,
    )
    target_words = max(80, int(total_words * (pct / 100.0)))
    catechese_bridge_words = (
        CATECHESE_BRIDGE_TARGET_WORDS if include_catechese_bridge else 0
    )
    total_words_budget = target_words + catechese_bridge_words
    templates: dict[str, str] = {}
    try:
        templates = ap._load_prompt_templates_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=service_account_json_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
            pref_langue=pref_langue,
        )
    except Exception:
        templates = {}

    voix_rows: list[dict] = []
    try:
        voix_rows = ap._load_voix_rules_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=service_account_json_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
        )
    except Exception:
        voix_rows = []
    if not voix_rows:
        st.warning(
            "Table `Voix_Audio` vide ou illisible — fallback voix **Achird** (synthèse et lectures). "
            "Vérifie l’onglet VOIX / Admin → Test ressources."
        )

    instructions_struct = templates.get("instructions_base_md") or Path("data/instructions_ia.md").read_text(
        encoding="utf-8"
    )
    # Double blind : la "secret sauce" n'est pas dans Sheets (A), mais dans st.secrets (B).
    try:
        s = st.secrets
        secret_sauce = str(s.get("IA_SECRET_SAUCE_MD") or s.get("ia_secret_sauce_md") or "").strip()
    except Exception:
        secret_sauce = ""
    instructions = (instructions_struct + "\n\n" + secret_sauce).strip() if secret_sauce else instructions_struct
    liturgical_context = "\n".join(
        [
            f"- Temps liturgique ({identity.periode or '—'}): {ap._explain_liturgical_time(identity.periode)}",
            f"- Couleur ({identity.couleur or '—'}): {ap._explain_liturgical_color(identity.couleur)}",
            f"- Année / cycle ({identity.annee or '—'}): {ap._explain_liturgical_cycle(identity.annee)}",
        ]
    )
    prompt = build_sunday_vertex_synthesis_prompt(
        instructions=instructions,
        length_words=int(target_words),
        include_takeaways=bool(include_takeaways),
        include_catechese_bridge=bool(include_catechese_bridge),
        catechese_bridge_words=(
            int(catechese_bridge_words) if include_catechese_bridge else None
        ),
        templates=templates,
        identity={
            "date": identity.date,
            "zone": identity.zone,
            "periode": identity.periode,
            "annee": identity.annee,
            "couleur": identity.couleur,
            "fete": identity.fete,
            "jour_liturgique_nom": ap._jour_liturgique(identity),
        },
        readings={
            "premiere_lecture": texts.premiere_lecture,
            "psaume": texts.psaume,
            "deuxieme_lecture": texts.deuxieme_lecture,
            "evangile": texts.evangile,
        },
        liturgical_context=liturgical_context,
        pref_langue=pref_langue,
    )

    source_hash = sha256(
        (identity.date + "|" + (texts.premiere_lecture or "") + "|" + (texts.psaume or "") + "|" + (texts.evangile or "")).encode(
            "utf-8"
        )
    ).hexdigest()

    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
    perf: dict[str, float | int | str] = {}
    with st.spinner("Génération IA (Gemini)…"):
        t0 = time.perf_counter()
        try:
            # Budget large : Gemini 2.5 peut consommer une part en « thinking ».
            max_out = min(8192, max(8192, int(total_words_budget * 3.0)))
            gen = vx.generate_text_auto(
                preferred_models=[
                    "gemini-2.0-flash",
                    "gemini-2.5-flash",
                    "gemini-flash-latest",
                    "gemini-pro-latest",
                ],
                prompt=prompt,
                max_output_tokens=max_out,
                thinking_budget=0,
            )
        except Exception as exc:
            if debug:
                st.exception(exc)
            return _flow_result(
                ok=False,
                level="error",
                message="Erreur lors de la génération de la synthèse. Active le mode debug pour les détails.",
            )
        t1 = time.perf_counter()
        perf["vertex_text_s"] = round(t1 - t0, 3)

    # Fiabilisation : relance si troncature Vertex, citations externes, ou synthèse trop courte.
    cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
    if not isinstance(cand0, dict):
        cand0 = {}
    fr = str(cand0.get("finishReason") or "").strip().upper()
    words_out = len((gen.text or "").split())
    has_citations = bool((cand0.get("citationMetadata") or {}).get("citations"))
    min_words = _synthesis_min_words(int(target_words))
    hard_truncated = _vertex_finish_truncated(fr)
    too_short = words_out < min_words
    # Relance si vraiment trop court, ou troncature avec peu de texte, ou citations + texte fragile.
    needs_retry = (not _synthesis_generation_usable(candidate=cand0, text=gen.text or "", min_words=min_words)) or (
        hard_truncated and too_short
    ) or (has_citations and too_short)
    if needs_retry:
        _flow_overlay_step(
            _overlay,
            "1/4 — Relance synthèse (sortie tronquée)…",
            hint="Vertex retente avec un prompt renforcé — encore 1–2 min.",
            t0=t_flow,
            flush=True,
        )
        # Prompt “durci” : aucune URL / aucune citation / uniquement textes fournis.
        hardened_prefix = templates.get("retry_hardened_prefix") or (
            "IMPORTANT — SOURCES: ne cite aucune source externe, aucune URL, aucun site web. "
            "Utilise exclusivement les textes AELF fournis ci-dessous. "
            "IMPORTANT — FORMAT: réponds uniquement avec la synthèse, sans préambule technique."
        )
        hardened = hardened_prefix.strip() + "\n\n" + prompt
        try:
            t0b = time.perf_counter()
            gen2 = vx.generate_text_auto(
                preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                prompt=hardened,
                max_output_tokens=8192,
                thinking_budget=0,
            )
            perf["vertex_text_retry_s"] = round(time.perf_counter() - t0b, 3)
            cand0b = ((gen2.raw or {}).get("candidates") or [{}])[0]
            if not isinstance(cand0b, dict):
                cand0b = {}
            fr2 = str(cand0b.get("finishReason") or "").strip().upper()
            words2 = len((gen2.text or "").split())
            retry_ok = _synthesis_generation_usable(candidate=cand0b, text=gen2.text or "", min_words=min_words)
            if retry_ok and words2 >= words_out:
                gen = gen2
                cand0 = cand0b
            elif _synthesis_generation_usable(candidate=cand0, text=gen.text or "", min_words=min_words):
                retry_fallback_note = (
                    "Relance Vertex non concluante — la première synthèse a été conservée et publiée."
                )
            elif words_out >= 50 or words2 >= 50:
                # Dernier recours : publier le meilleur des deux plutôt que d'échouer à vide.
                if words2 > words_out:
                    gen = gen2
                    cand0 = cand0b
                retry_fallback_note = (
                    "Synthèse partielle conservée (signal Vertex MAX_TOKENS/citations) — "
                    "tu peux régénérer plus tard si besoin."
                )
            else:
                detail = (
                    f"Relance : finishReason={fr2 or '—'}, {words2} mots (1er essai {words_out} mots)"
                    if debug
                    else "Réessaie dans quelques minutes, ou baisse temporairement la passerelle catéchèse."
                )
                return _flow_result(
                    ok=False,
                    level="error",
                    message=(
                        "Synthèse incomplète malgré une relance automatique "
                        "(texte trop court). " + detail
                    ),
                )
        except Exception as exc:
            if _synthesis_generation_usable(candidate=cand0, text=gen.text or "", min_words=min_words):
                retry_fallback_note = (
                    "Relance Vertex impossible (quota/erreur) — la première synthèse a été conservée."
                )
                if debug:
                    st.exception(exc)
            else:
                if debug:
                    st.exception(exc)
                return _flow_result(
                    ok=False,
                    level="error",
                    message="Relance automatique impossible (quota/erreur Vertex). Réessaie dans quelques minutes.",
                )

    if gen is None or not str(getattr(gen, "text", "") or "").strip():
        return _flow_result(ok=False, level="error", message="Réponse IA vide — aucun fichier publié.")

    if debug:
        usage = (gen.raw or {}).get("usageMetadata") or {}
        cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
        st.markdown("**Debug génération**")
        st.write(
            {
                "model": gen.model,
                "elapsed_s": perf.get("vertex_text_s"),
                "finishReason": cand0.get("finishReason"),
                "promptTokenCount": usage.get("promptTokenCount"),
                "candidatesTokenCount": usage.get("candidatesTokenCount"),
                "totalTokenCount": usage.get("totalTokenCount"),
                "text_chars": len(gen.text or ""),
                "text_words": len((gen.text or "").split()),
                "target_words_synthesis": int(target_words),
                "target_words_catechese_bridge": int(catechese_bridge_words),
                "target_words_total": int(total_words_budget),
                "maxOutputTokens": int(max_out),
            }
        )
        with st.expander("Prompt envoyé à Gemini (debug)", expanded=False):
            st.text_area("Prompt complet", value=prompt, height=320)
        with st.expander("Réponse brute Vertex (debug)", expanded=False):
            st.write(gen.raw)
        if str(cand0.get("finishReason") or "").strip().upper() in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH"):
            st.warning(
                "La synthèse semble tronquée (finishReason = MAX_TOKENS). "
                "Augmenter encore `maxOutputTokens` ou réduire le % demandé."
            )

    gcs = build_gcs_client(cfg.gcp_service_account)
    gs = build_gspread_client(cfg.gcp_service_account)

    gen_entity_id = sha256(f"{date_str}|{zone_key}|{source_hash}".encode("utf-8")).hexdigest()[:24]

    text_path = synthesis_text_path(date_str, gen_entity_id, pref_langue=pref_langue)
    _flow_overlay_step(
        _overlay,
        "2/4 — Enregistrement texte + audio synthèse…",
        hint=f"Publication de `{text_path}` puis `Audio/{date_label}/`.",
        t0=t_flow,
        flush=True,
    )
    ut0 = time.perf_counter()
    upload_text(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path, text=gen.text)
    perf["upload_text_s"] = round(time.perf_counter() - ut0, 3)

    try:
        row_gen = append_immutable_row(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="generations",
            values_by_col={
                "entity_id": gen_entity_id,
                "date": date_str,
                "zone": zone_key,
                "cycle": getattr(identity, "annee", None) or "",
                "season": getattr(identity, "periode", None) or "",
                "length": int(target_words),
                "prompt_version": "v1",
                "model": getattr(gen, "model", "") or "",
                "source_hash": source_hash,
                "text_gcs_path": text_path,
                "duration_text_s": _sheet_seconds(perf.get("vertex_text_s")),
                "duration_text_retry_s": _sheet_seconds(perf.get("vertex_text_retry_s") or 0),
                "duration_upload_text_s": _sheet_seconds(perf.get("upload_text_s")),
                "text_words": len((gen.text or "").split()),
            },
        )
    except Exception as exc:
        return _flow_result(
            ok=False,
            level="error",
            message=(
                f"Synthèse générée mais enregistrement Sheets (generations) impossible "
                f"({type(exc).__name__}). Vérifie l’onglet GEN / quotas Sheets."
            ),
        )

    voice_syn_res = _resolve_voice_for_identity(
        voix_rows, identity=identity, cible="synthese"
    )
    voice_syn = str(voice_syn_res["voice"])
    perf["voice_synthese"] = voice_syn
    perf["voice_synthese_rule_id"] = ((voice_syn_res.get("rule") or {}).get("#ID") or "")
    perf["voice_synthese_fallback"] = bool(voice_syn_res.get("fallback"))
    if debug:
        if voice_syn_res.get("fallback"):
            st.warning(
                f"Aucune règle Voix_Audio ne matche cette synthèse — fallback voix par défaut **{voice_syn}**."
            )
        else:
            st.caption(
                f"Voix synthèse retenue : **{voice_syn}** "
                f"(règle `#ID {perf['voice_synthese_rule_id']}`, score {voice_syn_res.get('score')})."
            )
    tts_payload = ap._compose_synthesis_tts_text(
        body=gen.text or "",
        templates=templates,
        periode=getattr(identity, "periode", None),
    )

    audio_route = "vertex"
    audio_bytes_norm = b""
    audio_mime_norm = "audio/wav"
    audio_ext = "wav"
    audio_ok = False
    audio_fail_msg = ""
    _flow_overlay_step(
        _overlay,
        "2/4 — Audio de la synthèse (TTS morcelé)…",
        hint=(
            f"Texte déjà dans `{text_path}`. Découpage en morceaux (~1400 car.) — "
            f"publication dans `Audio/{date_label}/` à la fin de cette sous-étape."
        ),
        t0=t_flow,
        flush=False,
    )
    with st.spinner("Synthèse audio (Vertex AI)…"):
        try:
            if not (tts_payload or "").strip():
                raise RuntimeError("Texte TTS synthèse vide après nettoyage.")
            at0 = time.perf_counter()
            audio_bytes_norm, audio_mime_norm, audio_ext = tts_spoken_audio_bytes(
                cfg=cfg,
                text=tts_payload,
                voice_name=voice_syn,
                vertex_client=vx,
                gemini_api_key=resolve_gemini_api_key(),
                sunday_date=_sunday_date_for_voice(identity),
                cible="synthese",
                pref_langue=pref_langue,
            )
            perf["audio_vertex_s"] = round(time.perf_counter() - at0, 3)
            audio_route = last_tts_route() or "vertex_tts"
            if not audio_bytes_norm:
                raise RuntimeError("Réponse audio synthèse vide.")
            audio_ok = True
        except Exception as exc:
            audio_fail_msg = f"{type(exc).__name__}: {str(exc)[:180]}"
            if debug:
                st.exception(exc)
            st.warning(
                f"Synthèse texte enregistrée, mais l'audio synthèse a échoué ({audio_fail_msg}). "
                "Poursuite éventuelle (lectures / PDF) — utilise « Compléter les manquants » pour l’audio."
            )

    if audio_ok:
        audio_path = audio_synth_path(
            date_str, gen_entity_id, audio_ext, pref_langue=pref_langue
        )
        uat0 = time.perf_counter()
        upload_bytes(
            gcs=gcs,
            bucket_name=cfg.gcs_bucket_name,
            path=audio_path,
            data=audio_bytes_norm,
            content_type=audio_mime_norm,
        )
        perf["upload_audio_s"] = round(time.perf_counter() - uat0, 3)
        perf["audio_route"] = audio_route

        append_immutable_row(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="audio",
            values_by_col={
                "entity_id": sha256(f"audio|{gen_entity_id}|{audio_path}".encode("utf-8")).hexdigest()[:24],
                "gen_entity_id": row_gen["entity_id"],
                "voice": voice_syn,
                "format": audio_ext,
                "gcs_path": audio_path,
                "kind": "synthese",
                "duration_tts_s": _sheet_seconds(perf.get("audio_vertex_s")),
                "duration_upload_s": _sheet_seconds(perf.get("upload_audio_s")),
                "tts_route": audio_route or "",
                "ambiance": _audio_ambiance_sheet_flag(),
            },
        )

        persist_sunday_bundle(
            date_str=date_str,
            zone=zone_key,
            synth_text=gen.text,
            audio_bytes=audio_bytes_norm,
            audio_mime=audio_mime_norm,
        )
    else:
        audio_path = ""
        persist_sunday_bundle(
            date_str=date_str,
            zone=zone_key,
            synth_text=gen.text,
            audio_bytes=None,
            audio_mime=None,
        )

    readings_cover_signed: str | None = None
    if generate_readings_audio:
        texts = _resolve_texts_for_readings_tts(
            texts=texts, identity=identity, gs=gs, cfg=cfg, zone=zone_key, pref_langue=pref_langue
        )
        readings_plain = plain_readings_for_tts(texts, pref_langue=pref_langue)
        if readings_plain.strip():
            try:
                _flow_overlay_step(
                    _overlay,
                    "3/4 — Audio des lectures AELF (TTS)…",
                    hint=(
                        "Découpage en plusieurs sections — souvent 3–8 min. "
                        f"Fichier final : `AudioLectures/{date_label}/`. "
                        "Rafraîchis la console GCS : les dossiers `Audio/` et `Syntheses/` "
                        "peuvent déjà contenir la synthèse."
                    ),
                    t0=t_flow,
                )
                with st.spinner("LumenVia génère l’audio des lectures (AELF)…"):
                    voice_read_res = _resolve_voice_for_identity(
                        voix_rows,
                        identity=identity,
                        cible="lectures",
                        exclude_voices=[voice_syn],
                    )
                    voice_read = str(voice_read_res["voice"])
                    perf["voice_lectures"] = voice_read
                    perf["voice_lectures_rule_id"] = ((voice_read_res.get("rule") or {}).get("#ID") or "")
                    perf["voice_lectures_fallback"] = bool(voice_read_res.get("fallback"))
                    if debug:
                        if voice_read_res.get("fallback"):
                            st.warning(
                                f"Aucune règle Voix_Audio (lectures) ne matche — fallback **{voice_read}**."
                            )
                        else:
                            st.caption(
                                f"Voix lectures retenue : **{voice_read}** "
                                f"(règle `#ID {perf['voice_lectures_rule_id']}`)."
                            )
                    readings_tts = compose_readings_tts_text(body=readings_plain, templates=templates)
                    rt0 = time.perf_counter()
                    r_bytes, r_mime, r_ext = tts_readings_audio_bytes(
                        cfg=cfg,
                        text=readings_tts,
                        voice_name=voice_read,
                        vertex_client=vx,
                        gemini_api_key=resolve_gemini_api_key(),
                        sunday_date=_sunday_date_for_voice(identity),
                        pref_langue=pref_langue,
                    )
                    perf["readings_tts_s"] = round(time.perf_counter() - rt0, 3)
                    readings_tts_route = last_tts_route()
                day_for_path = str(getattr(identity, "date", "") or "").strip()[:10]
                readings_path = audio_readings_path(
                    day_for_path, gen_entity_id, r_ext, pref_langue=pref_langue
                )
                ru0 = time.perf_counter()
                upload_bytes(
                    gcs=gcs,
                    bucket_name=cfg.gcs_bucket_name,
                    path=readings_path,
                    data=r_bytes,
                    content_type=r_mime,
                )
                perf["readings_upload_s"] = round(time.perf_counter() - ru0, 3)
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="audio",
                    values_by_col={
                        "entity_id": sha256(f"audio_lect|{gen_entity_id}|{readings_path}".encode("utf-8")).hexdigest()[
                            :24
                        ],
                        "gen_entity_id": row_gen["entity_id"],
                        "voice": voice_read,
                        "format": r_ext,
                        "gcs_path": readings_path,
                        "kind": "lectures",
                        "duration_tts_s": _sheet_seconds(perf.get("readings_tts_s")),
                        "duration_upload_s": _sheet_seconds(perf.get("readings_upload_s")),
                        "tts_route": readings_tts_route or "",
                        "ambiance": _audio_ambiance_sheet_flag(),
                    },
                )
                try:
                    readings_cover_signed = (
                        gcs_signed_url(
                            gcs=gcs,
                            bucket_name=str(cfg.gcs_bucket_name).strip(),
                            path=readings_path,
                        )
                        or None
                    )
                except Exception:
                    readings_cover_signed = None
            except Exception as ex:
                st.warning(f"Audio des lectures non publié (synthèse enregistrée quand même) : {ex}")
        else:
            st.warning(
                "Audio des lectures ignoré : le texte agrégé des quatre lectures (AELF) est vide — "
                "vérifie les lectures pour cette date (cache RDC / API AELF)."
            )

    # Optimisation : les downloads de vérification (Cloud → UI) sont coûteux.
    # On ne les fait que si debug est activé.
    if debug:
        st.subheader("Résumé du temps liturgique")
        try:
            dt0 = time.perf_counter()
            txt_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path)
            txt = txt_bytes.decode("utf-8", errors="replace")
            perf["download_text_verify_s"] = round(time.perf_counter() - dt0, 3)
        except Exception as e:
            txt = f"[Erreur lecture Cloud texte] {e}"
        st.text_area("Synthèse", value=txt, height=320)

        try:
            if audio_ok and audio_path:
                da0 = time.perf_counter()
                aud_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=audio_path)
                aud_play, aud_mime_play, _ = normalize_audio_bytes(audio_bytes=aud_bytes, mime_type=audio_mime_norm)
                perf["download_audio_verify_s"] = round(time.perf_counter() - da0, 3)
                st.subheader("Écouter le résumé")
                st.audio(aud_play, format=aud_mime_play)
            else:
                st.info("Pas d’audio synthèse à vérifier (échec TTS ou non demandé).")
        except Exception as e:
            st.error(f"Erreur lecture/lecture audio Cloud: {e}")

    # Fascicule PDF : toujours en dernier (après texte Vertex, audio synthèse, audio lectures) pour que la couverture
    # puisse réutiliser les URLs signées des pistes déjà uploadées sur GCS.
    if generate_pdf and cfg.gcs_bucket_name:
        if generate_readings_audio and not readings_cover_signed:
            st.info(
                "Le PDF sera généré **sans** lien « Écouter les lectures » sur la couverture : "
                "l’audio des lectures n’a pas été produit ou signé dans cette passe.",
                icon="ℹ️",
            )
        try:
            _flow_overlay_step(
                _overlay,
                "4/4 — Fascicule PDF…",
                hint=f"Assemblage puis envoi dans `Fascicules/{date_label}/`.",
                t0=t_flow,
            )
            tpdf0 = time.perf_counter()
            img_b = ap._fetch_liturgy_illustration_full_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
            back_cover_b = None
            try:
                y = str(date_str)[:4]
                back_cover_b = download_bytes(
                    gcs=gcs,
                    bucket_name=str(cfg.gcs_bucket_name).strip(),
                    path=f"Images/thumbs/montage_{y}.png",
                )
            except Exception:
                back_cover_b = None

            # Titre 2 lignes comme dans “Préparer le PDF…” (Psautier uniquement)
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

            # highlight index (best-effort)
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

            ilus_desc_pdf = ""
            if str(cfg.gsheet_id or "").strip():
                try:
                    ilus_desc_pdf = _latest_illustration_description_from_ilus(
                        gspread_client=gs,
                        spreadsheet_id=str(cfg.gsheet_id).strip(),
                        date_str=date_str,
                        zone=zone_key,
                    )
                except Exception:
                    ilus_desc_pdf = ""

            _base_pub = ""
            try:
                s = st.secrets
                _base_pub = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip()
            except Exception:
                pass
            aud_url, aud_note = pdf_synthesis_listen_url(
                date_str=date_str,
                public_app_url=_base_pub or None,
                gcs=gcs,
                bucket_name=str(cfg.gcs_bucket_name).strip(),
                gcs_audio_path=audio_path or None,
            )
            pdf_b = build_liturgy_sunday_pdf_bytes(
                image_bytes=img_b,
                week_title=week_title_pdf,
                date_line=pdf_cover_date_line(date_str, pref_langue),
                meta_line=pdf_cover_meta_line(
                    periode=getattr(identity, "periode", None),
                    annee=getattr(identity, "annee", None),
                    couleur=getattr(identity, "couleur", None),
                    pref_langue=pref_langue,
                ),
                **pdf_liturgy_reading_kwargs(texts),
                synthesis_text=gen.text,
                audio_listen_url=aud_url,
                audio_listen_note=aud_note,
                audio_readings_listen_url=readings_cover_signed,
                illustration_description=_pdf_illustration_description_localized(
                    text_fr=ilus_desc_pdf or "",
                    pref_langue=pref_langue,
                    cfg=cfg,
                ),
                about_markdown=about_markdown_for_lang(pref_langue),
                back_cover_image_bytes=back_cover_b,
                accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                back_cover_highlight_cell_index=highlight_idx,
                pref_langue=pref_langue,
            )
            fasc_path = fascicule_pdf_path(date_str, pref_langue=pref_langue)
            upload_bytes(
                gcs=gcs,
                bucket_name=str(cfg.gcs_bucket_name).strip(),
                path=fasc_path,
                data=pdf_b,
                content_type="application/pdf",
            )
            _append_pdf_export_row(
                gs=gs,
                cfg=cfg,
                date_str=date_str,
                zone=zone_key,
                gen_entity_id=gen_entity_id,
                gcs_path=fasc_path,
                duration_build_s=round(time.perf_counter() - tpdf0, 3),
            )
            st.session_state[f"liturgy_sunday_pdf_{date_str}_{pref_langue}"] = pdf_b
            # Nettoie l’ancienne clé sans langue (téléchargement sinon introuvable).
            st.session_state.pop(f"liturgy_sunday_pdf_{date_str}", None)
            perf["pdf_auto_s"] = round(time.perf_counter() - tpdf0, 3)
        except Exception as e:
            st.warning(f"PDF non généré automatiquement : {e}")
    if debug:
        total_keys = (
            "vertex_text_s",
            "upload_text_s",
            "audio_vertex_s",
            "audio_fallback_s",
            "tts_chunk_total_s",
            "upload_audio_s",
            "download_text_verify_s",
            "download_audio_verify_s",
        )
        perf["perf_total_tracked_s"] = round(
            sum(float(perf.get(k) or 0) for k in total_keys if isinstance(perf.get(k), (int, float))),
            3,
        )
        st.markdown("**Chronométrage (debug)**")
        st.write(perf)

    if audio_ok:
        parts = [f"Régénération terminée pour {date_label} — texte et audio synthèse publiés."]
        level = "success"
    else:
        parts = [
            f"Régénération partielle pour {date_label} — texte publié, "
            f"audio synthèse manquant ({audio_fail_msg or 'erreur TTS'})."
        ]
        level = "warning"
    if generate_readings_audio:
        parts.append("Audio des lectures inclus.")
    if generate_pdf:
        parts.append("Fascicule PDF inclus.")
    if retry_fallback_note:
        parts.insert(0, retry_fallback_note)
        level = "warning"
    return _flow_result(ok=True, level=level, message=" ".join(parts))


def _load_fr_master_synthesis_text(
    *,
    gs: object,
    gcs: object,
    cfg: object,
    date_str: str,
) -> tuple[str, str]:
    """Retourne ``(texte_fr, gen_entity_id_fr)`` depuis GEN/GCS zone France."""
    from core.readings_cache_loader import rdc_zone_for_pref_langue
    from core.sunday_existing_outputs import latest_generation_row_for_sunday

    day = str(date_str or "").strip()[:10]
    zone = rdc_zone_for_pref_langue("FR")
    gen = latest_generation_row_for_sunday(gs=gs, cfg=cfg, date_str=day, zone=zone)
    if not gen:
        return "", ""
    eid = str(gen.get("entity_id") or "").strip()
    tp = str(gen.get("text_gcs_path") or "").strip()
    if not tp:
        return "", eid
    try:
        text = (
            download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=tp)
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception:
        text = ""
    return text, eid


def _run_publish_lang_from_fr_pivot(
    *,
    cfg: object,
    gs: object,
    gcs: object,
    identity: object,
    texts: object,
    target_lang: str,
    fr_synth_text: str,
    generate_readings_audio: bool = True,
    generate_synth_audio: bool = True,
    generate_pdf: bool = True,
    generate_synth_text: bool = True,
    include_catechese_pdf: bool = True,
    force: bool = False,
    _overlay: object | None = None,
) -> dict[str, str]:
    """
    Publie une langue à partir du pivot FR :
    - lectures TTS = lectionnaire natif de la langue ;
    - synthèse = traduction programme du texte FR (pas de rédaction Vertex) ;
    - audio synthèse = TTS du script traduit ;
    - PDF = lectures natives + synthèse localisée.
    """
    import app as ap
    from core.readings_cache_loader import rdc_zone_for_pref_langue
    from core.sunday_media_status import media_status_for_lang
    from core.synthesis_localize import localize_synthesis_from_fr

    lg = coerce_liturgy_pref_langue(target_lang)
    day = str(getattr(identity, "date", "") or "").strip()[:10]
    zone = rdc_zone_for_pref_langue(lg)
    status = media_status_for_lang(
        gs=gs, gcs=gcs, cfg=cfg, date_str=day, pref_langue=lg
    )
    need_synth_audio = generate_synth_audio and (force or not status.synth_audio)
    need_readings = generate_readings_audio and (force or not status.readings_audio)
    need_pdf = generate_pdf and (force or not status.pdf)
    # Texte : seulement si demandé explicitement, ou manquant alors qu’audio synthèse / PDF en a besoin.
    need_text = generate_synth_text and (force or not status.synth_text)
    if (need_synth_audio or need_pdf) and not status.synth_text:
        need_text = True

    if not (need_text or need_synth_audio or need_readings or need_pdf):
        return {
            "level": "info",
            "message": f"{lg} : rien à faire pour la sélection.",
        }

    identity, texts = _ensure_texts_for_pref_langue(
        texts=texts, identity=identity, pref_langue=lg
    )
    fr_body = (fr_synth_text or "").strip()
    needs_pivot = need_text or need_synth_audio or need_pdf
    if needs_pivot and not fr_body:
        return {
            "level": "error",
            "message": f"{lg} : synthèse FR pivot introuvable — génère d’abord le français.",
        }

    localized = ""
    gen_entity_id = str(status.gen_entity_id or "").strip()
    text_path = ""

    if needs_pivot:
        if _overlay is not None:
            _flow_overlay_step(
                _overlay,
                f"LumenVia · {lg} — localisation de la synthèse…",
                hint="Traduction programme du pivot FR (pas de nouvelle rédaction IA).",
            )

        if lg == "FR":
            localized = fr_body
        else:
            localized = localize_synthesis_from_fr(fr_body, target_lang=lg)
        localized = strip_localized_from_banners(localized)

        source_hash = sha256(
            f"localized_from_fr|{day}|{lg}|{sha256(fr_body.encode('utf-8')).hexdigest()[:16]}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        gen_entity_id = sha256(f"{day}|{zone}|{source_hash}".encode("utf-8")).hexdigest()[:24]
        text_path = synthesis_text_path(day, gen_entity_id, pref_langue=lg)
    elif not gen_entity_id:
        gen_entity_id = sha256(
            f"readings_only|{day}|{zone}|{lg}".encode("utf-8")
        ).hexdigest()[:24]

    done: list[str] = []
    issues: list[str] = []

    if need_text:
        upload_text(
            gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path, text=localized
        )
        append_immutable_row(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="generations",
            values_by_col={
                "entity_id": gen_entity_id,
                "date": day,
                "zone": zone,
                "cycle": getattr(identity, "annee", None) or "",
                "season": getattr(identity, "periode", None) or "",
                "length": len(localized.split()),
                "prompt_version": "localized_from_fr_v1",
                "model": "mymemory+locale" if lg != "FR" else "fr_pivot",
                "source_hash": source_hash,
                "text_gcs_path": text_path,
                "text_words": len(localized.split()),
            },
        )
        done.append(f"texte {lg}")
    else:
        # Réutilise le gen existant si possible
        if status.gen_entity_id:
            gen_entity_id = status.gen_entity_id
            try:
                from core.content_locale_paths import synthesis_text_path_candidates

                for cand in synthesis_text_path_candidates(
                    day, gen_entity_id, pref_langue=lg
                ):
                    if blob_exists(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=cand):
                        text_path = cand
                        localized = strip_localized_from_banners(
                            download_bytes(
                                gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=cand
                            )
                            .decode("utf-8", errors="replace")
                            .strip()
                            or localized
                        )
                        break
            except Exception:
                pass

    voix_rows: list[dict] = []
    try:
        voix_rows = ap._load_voix_rules_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=service_account_json_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
        )
    except Exception:
        voix_rows = []

    vx = _readings_tts_vertex_client(cfg)
    templates: dict[str, str] = {}
    try:
        templates = ap._load_prompt_templates_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=service_account_json_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
            pref_langue=lg,
        )
    except Exception:
        templates = {}

    voice_syn = str(
        _resolve_voice_for_identity(voix_rows, identity=identity, cible="synthese")[
            "voice"
        ]
    )

    if need_synth_audio and localized.strip():
        try:
            if _overlay is not None:
                _flow_overlay_step(
                    _overlay,
                    f"LumenVia · {lg} — audio synthèse…",
                    hint="TTS du script traduit — peut prendre plusieurs minutes.",
                )
            from core.sunday_readings_tts import compose_synthesis_tts_text

            spoken = compose_synthesis_tts_text(body=localized, templates=templates)
            a_bytes, a_mime, a_ext = tts_spoken_audio_bytes(
                cfg=cfg,
                text=spoken,
                voice_name=voice_syn,
                vertex_client=vx,
                gemini_api_key=resolve_gemini_api_key(),
                sunday_date=_sunday_date_for_voice(identity),
                cible="synthese",
                pref_langue=lg,
            )
            a_path = audio_synth_path(day, gen_entity_id, a_ext, pref_langue=lg)
            upload_bytes(
                gcs=gcs,
                bucket_name=cfg.gcs_bucket_name,
                path=a_path,
                data=a_bytes,
                content_type=a_mime,
            )
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="audio",
                values_by_col={
                    "entity_id": sha256(
                        f"audio_syn|{gen_entity_id}|{a_path}".encode("utf-8")
                    ).hexdigest()[:24],
                    "gen_entity_id": gen_entity_id,
                    "voice": voice_syn,
                    "format": a_ext,
                    "gcs_path": a_path,
                    "kind": "synthese",
                    "tts_route": last_tts_route() or "",
                    "ambiance": _audio_ambiance_sheet_flag(),
                },
            )
            done.append(f"audio synthèse {lg}")
        except Exception as ex:
            issues.append(f"audio synthèse {lg}: {ex}")

    if need_readings:
        try:
            if _overlay is not None:
                _flow_overlay_step(
                    _overlay,
                    f"LumenVia · {lg} — audio lectures…",
                    hint="TTS du lectionnaire natif — 5–10 min possibles.",
                )
            texts = _resolve_texts_for_readings_tts(
                texts=texts,
                identity=identity,
                gs=gs,
                cfg=cfg,
                zone=zone,
                pref_langue=lg,
            )
            readings_plain = plain_readings_for_tts(texts, pref_langue=lg)
            if not readings_plain.strip():
                issues.append(f"lectures {lg} vides")
            else:
                voice_read = str(
                    _resolve_voice_for_identity(
                        voix_rows,
                        identity=identity,
                        cible="lectures",
                        exclude_voices=[voice_syn],
                    )["voice"]
                )
                readings_tts = compose_readings_tts_text(
                    body=readings_plain, templates=templates
                )
                r_bytes, r_mime, r_ext = tts_readings_audio_bytes(
                    cfg=cfg,
                    text=readings_tts,
                    voice_name=voice_read,
                    vertex_client=vx,
                    gemini_api_key=resolve_gemini_api_key(),
                    sunday_date=_sunday_date_for_voice(identity),
                    pref_langue=lg,
                )
                r_path = audio_readings_path(
                    day, gen_entity_id, r_ext, pref_langue=lg
                )
                upload_bytes(
                    gcs=gcs,
                    bucket_name=cfg.gcs_bucket_name,
                    path=r_path,
                    data=r_bytes,
                    content_type=r_mime,
                )
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="audio",
                    values_by_col={
                        "entity_id": sha256(
                            f"audio_lect|{gen_entity_id}|{r_path}".encode("utf-8")
                        ).hexdigest()[:24],
                        "gen_entity_id": gen_entity_id,
                        "voice": voice_read,
                        "format": r_ext,
                        "gcs_path": r_path,
                        "kind": "lectures",
                        "tts_route": last_tts_route() or "",
                        "ambiance": _audio_ambiance_sheet_flag(),
                    },
                )
                done.append(f"audio lectures {lg}")
        except Exception as ex:
            issues.append(f"audio lectures {lg}: {ex}")

    if need_pdf:
        try:
            if _overlay is not None:
                _flow_overlay_step(
                    _overlay,
                    f"LumenVia · {lg} — fascicule PDF…",
                    hint="Assemblage lectures natives + synthèse localisée.",
                )
            bucket = str(cfg.gcs_bucket_name).strip()
            img_b = None
            try:
                img_b = ap._fetch_liturgy_illustration_full_bytes(
                    gcs=gcs, cfg=cfg, date_str=day
                )
            except Exception:
                img_b = None
            ilus_desc = ""
            try:
                from core.weekly_email_urls import _latest_illustration_description_from_ilus

                ilus_desc = _latest_illustration_description_from_ilus(
                    gspread_client=gs,
                    spreadsheet_id=str(cfg.gsheet_id).strip(),
                    date_str=day,
                    zone=zone,
                )
            except Exception:
                ilus_desc = ""
            _base_pub = ""
            try:
                s = st.secrets
                _base_pub = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip()
            except Exception:
                pass
            aud_url, aud_note = pdf_synthesis_listen_url(
                date_str=day,
                public_app_url=_base_pub or None,
                gcs=gcs,
                bucket_name=bucket,
                gcs_audio_path=ap._synthesis_audio_gcs_path_for_gen(
                    gs=gs, cfg=cfg, gen_entity_id=gen_entity_id
                ),
                gs=gs,
                cfg=cfg,
                gen_entity_id=gen_entity_id,
            )
            readings_pdf_signed = None
            try:
                from core.sunday_existing_outputs import fetch_existing_readings_audio

                _ra, rpath, _rv = fetch_existing_readings_audio(
                    gs=gs, gcs=gcs, cfg=cfg, date_str=day, zone=zone
                )
                if rpath:
                    readings_pdf_signed = gcs_signed_url(
                        gcs=gcs, bucket_name=bucket, path=rpath
                    )
            except Exception:
                readings_pdf_signed = None
            back_cover_b = None
            try:
                y = str(day)[:4]
                back_cover_b = download_bytes(
                    gcs=gcs,
                    bucket_name=bucket,
                    path=f"Images/thumbs/montage_{y}.png",
                )
            except Exception:
                back_cover_b = None
            highlight_idx = None
            try:
                manifest = json.loads(
                    Path("data/manifests/illustration_pipeline.json").read_text(
                        encoding="utf-8"
                    )
                )
                targets = manifest.get("targets") or []
                year_dates = [
                    str(t.get("date") or "")[:10]
                    for t in targets
                    if str(t.get("date") or "").startswith(str(day)[:4])
                ]
                if day in year_dates:
                    highlight_idx = int(year_dates.index(day))
            except Exception:
                highlight_idx = None
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
            synth_for_pdf = localized
            if not include_catechese_pdf:
                try:
                    synth_for_pdf = ap._strip_catechese_bridge(synth_for_pdf)
                except Exception:
                    pass
            pdf_b = build_liturgy_sunday_pdf_bytes(
                image_bytes=img_b,
                week_title=week_title_pdf,
                date_line=pdf_cover_date_line(day, lg),
                meta_line=pdf_cover_meta_line(
                    periode=getattr(identity, "periode", None),
                    annee=getattr(identity, "annee", None),
                    couleur=getattr(identity, "couleur", None),
                    pref_langue=lg,
                ),
                **pdf_liturgy_reading_kwargs(texts),
                synthesis_text=synth_for_pdf,
                audio_listen_url=aud_url,
                audio_listen_note=aud_note,
                audio_readings_listen_url=readings_pdf_signed,
                illustration_description=_pdf_illustration_description_localized(
                    text_fr=ilus_desc or "",
                    pref_langue=lg,
                    cfg=cfg,
                ),
                about_markdown=about_markdown_for_lang(lg),
                back_cover_image_bytes=back_cover_b,
                accent_hex=liturgical_accent_hex(getattr(identity, "couleur", None)),
                back_cover_highlight_cell_index=highlight_idx,
                pref_langue=lg,
            )
            fasc_path = fascicule_pdf_path(day, pref_langue=lg)
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket,
                path=fasc_path,
                data=pdf_b,
                content_type="application/pdf",
            )
            _append_pdf_export_row(
                gs=gs,
                cfg=cfg,
                date_str=day,
                zone=zone,
                gen_entity_id=gen_entity_id,
                gcs_path=fasc_path,
                duration_build_s=0,
            )
            st.session_state[f"liturgy_sunday_pdf_{day}_{lg}"] = pdf_b
            done.append(f"PDF {lg}")
        except Exception as ex:
            issues.append(f"PDF {lg}: {ex}")

    if issues and not done:
        return {"level": "error", "message": f"{lg} : " + " · ".join(issues)}
    if issues:
        return {
            "level": "warning",
            "message": f"{lg} : " + ", ".join(done) + " — " + " · ".join(issues),
        }
    if not done:
        return {"level": "info", "message": f"{lg} : rien à faire."}
    return {"level": "success", "message": f"{lg} : " + ", ".join(done) + "."}


def _run_multilang_sunday_batch(
    *,
    cfg: object,
    gs: object,
    gcs: object,
    identity: object,
    texts: object,
    langs: list[str],
    generate_readings_audio: bool = True,
    generate_synth_audio: bool = True,
    generate_pdf: bool = True,
    include_catechese_pdf: bool = True,
    force: bool = False,
    ensure_fr_first: bool = True,
    pct: int = 20,
    include_takeaways: bool = True,
    include_catechese_bridge: bool = True,
    _overlay: object | None = None,
) -> dict[str, str]:
    """
    Lot multi-langues : FR rédigé (Vertex) si besoin, puis localisation programme + médias.
    """
    from core.readings_cache_loader import rdc_zone_for_pref_langue

    day = str(getattr(identity, "date", "") or "").strip()[:10]
    wanted = [coerce_liturgy_pref_langue(x) for x in langs]
    messages: list[str] = []
    worst = "success"

    fr_text, _fr_eid = _load_fr_master_synthesis_text(
        gs=gs, gcs=gcs, cfg=cfg, date_str=day
    )
    if ensure_fr_first and (not fr_text.strip() or force and "FR" in wanted):
        # Ne force le pivot FR que si au moins une langue hors lectures-only en a besoin,
        # ou si FR lui-même est demandé.
        needs_pivot_langs = bool(wanted)  # batch historique : toujours OK de générer FR
        if needs_pivot_langs and (not fr_text.strip() or (force and "FR" in wanted)):
            if _overlay is not None:
                _flow_overlay_step(
                    _overlay,
                    "LumenVia · FR — rédaction synthèse (Vertex)…",
                    hint="Pivot français requis avant localisation des autres langues.",
                )
            total_words = sum(
                len(str(getattr(texts, k, "") or "").split())
                for k in (
                    "premiere_lecture",
                    "psaume",
                    "deuxieme_lecture",
                    "evangile",
                )
            )
            fr_flow = _run_generate_sunday_flow(
                _overlay=_overlay or st.empty(),
                identity=identity,
                texts=texts,
                zone=rdc_zone_for_pref_langue("FR"),
                total_words=max(total_words, 80),
                pct=int(pct),
                include_takeaways=include_takeaways,
                include_catechese_bridge=include_catechese_bridge,
                generate_pdf=generate_pdf and "FR" in wanted,
                generate_readings_audio=generate_readings_audio and "FR" in wanted,
                debug=False,
                cfg=cfg,
                pref_langue="FR",
            )
            messages.append(str(fr_flow.get("message") or "FR généré."))
            if fr_flow.get("level") == "error":
                return {
                    "level": "error",
                    "message": "Échec pivot FR — " + str(fr_flow.get("message") or ""),
                }
            fr_text, _fr_eid = _load_fr_master_synthesis_text(
                gs=gs, gcs=gcs, cfg=cfg, date_str=day
            )

    # FR requis seulement si on localise (pas pour lectures natives seules).
    needs_any_pivot = bool(
        generate_synth_audio or generate_pdf or ("FR" in wanted and ensure_fr_first)
    )
    if needs_any_pivot and not fr_text.strip():
        return {
            "level": "error",
            "message": "Synthèse FR absente — impossible de localiser les autres langues.",
        }

    for i, lg in enumerate(wanted, start=1):
        if _overlay is not None:
            _flow_overlay_step(
                _overlay,
                f"Publication multi-langues — {lg} ({i}/{len(wanted)})…",
                hint="Localisation FR→langue puis audios / PDF selon les cases cochées.",
            )
        if lg == "FR" and ensure_fr_first and not force:
            # Déjà traité par le flux FR (sauf pièces manquantes hors force).
            from core.sunday_media_status import media_status_for_lang

            st_fr = media_status_for_lang(
                gs=gs, gcs=gcs, cfg=cfg, date_str=day, pref_langue="FR"
            )
            missing = []
            if generate_readings_audio and not st_fr.readings_audio:
                missing.append("readings")
            if generate_synth_audio and not st_fr.synth_audio:
                missing.append("synth_audio")
            if generate_pdf and not st_fr.pdf:
                missing.append("pdf")
            if not missing:
                messages.append("FR : déjà prêt.")
                continue
        res = _run_publish_lang_from_fr_pivot(
            cfg=cfg,
            gs=gs,
            gcs=gcs,
            identity=identity,
            texts=texts,
            target_lang=lg,
            fr_synth_text=fr_text,
            generate_readings_audio=generate_readings_audio,
            generate_synth_audio=generate_synth_audio,
            generate_pdf=generate_pdf,
            include_catechese_pdf=include_catechese_pdf,
            force=force,
            _overlay=_overlay,
        )
        messages.append(str(res.get("message") or lg))
        lv = str(res.get("level") or "info")
        if lv == "error":
            worst = "error"
        elif lv == "warning" and worst == "success":
            worst = "warning"

    return {"level": worst, "message": "\n".join(messages)}


def _run_multilang_from_cell_selection(
    *,
    cfg: object,
    gs: object,
    gcs: object,
    identity: object,
    texts: object,
    selected: list[tuple[str, str]],
    include_catechese_pdf: bool = True,
    pct: int = 20,
    include_takeaways: bool = True,
    include_catechese_bridge: bool = True,
    force_kinds: set[tuple[str, str]] | None = None,
    _overlay: object | None = None,
) -> dict[str, str]:
    """
    Génération granulaire depuis le tableau (paires langue × média).

    ``force_kinds`` : paires déjà publiées à régénérer (ex. retester la bande-son).
    """
    from core.readings_cache_loader import rdc_zone_for_pref_langue

    day = str(getattr(identity, "date", "") or "").strip()[:10]
    force_set = {
        (coerce_liturgy_pref_langue(a), str(b)) for a, b in (force_kinds or set())
    }
    by_sel: dict[str, set[str]] = {}
    for lg0, kind in selected:
        lg = coerce_liturgy_pref_langue(lg0)
        by_sel.setdefault(lg, set()).add(str(kind))

    if not by_sel:
        return {"level": "info", "message": "Aucune sélection."}

    messages: list[str] = []
    worst = "success"
    fr_text, _fr_eid = _load_fr_master_synthesis_text(
        gs=gs, gcs=gcs, cfg=cfg, date_str=day
    )

    needs_fr_pivot = any(
        (lg != "FR" and (kinds & {"synth_text", "synth_audio", "pdf"}))
        or (lg == "FR" and ("synth_text" in kinds))
        for lg, kinds in by_sel.items()
    )
    fr_kinds = by_sel.get("FR", set())
    force_fr_text = ("FR", "synth_text") in force_set

    if needs_fr_pivot and (not fr_text.strip() or force_fr_text):
        if _overlay is not None:
            _flow_overlay_step(
                _overlay,
                "LumenVia · FR — rédaction synthèse (Vertex)…",
                hint=(
                    "Pivot français — régénération."
                    if force_fr_text
                    else "Pivot français requis."
                ),
            )
        total_words = sum(
            len(str(getattr(texts, k, "") or "").split())
            for k in (
                "premiere_lecture",
                "psaume",
                "deuxieme_lecture",
                "evangile",
            )
        )
        gen_fr_pdf = "pdf" in fr_kinds
        gen_fr_readings = "readings_audio" in fr_kinds
        fr_flow = _run_generate_sunday_flow(
            _overlay=_overlay or st.empty(),
            identity=identity,
            texts=texts,
            zone=rdc_zone_for_pref_langue("FR"),
            total_words=max(total_words, 80),
            pct=int(pct),
            include_takeaways=include_takeaways,
            include_catechese_bridge=include_catechese_bridge,
            generate_pdf=gen_fr_pdf,
            generate_readings_audio=gen_fr_readings,
            debug=False,
            cfg=cfg,
            pref_langue="FR",
        )
        messages.append(str(fr_flow.get("message") or "FR généré."))
        if fr_flow.get("level") == "error":
            return {
                "level": "error",
                "message": "Échec pivot FR — " + str(fr_flow.get("message") or ""),
            }
        fr_text, _fr_eid = _load_fr_master_synthesis_text(
            gs=gs, gcs=gcs, cfg=cfg, date_str=day
        )
        # Le flux Vertex produit texte + audio synthèse (+ PDF/lectures si demandés).
        fr_kinds.discard("synth_text")
        fr_kinds.discard("synth_audio")
        if gen_fr_pdf:
            fr_kinds.discard("pdf")
        if gen_fr_readings:
            fr_kinds.discard("readings_audio")
        if not fr_kinds:
            by_sel.pop("FR", None)
        else:
            by_sel["FR"] = fr_kinds

    order = [lg for lg in ("FR", "DE", "EN", "ES", "IT", "PT") if lg in by_sel]
    for extra in by_sel:
        if extra not in order:
            order.append(extra)

    for i, lg in enumerate(order, start=1):
        kinds = by_sel.get(lg) or set()
        if not kinds:
            continue
        if _overlay is not None:
            _kind_lbl = {
                "synth_text": "synthèse texte",
                "synth_audio": "audio synthèse",
                "readings_audio": "audio lectures",
                "pdf": "PDF",
            }
            labels = [_kind_lbl.get(k, k) for k in sorted(kinds)]
            _flow_overlay_step(
                _overlay,
                f"LumenVia · {lg} ({i}/{len(order)}) — {' · '.join(labels)}…",
                hint="Progression détaillée : localisation / TTS / PDF selon la sélection.",
            )
        force_lg = any((lg, k) in force_set for k in kinds)
        res = _run_publish_lang_from_fr_pivot(
            cfg=cfg,
            gs=gs,
            gcs=gcs,
            identity=identity,
            texts=texts,
            target_lang=lg,
            fr_synth_text=fr_text,
            generate_synth_text="synth_text" in kinds,
            generate_readings_audio="readings_audio" in kinds,
            generate_synth_audio="synth_audio" in kinds,
            generate_pdf="pdf" in kinds,
            include_catechese_pdf=include_catechese_pdf,
            force=force_lg,
            _overlay=_overlay,
        )
        messages.append(str(res.get("message") or lg))
        lv = str(res.get("level") or "info")
        if lv == "error":
            worst = "error"
        elif lv == "warning" and worst == "success":
            worst = "warning"

    return {"level": worst, "message": "\n".join(messages)}
