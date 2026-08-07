"""État des médias dimanche par langue (GCS / chemins localisés)."""

from __future__ import annotations

from dataclasses import dataclass

from core.content_locale_paths import (
    audio_readings_path_candidates,
    audio_synth_path_candidates,
    fascicule_pdf_path_candidates,
    synthesis_text_path_candidates,
)
from core.liturgy_day import supported_liturgy_langs
from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue
from core.readings_cache_loader import rdc_zone_for_pref_langue
from core.storage import blob_exists
from core.sunday_existing_outputs import (
    audio_ambiance_for_gen,
    audio_voice_for_gen,
    fetch_existing_sunday_bundle,
    has_readings_audio_for_gen,
    latest_generation_row_for_sunday,
    readings_audio_gcs_path_for_gen,
    sheet_day_key,
    synthesis_audio_gcs_path_for_gen,
)
from core.sunday_tts_labels import format_audio_voice_info


@dataclass(frozen=True)
class LangMediaStatus:
    lang: str
    zone: str
    synth_text: bool
    synth_audio: bool
    readings_audio: bool
    pdf: bool
    gen_entity_id: str = ""
    synth_text_url: str | None = None
    synth_audio_url: str | None = None
    readings_audio_url: str | None = None
    pdf_url: str | None = None
    synth_audio_voice: str | None = None
    readings_audio_voice: str | None = None
    synth_audio_voice_info: str | None = None
    readings_audio_voice_info: str | None = None
    # True = bande-son / ambiance ; False = voix seule ; None = inconnu (anciens fichiers)
    synth_audio_ambiance: bool | None = None
    readings_audio_ambiance: bool | None = None

    @property
    def ready_count(self) -> int:
        return sum(
            [
                self.synth_text,
                self.synth_audio,
                self.readings_audio,
                self.pdf,
            ]
        )

    @property
    def all_ready(self) -> bool:
        return self.ready_count >= 4


def _first_blob_path(gcs: object, bucket: str, paths: list[str]) -> str | None:
    if not gcs or not bucket:
        return None
    for p in paths:
        path = str(p or "").strip()
        if not path:
            continue
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket, path=path):
                return path
        except Exception:
            continue
    return None


def _any_blob(gcs: object, bucket: str, paths: list[str]) -> bool:
    return _first_blob_path(gcs, bucket, paths) is not None


def _sign_gcs_path(
    *,
    gcs: object | None,
    bucket: str,
    path: str | None,
    expires_s: int = 7 * 24 * 3600,
) -> str | None:
    """URL signée sans re-vérifier l’existence (le chemin a déjà été résolu)."""
    p = str(path or "").strip()
    if not gcs or not bucket or not p:
        return None
    try:
        return gcs.bucket(bucket).blob(p).generate_signed_url(
            version="v4",
            expiration=int(expires_s),
            method="GET",
        )
    except Exception:
        return None


