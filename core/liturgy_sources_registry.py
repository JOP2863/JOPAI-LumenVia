"""Registre des sources de lectionnaire multi-langues (pas de traduction maison).

Règle produit : une source n’est « acceptable » que si elle fournit les **textes complets**
de la messe du jour dans la langue cible (pas seulement refs, évangile seul, ou calendrier).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Priorité produit (FR → DE → EN → ES → IT)
LANG_PRIORITY: tuple[str, ...] = ("FR", "DE", "EN", "ES", "IT")

SourceStatus = Literal["production", "candidate", "excluded", "unproven"]


@dataclass(frozen=True)
class LiturgySourceSpec:
    """Une API / fournisseur de textes liturgiques."""

    id: str
    label: str
    lang: str  # ISO 639-1 majuscules (FR, DE, …)
    status: SourceStatus
    provides_full_mass_texts: bool
    """True seulement si on a déjà prouvé lecture1+psaume(+lecture2)+évangile en texte intégral."""
    endpoint_template: str
    """URL avec ``{date}`` = YYYY-MM-DD et éventuellement ``{date_compact}`` = YYYYMMDD."""
    notes: str
    license_note: str = ""


# Sources déclarées pour le lab (validation manuelle via la page admin).
LITURGY_SOURCES: tuple[LiturgySourceSpec, ...] = (
    LiturgySourceSpec(
        id="aelf_france",
        label="AELF — messe (France)",
        lang="FR",
        status="production",
        provides_full_mass_texts=True,
        endpoint_template="https://api.aelf.org/v1/messes/{date}/france",
        notes="Source canonique LumenVia actuelle. Informations : /v1/informations/{date}/france.",
        license_note="Usage app conforme à l’API publique AELF (pas de clé).",
    ),
    LiturgySourceSpec(
        id="universalis_mass",
        label="Universalis — JSON messe",
        lang="EN",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://universalis.com/{date_compact}/jsonpmass.js",
        notes="Candidat EN : à valider (JSONP/JSON, textes complets, licence commerciale).",
        license_note="Vérifier ToS Universalis avant prod / TTS / e-mail.",
    ),
    LiturgySourceSpec(
        id="usccb_readings",
        label="USCCB — lectures (NABRE)",
        lang="EN",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://bible.usccb.org/bible/readings/{date_compact}.cfm",
        notes="Souvent HTML ; spike : existe-t-il un JSON officiel messe complète ?",
        license_note="NABRE — droits USCCB à vérifier.",
    ),
    LiturgySourceSpec(
        id="katholisch_readings",
        label="Katholisch.de — readings API (déclaré)",
        lang="DE",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://www.katholisch.de/api/liturgy/readings/{date}",
        notes="Endpoint à prouver (HTTP + JSON messe complète). Alternatives : liturgie.de.",
        license_note="Droits épiscopat DE à confirmer.",
    ),
    LiturgySourceSpec(
        id="liturgie_de_api",
        label="Deutsches Liturgisches Institut (déclaré)",
        lang="DE",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://www.liturgie.de/api",
        notes="URL racine — spike pour trouver l’endpoint messe du jour.",
        license_note="À confirmer.",
    ),
    LiturgySourceSpec(
        id="comunita_it",
        label="Comunita.it — liturgia (déclaré)",
        lang="IT",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://comunita.it/api/liturgia/{date}",
        notes="À prouver : JSON Prima Lettura / Salmo / Vangelo complets.",
        license_note="CEI / droits IT à confirmer.",
    ),
    LiturgySourceSpec(
        id="cee_liturgia",
        label="CEE Espagne — liturgia (déclaré)",
        lang="ES",
        status="unproven",
        provides_full_mass_texts=False,
        endpoint_template="https://www.conferenciaepiscopal.es/api/liturgia/{date}",
        notes="À prouver : messe complète ES.",
        license_note="CEE — droits à confirmer.",
    ),
    LiturgySourceSpec(
        id="evangeli_net_es",
        label="Evangeli.net — daily reading ES",
        lang="ES",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://evangeli.net/evangelio/api/daily-reading/es/{date}",
        notes="Exclu pour LumenVia : typiquement évangile (+commentaire), pas lectionnaire complet.",
        license_note="N/A tant qu’exclu.",
    ),
    LiturgySourceSpec(
        id="romcal_calapi",
        label="Romcal / Calapi — calendrier uniquement",
        lang="FR",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://calapi.inadiutorium.cz/api/v0/fr/calendars/default/{date}",
        notes="Exclu pour les textes : calendrier / refs, pas lectionnaire intégral.",
        license_note="Utile plus tard pour fêtes/couleurs, pas pour les lectures.",
    ),
)


def sources_for_lang(lang: str) -> list[LiturgySourceSpec]:
    lg = str(lang or "").strip().upper()
    return [s for s in LITURGY_SOURCES if s.lang == lg]


def sources_by_priority(*, include_excluded: bool = False) -> list[LiturgySourceSpec]:
    out: list[LiturgySourceSpec] = []
    for lg in LANG_PRIORITY:
        for s in sources_for_lang(lg):
            if not include_excluded and s.status == "excluded":
                continue
            out.append(s)
    # Orphelins hors priorité (aucun aujourd’hui)
    known = {s.id for s in out}
    for s in LITURGY_SOURCES:
        if s.id in known:
            continue
        if not include_excluded and s.status == "excluded":
            continue
        out.append(s)
    return out


def format_endpoint(spec: LiturgySourceSpec, *, date_iso: str) -> str:
    ds = str(date_iso or "").strip()[:10]
    compact = ds.replace("-", "")
    return (
        (spec.endpoint_template or "")
        .replace("{date}", ds)
        .replace("{date_compact}", compact)
    )
