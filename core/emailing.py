from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from core.sheets_db import sheet_row_status_is_live


def _email_tpl_status_cell(row: Mapping[str, Any]) -> str:
    """Lit la cellule « statut ligne » (synonymes possibles dans l’onglet Sheets)."""

    for key in ("status", "Statut", "Status"):
        s = row.get(key)
        if str(s or "").strip():
            return str(s or "").strip()
    return ""


def email_template_row_is_live(row: Mapping[str, Any]) -> bool:
    """Indique si une ligne de l’onglet **templates e-mail** est la version **courante** du point de vue métier.

    **Seul** ``status`` / ``Statut`` / ``Status`` compte : la valeur doit être **Actif** (ou équivalent
    accepté par ``sheet_row_status_is_live``). Dès qu’elle est **Inactif** (ou équivalent hors service),
    la ligne est ignorée pour l’édition, l’**envoi manuel** et le **choix du template par le scheduler**.

    La colonne ``active`` (si elle existe encore sur la feuille) **n’est pas lue** ici : elle peut servir à
    d’autres besoins de pilotage « campagne planifiée » dans la feuille, mais ne participe pas à cette décision.

    Une ligne **sans** statut renseigné n’est pas considérée comme version courante (évite les lignes ambiguës).
    """

    st_raw = _email_tpl_status_cell(row)
    if not st_raw:
        return False
    return sheet_row_status_is_live(st_raw)


def pick_latest_live_email_template(
    rows: Iterable[dict[str, Any]],
    *,
    template_key: str,
    channel: str = "email",
    language_in: tuple[str, ...] | None = ("fr", "fr-fr", "france", ""),
) -> dict[str, Any] | None:
    """Parmi les lignes dont le **statut** est encore **Actif** (voir ``email_template_row_is_live``), retourne
    la plus récente selon ``version`` puis ``created_at`` (la colonne ``active`` n’intervient pas).

    Retour ``None`` s’il n’y a aucune ligne pertinente."""

    tk = str(template_key or "").strip()
    ch_l = str(channel or "email").strip().lower()

    pool: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("template_key") or "").strip() != tk:
            continue
        if str(r.get("channel") or "").strip().lower() != ch_l:
            continue
        if language_in is not None:
            lang = str(r.get("language") or "").strip().lower()
            if lang not in language_in:
                continue
        if not email_template_row_is_live(r):
            continue
        pool.append(r)

    if not pool:
        return None

    def _key(rep: dict[str, Any]) -> tuple[int, str, str]:
        v_raw = str(rep.get("version") or "").strip()
        vn = int(v_raw) if v_raw.isdigit() else -1
        return (
            vn,
            str(rep.get("created_at") or ""),
            str(rep.get("row_id") or rep.get("entity_id") or ""),
        )

    return max(pool, key=_key)


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
        "url_audio_readings",
        "url_illustration",
        "illustration_description",
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

