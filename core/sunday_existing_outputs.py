"""Lecture des médias déjà produits (Sheets + GCS) pour un dimanche donné."""

from __future__ import annotations

from core.audio_utils import normalize_audio_bytes
from core.illustration_thumbs import THUMB_GCS_PREFIX
from core.sheets_db import fetch_records
from core.storage import blob_exists, download_bytes
from core.weekly_email_urls import is_readings_audio_gcs_path as _is_readings_audio_gcs_path


def sheet_day_key(raw: object) -> str:
    """Normalise une date issue de Sheets (YYYY-MM-DD ou préfixe ISO) pour comparaisons fiables."""
    s = str(raw or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def fetch_existing_readings_audio(
    *,
    gs: object,
    gcs: object,
    cfg: object,
    date_str: str,
    zone: str,
) -> tuple[tuple[bytes, str] | None, str | None]:
    """Dernier audio « lectures seules ''AudioLectures/'' pour la dernière génération du jour."""
    try:
        day = sheet_day_key(date_str)
        # limit=0 : parcourir tout l’onglet — fetch_records ne fait que tronquer la liste déjà chargée par gspread.
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=0)
        gens_day = [
            g
            for g in gens
            if sheet_day_key(g.get("date")) == day and str(g.get("zone", "")).strip() == zone
        ]
        if not gens_day:
            return None, None
        latest = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        gen_eid = str(latest.get("entity_id") or "").strip()
        if not gen_eid:
            return None, None

        audios = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=0)
        aud_rows = [
            a
            for a in audios
            if str(a.get("gen_entity_id", "")).strip() == gen_eid
            and _is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))
        ]
        if not aud_rows:
            prefix = f"AudioLectures/{day}/".replace("\\", "/")
            aud_rows = [
                a
                for a in audios
                if _is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))
                and prefix in str(a.get("gcs_path") or "").replace("\\", "/")
            ]
        if not aud_rows:
            return None, None
        aud = sorted(aud_rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
        path = str(aud.get("gcs_path") or "").strip()
        if not path:
            return None, None
        raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        mime_guess = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        b, mime, _ = normalize_audio_bytes(audio_bytes=raw, mime_type=mime_guess)
        return (b, mime), path
    except Exception:
        return None, None


def latest_generation_row_for_sunday(*, gs: object, cfg: object, date_str: str, zone: str) -> dict | None:
    """Dernière ligne ``generations`` pour un dimanche et une zone."""
    try:
        day = sheet_day_key(date_str)
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=0)
        gens_day = [
            g
            for g in gens
            if sheet_day_key(g.get("date")) == day and str(g.get("zone", "")).strip() == zone
        ]
        if not gens_day:
            return None
        return sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
    except Exception:
        return None


def has_readings_audio_for_gen(
    *,
    gs: object,
    cfg: object,
    gen_entity_id: str,
    gcs: object | None = None,
) -> bool:
    """True si une ligne ``audio`` lectures existe et que l’objet GCS est présent (si ``gcs`` fourni)."""
    ge = str(gen_entity_id or "").strip()
    if not ge:
        return False
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    try:
        audios = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=0)
        for a in audios:
            if str(a.get("gen_entity_id") or "").strip() != ge:
                continue
            path = str(a.get("gcs_path") or "").strip()
            if not path or not _is_readings_audio_gcs_path(path):
                continue
            if gcs is not None and bucket:
                try:
                    if blob_exists(gcs=gcs, bucket_name=bucket, path=path):
                        return True
                except Exception:
                    continue
            else:
                return True
        return False
    except Exception:
        return False


def synthesis_audio_gcs_path_for_gen(*, gs: object, cfg: object, gen_entity_id: str) -> str | None:
    ge = str(gen_entity_id or "").strip()
    if not ge:
        return None
    try:
        audios = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=0)
        rows = [
            a
            for a in audios
            if str(a.get("gen_entity_id") or "").strip() == ge
            and not _is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))
        ]
        if not rows:
            return None
        aud = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
        p = str(aud.get("gcs_path") or "").strip()
        return p or None
    except Exception:
        return None


