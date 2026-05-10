"""Construction d’URL et lien HTML pour l’enquête « Donner mon avis » (e-mails)."""

from __future__ import annotations

import re
from urllib.parse import quote_plus

_FEEDBACK_SURVEY_CTA_RE = re.compile(
    r"👉\s*(?:\[\s*)?Donner\s+mon\s+avis\s+sur\s+cette\s+expérience(?:\s*\])?",
    flags=re.IGNORECASE,
)


def build_feedback_survey_url(*, base_url: str, recipient_email: str | None = None) -> str:
    """``base_url`` sans slash final ; renvoie une URL absolue ou ``""`` si base vide."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    em = str(recipient_email or "").strip().lower()
    url = f"{base}/?route=feedback"
    try:
        if em and bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)):
            url += f"&email={quote_plus(em)}"
    except Exception:
        pass
    return url


def wrap_feedback_cta_with_link(fragment: str, *, survey_url: str) -> str:
    """Encapsule la phrase 👉 … Donner mon avis … en lien ``<a>``."""
    txt = fragment or ""
    if not txt or "👉" not in txt or "avis" not in txt.lower():
        return txt
    if not survey_url:
        return txt

    def repl(m: re.Match[str]) -> str:
        lbl = (m.group(0) or "").strip()
        return (
            f'<a href="{survey_url}" target="_blank" rel="noopener noreferrer" '
            'style="color:#0d9488;font-weight:700;text-decoration:underline;">'
            f"{lbl}</a>"
        )

    return _FEEDBACK_SURVEY_CTA_RE.sub(repl, txt)
