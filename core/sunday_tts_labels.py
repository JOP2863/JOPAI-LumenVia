"""Libellés d’affichage voix / accent TTS (UI dimanche)."""

from __future__ import annotations

from datetime import date


def tts_voice_display_label(voice_name: str | None) -> str | None:
    raw = (voice_name or "").strip()
    if not raw:
        return None
    try:
        from core.gemini_tts_catalog import load_gemini_tts_voice_catalog

        mapping, _ = load_gemini_tts_voice_catalog()
        return str(mapping.get(raw) or raw)
    except Exception:
        return raw


def tts_accent_display_label(
    *,
    date_str: str,
    cible: str,
    voice_name: str | None,
) -> str | None:
    if not (voice_name or "").strip():
        return None
    try:
        from core.sunday_readings_tts import tts_french_accent_label_fr

        d = date.fromisoformat(str(date_str)[:10])
        return tts_french_accent_label_fr(
            sunday_date=d, cible=cible, voice_name=voice_name
        )
    except Exception:
        return None


def format_audio_voice_info(
    *,
    date_str: str,
    cible: str,
    voice_name: str | None,
) -> str | None:
    """
    Ligne d’info UI, ex. :
    ``Voix : Kore — neutre, posée (Firm) · Accent : Sud de la France (Midi)``
    """
    lab = tts_voice_display_label(voice_name)
    acc = tts_accent_display_label(
        date_str=date_str, cible=cible, voice_name=voice_name
    )
    parts: list[str] = []
    if lab:
        parts.append(f"Voix : {lab}")
    if acc:
        parts.append(f"Accent : {acc}")
    return " · ".join(parts) if parts else None
