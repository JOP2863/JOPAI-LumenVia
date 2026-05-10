"""Libellés et explications liturgiques (affichage humain, PDF, e-mail)."""

from __future__ import annotations

import re

from core.liturgy_theme import norm_key


def explain_liturgical_time(periode: str | None) -> str:
    k = norm_key(periode)
    hints: dict[str, str] = {
        "avent": "Temps de préparation à la venue du Seigneur : conversion douce, veille et espérance.",
        "noel": "Temps qui célèbre l’Incarnation : la Parole faite chair parmi nous.",
        "temps_ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "careme": "Temps de préparation pascale : prière, jeûne (intérieur) et partage.",
        "saint": "Mémoire ou fête d’un saint : exemplarité concrète de la foi.",
        "pascal": "Temps pascal : les cinquante jours qui prolongent la joie de la Résurrection jusqu’à la Pentecôte.",
        "pentecote": "Solennité de l’effusion de l’Esprit Saint sur l’Église.",
    }
    if k in hints:
        return hints[k]
    if "pentecot" in k:
        return hints["pentecote"]
    return "Grand mouvement liturgique qui colore la prière et la lecture de la Parole ce jour-là."


def explain_liturgical_color(couleur: str | None) -> str:
    k = norm_key(couleur)
    hints: dict[str, str] = {
        "blanc": "Couleur de joie et de gloire : grandes fêtes du Seigneur et de Marie (selon le temps).",
        "vert": "Couleur du Temps Ordinaire : vie chrétienne qui grandit dans la fidélité.",
        "rouge": "Couleur du martyre et de l’Esprit : don total et charité jusqu’au bout.",
        "violet": "Couleur de pénitence et d’attente : conversion et préparation (Avent/Carême selon le temps).",
        "rose": "Couleur d’allégement ponctuel au milieu de l’attente (Guadete / Laetare).",
        "noir": "Solennité funéraire ou jour marqué par le deuil liturgique.",
    }
    return hints.get(k, "La couleur vestimentaire traduit visuellement le climat liturgique du jour.")


def explain_liturgical_cycle(annee: str | None) -> str:
    k = norm_key(annee)
    hints: dict[str, str] = {
        "a": "Année A : le dimanche met souvent en avant l’Évangile selon Matthieu.",
        "b": "Année B : le dimanche met souvent en avant l’Évangile selon Marc.",
        "c": "Année C : le dimanche met souvent en avant l’Évangile selon Luc.",
        "annee_i": "Année des lectures propres au Temps Ordinaire (Année I).",
        "annee_ii": "Année des lectures propres au Temps Ordinaire (Année II).",
        "i": "Année des lectures propres au Temps Ordinaire (Année I).",
        "ii": "Année des lectures propres au Temps Ordinaire (Année II).",
    }
    return hints.get(k, "Le cycle liturgique fait tourner les lectures dominicales pour nourrir la foi sur plusieurs années.")


def normalize_roman_liturgy_token(token: str) -> str:
    """
    Met en majuscules les nombres romains (AELF peut renvoyer « Iii », « Ii », etc.).
    Ne modifie que les jetons composés uniquement des lettres I, V, X, L, C, D, M.
    """
    if not token:
        return token
    prefix = ""
    suffix = ""
    core = token
    while core and not core[0].isalpha():
        prefix += core[0]
        core = core[1:]
    while core and not core[-1].isalpha():
        suffix = core[-1] + suffix
        core = core[:-1]
    if not core or any(not c.isalpha() for c in core):
        return token
    if len(core) > 15:
        return token
    if not all(c.upper() in "IVXLCDM" for c in core):
        return token
    return prefix + core.upper() + suffix


def liturgy_display_label(s: str | None) -> str:
    """Majuscules d'usage (ex. Pascal, Blanc, Temps Ordinaire) ; articles courts en minuscules."""
    if not s or not str(s).strip():
        return "—"
    raw = str(s).strip().replace("_", " ")
    small = {"de", "du", "des", "la", "le", "les", "et", "à", "au", "aux", "en", "un", "une"}
    parts = raw.split()
    out: list[str] = []
    for i, p in enumerate(parts):
        lw = p.lower()
        if i > 0 and lw in small:
            out.append(lw)
        else:
            titled = p[:1].upper() + p[1:].lower() if p else ""
            out.append(normalize_roman_liturgy_token(titled))
    return " ".join(out) if out else "—"


def cycle_year_display(s: str | None) -> str:
    if not s or not str(s).strip():
        return "—"
    t = str(s).strip()
    if len(t) <= 2 and t.upper() in ("A", "B", "C"):
        return t.upper()
    return liturgy_display_label(t)


def extract_liturgical_week_num(semaine: str | None) -> str | None:
    if not semaine:
        return None
    m = re.match(r"\s*(\d+)", semaine.strip())
    return m.group(1) if m else None


def jour_liturgique_nom(identity: object) -> str | None:
    v = getattr(identity, "jour_liturgique_nom", None)
    return (str(v).strip() if v else None) or None


def jopai_mark_html() -> str:
    """Marque immuable : JOP (gras) + AI (italique) + © (exposant)."""
    return (
        '<span class="lv-jopai-mark">'
        '<span class="lv-jop">JOP</span><span class="lv-ai">AI</span><sup>©</sup>'
        "</span>"
    )


def liturgy_cover_pdf_title(identity: object) -> str:
    wn = extract_liturgical_week_num(getattr(identity, "semaine", None))
    temps = liturgy_display_label(getattr(identity, "periode", None))
    if wn and temps and temps != "—":
        return f"Semaine {wn} · {temps}"
    if wn:
        return f"Semaine {wn}"
    if temps and temps != "—":
        return temps
    return "La Lumière du Dimanche"
