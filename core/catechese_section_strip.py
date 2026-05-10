"""Retrait Markdown de la passerelle catéchèse pour exports PDF."""

from __future__ import annotations

import re

_CATECHESE_SECTION_TITLE = "Passerelle catéchèse — L’écho des paraboles"


def strip_catechese_bridge(text: str | None) -> str | None:
    """Retire la section « Passerelle catéchèse… » du Markdown si présente (pour option PDF)."""
    if not text:
        return text
    s = str(text)
    pat = re.compile(
        r"(?is)\n{0,2}(?:#{2,3}\s*|\\*\\*\\s*)"
        + re.escape(_CATECHESE_SECTION_TITLE)
        + r".*?(?=(?:\n#{2,3}\s)|\\Z)"
    )
    out = re.sub(pat, "\n", s).strip()
    return out
