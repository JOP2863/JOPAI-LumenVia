"""Journal local de consommation Gemini / Vertex (tokens) — FinOps runtime POD."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_DIR = _REPO_ROOT / "data"
_FICHIER_USAGE = _DATA_DIR / "gemini_usage.json"
_FICHIER_BUDGET = _DATA_DIR / "gemini_budget.json"
_LOCK = threading.Lock()

# Grille paid tier (USD / 1M tokens) — source ai.google.dev/gemini-api/docs/pricing.
_TARIFS: dict[str, dict[str, float]] = {
    "gemini-2.5-flash-image": {"input": 0.3, "output": 2.5, "image_out": 30.0},
    "gemini-2.5-flash": {"input": 0.3, "output": 2.5},
    "gemini-2.5-flash-lite": {"input": 0.1, "output": 0.4},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.0-flash": {"input": 0.1, "output": 0.4},
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.3},
    "gemini-3-flash": {"input": 0.5, "output": 3.0},
}
_TARIF_DEFAUT = {"input": 0.3, "output": 2.5}
_BUDGET_DEFAUT = {"budgetMensuelEur": 50.0, "seuilAlertePct": 80.0, "tauxUsdEur": 0.92}
_MAX_ENTREES = 5000
_TARIFS_MAJ = "2026-08 — grille publique Google AI (paid tier)"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mois_cle(iso_date: str | None = None) -> str:
    if iso_date and len(iso_date) >= 7:
        return iso_date[:7]
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _modele_court(modele: str) -> str:
    m = str(modele or "").strip()
    if ":" in m:
        m = m.split(":", 1)[-1]
    return m.lower()


def _tarif_pour_modele(modele: str) -> dict[str, float]:
    cle = _modele_court(modele)
    if cle in _TARIFS:
        return _TARIFS[cle]
    best: dict[str, float] | None = None
    best_len = 0
    for k, t in _TARIFS.items():
        if cle.startswith(k) and len(k) > best_len:
            best = t
            best_len = len(k)
    if best:
        return best
    if "image" in cle:
        return {"input": 0.3, "output": 2.5, "image_out": 30.0}
    if "pro" in cle:
        return _TARIFS["gemini-2.5-pro"]
    if "lite" in cle:
        return _TARIFS["gemini-2.5-flash-lite"]
    if "tts" in cle:
        return _TARIFS.get("gemini-2.5-flash", _TARIF_DEFAUT)
    return dict(_TARIF_DEFAUT)


def estimer_cout_usd(*, modele: str, prompt_tokens: int, candidates_tokens: int) -> float:
    t = _tarif_pour_modele(modele)
    out_rate = float(t.get("image_out") or t.get("output") or 2.5)
    prompt = max(0, int(prompt_tokens or 0))
    cand = max(0, int(candidates_tokens or 0))
    return (prompt * float(t.get("input") or 0.3) + cand * out_rate) / 1_000_000.0


def _arrondi_cout(n: float) -> float:
    return round(float(n), 6)


def tokens_from_usage_metadata(raw: dict[str, Any] | None) -> tuple[int, int, int]:
    """Extrait (prompt, candidates, total) depuis ``usageMetadata`` Gemini/Vertex."""
    meta = (raw or {}).get("usageMetadata") or (raw or {}).get("usage_metadata") or {}
    if not isinstance(meta, dict):
        return 0, 0, 0
    prompt = int(meta.get("promptTokenCount") or meta.get("prompt_token_count") or 0)
    cand = int(
        meta.get("candidatesTokenCount")
        or meta.get("candidates_token_count")
        or meta.get("outputTokenCount")
        or 0
    )
    total = int(meta.get("totalTokenCount") or meta.get("total_token_count") or 0)
    if total <= 0:
        total = prompt + cand
    return max(0, prompt), max(0, cand), max(0, total)


def _lire_usage() -> dict[str, Any]:
    try:
        raw = json.loads(_FICHIER_USAGE.read_text(encoding="utf-8"))
        entrees = raw.get("entrees") if isinstance(raw, dict) else None
        return {"entrees": list(entrees) if isinstance(entrees, list) else []}
    except Exception:
        return {"entrees": []}


def _ecrire_usage(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _FICHIER_USAGE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def lire_budget_gemini() -> dict[str, float]:
    try:
        raw = json.loads(_FICHIER_BUDGET.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(_BUDGET_DEFAUT)
        out = dict(_BUDGET_DEFAUT)
        for k in ("budgetMensuelEur", "seuilAlertePct", "tauxUsdEur"):
            try:
                v = float(raw.get(k))
                if v == v:  # not NaN
                    out[k] = v
            except Exception:
                pass
        return out
    except Exception:
        return dict(_BUDGET_DEFAUT)


def enregistrer_usage_gemini(
    *,
    modele: str,
    usage: str = "autre",
    prompt_tokens: int = 0,
    candidates_tokens: int = 0,
    total_tokens: int = 0,
    meta: dict[str, Any] | None = None,
    raw_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append une entrée au journal local (fichier hors Git)."""
    if raw_response is not None and prompt_tokens == 0 and candidates_tokens == 0 and total_tokens == 0:
        prompt_tokens, candidates_tokens, total_tokens = tokens_from_usage_metadata(raw_response)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    candidates_tokens = max(0, int(candidates_tokens or 0))
    total_tokens = max(0, int(total_tokens or 0)) or (prompt_tokens + candidates_tokens)
    entree = {
        "id": str(uuid4()),
        "date": _utc_now_iso(),
        "modele": str(modele or "inconnu").strip() or "inconnu",
        "usage": str(usage or "autre").strip() or "autre",
        "promptTokens": prompt_tokens,
        "candidatesTokens": candidates_tokens,
        "totalTokens": total_tokens,
        "meta": meta or {},
    }
    with _LOCK:
        data = _lire_usage()
        data["entrees"].append(entree)
        if len(data["entrees"]) > _MAX_ENTREES:
            data["entrees"] = data["entrees"][-_MAX_ENTREES:]
        try:
            _ecrire_usage(data)
        except Exception:
            pass
    return entree


