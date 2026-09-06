"""Compteurs HTTP des canaux liturgiques gratuits (aelf / evangelizo / universalis).

O(1) : incrément de compteur mensuel. Pas de métier liturgie, pas de secrets.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_DIR = _REPO_ROOT / "data"
_FICHIER_USAGE = _DATA_DIR / "http_usage.json"
_LOCK = threading.Lock()
CANAUX_GRATUITS = ("aelf", "evangelizo", "universalis")


def _mois_cle(iso_date: str | None = None) -> str:
    if iso_date and len(iso_date) >= 7:
        return iso_date[:7]
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _lire() -> dict[str, Any]:
    try:
        raw = json.loads(_FICHIER_USAGE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("par_mois"), dict):
            return raw
    except Exception:
        pass
    return {"par_mois": {}}


def _ecrire(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FICHIER_USAGE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_FICHIER_USAGE)


def noter_hit(canal: str) -> None:
    """Incrémente un hit du mois UTC. Best-effort : n’échoue jamais l’appel métier."""
    key = str(canal or "").strip().lower()
    if key not in CANAUX_GRATUITS:
        return
    try:
        mois = _mois_cle()
        with _LOCK:
            data = _lire()
            bucket = data.setdefault("par_mois", {}).setdefault(mois, {})
            if not isinstance(bucket, dict):
                bucket = {}
                data["par_mois"][mois] = bucket
            bucket[key] = int(bucket.get(key) or 0) + 1
            _ecrire(data)
    except Exception:
        return


def resume_hits(canal: str, *, mois: str | None = None) -> dict[str, Any]:
    """Agrégat du mois pour un canal gratuit (0 si journal absent)."""
    key = str(canal or "").strip().lower()
    cle = mois or _mois_cle()
    with _LOCK:
        bucket = (_lire().get("par_mois") or {}).get(cle) or {}
    appels = 0
    if isinstance(bucket, dict):
        try:
            appels = max(0, int(bucket.get(key) or 0))
        except (TypeError, ValueError):
            appels = 0
    return {
        "canal": key,
        "presence": "actif" if appels > 0 else "declare",
        "tarif": "gratuit",
        "mois": cle,
        "appels": appels,
        "unite": "hits",
        "quantite": appels,
        "coutEur": 0,
        "budgetMensuelEur": None,
        "pctBudget": None,
        "alerte": "ok",
    }
