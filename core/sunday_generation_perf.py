"""Chargement et agrégation des métriques de génération (GEN / AUD / PDFX)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.sheets_db import sheet_row_status_is_live


def parse_sheet_float(raw: object) -> float | None:
    s = str(raw if raw is not None else "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_sheet_int(raw: object) -> int | None:
    v = parse_sheet_float(raw)
    if v is None:
        return None
    return int(round(v))


def infer_audio_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("synthese", "lectures"):
        return kind
    path = str(row.get("gcs_path") or "").replace("\\", "/")
    if "AudioLectures/" in path:
        return "lectures"
    return "synthese"


def _parse_date_key(raw: object) -> str:
    s = str(raw or "").strip()[:10]
    return s if len(s) == 10 else ""


def live_generations_with_perf(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        date_key = _parse_date_key(r.get("date"))
        if not date_key:
            continue
        dur_text = parse_sheet_float(r.get("duration_text_s"))
        dur_retry = parse_sheet_float(r.get("duration_text_retry_s"))
        dur_up = parse_sheet_float(r.get("duration_upload_text_s"))
        words = parse_sheet_int(r.get("text_words"))
        if dur_text is None and dur_retry is None and dur_up is None and words is None:
            continue
        out.append(
            {
                "date": date_key,
                "entity_id": str(r.get("entity_id") or "").strip(),
                "duration_text_s": dur_text,
                "duration_text_retry_s": dur_retry or 0.0,
                "duration_upload_text_s": dur_up,
                "text_words": words,
                "model": str(r.get("model") or "").strip(),
                "created_at": str(r.get("created_at") or "").strip(),
            }
        )
    out.sort(key=lambda x: x["date"])
    return out


def live_audio_with_perf(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        dur_tts = parse_sheet_float(r.get("duration_tts_s"))
        dur_up = parse_sheet_float(r.get("duration_upload_s"))
        if dur_tts is None and dur_up is None:
            continue
        kind = infer_audio_kind(r)
        gen_eid = str(r.get("gen_entity_id") or "").strip()
        out.append(
            {
                "kind": kind,
                "gen_entity_id": gen_eid,
                "duration_tts_s": dur_tts,
                "duration_upload_s": dur_up,
                "tts_route": str(r.get("tts_route") or "").strip(),
                "gcs_path": str(r.get("gcs_path") or "").strip(),
                "created_at": str(r.get("created_at") or "").strip(),
            }
        )
    return out


def live_pdf_with_perf(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        dur = parse_sheet_float(r.get("duration_build_s"))
        if dur is None:
            continue
        date_key = _parse_date_key(r.get("date_semaine_liturgique") or r.get("range_start"))
        out.append(
            {
                "date": date_key,
                "gen_entity_id": str(r.get("gen_entity_id") or "").strip(),
                "kind": str(r.get("kind") or "fascicule_dimanche").strip(),
                "duration_build_s": dur,
                "created_at": str(r.get("created_at") or "").strip(),
            }
        )
    out.sort(key=lambda x: x.get("date") or "")
    return out


def join_perf_by_date(
    *,
    generations: list[dict[str, Any]],
    audios: list[dict[str, Any]],
    pdfs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Une ligne par date de dimanche (dernière génération connue pour cette date)."""
    by_date: dict[str, dict[str, Any]] = {}
    for g in generations:
        d = g["date"]
        prev = by_date.get(d) or {"date": d}
        prev.update(
            {
                "duration_text_s": g.get("duration_text_s"),
                "duration_text_retry_s": g.get("duration_text_retry_s"),
                "duration_upload_text_s": g.get("duration_upload_text_s"),
                "text_words": g.get("text_words"),
                "gen_entity_id": g.get("entity_id"),
            }
        )
        by_date[d] = prev

    audio_by_gen: dict[str, dict[str, float | None]] = {}
    for a in audios:
        ge = a.get("gen_entity_id") or ""
        if not ge:
            continue
        slot = audio_by_gen.setdefault(ge, {})
        k = a["kind"]
        slot[f"duration_tts_{k}_s"] = a.get("duration_tts_s")
        slot[f"duration_upload_{k}_s"] = a.get("duration_upload_s")

    for d, row in by_date.items():
        ge = str(row.get("gen_entity_id") or "")
        if ge and ge in audio_by_gen:
            row.update(audio_by_gen[ge])

    pdf_by_date = {p["date"]: p for p in pdfs if p.get("date")}
    for d, row in by_date.items():
        if d in pdf_by_date:
            row["duration_pdf_s"] = pdf_by_date[d].get("duration_build_s")

    rows = list(by_date.values())
    rows.sort(key=lambda x: x["date"])
    return rows


def mean_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [parse_sheet_float(r.get(key)) for r in rows]
    nums = [v for v in vals if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)
