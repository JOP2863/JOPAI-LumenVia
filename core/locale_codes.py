"""Pays (``users.country``, ISO 3166-1) et langue de consultation (``users.pref_langue``, ISO 639-1).

Les deux codes sont stockés en **majuscules** sur 2 lettres (ex. ``FR``).
Détails dans la table Sheets ``langues_pays`` (onglet **LGP**).
"""

from __future__ import annotations

from typing import Iterable

DEFAULT_PREF_LANGUE = "FR"
DEFAULT_COUNTRY = "FR"

# Domaines dans ``langues_pays.domaine``
DOMAINE_LANGUE = "langue"
DOMAINE_PAYS = "pays"

# Graine minimale + langues européennes courantes (codes ISO 639-1 en majuscules).
SEED_LANGUES: tuple[tuple[str, str, str], ...] = (
    # code, libelle, bcp47
    ("FR", "Français", "fr-FR"),
    ("EN", "English", "en-GB"),
    ("ES", "Español", "es-ES"),
    ("IT", "Italiano", "it-IT"),
    ("DE", "Deutsch", "de-DE"),
    ("PT", "Português", "pt-PT"),
    ("NL", "Nederlands", "nl-NL"),
    ("PL", "Polski", "pl-PL"),
)

SEED_PAYS: tuple[tuple[str, str], ...] = (
    # code ISO 3166-1 alpha-2, libelle FR
    ("FR", "France"),
    ("BE", "Belgique"),
    ("CH", "Suisse"),
    ("CA", "Canada"),
    ("LU", "Luxembourg"),
    ("MC", "Monaco"),
    ("SN", "Sénégal"),
    ("CI", "Côte d’Ivoire"),
    ("CM", "Cameroun"),
    ("MG", "Madagascar"),
    ("HT", "Haïti"),
    ("MA", "Maroc"),
    ("DZ", "Algérie"),
    ("TN", "Tunisie"),
    ("RE", "La Réunion"),
    ("GP", "Guadeloupe"),
    ("MQ", "Martinique"),
    ("GF", "Guyane"),
    ("YT", "Mayotte"),
    ("NC", "Nouvelle-Calédonie"),
    ("PF", "Polynésie française"),
)


def normalize_pref_langue(code: object | None) -> str:
    """ISO 639-1 (2 lettres, **majuscules**). Défaut ``FR``."""
    c = str(code or "").strip().upper().replace("_", "-")
    if "-" in c:
        c = c.split("-", 1)[0]
    if len(c) >= 2 and c[:2].isalpha():
        return c[:2]
    return DEFAULT_PREF_LANGUE


def normalize_country(code: object | None) -> str:
    """ISO 3166-1 alpha-2 (2 lettres, majuscules). Défaut ``FR``."""
    c = str(code or "").strip().upper().replace("_", "-")
    if "-" in c:
        c = c.split("-", 1)[0]
    if len(c) >= 2 and c[:2].isalpha():
        return c[:2]
    return DEFAULT_COUNTRY


# Alias historiques (évite de casser d’éventuels imports).
normalize_nationalite = normalize_country
DEFAULT_NATIONALITE = DEFAULT_COUNTRY


def user_pref_langue(row: dict | None) -> str:
    return normalize_pref_langue((row or {}).get("pref_langue"))


def user_country(row: dict | None) -> str:
    return normalize_country((row or {}).get("country"))


def user_nationalite(row: dict | None) -> str:
    """Deprecated : utilise ``user_country`` (champ ``country``)."""
    return user_country(row)


def options_from_langues_pays_rows(
    rows: Iterable[dict],
    *,
    domaine: str,
) -> list[tuple[str, str]]:
    """Liste ``(code, libelle)`` Actif pour selectboxes, triée par libellé."""
    dom = str(domaine or "").strip().lower()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        if str(r.get("domaine") or "").strip().lower() != dom:
            continue
        st = str(r.get("status") or "").strip().lower()
        if st and st not in ("actif", "active", "1", "true", "oui", "yes"):
            if st in ("inactif", "inactive", "0", "false", "non", "no"):
                continue
        code = str(r.get("code") or "").strip()
        if dom == DOMAINE_LANGUE:
            code = normalize_pref_langue(code)
        else:
            code = normalize_country(code)
        if not code or code in seen:
            continue
        lib = str(r.get("libelle") or "").strip() or code
        seen.add(code)
        out.append((code, f"{lib} ({code})"))
    out.sort(key=lambda t: t[1].casefold())
    return out


def fallback_langue_options() -> list[tuple[str, str]]:
    return [(c, f"{lib} ({c})") for c, lib, _ in SEED_LANGUES]


def fallback_pays_options() -> list[tuple[str, str]]:
    return [(c, f"{lib} ({c})") for c, lib in SEED_PAYS]
