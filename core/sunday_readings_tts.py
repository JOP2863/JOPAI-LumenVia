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


def spoken_text_for_tts(body: str) -> str:
    """
    Texte envoyé tel quel à Vertex ou Gemini TTS.

    Les modèles ne distinguent pas « consigne » et « contenu » : tout le champ ``text``
    est prononcé. Le style oral est porté par ``Voix_Audio`` (nom de voix), pas par les
    clés ``audio_style_*`` dans Sheets.
    """
    return (body or "").strip()


def plain_readings_for_tts(texts: object) -> str:
    """Texte continu pour TTS des quatre lectures AELF (sans HTML)."""
    parts: list[str] = []

    def _seg(title: str, body: str | None) -> None:
        raw = clean_aelf_text_for_display(body or "")
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = " ".join(raw.split())
        if raw.strip():
            parts.append(f"{title}. {raw.strip()}")

    _seg("Première lecture", getattr(texts, "premiere_lecture", None))
    _seg("Psaume", getattr(texts, "psaume", None))
    _seg("Deuxième lecture", getattr(texts, "deuxieme_lecture", None))
    _seg("Évangile", getattr(texts, "evangile", None))
    return "\n\n".join(parts).strip()


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
