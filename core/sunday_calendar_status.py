"""Statut indicatif des contenus publiés par dimanche (pour mini-calendrier page Dimanche)."""

from __future__ import annotations

from datetime import date

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.sheets_db import build_gspread_client, fetch_records
from core.storage import blob_exists
from core.weekly_email_urls import is_readings_audio_gcs_path


def compute_month_content_status(
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
    ``service_account_fp`` : conservé pour compatibilité avec la clé de cache Streamlit (non utilisé ici).
    """
    del service_account_fp  # clé de cache uniquement
    out: dict[str, dict[str, bool]] = {}
    try:
        gs = build_gspread_client(load_config().gcp_service_account)
        gens = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="generations", limit=6000, use_cache=True)
        aud = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="audio", limit=6000, use_cache=True)
    except Exception:
        gens, aud = [], []

    ypref = f"{int(year)}-"
    gen_by_date: dict[str, dict] = {}
    for r in gens:
        if str(r.get("zone") or "").strip() != zone:
            continue
        ds = str(r.get("date") or "").strip()[:10]
        if len(ds) != 10 or not ds.startswith(ypref):
            continue
        try:
            d = date.fromisoformat(ds)
        except Exception:
            continue
        if d.year != int(year) or d.month != int(month):
            continue
        prev = gen_by_date.get(ds)
        if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
            gen_by_date[ds] = r

    allowed_gen_ids = {
        str((g or {}).get("entity_id") or "").strip()
        for g in gen_by_date.values()
        if str((g or {}).get("entity_id") or "").strip()
    }
    audio_gen_ids = {
        str(r.get("gen_entity_id") or "").strip()
        for r in aud
        if str(r.get("gen_entity_id") or "").strip() in allowed_gen_ids
        and not is_readings_audio_gcs_path(str(r.get("gcs_path") or ""))
    }
    readings_audio_gen_ids = {
        str(r.get("gen_entity_id") or "").strip()
        for r in aud
        if str(r.get("gen_entity_id") or "").strip() in allowed_gen_ids
        and is_readings_audio_gcs_path(str(r.get("gcs_path") or ""))
    }

    pdf_exists: set[str] = set()
    if bucket_name:
        try:
            cfg2 = load_config()
            gcs = build_gcs_client(cfg2.gcp_service_account)
            for ds in gen_by_date.keys():
                path = f"Fascicules/{ds}/lumenvia_dimanche_{ds}.pdf"
                try:
                    if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                        pdf_exists.add(ds)
                except Exception:
                    continue
        except Exception:
            pass

    import calendar as _cal

    cal = _cal.Calendar(firstweekday=0)
    for d in cal.itermonthdates(int(year), int(month)):
        if d.month != int(month):
            continue
        if d.weekday() != 6:
            continue
        ds = d.isoformat()
        g = gen_by_date.get(ds)
        has_text = bool(str((g or {}).get("text_gcs_path") or "").strip())
        gen_id = str((g or {}).get("entity_id") or "").strip()
        has_audio = bool(gen_id and gen_id in audio_gen_ids)
        has_readings_audio = bool(gen_id and gen_id in readings_audio_gen_ids)
        out[ds] = {
            "text": has_text,
            "audio": has_audio,
            "pdf": (ds in pdf_exists),
            "readings_audio": has_readings_audio,
        }
    return out
