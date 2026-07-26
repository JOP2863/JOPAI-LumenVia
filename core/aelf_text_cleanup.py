"""Nettoyage texte AELF pour affichage ou TTS (sans dépendance Streamlit)."""

from __future__ import annotations

import re

# Rubriques du lectionnaire injectées dans ``contenu`` HTML (API AELF), pas de l'Écriture.
# Souvent en fin de lecture (sans variante jointe), parfois suivies d'une forme courte.
_AELF_LECTIONARY_RUBRIC_LINE_RE = re.compile(
    r"(?iu)^\s*(?:"
    r"OU\s+LECTURE\s+BR[EÈ]VE\.?"
    r"|OU\s+BIEN\.?"
    r"|OU\s+AU\s+CHOIX\.?"
    r"|Ou\s+bien\s*,?\s*lecture\s+br[eè]ve\s*:?"
    r"|ANN[EÉ]E\s+[ABC]\b.*"  # ex. « ANNÉE A 2026 » (rare)
    r")\s*$"
)


def strip_aelf_lectionary_rubrics(text: str) -> str:
    """
    Retire les rubriques AELF (« OU LECTURE BREVE », « OU BIEN », …).

    Si une telle ligne apparaît, on coupe à partir d'elle (la variante courte
    éventuelle qui suit n'est pas conservée — LumenVia ne lit qu'une forme).
    Ne touche pas aux paragraphes scripturaires du type « Ou encore : … ».
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
    out = re.sub(r"\n{3,}", "\n\n", out)
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