def media_status_for_lang(
    *,
    gs: object,
    gcs: object | None,
    cfg: object,
    date_str: str,
    pref_langue: object,
) -> LangMediaStatus:
    """Statut GCS / Sheets pour un couple (date, langue)."""
    lg = normalize_pref_langue(pref_langue)
    day = sheet_day_key(date_str)
    zone = rdc_zone_for_pref_langue(lg)
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    gen = latest_generation_row_for_sunday(gs=gs, cfg=cfg, date_str=day, zone=zone)
    gen_eid = ""
    synth_text = False
    synth_audio = False
    readings = False
    synth_text_path: str | None = None
    synth_audio_path: str | None = None
    readings_path: str | None = None

    if gen:
        tp = str(gen.get("text_gcs_path") or "").replace("\\", "/")
        # Accepter la génération si chemin localisé ou (FR) legacy.
        ok_lang = (
            f"/{lg}/" in tp
            or tp.startswith(f"{lg}/")
            or (
                lg == DEFAULT_PREF_LANGUE
                and not any(f"/{x}/" in tp for x in ("DE", "EN", "ES", "IT", "de", "en", "es", "it"))
            )
        )
        if ok_lang:
            gen_eid = str(gen.get("entity_id") or "").strip()
            if gcs and bucket and gen_eid:
                synth_text_path = _first_blob_path(
                    gcs,
                    bucket,
                    synthesis_text_path_candidates(day, gen_eid, pref_langue=lg),
                )
                if not synth_text_path and tp:
                    try:
                        if blob_exists(gcs=gcs, bucket_name=bucket, path=tp):
                            synth_text_path = tp
                    except Exception:
                        synth_text_path = None
                synth_text = bool(synth_text_path)

                sheet_syn = synthesis_audio_gcs_path_for_gen(
                    gs=gs, cfg=cfg, gen_entity_id=gen_eid
                )
                syn_cands = (
                    ([sheet_syn] if sheet_syn else [])
                    + audio_synth_path_candidates(day, gen_eid, "wav", pref_langue=lg)
                    + audio_synth_path_candidates(day, gen_eid, "mp3", pref_langue=lg)
                )
                synth_audio_path = _first_blob_path(gcs, bucket, syn_cands)
                synth_audio = bool(synth_audio_path)

                sheet_read = readings_audio_gcs_path_for_gen(
                    gs=gs, cfg=cfg, gen_entity_id=gen_eid
                )
                if sheet_read and _first_blob_path(gcs, bucket, [sheet_read]):
                    readings_path = sheet_read
                    readings = True
                else:
                    readings = has_readings_audio_for_gen(
                        gs=gs, cfg=cfg, gen_entity_id=gen_eid, gcs=gcs
                    )
                    if not readings:
                        readings_path = _first_blob_path(
                            gcs,
                            bucket,
                            audio_readings_path_candidates(day, gen_eid, "wav", pref_langue=lg)
                            + audio_readings_path_candidates(day, gen_eid, "mp3", pref_langue=lg),
                        )
                        readings = bool(readings_path)
                    elif sheet_read:
                        readings_path = sheet_read
            elif gen_eid:
                # Sans GCS : se fier au bundle Sheets.
                try:
                    audio, text, _path, _v = fetch_existing_sunday_bundle(
                        gs=gs,
                        gcs=gcs,
                        cfg=cfg,
                        date_str=day,
                        zone=zone,
                        pref_langue=lg,
                    )
                    synth_text = bool((text or "").strip())
                    synth_audio = audio is not None
                    readings = has_readings_audio_for_gen(
                        gs=gs, cfg=cfg, gen_entity_id=gen_eid, gcs=None
                    )
                except Exception:
                    pass

    pdf_path: str | None = None
    pdf = False
    if gcs and bucket:
        pdf_path = _first_blob_path(gcs, bucket, fascicule_pdf_path_candidates(day, pref_langue=lg))
        pdf = bool(pdf_path)

    synth_voice: str | None = None
    readings_voice: str | None = None
    synth_amb: bool | None = None
    readings_amb: bool | None = None
    if gen_eid and (synth_audio or readings):
        if synth_audio:
            synth_voice = audio_voice_for_gen(
                gs=gs, cfg=cfg, gen_entity_id=gen_eid, readings=False
            )
            synth_amb = audio_ambiance_for_gen(
                gs=gs, cfg=cfg, gen_entity_id=gen_eid, readings=False
            )
        if readings:
            readings_voice = audio_voice_for_gen(
                gs=gs, cfg=cfg, gen_entity_id=gen_eid, readings=True
            )
            readings_amb = audio_ambiance_for_gen(
                gs=gs, cfg=cfg, gen_entity_id=gen_eid, readings=True
            )

    return LangMediaStatus(
        lang=lg,
        zone=zone,
        synth_text=bool(synth_text),
        synth_audio=bool(synth_audio),
        readings_audio=bool(readings),
        pdf=bool(pdf),
        gen_entity_id=gen_eid,
        synth_text_url=_sign_gcs_path(gcs=gcs, bucket=bucket, path=synth_text_path),
        synth_audio_url=_sign_gcs_path(gcs=gcs, bucket=bucket, path=synth_audio_path),
        readings_audio_url=_sign_gcs_path(gcs=gcs, bucket=bucket, path=readings_path),
        pdf_url=_sign_gcs_path(gcs=gcs, bucket=bucket, path=pdf_path),
        synth_audio_voice=synth_voice,
        readings_audio_voice=readings_voice,
        synth_audio_voice_info=format_audio_voice_info(
            date_str=day, cible="synthese", voice_name=synth_voice
        )
        if synth_voice
        else None,
        readings_audio_voice_info=format_audio_voice_info(
            date_str=day, cible="lectures", voice_name=readings_voice
        )
        if readings_voice
        else None,
        synth_audio_ambiance=synth_amb,
        readings_audio_ambiance=readings_amb,
    )


def media_status_matrix(
    *,
    gs: object,
    gcs: object | None,
    cfg: object,
    date_str: str,
    langs: tuple[str, ...] | None = None,
) -> list[LangMediaStatus]:
    """Une ligne par langue supportée."""
    use = langs or supported_liturgy_langs()
    return [
        media_status_for_lang(
            gs=gs, gcs=gcs, cfg=cfg, date_str=date_str, pref_langue=lg
        )
        for lg in use
    ]


def status_mark(ok: bool) -> str:
    return "✅" if ok else "⬜"


def status_cell(ok: bool, url: str | None, *, icon: str) -> str:
    """Case tableau : pastille + icône cliquable si URL signée disponible."""
    if not ok:
        return "⬜"
    if url:
        return f"✅ [{icon}]({url})"
    return "✅"


__all__ = [
    "LangMediaStatus",
    "media_status_for_lang",
    "media_status_matrix",
    "status_cell",
    "status_mark",
]
