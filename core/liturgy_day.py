"""Facade multi-sources : lectures du jour selon la langue (pas de traduction maison).

Règle produit :
- ``FR`` → AELF (ne pas remplacer ce qui marche)
- ``DE`` / ``EN`` / ``ES`` / ``IT`` → Evangelizo Reader Feed
- Universalis reste disponible en Lab / secours, pas la route produit EN
"""

from __future__ import annotations

from typing import Literal

from core.aelf import AelfDayIdentity, AelfTexts, fetch_aelf_day
from core.evangelizo import fetch_evangelizo_for_product_lang
from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue

PrefLang = Literal["FR", "DE", "EN", "ES", "IT"]

# Langue produit → id registre (source câblée)
PRODUCT_LANG_TO_SOURCE_ID: dict[str, str] = {
    "FR": "aelf_france",
    "DE": "evangelizo_de",
    "EN": "evangelizo_en_am",
    "ES": "evangelizo_es_sp",
    "IT": "evangelizo_it",
}


def coerce_liturgy_pref_langue(pref_langue: object | None) -> str:
    """Langue supportée par la facade ; sinon repli ``FR`` (pas de traduction maison)."""
    lg = normalize_pref_langue(pref_langue)
    if lg in PRODUCT_LANG_TO_SOURCE_ID:
        return lg
    return DEFAULT_PREF_LANGUE


def fetch_liturgy_day(
    date_iso: str,
    *,
    pref_langue: str = "FR",
) -> tuple[AelfDayIdentity, AelfTexts, str]:
    """
    Retourne ``(identity, texts, source_id)`` pour la langue demandée.

    - ``FR`` → AELF france
    - ``DE`` / ``EN`` / ``ES`` / ``IT`` → Evangelizo (codes Reader DE / AM / SP / IT)
    """
    lg = coerce_liturgy_pref_langue(pref_langue)
    date_iso = str(date_iso or "").strip()[:10]
    if lg == "FR":
        ident, texts = fetch_aelf_day(date_iso, zone="france")
        return ident, texts, PRODUCT_LANG_TO_SOURCE_ID["FR"]
    if lg in ("DE", "EN", "ES", "IT"):
        ident, texts, _payload = fetch_evangelizo_for_product_lang(date_iso, pref_langue=lg)
        return ident, texts, PRODUCT_LANG_TO_SOURCE_ID[lg]
    raise ValueError(
        f"Langue non couverte pour les lectures natives : {lg!r} "
        f"(disponibles : {', '.join(supported_liturgy_langs())}). Pas de traduction maison."
    )


def supported_liturgy_langs() -> tuple[str, ...]:
    return ("FR", "DE", "EN", "ES", "IT")


def source_id_for_pref_langue(pref_langue: str) -> str:
    lg = coerce_liturgy_pref_langue(pref_langue)
    return PRODUCT_LANG_TO_SOURCE_ID[lg]
