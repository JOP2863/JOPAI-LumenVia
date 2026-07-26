"""Nettoyage texte AELF pour affichage ou TTS (sans dépendance Streamlit)."""

from __future__ import annotations

import re

# HTML : paragraphes isolés (casse AELF = majuscules).
_AELF_RUBRIC_HTML_P_RE = re.compile(
    r"(?s)<p[^>]*>\s*(?:"
    r"OU\s+LECTURE\s+BR[EÈ]VE\.?"
    r"|OU\s+BIEN\.?"
    r"|OU\s+AU\s+CHOIX\.?"
    r"|Ou\s+bien\s*,?\s*lecture\s+br[eè]ve\s*:?"
    r")\s*</p>",
    re.IGNORECASE,
)

# « OU LECTURE BREVE » — jamais de l'Écriture ; couper à partir de là (casse libre).
_AELF_OU_LECTURE_BREVE_CUT_RE = re.compile(
    r"(?is)(?:^|\s+)OU\s+LECTURE\s+BR[EÈ]VE\.?[\s\S]*$"
)

# Autres rubriques AELF en MAJUSCULES (éviter de couper un « ou bien » narratif).
_AELF_RUBRIC_CAPS_LINE_RE = re.compile(
    r"(?m)^\s*(?:OU\s+BIEN\.?|OU\s+AU\s+CHOIX\.?|Ou\s+bien\s*,?\s*lecture\s+br[eè]ve\s*:?|ANN[EÉ]E\s+[ABC]\b.*)\s*$"
)
_AELF_RUBRIC_CAPS_TRAILING_RE = re.compile(
    r"(?:\s+)(?:OU\s+BIEN\.?|OU\s+AU\s+CHOIX\.?)\s*$"
)
_AELF_RUBRIC_CAPS_MID_CUT_RE = re.compile(
    r"(?:\s+)(?:OU\s+BIEN\.?|OU\s+AU\s+CHOIX\.?)(?:\s+|$)"
)


def strip_aelf_lectionary_rubrics_html(html: str) -> str:
    """Retire les ``<p>OU LECTURE BREVE</p>`` (etc.) du HTML brut AELF."""
    if not html:
        return ""
    return _AELF_RUBRIC_HTML_P_RE.sub("", html)


def strip_aelf_lectionary_rubrics(text: str) -> str:
    """
    Retire les rubriques AELF (« OU LECTURE BREVE », « OU BIEN », …).

    - ``OU LECTURE BREVE`` : coupe à partir du marqueur (souvent collé après aplatissement RDC).
    - ``OU BIEN`` / ``OU AU CHOIX`` : uniquement la forme majuscules AELF (pas un « ou bien » narratif).
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s.strip():
        return ""

    # Lignes entières type rubrique.
    lines = s.split("\n")
    kept: list[str] = []
    for ln in lines:
        if _AELF_RUBRIC_CAPS_LINE_RE.match(ln.strip()) or re.match(
            r"(?i)^\s*OU\s+LECTURE\s+BR[EÈ]VE\.?\s*$", ln.strip()
        ):
            break
        kept.append(ln)
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    # Suffixe / reste collé.
    out = _AELF_OU_LECTURE_BREVE_CUT_RE.sub("", out).strip()
    out = _AELF_RUBRIC_CAPS_TRAILING_RE.sub("", out).strip()
    m = _AELF_RUBRIC_CAPS_MID_CUT_RE.search(out)
    if m:
        out = out[: m.start()].rstrip()

    return out.strip()


def aelf_text_still_has_lectionary_rubric(text: str | None) -> bool:
    """True si une rubrique lectionnaire est encore présente (garde-fou post-nettoyage)."""
    t = str(text or "")
    if re.search(r"(?i)OU\s+LECTURE\s+BR[EÈ]VE", t):
        return True
    if re.search(r"(?:^|\s)(?:OU\s+BIEN|OU\s+AU\s+CHOIX)(?:\s|$)", t):
        return True
    return False


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
