"""Préparation texte pour TTS des lectures et de la synthèse."""

from __future__ import annotations

import re

from core.aelf_text_cleanup import clean_aelf_text_for_display
from core.liturgy_theme import norm_key


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


def compose_synthesis_tts_text(*, body: str, templates: dict[str, str], periode: str | None) -> str:
    """Préfixe instructions TTS (Levier B) + corps synthèse."""
    base = (templates.get("audio_style_default") or "").strip()
    k = norm_key(periode)
    extras: list[str] = []
    if k == "pascal" or "pascal" in k:
        x = (templates.get("audio_style_paques") or "").strip()
        if x:
            extras.append(x)
    elif "careme" in k:
        x = (templates.get("audio_style_careme") or "").strip()
        if x:
            extras.append(x)
    parts: list[str] = []
    if base:
        parts.append(base)
    parts.extend(extras)
    parts.append((body or "").strip())
    return "\n\n".join(parts)


def compose_readings_tts_text(*, body: str, templates: dict[str, str]) -> str:
    lect = (templates.get("audio_style_lectures") or "").strip()
    b = (body or "").strip()
    if lect:
        return lect + "\n\n" + b
    return b
