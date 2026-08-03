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
        notes="Source canonique LumenVia. Adapter : core/aelf.py. Informations : /v1/informations/{date}/france.",
        license_note="Usage app conforme à l’API publique AELF (pas de clé).",
    ),
    LiturgySourceSpec(
        id="universalis_mass",
        label="Universalis — JSONP messe (Lab / secours)",
        lang="EN",
        status="candidate",
        provides_full_mass_texts=True,
        endpoint_template="https://universalis.com/{date_compact}/jsonpmass.js",
        notes=(
            "Non câblé en produit : EN produit = evangelizo_en_am. "
            "Adapter Lab : core/universalis.py. Horizon JSONP ~4 j. "
            "Mail contact 2026-08-03 — gate licence fermée."
        ),
        license_note=(
            "Universalis Publishing Ltd. Voir data/universalis_license_checklist.json. "
            "Pas la route produit tant qu’Evangelizo couvre l’EN."
        ),
    ),
    LiturgySourceSpec(
        id="evangelizo_de",
        label="Evangelizo — Reader Feed DE",
        lang="DE",
        status="candidate",
        provides_full_mass_texts=True,
        endpoint_template=(
            "https://feed.evangelizo.org/v2/reader.php?date={date_compact}&type=xml&lang=DE"
        ),
        notes=(
            "Route produit DE via core/liturgy_day.py. Adapter : core/evangelizo.py. "
            "Lab 2026-08 : 7/7 messe OK. Horizon ≈ 30 j. ToS à checklist avant e-mail/TTS/PDF."
        ),
        license_note=(
            "L’Évangile au Quotidien / Evangelizo — Reader destiné à l’affichage web ; "
            "ToS / redistribution e-mail/TTS/PDF à confirmer avant prod canaux larges."
        ),
    ),
    LiturgySourceSpec(
        id="evangelizo_en_am",
        label="Evangelizo — Reader Feed EN (AM)",
        lang="EN",
        status="candidate",
        provides_full_mass_texts=True,
        endpoint_template=(
            "https://feed.evangelizo.org/v2/reader.php?date={date_compact}&type=xml&lang=AM"
        ),
        notes=(
            "Route produit EN (code Reader AM). Lab 2026-08 : 7/7 messe OK. "
            "Remplace Universalis comme source câblée. Horizon ≈ 30 j."
        ),
        license_note="Idem Evangelizo — valider ToS avant e-mail / TTS / PDF larges.",
    ),
    LiturgySourceSpec(
        id="evangelizo_es_sp",
        label="Evangelizo — Reader Feed ES (SP)",
        lang="ES",
        status="candidate",
        provides_full_mass_texts=True,
        endpoint_template=(
            "https://feed.evangelizo.org/v2/reader.php?date={date_compact}&type=xml&lang=SP"
        ),
        notes=(
            "Route produit ES (code Reader SP). Lab 2026-08 : 7/7. Adapter : core/evangelizo.py."
        ),
        license_note="Idem Evangelizo — valider ToS avant e-mail / TTS / PDF larges.",
    ),
    LiturgySourceSpec(
        id="evangelizo_it",
        label="Evangelizo — Reader Feed IT",
        lang="IT",
        status="candidate",
        provides_full_mass_texts=True,
        endpoint_template=(
            "https://feed.evangelizo.org/v2/reader.php?date={date_compact}&type=xml&lang=IT"
        ),
        notes=(
            "Route produit IT. Lab 2026-08 : 7/7. Adapter : core/evangelizo.py."
        ),
        license_note="Idem Evangelizo — valider ToS avant e-mail / TTS / PDF larges.",
    ),
    LiturgySourceSpec(
        id="usccb_readings",
        label="USCCB — lectures (NABRE)",
        lang="EN",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://bible.usccb.org/bible/readings/{date_compact}.cfm",
        notes="Lab 2026-08 : 404 / 403 (bot-check Varnish). Pas d’API JSON publique messe complète.",
        license_note="NABRE — droits USCCB.",
    ),
    LiturgySourceSpec(
        id="katholisch_readings",
        label="Katholisch.de — readings API (déclaré)",
        lang="DE",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://www.katholisch.de/api/liturgy/readings/{date}",
        notes="Lab 2026-08 : HTTP 404. Remplacé comme piste par evangelizo_de (Reader réel).",
        license_note="Droits épiscopat DE à confirmer.",
    ),
    LiturgySourceSpec(
        id="liturgie_de_api",
        label="Deutsches Liturgisches Institut (déclaré)",
        lang="DE",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://www.liturgie.de/api",
        notes="Lab 2026-08 : HTTP 404 sur /api. Pas d’endpoint messe trouvé.",
        license_note="À confirmer.",
    ),
    LiturgySourceSpec(
        id="comunita_it",
        label="Comunita.it — liturgia (déclaré)",
        lang="IT",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://comunita.it/api/liturgia/{date}",
        notes="Lab 2026-08 : HTTP 404. Remplacé comme piste par evangelizo_it.",
        license_note="CEI / droits IT à confirmer.",
    ),
    LiturgySourceSpec(
        id="cee_liturgia",
        label="CEE Espagne — liturgia (déclaré)",
        lang="ES",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://www.conferenciaepiscopal.es/api/liturgia/{date}",
        notes="Lab 2026-08 : HTTP 404. Remplacé comme piste par evangelizo_es_sp (lang=SP).",
        license_note="CEE — droits à confirmer.",
    ),
    LiturgySourceSpec(
        id="gemini_fake_evangelizo_rest",
        label="(exclu) REST Gemini levangileauquotidien.org/api/v1",
        lang="FR",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://levangileauquotidien.org/api/v1/fr/reading/{date}",
        notes=(
            "Suggestion LLM non vérifiée : HTTP 200 mais Content-Type text/html (SPA Angular), "
            "pas de JSON. Idem /en|/de|/es|/it. Ciudad Redonda /api/v1/evangelio/{date} → 404. "
            "api.evangelizo.org → 401 (auth). Utiliser feed.evangelizo.org/v2/reader.php."
        ),
        license_note="N/A — endpoint inexistant.",
    ),
    LiturgySourceSpec(
        id="evangeli_net_es",
        label="Evangeli.net — daily reading ES",
        lang="ES",
        status="excluded",
        provides_full_mass_texts=False,
        endpoint_template="https://evangeli.net/evangelio/api/daily-reading/es/{date}",
        notes="Exclu : typiquement évangile (+commentaire), pas lectionnaire complet.",
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
