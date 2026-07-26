"""Nettoyage texte AELF pour affichage ou TTS (sans dépendance Streamlit)."""

from __future__ import annotations

import re

# Rubriques du lectionnaire injectées dans ``contenu`` HTML (API AELF), pas de l'Écriture.
_AELF_RUBRIC_CORE = (
    r"(?:"
    r"OU\s+LECTURE\s+BR[EÈ]VE\.?"
    r"|OU\s+BIEN\.?"
    r"|OU\s+AU\s+CHOIX\.?"
    r"|Ou\s+bien\s*,?\s*lecture\s+br[eè]ve\s*:?"
    r"|ANN[EÉ]E\s+[ABC]\b[^\n]*"
    r")"
)

# Ligne entière = rubrique seule.
_AELF_LECTIONARY_RUBRIC_LINE_RE = re.compile(
    rf"(?iu)^\s*{_AELF_RUBRIC_CORE}\s*$"
)

# Après aplatissement RDC (``\\s+`` → espace), la rubrique colle à la fin de la phrase.
_AELF_RUBRIC_TRAILING_INLINE_RE = re.compile(
    rf"(?iu)\s+{_AELF_RUBRIC_CORE}\s*$"
)

# Délimiteur au milieu d'un texte aplati (« … Seigneur. OU BIEN forme courte »).
_AELF_RUBRIC_MID_CUT_RE = re.compile(
    rf"(?iu)\s+(?:OU\s+LECTURE\s+BR[EÈ]VE|OU\s+BIEN|OU\s+AU\s+CHOIX)\.?(?:\s+|$)"
)


def strip_aelf_lectionary_rubrics(text: str) -> str:
    """
    Retire les rubriques AELF (« OU LECTURE BREVE », « OU BIEN », …).

    - Ligne entière isolée → coupe à partir de cette ligne (variante courte éventuelle exclue).
    - Même marqueur collé en fin de phrase (après aplatissement RDC) → suffixe retiré.
    - Ne touche pas aux paragraphes scripturaires « Ou encore : … ».
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s.strip():
        return ""

    lines = s.split("\n")
    kept: list[str] = []
    for ln in lines:
        if _AELF_LECTIONARY_RUBRIC_LINE_RE.match(ln.strip()):
            break
        kept.append(ln)
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    # Suffixe collé (ex. « … Parole de Dieu. OU LECTURE BREVE »).
    out = _AELF_RUBRIC_TRAILING_INLINE_RE.sub("", out).strip()

    # Variante jointe sur la même ligne après aplatissement.
    m = _AELF_RUBRIC_MID_CUT_RE.search(out)
    if m:
        out = out[: m.start()].rstrip()

    return out.strip()


def clean_aelf_text_for_display(text: str) -> str:
    """
    Nettoyage "présentation" (affichage, PDF, TTS) + rubriques lectionnaire.
    - Normalise retours ligne: \\r\\n -> \\n
    - Nettoie les espaces fin de ligne
    - Évite les blocs trop aérés
    - Retire « OU LECTURE BREVE » / « OU BIEN » / etc. (API AELF)
    """
    s = strip_aelf_lectionary_rubrics(text or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln.strip() == "":
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()
