"""Utilitaires audio / MIME pour pipelines TTS."""

from __future__ import annotations


def count_words(text: str) -> int:
    return len([w for w in (text or "").replace("\n", " ").split(" ") if w.strip()])


def ext_from_mime(mime: str | None) -> str:
    m = (mime or "").lower()
    if "audio/wav" in m or "audio/x-wav" in m or "wav" in m:
        return "wav"
    if "audio/mpeg" in m or "mp3" in m:
        return "mp3"
    if "audio/ogg" in m or "ogg" in m:
        return "ogg"
    if m.startswith("audio/"):
        return "wav"
    return "bin"
