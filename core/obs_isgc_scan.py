"""Scan ISGC LumenVia — chemin froid uniquement (CLI / bouton Mesurer). Jamais au boot Streamlit."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.obs_isgc_cache import isgc_cache_path
from core.system_audit import default_repository_root, run_granularity_audit

IDEAL_MU_LOC = 275
SEUIL_LIGNES = 960
SEUIL_ALERTE = 600
W_GRAN = 0.30
W_HYPER = 0.25
W_ORCH = 0.15
W_LAT = 0.20
W_DOC = 0.10
_EXCLUDED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".cursor",
        "node_modules",
        ".cache",
        ".streamlit",
        ".pytest_cache",
        "dist",
        "build",
    }
)


def _lettre(score: int) -> tuple[str, str]:
    if score >= 85:
        return "A", "Architecture saine"
    if score >= 70:
        return "B", "Surveillance légère"
    if score >= 55:
        return "C", "Fragmentation planifiable"
    if score >= 40:
        return "D", "Risque navigation cognitive"
    return "E", "Action chirurgicale urgente"


def _score_orch(lignes: int) -> float:
    if lignes <= 150:
        return 100.0
    if lignes <= 400:
        return max(65.0, 100.0 - (35.0 * (lignes - 150)) / 250.0)
    if lignes >= 800:
        return 10.0
    return max(10.0, 65.0 - (55.0 * (lignes - 400)) / 400.0)


def _normalite_corps(lignes: int, mean: float, pstdev: float) -> str:
    z = abs((lignes - mean) / pstdev) if pstdev > 0 else 0.0
    if lignes >= SEUIL_LIGNES or z > 2:
        return "hors"
    if lignes > SEUIL_ALERTE or z > 1:
        return "modere"
    return "ok"


def _compter_lignes(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
    except OSError:
        return 0


def _walk_md_lignes(root: Path) -> tuple[int, int]:
    fichiers = 0
    lignes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath).name
        if base in _EXCLUDED:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED]
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            fichiers += 1
            lignes += _compter_lignes(Path(dirpath) / name)
    return fichiers, lignes


def _depot(root: Path, loc: int) -> dict[str, Any]:
    taille = 0
    n_fichiers = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath).name
        if base in _EXCLUDED:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                taille += p.stat().st_size
                n_fichiers += 1
            except OSError:
                continue
    cdc = _compter_lignes(root / "docs" / "cdc.md")
    deps = "requirements.txt" if (root / "requirements.txt").is_file() else "—"
    return {
        "tailleMo": round(taille / (1024 * 1024), 1),
        "fichiers": n_fichiers,
        "loc": int(loc),
        "cdcLignes": int(cdc),
        "stack": "python_streamlit",
        "deps": deps,
    }


def mesurer_isgc(*, root: Path | None = None, dest: Path | None = None) -> dict[str, Any]:
    """Walk dépôt + écriture cache. Interdit depuis le heartbeat / premier run."""
    repo = (root or default_repository_root()).resolve()
    audit = run_granularity_audit(repo)
    loc = sum(int(r.line_count) for r in audit.rows)
    corps = [r for r in audit.rows if r.zone == "corps"]
    sommet = [r for r in audit.rows if r.zone == "sommet"]
    lignes_corps = [int(r.line_count) for r in corps]
    n_corps = len(lignes_corps)
    mean = (sum(lignes_corps) / n_corps) if n_corps else 0.0
    if n_corps >= 2:
        var = sum((x - mean) ** 2 for x in lignes_corps) / n_corps
        pstdev = math.sqrt(var)
    else:
        pstdev = 0.0

    if n_corps:
        ok = sum(1 for x in lignes_corps if _normalite_corps(x, mean, pstdev) == "ok")
        mod = sum(1 for x in lignes_corps if _normalite_corps(x, mean, pstdev) == "modere")
        hors = sum(1 for x in lignes_corps if _normalite_corps(x, mean, pstdev) == "hors")
        granularite = max(0.0, min(100.0, (ok / n_corps) * 100.0 - hors * 12.0 - mod * 4.0))
    else:
        granularite = 50.0

    alertes = sum(1 for x in lignes_corps if x >= SEUIL_ALERTE)
    max_loc = max(lignes_corps) if lignes_corps else 0
    if n_corps > 0:
        hypertrophie = max(
            0.0,
            min(
                100.0,
                100.0
                - min(100.0, (max_loc / SEUIL_LIGNES) * 100.0) * 0.55
                - (alertes / n_corps) * 100.0 * 0.45,
            ),
        )
    else:
        hypertrophie = 50.0

    orchestration = (
        sum(_score_orch(int(f.line_count)) for f in sommet) / len(sommet) if sommet else 80.0
    )
    lourds = sum(1 for x in lignes_corps if x >= SEUIL_ALERTE)
    latence = 75.0 if not n_corps else max(0.0, min(100.0, 95.0 - (lourds / n_corps) * 80.0))

    _md_n, md_lignes = _walk_md_lignes(repo)
    if loc > 0:
        ratio = md_lignes / loc
        if ratio <= 0.15:
            documentation = 95.0
        elif ratio >= 0.35:
            documentation = max(20.0, 100.0 - (ratio - 0.35) * 180.0)
        else:
            documentation = 95.0 - ((ratio - 0.15) / 0.2) * 35.0
    else:
        documentation = 70.0

    piliers = {
        "granularite": round(granularite, 1),
        "hypertrophie": round(hypertrophie, 1),
        "orchestration": round(orchestration, 1),
        "latence": round(latence, 1),
        "documentation": round(documentation, 1),
    }
    score = int(
        round(
            max(
                0.0,
                min(
                    100.0,
                    W_GRAN * piliers["granularite"]
                    + W_HYPER * piliers["hypertrophie"]
                    + W_ORCH * piliers["orchestration"]
                    + W_LAT * piliers["latence"]
                    + W_DOC * piliers["documentation"],
                ),
            )
        )
    )
    lettre, label = _lettre(score)
    scan_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = {
        "statut": "ok",
        "isgc": {
            "lettre": lettre,
            "score": score,
            "label": label,
            "piliers": piliers,
            "scanAt": scan_at,
            "ageCacheSec": 0,
        },
        "depot": _depot(repo, loc),
    }
    path = dest or isgc_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return payload


if __name__ == "__main__":
    out = mesurer_isgc()
    isgc = out.get("isgc") or {}
    depot = out.get("depot") or {}
    print(
        f"ISGC {isgc.get('lettre')} {isgc.get('score')} "
        f"tailleMo={depot.get('tailleMo')} -> {isgc_cache_path()}"
    )
