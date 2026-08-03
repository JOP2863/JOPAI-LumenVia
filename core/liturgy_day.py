"""Facade multi-sources : lectures du jour selon la langue (pas de traduction maison)."""

from __future__ import annotations

from typing import Literal

from core.aelf import AelfDayIdentity, AelfTexts, fetch_aelf_day
from core.universalis import fetch_universalis_mass

PrefLang = Literal["FR", "EN"]


def fetch_liturgy_day(
    date_iso: str,
    *,
    pref_langue: str = "FR",
) -> tuple[AelfDayIdentity, AelfTexts, str]:
    """
    Retourne ``(identity, texts, source_id)`` pour la langue demandée.

    - ``FR`` → AELF france
    - ``EN`` → Universalis JSONP (horizon limité ; licence à respecter)
    """
    lg = str(pref_langue or "FR").strip().upper() or "FR"
    date_iso = str(date_iso or "").strip()[:10]
    if lg == "FR":
        ident, texts = fetch_aelf_day(date_iso, zone="france")
        return ident, texts, "aelf_france"
    if lg == "EN":
        ident, texts, _payload = fetch_universalis_mass(date_iso)
        return ident, texts, "universalis_mass"
    raise ValueError(
        f"Langue non couverte encore pour les lectures natives : {lg!r} "
        "(disponibles : FR, EN). Pas de traduction maison."
    )


def supported_liturgy_langs() -> tuple[str, ...]:
    return ("FR", "EN")
