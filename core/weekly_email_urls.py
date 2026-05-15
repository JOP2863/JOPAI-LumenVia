"""Liens signés (PDF, audios, illustration) pour l’e-mail hebdomadaire — logique pure core (Sheets + GCS)."""

from __future__ import annotations

from core.gcp_clients import build_gcs_client
from core.gcs_signed_urls import gcs_first_signed_url, gcs_signed_url
from core.sheets_db import fetch_records, sheet_row_status_is_live


def _latest_illustration_description_from_ilus(
    *,
    gspread_client: object,
    spreadsheet_id: str,
    date_str: str,
    zone: str,
) -> str:
    """Dernière ligne **Actif** de ``liturgy_illustrations`` / ILUS pour (date, zone)."""
    sid = str(spreadsheet_id or "").strip()
    if not sid:
        return ""
    d = str(date_str or "").strip()[:10]
    z = str(zone or "").strip()
    if len(d) != 10:
        return ""
    try:
        rows = fetch_records(
            gspread_client=gspread_client,
            spreadsheet_id=sid,
            table="liturgy_illustrations",
            limit=0,
        )
    except Exception:
        return ""
    cand = [
        r
        for r in rows
        if str(r.get("date") or "").strip()[:10] == d
        and str(r.get("zone") or "").strip() == z
        and sheet_row_status_is_live(r.get("status"))
    ]
    if not cand:
        return ""
    cand.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return str((cand[0] or {}).get("description_illustration") or "").strip()


def is_readings_audio_gcs_path(path: str) -> bool:
    """Objets « lectures intégrales » : préfixe dédié (distinct de ``Audio/…`` synthèse)."""
    p = (path or "").strip().replace("\\", "/")
    return p.startswith("AudioLectures/")


def weekly_email_signed_urls(
    *,
    cfg: object,
    gs: object,
    date_str: str,
    zone: str = "france",
) -> dict[str, str]:
    """PDF, audio synthèse, audio lectures (AudioLectures/), illustration — URLs signées pour l’e-mail hebdo."""
    out: dict[str, str] = {
        "url_pdf": "",
        "url_audio": "",
        "url_audio_readings": "",
        "url_illustration": "",
        "illustration_description": "",
    }
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    if not bucket or not getattr(cfg, "gcp_service_account", None):
        return out
    try:
        gcs = build_gcs_client(cfg.gcp_service_account)
    except Exception:
        return out
    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
    if gsheet_id:
        try:
            out["illustration_description"] = _latest_illustration_description_from_ilus(
                gspread_client=gs,
                spreadsheet_id=gsheet_id,
                date_str=date_str,
                zone=zone,
            )
        except Exception:
            pass
    p_pdf = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
    try:
        out["url_pdf"] = gcs_signed_url(gcs=gcs, bucket_name=bucket, path=p_pdf) or ""
    except Exception:
        pass
    year = date_str[:4]
    cand = [f"Images/illustrations/{year}/{date_str}{ext}" for ext in (".webp", ".png", ".jpg", ".jpeg")]
    try:
        out["url_illustration"] = (
            gcs_first_signed_url(gcs=gcs, bucket_name=bucket, candidate_paths=cand) or ""
        )
    except Exception:
        pass
    try:
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=0)
        gens_d = [
            g
            for g in gens
            if str(g.get("date") or "").strip()[:10] == date_str and str(g.get("zone") or "").strip() == zone
        ]
        gens_d.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        gen_id = str((gens_d[0] or {}).get("entity_id") or "").strip() if gens_d else ""
        if not gen_id:
            return out
        aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=0)
        aud_d = [a for a in aud_rows if str(a.get("gen_entity_id") or "").strip() == gen_id]
        syn_rows = [a for a in aud_d if not is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))]
        read_rows = [a for a in aud_d if is_readings_audio_gcs_path(str(a.get("gcs_path") or ""))]
        syn_rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        read_rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        p_syn = str((syn_rows[0] or {}).get("gcs_path") or "").strip() if syn_rows else ""
        p_read = str((read_rows[0] or {}).get("gcs_path") or "").strip() if read_rows else ""
        if p_syn:
            out["url_audio"] = gcs_signed_url(gcs=gcs, bucket_name=bucket, path=p_syn) or ""
        if p_read:
            out["url_audio_readings"] = gcs_signed_url(gcs=gcs, bucket_name=bucket, path=p_read) or ""
    except Exception:
        pass
    return out
