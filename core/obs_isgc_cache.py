"""Lecture seule du cache ISGC — jamais un walk de dépôt."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ISGC_REL = Path("data") / "isgc_telemetry.json"


def isgc_cache_path() -> Path:
    return _REPO_ROOT / _ISGC_REL


def lire_snapshot_integrite() -> dict[str, Any]:
    """
    Recopie le dernier cache ``data/isgc_telemetry.json``.
    O(1) : lecture JSON, pas de scan. Cache absent → statut absent.
    """
    path = isgc_cache_path()
    if not path.is_file():
        return {"statut": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"statut": "absent"}
    if not isinstance(data, dict):
        return {"statut": "absent"}

    isgc = data.get("isgc") if isinstance(data.get("isgc"), dict) else None
    depot = data.get("depot") if isinstance(data.get("depot"), dict) else None
    if isgc is None and (data.get("lettre") or data.get("score") is not None):
        isgc = {
            k: data[k]
            for k in ("lettre", "score", "label", "piliers", "scanAt")
            if k in data
        }

    age = None
    try:
        age = int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
    except OSError:
        age = None
    if isinstance(isgc, dict) and age is not None:
        isgc = {**isgc, "ageCacheSec": max(0, age)}

    if not isgc and not depot:
        return {"statut": "absent"}
    return {
        "statut": data.get("statut") or "ok",
        "isgc": isgc,
        "depot": depot,
    }
