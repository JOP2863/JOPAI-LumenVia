"""Passerelle catéchèse : titre canonique, détection Markdown et retrait PDF."""

from __future__ import annotations

import re

CATECHESE_SECTION_TITLE = "Passerelle catéchèse"
# Apostrophe typographique (synthèses déjà générées) et variante ASCII.
CATECHESE_SECTION_TITLE_LEGACY = "Passerelle catéchèse — L\u2019écho des paraboles"
CATECHESE_SECTION_TITLE_LEGACY_ASCII = "Passerelle catéchèse — L'écho des paraboles"

CATECHESE_SECTION_TITLES: tuple[str, ...] = (
    CATECHESE_SECTION_TITLE,
    CATECHESE_SECTION_TITLE_LEGACY,
    CATECHESE_SECTION_TITLE_LEGACY_ASCII,
)

CATECHESE_TTS_INTRO = f"{CATECHESE_SECTION_TITLE}."


def find_catechese_section_index(text: str | None) -> int:
    """Index du début de la passerelle (titre court ou legacy), ou -1."""
    if not text:
        return -1
    low = str(text).lower()
    found = -1
    for title in CATECHESE_SECTION_TITLES:
        idx = low.find(title.lower())
        if idx >= 0 and (found < 0 or idx < found):
            found = idx
    return found


def strip_catechese_title_prefix(text: str | None) -> str:
    """Retire le titre de section (court ou legacy) en tête du bloc passerelle."""
    body = (text or "").strip()
    for title in sorted(CATECHESE_SECTION_TITLES, key=len, reverse=True):
        if body.lower().startswith(title.lower()):
            body = body[len(title) :].lstrip(" \t\r\n-:#")
            break
    return body


def strip_catechese_bridge(text: str | None) -> str | None:
    """Retire la section passerelle catéchèse du Markdown si présente (pour option PDF)."""
    if not text:
        return text
    s = str(text)
    titles_alt = "|".join(re.escape(t) for t in CATECHESE_SECTION_TITLES)
    pat = re.compile(
        r"(?is)\n{0,2}(?:#{2,3}\s*|\*\*\s*)(" + titles_alt + r").*?(?=(?:\n#{2,3}\s)|\Z)"
    )
    out = re.sub(pat, "\n", s).strip()
    return out
