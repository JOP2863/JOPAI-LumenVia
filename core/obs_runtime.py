"""Contrat runtime ``jopai-obs-1`` + battement fichier pour NEXUS (pas d’URL /api/health).

Canaux : gemini (payant) + aelf / evangelizo / universalis (gratuit, 0 €).
Pas de GCS en Runtime € (connectivité seulement). Pas de maille tenant (un foyer).
ISGC : lecture du dernier cache, jamais un scan ici.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.gemini_usage import resume_usage_gemini
from core.obs_http_usage import CANAUX_GRATUITS, resume_hits
from core.obs_isgc_cache import lire_snapshot_integrite

SCHEMA_OBS = "jopai-obs-1"
APPLICATION = "JOPAI-LumenVia"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FICHIER_BATTEMENT = _REPO_ROOT / "data" / "nexus_heartbeat.json"
_PROCESS_START = time.monotonic()
# Keepalive mtime ~60 s (admin Streamlit = beaucoup de reruns).
_LAST_WRITE_MONO = 0.0
_MIN_INTERVAL_S = 60.0
_SECRETS_MARKERS = ("private_key", "api_key", "client_secret")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _app_version() -> str:
    for name in ("VERSION", "version.txt"):
        p = _REPO_ROOT / name
        if p.is_file():
            try:
                v = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if v:
                    return v
            except Exception:
                pass
    return "0.1.0"


def _canal_gemini(gemini: dict[str, Any]) -> dict[str, Any]:
    mois = gemini.get("moisCourant") or {}
    budget = gemini.get("budget") or {}
    alerte = gemini.get("alerte") or {}
    appels = int(mois.get("appels") or gemini.get("appels") or 0)
    tokens = int(mois.get("totalTokens") or gemini.get("totalTokens") or 0)
    return {
        "canal": "gemini",
        "presence": "actif" if appels > 0 else "declare",
        "tarif": "payant",
        "mois": str(mois.get("cle") or ""),
        "appels": appels,
        "unite": "tokens",
        "quantite": tokens,
        "totalTokens": tokens,
        "coutEur": float(mois.get("coutEur") if mois.get("coutEur") is not None else gemini.get("coutEur") or 0),
        "budgetMensuelEur": float(budget.get("budgetMensuelEur") or 50),
        "pctBudget": alerte.get("pctBudget"),
        "alerte": str(alerte.get("niveau") or "ok"),
    }


def construire_canaux(*, gemini: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    g = gemini if gemini is not None else resume_usage_gemini(limite=5)
    ligne_g = _canal_gemini(g)
    mois = str(ligne_g.get("mois") or "")
    canaux: list[dict[str, Any]] = [ligne_g]
    for nom in CANAUX_GRATUITS:
        canaux.append(resume_hits(nom, mois=mois or None))
    return canaux


def construire_contrat_obs(
    *,
    version: str | None = None,
    postgres: bool | None = None,
    stockage: str = "gcs",
) -> dict[str, Any]:
    gemini_raw = resume_usage_gemini(limite=5)
    canaux = construire_canaux(gemini=gemini_raw)
    gemini = next((c for c in canaux if c.get("canal") == "gemini"), {})
    # Plat = Gemini (rétrocompat NEXUS) ; canaux[] est la vérité. GCS hors somme €.
    return {
        "schema": SCHEMA_OBS,
        "vitalite": {
            "horodatage": _utc_now_iso(),
            "version": str(version or _app_version()),
            "uptimeSec": int(max(0, round(time.monotonic() - _PROCESS_START))),
        },
        "connectivite": {
            "postgres": postgres,
            "stockage": stockage,
        },
        "finops": {
            "runtime": {
                "canaux": canaux,
                **{
                    k: gemini[k]
                    for k in (
                        "canal",
                        "mois",
                        "appels",
                        "totalTokens",
                        "coutEur",
                        "budgetMensuelEur",
                        "pctBudget",
                        "alerte",
                    )
                    if k in gemini
                },
            },
            "atelier": {"statut": "not_implemented"},
        },
        "integrite": lire_snapshot_integrite(),
    }


def ecrire_battement(contrat: dict[str, Any] | None = None) -> Path | None:
    """Écrit ``data/nexus_heartbeat.json`` (ok + contrat). Best-effort. Pas de scan ISGC."""
    try:
        payload = {
            "ok": True,
            "application": APPLICATION,
            "contrat": contrat or construire_contrat_obs(),
        }
        dump = json.dumps(payload, ensure_ascii=False, indent=2)
        low = dump.lower()
        if any(m in low for m in _SECRETS_MARKERS):
            return None
        _FICHIER_BATTEMENT.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FICHIER_BATTEMENT.with_suffix(".json.tmp")
        tmp.write_text(dump + "\n", encoding="utf-8")
        tmp.replace(_FICHIER_BATTEMENT)
        return _FICHIER_BATTEMENT
    except Exception:
        return None


def maybe_ecrire_battement(*, force: bool = False) -> Path | None:
    """
    Battement throttlé (run Streamlit / admin) — pas à chaque widget :
    intervalle minimal ``_MIN_INTERVAL_S`` sauf ``force=True``.
    Pas de walk dépôt, pas de scan ISGC.
    """
    global _LAST_WRITE_MONO
    now = time.monotonic()
    if not force and (now - _LAST_WRITE_MONO) < _MIN_INTERVAL_S:
        return None
    path = ecrire_battement()
    if path is not None:
        _LAST_WRITE_MONO = now
    return path


def maybe_ecrire_battement_admin(*, force: bool = False) -> Path | None:
    """Alias historique — même throttle que ``maybe_ecrire_battement``."""
    return maybe_ecrire_battement(force=force)
