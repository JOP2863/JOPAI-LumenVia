"""Chargement des lectures depuis l’onglet ``readings_cache`` (RDC)."""

from __future__ import annotations

import re
from datetime import date

from core.aelf import AelfDayIdentity, AelfTexts
from core.sheets_db import fetch_records, sheet_row_status_is_live


def _cache_date_key(raw: object) -> str:
    s = str(raw or "").strip()
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


def _normalize_cached_text(s: str | None) -> str:
    raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _row_has_readings(row: dict) -> bool:
    for k in ("premiere_lecture", "psaume", "deuxieme_lecture", "evangile"):
        if str(row.get(k) or "").strip():
            return True
    return False


def load_aelf_from_readings_cache(
    *,
    gs: object,
    spreadsheet_id: str,
    date_str: str,
    zone: str = "france",
) -> tuple[AelfDayIdentity, AelfTexts] | None:
    """Dernière ligne RDC exploitable pour (date, zone), ou ``None``."""
    day = _cache_date_key(date_str)
    if not day:
        return None
    try:
        rows = fetch_records(
            gspread_client=gs,
            spreadsheet_id=spreadsheet_id,
            table="readings_cache",
            limit=0,
            use_cache=True,
        )
    except Exception:
        return None
    hits = [
        r
        for r in rows
        if _cache_date_key(r.get("date")) == day
        and str(r.get("zone") or "").strip() == zone
        and sheet_row_status_is_live(r.get("status"))
        and not str(r.get("error") or "").strip()
        and _row_has_readings(r)
    ]
    if not hits:
        return None
    best = sorted(hits, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
    p1 = _normalize_cached_text(str(best.get("premiere_lecture") or "")) or None
    ps = _normalize_cached_text(str(best.get("psaume") or "")) or None
    p2 = _normalize_cached_text(str(best.get("deuxieme_lecture") or "")) or None
    ev = _normalize_cached_text(str(best.get("evangile") or "")) or None
    identity = AelfDayIdentity(
        date=str(best.get("date") or day)[:10],
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
    return identity, texts
