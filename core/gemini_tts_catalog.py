from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CATALOG = _REPO_ROOT / "data" / "gemini_tts_voices.json"


@lru_cache(maxsize=2)
def load_gemini_tts_voice_catalog(*, catalog_path: str | None = None) -> tuple[dict[str, str], str]:
    """
    Retourne (name -> label_fr, readme).
    Le fichier JSON est la source « produit » pour rester à jour sans redéployer la liste en dur dans le code.
    """
    p = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG
    readme = ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "Achird": "Achird",
            "Kore": "Kore",
        }, ""

    if isinstance(raw, dict):
        readme = str(raw.get("_readme") or raw.get("readme") or "").strip()
        voices = raw.get("voices") or []
    else:
        voices = raw if isinstance(raw, list) else []

    out: dict[str, str] = {}
    for v in voices:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()
        if not name:
            continue
        lab = str(v.get("label_fr") or v.get("label") or name).strip() or name
        out[name] = lab
    if not out:
        out = {"Achird": "Achird", "Kore": "Kore"}
    return out, readme


def gemini_tts_voice_names_ordered(*, catalog_path: str | None = None) -> list[str]:
    mapping, _ = load_gemini_tts_voice_catalog(catalog_path=catalog_path)
    return sorted(mapping.keys(), key=lambda x: x.lower())
