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


def _normalize_cached_multiline(s: str | None) -> str | None:
    raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return raw or None


def _row_has_readings(row: dict) -> bool:
    for k in ("premiere_lecture", "psaume", "deuxieme_lecture", "evangile"):
        if str(row.get(k) or "").strip():
            return True
    return False


def _optional_meta(row: dict, key: str) -> str | None:
    v = str(row.get(key) or "").strip()
    return v or None


def aelf_texts_from_readings_cache_row(row: dict) -> AelfTexts:
    p1 = _normalize_cached_text(str(row.get("premiere_lecture") or "")) or None
    ps = _normalize_cached_multiline(str(row.get("psaume") or ""))
    p2 = _normalize_cached_text(str(row.get("deuxieme_lecture") or "")) or None
    ev = _normalize_cached_text(str(row.get("evangile") or "")) or None
    return AelfTexts(
        premiere_lecture=p1,
        psaume=ps,
        deuxieme_lecture=p2,
        evangile=ev,
        premiere_lecture_intro=_optional_meta(row, "premiere_lecture_intro"),
        premiere_lecture_ref=_optional_meta(row, "premiere_lecture_ref"),
        psaume_intro=_optional_meta(row, "psaume_intro"),
        psaume_ref=_optional_meta(row, "psaume_ref"),
        psaume_refrain=_normalize_cached_multiline(str(row.get("psaume_refrain") or "")),
        psaume_ref_refrain=_optional_meta(row, "psaume_ref_refrain"),
        deuxieme_lecture_intro=_optional_meta(row, "deuxieme_lecture_intro"),
        deuxieme_lecture_ref=_optional_meta(row, "deuxieme_lecture_ref"),
        evangile_intro=_optional_meta(row, "evangile_intro"),
        evangile_ref=_optional_meta(row, "evangile_ref"),
    )


def readings_cache_row_from_aelf_texts(*, ds: str, zone: str, identity, texts) -> dict[str, str]:
    """Ligne ``readings_cache`` prête pour ``append_immutable_row``."""

    def _txt(v: object) -> str:
        return _normalize_cached_text(str(v or ""))

    def _ml(v: object) -> str:
        return (str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip())

    return {
        "date": ds,
        "zone": zone,
        "periode": getattr(identity, "periode", None) or "",
        "semaine": getattr(identity, "semaine", None) or "",
        "annee": getattr(identity, "annee", None) or "",
        "couleur": getattr(identity, "couleur", None) or "",
        "fete": getattr(identity, "fete", None) or "",
        "jour_liturgique_nom": getattr(identity, "jour_liturgique_nom", None) or "",
        "premiere_lecture": _txt(getattr(texts, "premiere_lecture", None)),
        "premiere_lecture_intro": _txt(getattr(texts, "premiere_lecture_intro", None)),
        "premiere_lecture_ref": _txt(getattr(texts, "premiere_lecture_ref", None)),
        "psaume": _ml(getattr(texts, "psaume", None)),
        "psaume_intro": _txt(getattr(texts, "psaume_intro", None)),
        "psaume_ref": _txt(getattr(texts, "psaume_ref", None)),
        "psaume_refrain": _ml(getattr(texts, "psaume_refrain", None)),
        "psaume_ref_refrain": _txt(getattr(texts, "psaume_ref_refrain", None)),
        "deuxieme_lecture": _txt(getattr(texts, "deuxieme_lecture", None)),
        "deuxieme_lecture_intro": _txt(getattr(texts, "deuxieme_lecture_intro", None)),
        "deuxieme_lecture_ref": _txt(getattr(texts, "deuxieme_lecture_ref", None)),
        "evangile": _txt(getattr(texts, "evangile", None)),
        "evangile_intro": _txt(getattr(texts, "evangile_intro", None)),
        "evangile_ref": _txt(getattr(texts, "evangile_ref", None)),
        "source": "aelf_api_prefetch",
        "error": "",
    }


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
    return identity, aelf_texts_from_readings_cache_row(best)
