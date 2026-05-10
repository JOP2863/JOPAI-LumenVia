"""Nettoyage texte AELF pour affichage ou TTS (sans dépendance Streamlit)."""

from __future__ import annotations


def clean_aelf_text_for_display(text: str) -> str:
    """
    Nettoyage "présentation" uniquement (ne change pas la logique API).
    - Normalise retours ligne: \\r\\n -> \\n
    - Nettoie les espaces fin de ligne
    - Évite les blocs trop aérés
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
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
