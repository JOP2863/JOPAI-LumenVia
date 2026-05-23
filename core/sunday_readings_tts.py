"""Préparation texte pour TTS des lectures et de la synthèse (Vertex + Gemini API)."""

from __future__ import annotations

import re

from core.aelf_text_cleanup import clean_aelf_text_for_display

# Clés ``Paramètres_IA`` (Levier B) : documentation admin / choix de voix — jamais lues à voix haute.
AUDIO_STYLE_TEMPLATE_KEYS = frozenset(
    {
        "audio_style_default",
        "audio_style_paques",
        "audio_style_careme",
        "audio_style_lectures",
    }
)

# Débuts typiques des consignes ``audio_style_*`` (anciennes versions les concaténaient au TTS).
_TTS_ADMIN_PREAMBLE_PREFIXES: tuple[str, ...] = (
    "tu es lecteur du lectionnaire",
    "tu es la voix de lumenvia",
    "lis le texte suivant en français",
    "accent léger de joie",
    "garde une gravité paisible",
)

# Titres injectés par ``plain_readings_for_tts`` — point de départ du contenu parlé.
_READINGS_TTS_SECTION_MARKERS: tuple[str, ...] = (
    "Première lecture",
    "Premiere lecture",
    "Psaume",
    "Deuxième lecture",
    "Deuxieme lecture",
    "Évangile",
    "Evangile",
)


def strip_tts_admin_preamble(text: str) -> str:
    """
    Retire une consigne ``audio_style_*`` en tête si elle a été concaténée (régression ou cache).

    Ne modifie pas un texte qui commence déjà par une section liturgique seule.
    """
    t = (text or "").strip()
    if not t:
        return t
    # Consigne collée juste après « Première lecture. » (cache Sheets / ancien pipeline).
    t = re.sub(
        r"(?is)^(Première lecture|Premiere lecture)\.\s*"
        r"tu es lecteur du lectionnaire[^.]*\.\s*",
        r"\1. ",
        t,
    )
    low_head = t[:400].lower()
    has_admin_lead = any(p in low_head for p in _TTS_ADMIN_PREAMBLE_PREFIXES) or low_head.startswith(
        "tu es "
    )
    if not has_admin_lead:
        return t
    for marker in _READINGS_TTS_SECTION_MARKERS:
        idx = t.find(marker)
        if idx > 0:
            return t[idx:].strip()
    # Synthèse ou autre : retirer le premier paragraphe « consigne » si plusieurs blocs.
    parts = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    while len(parts) > 1:
        head = parts[0].lower()
        if any(head.startswith(p) for p in _TTS_ADMIN_PREAMBLE_PREFIXES):
            parts.pop(0)
            continue
        break
    return "\n\n".join(parts).strip() if parts else t


def spoken_text_for_tts(body: str) -> str:
    """
    Texte envoyé tel quel à Vertex ou Gemini TTS.

    Les modèles ne distinguent pas « consigne » et « contenu » : tout le champ ``text``
    est prononcé. Le style oral est porté par ``Voix_Audio`` (nom de voix), pas par les
    clés ``audio_style_*`` dans Sheets.
    """
    return strip_tts_admin_preamble((body or "").strip())


def plain_readings_for_tts(texts: object) -> str:
    """Texte continu pour TTS des quatre lectures AELF (sans HTML)."""
    parts: list[str] = []

    def _seg(title: str, body: str | None) -> None:
        raw = clean_aelf_text_for_display(body or "")
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = " ".join(raw.split())
        raw = strip_tts_admin_preamble(raw)
        if raw.strip():
            parts.append(f"{title}. {raw.strip()}")

    _seg("Première lecture", getattr(texts, "premiere_lecture", None))
    _seg("Psaume", getattr(texts, "psaume", None))
    _seg("Deuxième lecture", getattr(texts, "deuxieme_lecture", None))
    _seg("Évangile", getattr(texts, "evangile", None))
    return strip_tts_admin_preamble("\n\n".join(parts).strip())


def compose_synthesis_tts_text(
    *,
    body: str,
    templates: dict[str, str] | None = None,
    periode: str | None = None,
) -> str:
    """Texte lu pour l’audio de la synthèse (sans préfixes ``audio_style_*``)."""
    del templates, periode  # compatibilité des appels existants
    return spoken_text_for_tts(body)


def compose_readings_tts_text(*, body: str, templates: dict[str, str] | None = None) -> str:
    """Texte lu pour l’audio des lectures intégrales (sans préfixe ``audio_style_lectures``)."""
    del templates
    return spoken_text_for_tts(body)