def pdf_synthesis_listen_url(
    *,
    date_str: str,
    public_app_url: str | None,
    gcs: object,
    bucket_name: str,
    gcs_audio_path: str | None = None,
    gs: object | None = None,
    cfg: object | None = None,
    gen_entity_id: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Lien « Écouter la synthèse » pour la couverture PDF.

    Préfère une URL signée GCS (fichier ``Audio/…``) lorsqu’elle est disponible ;
    sinon retombe sur le lien public app (``PUBLIC_APP_URL`` + ``?sunday=``).
    """
    from core.gcs_signed_urls import gcs_signed_url
    from core.public_listen_url import public_app_listen_url

    url, note = public_app_listen_url(date_str=date_str, base_public_app_url=public_app_url)
    p = (gcs_audio_path or "").strip()
    if not p and gs is not None and cfg is not None:
        ge = str(gen_entity_id or "").strip()
        if ge:
            p = synthesis_audio_gcs_path_for_gen(gs=gs, cfg=cfg, gen_entity_id=ge) or ""
    bucket = str(bucket_name or "").strip()
    if p and bucket:
        try:
            signed = gcs_signed_url(gcs=gcs, bucket_name=bucket, path=p)
            if signed:
                return signed, note
        except Exception:
            pass
    return url, note


def fetch_existing_sunday_bundle(
    *,
    gs: object,
    gcs: object,
    cfg: object,
    date_str: str,
    zone: str,
) -> tuple[tuple[bytes, str] | None, str | None, str | None]:
    """Dernière génération du jour : (audio bytes, mime) + texte synthèse GCS + path audio."""
    try:
        day = sheet_day_key(date_str)
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=0)
        gens_day = [
            g
            for g in gens
            if sheet_day_key(g.get("date")) == day and str(g.get("zone", "")).strip() == zone
        ]
        if not gens_day:
            return None, None, None
        latest = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        gen_eid = str(latest.get("entity_id") or "").strip()
        if not gen_eid:
            return None, None, None

        syn_text: str | None = None
        tp = str(latest.get("text_gcs_path") or "").strip()
        if tp:
            try:
                syn_text = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=tp).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                syn_text = None

        audios = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=0)
        aud_rows = [
            a
            for a in audios
            if str(a.get("gen_entity_id", "")).strip() == gen_eid
            and not _is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))
        ]
        if not aud_rows:
            return None, syn_text, None
        aud = sorted(aud_rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
        path = str(aud.get("gcs_path") or "").strip()
        if not path:
            return None, syn_text, None
        raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        mime_guess = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        b, mime, _ = normalize_audio_bytes(audio_bytes=raw, mime_type=mime_guess)
        return (b, mime), syn_text, path
    except Exception:
        return None, None, None


def fetch_liturgy_illustration_display_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """Vignette ``Images/thumbs`` si présente, sinon image pleine taille (affiches / grille)."""
    year = date_str[:4]
    thumb_path = f"{THUMB_GCS_PREFIX}/{year}/{date_str}.webp"
    try:
        return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=thumb_path)
    except Exception:
        pass
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        path = f"Images/illustrations/{year}/{date_str}{ext}"
        try:
            return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        except Exception:
            continue
    return None


def fetch_liturgy_illustration_full_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """Image pleine résolution (ex. couverture PDF), sans passer par la vignette."""
    year = date_str[:4]
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        path = f"Images/illustrations/{year}/{date_str}{ext}"
        try:
            return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        except Exception:
            continue
    return None


def fetch_existing_fascicule_pdf_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """PDF déjà généré et stocké sous Fascicules/ (si présent)."""
    path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
    try:
        return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
    except Exception:
        return None
