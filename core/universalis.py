"""Universalis — lectures de la messe (JSONP), anglais.

Règle produit : textes natifs EN, pas de traduction maison.
Licence : le JSON inclut un copyright ; affichage obligatoire côté UI ;
usage commercial / e-mail / TTS à valider avec Universalis Publishing Ltd.
Horizon : le JSONP gratuit ne couvre souvent que quelques jours (au-delà → page HTML « Other dates »).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

import requests

from core.aelf import AelfDayIdentity, AelfTexts

UNIVERSALIS_JSONP_TEMPLATE = "https://universalis.com/{date_compact}/jsonpmass.js"
UNIVERSALIS_HOME_URL = "https://universalis.com/"
UNIVERSALIS_MASS_READINGS_URL = "https://universalis.com/mass.htm"
UNIVERSALIS_JSONP_DOC_URL = "https://universalis.com/n-jsonp-technical.htm"
_UA = "JOPAI-LumenVia-Universalis/0.1"

# Canaux autorisés tant qu’aucun accord écrit n’est obtenu (voir data/universalis_license_checklist.json).
UNIVERSALIS_ALLOWED_WITHOUT_WRITTEN_OK: frozenset[str] = frozenset({"lab", "admin_spike"})
UNIVERSALIS_BLOCKED_WITHOUT_WRITTEN_OK: frozenset[str] = frozenset(
    {"email", "sms", "tts", "pdf", "production_ui", "ai_synthesis_public"}
)


def attribution_html(*, copyright_text: str = "") -> str:
    """Bloc attribution obligatoire (lien + copyright payload si fourni)."""
    notice = (copyright_text or "").strip()
    parts = [
        'Source: <a href="https://universalis.com/" rel="noopener noreferrer">Universalis</a>',
        '(<a href="https://universalis.com/mass.htm" rel="noopener noreferrer">Mass readings</a>).',
    ]
    if notice:
        parts.append(html_lib.escape(notice))
    return " ".join(parts)


def attribution_plain(*, copyright_text: str = "") -> str:
    notice = (copyright_text or "").strip()
    base = "Source: Universalis — https://universalis.com/ (Mass readings)."
    return f"{base} {notice}".strip() if notice else base


def channel_allowed(channel: str, *, written_permission: bool = False) -> bool:
    """True si le canal est utilisable sous la politique LumenVia actuelle."""
    ch = str(channel or "").strip().lower()
    if written_permission:
        return True
    if ch in UNIVERSALIS_BLOCKED_WITHOUT_WRITTEN_OK:
        return False
    return ch in UNIVERSALIS_ALLOWED_WITHOUT_WRITTEN_OK or ch in {"web_jsonp_client"}


class UniversalisError(RuntimeError):
    """Erreur d’accès ou de parsing Universalis."""


class UniversalisHorizonError(UniversalisError):
    """Date hors horizon JSONP (redirection vers n-otherdates.htm)."""


def _date_compact(date_iso: str) -> str:
    return str(date_iso or "").strip()[:10].replace("-", "")


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def parse_universalis_jsonp(raw: str) -> dict[str, Any]:
    """Extrait l’objet JSON de ``universalisCallback({...});``."""
    s = (raw or "").strip()
    if not s:
        raise UniversalisError("Réponse Universalis vide")
    low = s[:200].lower()
    if "<html" in low or "other dates" in low:
        raise UniversalisHorizonError(
            "Réponse HTML (souvent hors horizon JSONP / n-otherdates.htm), pas de messe JSON."
        )
    if s.startswith("universalisCallback("):
        s = s[len("universalisCallback(") :]
    s = s.strip()
    if s.endswith(");"):
        s = s[:-2]
    elif s.endswith(")"):
        s = s[:-1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as ex:
        raise UniversalisError(f"JSONP illisible : {ex}") from ex
    if not isinstance(obj, dict):
        raise UniversalisError("JSONP : objet attendu")
    return obj


def _reading_parts(block: Any) -> tuple[str | None, str | None, str | None]:
    """Retourne (texte, ref/source, heading)."""
    if not isinstance(block, dict):
        return None, None, None
    text = _strip_html(str(block.get("text") or ""))
    source = _strip_html(str(block.get("source") or ""))
    heading = _strip_html(str(block.get("heading") or ""))
    return (text or None), (source or None), (heading or None)


def payload_to_aelf_texts(payload: dict[str, Any]) -> AelfTexts:
    """Mappe Mass_R1 / Mass_Ps / Mass_R2 / Mass_G vers le contrat AelfTexts."""
    b1, r1, h1 = _reading_parts(payload.get("Mass_R1"))
    bp, rp, hp = _reading_parts(payload.get("Mass_Ps"))
    b2, r2, h2 = _reading_parts(payload.get("Mass_R2"))
    be, re_, he = _reading_parts(payload.get("Mass_G"))
    # Intro = heading Universalis (équivalent proche d’intro_lue)
    return AelfTexts(
        premiere_lecture=b1,
        psaume=bp,
        deuxieme_lecture=b2,
        evangile=be,
        premiere_lecture_intro=h1,
        premiere_lecture_ref=r1,
        psaume_intro=hp,
        psaume_ref=rp,
        deuxieme_lecture_intro=h2,
        deuxieme_lecture_ref=r2,
        evangile_intro=he,
        evangile_ref=re_,
    )


def payload_to_identity(payload: dict[str, Any], *, date_iso: str) -> AelfDayIdentity:
    day_html = str(payload.get("day") or "")
    day_txt = _strip_html(day_html)
    date_label = _strip_html(str(payload.get("date") or ""))
    return AelfDayIdentity(
        date=str(date_iso or "")[:10],
        zone="universalis",
        periode=None,
        semaine=None,
        annee=None,
        couleur=None,
        fete=day_txt or date_label or None,
        jour_liturgique_nom=day_txt or date_label or None,
    )


def copyright_notice(payload: dict[str, Any]) -> str:
    raw = payload.get("copyright")
    if isinstance(raw, dict):
        return _strip_html(str(raw.get("text") or raw.get("html") or json.dumps(raw, ensure_ascii=False)))
    return _strip_html(str(raw or ""))


def is_full_mass(texts: AelfTexts) -> bool:
    def _ok(v: object) -> bool:
        return bool(str(v or "").strip()) and len(str(v).strip()) > 40

    return _ok(texts.premiere_lecture) and _ok(texts.psaume) and _ok(texts.evangile)


def fetch_universalis_mass(
    date_iso: str,
    *,
    timeout_s: float = 20.0,
) -> tuple[AelfDayIdentity, AelfTexts, dict[str, Any]]:
    """
    Récupère la messe Universalis pour ``date_iso`` (YYYY-MM-DD).

    Retourne ``(identity, texts, raw_payload)``.
    """
    date_iso = str(date_iso or "").strip()[:10]
    url = UNIVERSALIS_JSONP_TEMPLATE.format(date_compact=_date_compact(date_iso))
    try:
        r = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": _UA, "Accept": "application/javascript, text/*, */*"},
            allow_redirects=True,
        )
    except requests.RequestException as ex:
        raise UniversalisError(f"HTTP Universalis : {ex}") from ex

    final = str(r.url or "")
    if "otherdates" in final.lower() or "n-otherdates" in final.lower():
        raise UniversalisHorizonError(
            f"Date {date_iso} hors horizon JSONP (redirigé vers {final})."
        )
    if r.status_code >= 400:
        raise UniversalisError(f"HTTP {r.status_code} sur {url}")

    payload = parse_universalis_jsonp(r.text or "")
    # Garde-fou : payload sans Mass_G / Mass_R1
    if "Mass_R1" not in payload and "Mass_G" not in payload:
        raise UniversalisError("Payload sans Mass_R1 / Mass_G")

    identity = payload_to_identity(payload, date_iso=date_iso)
    texts = payload_to_aelf_texts(payload)
    return identity, texts, payload
