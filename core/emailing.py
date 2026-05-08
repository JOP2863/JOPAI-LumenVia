from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


_TAG_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@dataclass(frozen=True)
class EmailTemplate:
    subject: str
    body: str


def render_template(tpl: EmailTemplate, *, values: dict[str, str]) -> EmailTemplate:
    """
    Remplace les balises {{tag}} par des valeurs.
    Les balises non résolues restent visibles (pour détecter un oubli).
    """

    def _sub(s: str) -> str:
        def repl(m: re.Match) -> str:
            k = str(m.group(1) or "").strip()
            if not k:
                return m.group(0)
            return str(values.get(k, m.group(0)))

        return re.sub(_TAG_RE, repl, s or "")

    return EmailTemplate(subject=_sub(tpl.subject), body=_sub(tpl.body))


def supported_tags() -> tuple[str, ...]:
    return (
        "prenom",
        "nom",
        "date_dimanche",
        "nom_du_dimanche",
        "url_pdf",
        "url_audio",
        "url_illustration",
        "url_app",
        "optout_url",
    )


def french_day_month_year(d: date) -> str:
    mois = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    return f"{d.day} {mois[d.month - 1]} {d.year}"

