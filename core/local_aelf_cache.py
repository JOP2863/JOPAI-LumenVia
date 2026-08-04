from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from core.aelf import AelfDayIdentity, AelfTexts

_CACHE_ROOT = Path(".cache") / "lumenvia"


def _snapshot_path(date_str: str, zone: str) -> Path:
    safe_zone = zone.replace("/", "_").replace("\\", "_")
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return _CACHE_ROOT / f"aelf_{date_str}_{safe_zone}.json"


def persist_aelf_snapshot(date_str: str, zone: str, identity: AelfDayIdentity, texts: AelfTexts) -> bool:
    """Écrit le snapshot local. Retourne False si le disque refuse (Cloud read-only, etc.)."""
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": asdict(identity),
        "texts": asdict(texts),
    }
    raw = json.dumps(payload, ensure_ascii=False)
    try:
        p = _snapshot_path(date_str, zone)
        p.write_text(raw, encoding="utf-8")
        return True
    except OSError:
        return False


def load_aelf_snapshot(date_str: str, zone: str) -> tuple[AelfDayIdentity, AelfTexts, str] | None:
    p = _snapshot_path(date_str, zone)
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        id_raw = payload.get("identity") or {}
        tx_raw = payload.get("texts") or {}
        identity = AelfDayIdentity(**{f.name: id_raw.get(f.name) for f in fields(AelfDayIdentity)})
        texts = AelfTexts(**{f.name: tx_raw.get(f.name) for f in fields(AelfTexts)})
        cached_at = str(payload.get("cached_at") or "")
        return identity, texts, cached_at
    except Exception:
        return None


def load_aelf_snapshot_for_zones(
    date_str: str, zones: list[str] | tuple[str, ...]
) -> tuple[AelfDayIdentity, AelfTexts, str] | None:
    """Charge le premier snapshot trouvé parmi ``zones`` (canonique puis alias)."""
    seen: set[str] = set()
    for z in zones:
        key = str(z or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        snap = load_aelf_snapshot(date_str, key)
        if snap:
            return snap
    return None
