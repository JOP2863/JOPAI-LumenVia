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


def persist_aelf_snapshot(date_str: str, zone: str, identity: AelfDayIdentity, texts: AelfTexts) -> None:
    p = _snapshot_path(date_str, zone)
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": asdict(identity),
        "texts": asdict(texts),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
