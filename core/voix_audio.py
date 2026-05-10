from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from core.sheets_db import sheet_row_status_is_live

# Aligné sur le seed `Voix_Audio` (table VOIX) — si aucune règle ne matche.
DEFAULT_GEMINI_TTS_VOICE = "Achird"


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def norm_slug(s: str | None) -> str:
    t = _strip_accents((s or "").strip().lower())
    return "".join(ch if ch.isalnum() else "_" for ch in t).strip("_")


def _parse_date_effet(v: object) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return None


def bucket_temps_liturgique(periode: str | None) -> str:
    """Regroupe les libellés AELF vers des buckets stables pour matcher la colonne Temps_Liturgique."""
    k = norm_slug(periode)
    if not k:
        return ""
    if "careme" in k:
        return "careme"
    if "pascal" in k:
        return "pascal"
    if "avent" in k:
        return "avent"
    if "noel" in k or "natal" in k:
        return "noel"
    if "pentecot" in k:
        return "pentecote"
    if k in ("temps_ordinaire", "ordinaire") or "ordinaire" in k:
        return "ordinaire"
    if k == "saint" or k.startswith("saint_"):
        return "saint"
    return k


def _temps_rule_matches(rule_val: str | None, periode: str | None) -> bool:
    rv = norm_slug(rule_val)
    if not rv or rv == "*":
        return True
    bucket = bucket_temps_liturgique(periode)
    pk = norm_slug(periode)
    return rv == bucket or (pk and rv == pk)


def _couleur_rule_matches(rule_val: str | None, couleur: str | None) -> bool:
    rv = norm_slug(rule_val)
    if not rv or rv == "*":
        return True
    return rv == norm_slug(couleur)


def _cible_rule_matches(rule_val: str | None, cible: str) -> bool:
    rv = norm_slug(rule_val)
    if not rv or rv == "*":
        return True
    # Accepte synthese / synthèse / synthesis
    want = norm_slug(cible)
    if rv in ("synthese", "synthesis") and want in ("synthese", "synthesis"):
        return True
    return rv == want


def _row_voice(r: Mapping[str, Any]) -> str:
    return str(r.get("Voix") or r.get("voix") or "").strip()


def _row_version(r: Mapping[str, Any]) -> int:
    try:
        return int(str(r.get("Version") or "0").strip())
    except Exception:
        return 0


def resolve_voice(
    rows: Iterable[Mapping[str, Any]],
    *,
    cible: str,
    couleur: str | None,
    periode: str | None,
    today: date | None = None,
) -> dict[str, Any]:
    """
    Choisit une voix Gemini TTS parmi les règles `Voix_Audio` (VOIX).
    Spécificité : +1 si Cible non *, +2 si Couleur non *, +2 si Temps non *.
    Tie-break : Version desc, puis Date_Effet desc.

    Retourne un dict détaillé pour l'UX (voix retenue, règle gagnante, score, fallback).
    """
    t = today or date.today()
    scored: list[tuple[int, int, str, str, dict]] = []

    for r in rows:
        if not sheet_row_status_is_live(r.get("Statut")):
            continue
        de = _parse_date_effet(r.get("Date_Effet"))
        if de is not None and de > t:
            continue
        if not _cible_rule_matches(str(r.get("Cible") or r.get("cible") or ""), cible):
            continue
        if not _couleur_rule_matches(str(r.get("Couleur") or r.get("couleur") or ""), couleur):
            continue
        if not _temps_rule_matches(str(r.get("Temps_Liturgique") or r.get("Temps Liturgique") or ""), periode):
            continue

        voice = _row_voice(r)
        if not voice:
            continue

        score = 0
        c = norm_slug(r.get("Cible"))
        if c and c != "*":
            score += 1
        col = norm_slug(r.get("Couleur"))
        if col and col != "*":
            score += 2
        tm = norm_slug(r.get("Temps_Liturgique"))
        if tm and tm != "*":
            score += 2

        ver = _row_version(r)
        de_s = str(r.get("Date_Effet") or "")
        scored.append((score, ver, de_s, voice, dict(r)))

    if not scored:
        return {
            "voice": DEFAULT_GEMINI_TTS_VOICE,
            "rule": None,
            "score": 0,
            "fallback": True,
            "reason": "Aucune règle Voix_Audio ne correspond — fallback en dur sur la voix par défaut.",
        }

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    score, _ver, _de, voice, rule = scored[0]
    return {
        "voice": voice,
        "rule": rule,
        "score": score,
        "fallback": False,
        "reason": "Règle la plus spécifique sélectionnée (score, version, date d'effet).",
    }


def pick_voice_name(
    rows: Iterable[Mapping[str, Any]],
    *,
    cible: str,
    couleur: str | None,
    periode: str | None,
    today: date | None = None,
) -> str:
    """Wrapper rétro-compatible : renvoie uniquement le nom de voix retenu."""
    return str(
        resolve_voice(rows, cible=cible, couleur=couleur, periode=periode, today=today)["voice"]
    )
