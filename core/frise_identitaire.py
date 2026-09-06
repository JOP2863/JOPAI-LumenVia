"""Frise identitaire publiée — copie locale seulement.

Pas d’appel NEXUS au runtime, pas de rotation, pas de Genèse.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRISE_REL = Path("data") / "frises" / "lumen_via.mp4"
_CARTE = _REPO_ROOT / "docs" / "identite" / "carte.yaml"


def chemin_frise_identitaire() -> Path | None:
    """MP4 local recopié depuis la publication NEXUS. Absent → None (rien à afficher)."""
    path = _REPO_ROOT / _FRISE_REL
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def nom_et_baseline() -> tuple[str, str]:
    """Nom + baseline depuis la carte locale. Pas de lecture NEXUS."""
    nom = "JOPAI-LumenVia"
    baseline = "Votre compagnon de recueillement et de compréhension liturgique."
    if not _CARTE.is_file():
        return nom, baseline
    try:
        import yaml

        data = yaml.safe_load(_CARTE.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return nom, baseline
        w = str(data.get("wordmark_texte") or "").strip()
        b = str(data.get("baseline_metier") or "").strip()
        if w:
            nom = w
        if b:
            baseline = b
    except Exception:
        pass
    return nom, baseline
