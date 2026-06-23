"""Métadonnées de lecture AELF (intro_lue, référence) — affichage et TTS."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ligne injectée par ``plain_readings_for_tts`` : §intro§réf§
_READINGS_TTS_META_LINE_RE = re.compile(r"^§([^§]*)§([^§]*)§\s*$")


@dataclass(frozen=True)
class LiturgyTtsSection:
    title: str
    body: str
    intro_lue: str | None = None
    ref: str | None = None


def reading_caption(*, intro_lue: str | None, ref: str | None) -> str | None:
    """Sous-titre affiché sous le titre de section (missel)."""
    intro = (intro_lue or "").strip()
    reference = (ref or "").strip()
    if intro and reference:
        return f"{intro} — {reference}"
    if intro:
        return intro
    if reference:
        return reference
    return None


def compose_psalm_text(*, refrain: str | None, body: str | None) -> str | None:
    """Texte du psaume responsorial : refrain puis versets."""
    r = (refrain or "").strip()
    b = (body or "").strip()
    if r and b:
        return f"{r}\n\n{b}"
    return r or b or None


def liturgy_tts_sections_from_texts(texts: object) -> list[LiturgyTtsSection]:
    """Sections orales pour le TTS des lectures intégrales."""

    def _field(name: str) -> str | None:
        v = getattr(texts, name, None)
        s = (v or "").strip() if v is not None else ""
        return s or None

    out: list[LiturgyTtsSection] = []

    def _add(
        title: str,
        body_key: str,
        intro_key: str,
        ref_key: str,
        *,
        refrain_key: str | None = None,
    ) -> None:
        body = _field(body_key)
        refrain = _field(refrain_key) if refrain_key else None
        full = compose_psalm_text(refrain=refrain, body=body) if refrain_key else body
        if not full:
            return
        out.append(
            LiturgyTtsSection(
                title=title,
                intro_lue=_field(intro_key),
                ref=_field(ref_key),
                body=full,
            )
        )

    _add("Première lecture", "premiere_lecture", "premiere_lecture_intro", "premiere_lecture_ref")
    _add("Psaume", "psaume", "psaume_intro", "psaume_ref", refrain_key="psaume_refrain")
    _add("Deuxième lecture", "deuxieme_lecture", "deuxieme_lecture_intro", "deuxieme_lecture_ref")
    _add("Évangile", "evangile", "evangile_intro", "evangile_ref")
    return out


def encode_readings_tts_meta_line(*, intro_lue: str | None, ref: str | None) -> str | None:
    intro = (intro_lue or "").strip()
    reference = (ref or "").strip()
    if not intro and not reference:
        return None
    return f"§{intro}§{reference}§"


def split_readings_tts_body_meta(body: str) -> tuple[str | None, str | None, str]:
    """
    Retire une ligne ``§intro§ref§`` en tête du corps (format ``plain_readings_for_tts``).
    """
    raw = (body or "").strip()
    if not raw:
        return None, None, ""
    parts = raw.split("\n\n", 1)
    first = parts[0].strip()
    m = _READINGS_TTS_META_LINE_RE.match(first)
    if not m:
        return None, None, raw
    intro = (m.group(1) or "").strip() or None
    ref = (m.group(2) or "").strip() or None
    rest = parts[1].strip() if len(parts) > 1 else ""
    return intro, ref, rest


def oral_reading_intro_phrase(
    title: str,
    *,
    intro_lue: str | None,
    ref: str | None = None,
) -> str:
    """
    Annonce orale d'une section (remplace « selon le lectionnaire » par l'``intro_lue`` AELF).
    """
    norm = (title or "").strip()
    low = norm.lower()
    if low.startswith("première") or low.startswith("premiere"):
        label = "Première lecture"
    elif low.startswith("deuxième") or low.startswith("deuxieme"):
        label = "Deuxième lecture"
    elif low.startswith("psaume"):
        label = "Le Psaume"
    elif low.startswith("évangile") or low.startswith("evangile"):
        label = "Évangile"
    else:
        label = norm or "Lecture"

    intro = (intro_lue or "").strip()
    if intro:
        if not intro.endswith("."):
            intro += "."
        return f"{label}. {intro}"

    if label == "Première lecture":
        return "Première lecture. Écoutez la première lecture de la Parole."
    if label == "Le Psaume":
        reference = (ref or "").strip()
        if reference:
            if not reference.endswith("."):
                reference += "."
            return f"Le Psaume. {reference}"
        return "Le Psaume."
    return f"{label}."


def pdf_liturgy_reading_kwargs(texts: object) -> dict[str, str | None]:
    """Arguments ``premiere_lecture`` / intro / ref pour ``build_liturgy_sunday_pdf_bytes``."""

    def _t(name: str) -> str | None:
        v = getattr(texts, name, None)
        s = (v or "").strip() if v is not None else ""
        return s or None

    return {
        "premiere_lecture": _t("premiere_lecture"),
        "premiere_lecture_intro": _t("premiere_lecture_intro"),
        "premiere_lecture_ref": _t("premiere_lecture_ref"),
        "psaume": compose_psalm_text(refrain=_t("psaume_refrain"), body=_t("psaume")),
        "psaume_intro": _t("psaume_intro"),
        "psaume_ref": _t("psaume_ref"),
        "deuxieme_lecture": _t("deuxieme_lecture"),
        "deuxieme_lecture_intro": _t("deuxieme_lecture_intro"),
        "deuxieme_lecture_ref": _t("deuxieme_lecture_ref"),
        "evangile": _t("evangile"),
        "evangile_intro": _t("evangile_intro"),
        "evangile_ref": _t("evangile_ref"),
    }