def enregistrer_usage_depuis_reponse(
    *,
    modele: str,
    usage: str,
    raw: dict[str, Any] | None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Best-effort : n’échoue jamais l’appel métier."""
    try:
        return enregistrer_usage_gemini(
            modele=modele,
            usage=usage,
            raw_response=raw,
            meta=meta,
        )
    except Exception:
        return None


def _evaluer_alerte(encours_eur: float, budget: dict[str, float]) -> dict[str, Any]:
    plafond = float(budget.get("budgetMensuelEur") or 0)
    if plafond <= 0:
        return {
            "niveau": "inactif",
            "pctBudget": None,
            "message": "Pas de budget mensuel défini — alerte désactivée.",
        }
    pct = (encours_eur / plafond) * 100.0
    seuil = float(budget.get("seuilAlertePct") or 80)
    if pct >= 100:
        return {
            "niveau": "depasse",
            "pctBudget": int(round(pct)),
            "message": f"Budget mensuel dépassé ({pct:.0f} %).",
        }
    if pct >= seuil:
        return {
            "niveau": "attention",
            "pctBudget": int(round(pct)),
            "message": f"Seuil d’alerte atteint ({pct:.0f} % du budget).",
        }
    return {
        "niveau": "ok",
        "pctBudget": int(round(pct)),
        "message": f"Encours sous le seuil d’alerte ({pct:.0f} % du budget).",
    }


def resume_usage_gemini(*, limite: int = 5) -> dict[str, Any]:
    budget = lire_budget_gemini()
    taux = float(budget.get("tauxUsdEur") or 0.92)
    with _LOCK:
        entrees = list(_lire_usage().get("entrees") or [])
    mois = _mois_cle()
    mois_entrees = [e for e in entrees if str(e.get("date") or "").startswith(mois)]
    total_tokens = 0
    prompt_tokens = 0
    candidates_tokens = 0
    cout_usd = 0.0
    for e in mois_entrees:
        total_tokens += int(e.get("totalTokens") or 0)
        prompt_tokens += int(e.get("promptTokens") or 0)
        candidates_tokens += int(e.get("candidatesTokens") or 0)
        cout_usd += estimer_cout_usd(
            modele=str(e.get("modele") or ""),
            prompt_tokens=int(e.get("promptTokens") or 0),
            candidates_tokens=int(e.get("candidatesTokens") or 0),
        )
    cout_eur = _arrondi_cout(cout_usd * taux)
    alerte = _evaluer_alerte(cout_eur, budget)
    return {
        "totalTokens": total_tokens,
        "promptTokens": prompt_tokens,
        "candidatesTokens": candidates_tokens,
        "appels": len(mois_entrees),
        "coutUsd": _arrondi_cout(cout_usd),
        "coutEur": cout_eur,
        "moisCourant": {
            "cle": mois,
            "totalTokens": total_tokens,
            "appels": len(mois_entrees),
            "coutUsd": _arrondi_cout(cout_usd),
            "coutEur": cout_eur,
        },
        "budget": budget,
        "alerte": alerte,
        "tarifsMaj": _TARIFS_MAJ,
        "dernieres": entrees[-max(0, int(limite)) :],
    }
