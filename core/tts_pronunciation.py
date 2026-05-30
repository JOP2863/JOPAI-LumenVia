"""Dictionnaire de prononciation appliqué à tout texte envoyé au TTS (lectures + synthèse)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_DEFAULT_JSON = Path(__file__).resolve().parent.parent / "data" / "tts_pronunciation_fr.json"

# Surcharges depuis ``Paramètres_IA`` (clé ``tts_pronunciation``), remplacées à chaque refresh.
_runtime_sheet_overrides: dict[str, str] = {}


def _parse_pronunciation_mapping(raw: str) -> dict[str, str]:
    """Accepte un objet JSON ou des lignes ``mot|forme`` / ``mot -> forme``."""
    text = (raw or "").strip()
    if not text:
        return {}
    # Retire un éventuel bloc ```json … ```
    fence = re.match(r"(?is)^```(?:json)?\s*(.*?)```\s*$", text)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            if isinstance(data.get("rules"), list):
                out: dict[str, str] = {}
                for item in data["rules"]:
                    if not isinstance(item, dict):
                        continue
                    src = str(item.get("word") or item.get("from") or "").strip()
                    dst = str(item.get("speak") or item.get("to") or "").strip()
                    if src and dst:
                        out[src] = dst
                return out
            return {
                str(k).strip(): str(v).strip()
                for k, v in data.items()
                if str(k).strip() and str(v).strip() and not str(k).startswith("_")
            }
        return {}
    out: dict[str, str] = {}
    for ln in text.splitlines():
        line = ln.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            left, right = line.split("|", 1)
        elif "->" in line:
            left, right = line.split("->", 1)
        elif "\t" in line:
            left, right = line.split("\t", 1)
        else:
            continue
        src = left.strip()
        dst = right.strip()
        if src and dst:
            out[src] = dst
    return out


@lru_cache(maxsize=1)
def _load_file_rules(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        if isinstance(data.get("rules"), list):
            return _parse_pronunciation_mapping(json.dumps(data, ensure_ascii=False))
        return {
            str(k).strip(): str(v).strip()
            for k, v in data.items()
            if str(k).strip() and str(v).strip()
        }
    return {}


def clear_tts_pronunciation_file_cache() -> None:
    """Invalide le cache du fichier JSON (après régénération admin)."""
    _load_file_rules.cache_clear()


def get_tts_pronunciation_rules(*, json_path: str | None = None) -> dict[str, str]:
    """Règles fusionnées : fichier dépôt + surcharges Sheets (prioritaires)."""
    path = str(json_path or _DEFAULT_JSON)
    merged = dict(_load_file_rules(path))
    merged.update(_runtime_sheet_overrides)
    return merged


def tts_pronunciation_breakdown() -> dict[str, object]:
    """Répartition fichier dépôt / surcharges Sheets / fusion effective."""
    path = str(_DEFAULT_JSON)
    file_rules = dict(_load_file_rules(path))
    sheet_rules = dict(_runtime_sheet_overrides)
    merged = {**file_rules, **sheet_rules}
    return {
        "json_path": path,
        "file": file_rules,
        "sheet": sheet_rules,
        "merged": merged,
    }


def refresh_tts_pronunciation_overrides_from_templates(templates: dict[str, str] | None) -> None:
    """Met à jour les surcharges depuis le contenu ``tts_pronunciation`` de ``Paramètres_IA``."""
    global _runtime_sheet_overrides
    raw = str((templates or {}).get("tts_pronunciation") or "").strip()
    _runtime_sheet_overrides = _parse_pronunciation_mapping(raw) if raw else {}


def apply_tts_pronunciation(text: str, *, rules: dict[str, str] | None = None) -> str:
    """
    Remplace des mots entiers par une graphie mieux lue par Gemini TTS.

    N'affecte que le texte **parlé** (pas le PDF ni l'affichage web).
    """
    t = text or ""
    if not t.strip():
        return t
    mapping = rules if rules is not None else get_tts_pronunciation_rules()
    if not mapping:
        return t
    # Mots longs d'abord pour éviter les remplacements partiels.
    for src in sorted(mapping.keys(), key=len, reverse=True):
        dst = mapping[src]
        if not src or not dst or src == dst:
            continue
        pattern = r"(?<![\wÀ-ÖØ-öø-ÿ])" + re.escape(src) + r"(?![\wÀ-ÖØ-öø-ÿ])"
        t = re.sub(pattern, dst, t)
    return t
