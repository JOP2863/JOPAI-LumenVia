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


_CLES_LECTURE_LEGACY_TEMPLATE_PHRASES: tuple[str, ...] = (
    "les clés de lecture de ce dimanche {{nom_du_dimanche}} ({{date_dimanche}})",
    "les clés de lecture de ce dimanche {{nom_du_dimanche}}",
    "les clés de lecture de ce {{nom_du_dimanche}}",
)

_CLES_LECTURE_CANONICAL_TEMPLATE_PHRASE = (
    "les clés de lecture de la célébration de ce dimanche {{date_dimanche}}"
)

_CLES_LECTURE_RENDERED_RE = re.compile(
    r"les clés de lecture de ce(?:\s+dimanche)?(?:\s*\([^)]+\))?(?:\s+[^.\n]+)?",
    re.IGNORECASE,
)


def email_cles_lecture_celebration_phrase(*, date_label: str) -> str:
    """Phrase canonique après « Nous avons préparé pour vous … » (date = ``{{date_dimanche}}`` résolu)."""
    d = str(date_label or "").strip()
    if not d:
        return "les clés de lecture de la célébration de ce dimanche"
    return f"les clés de lecture de la célébration de ce dimanche {d}"


def normalize_email_body_liturgy_clause(body: str) -> str:
    """Réécrit les anciennes formulations ``… de ce {{nom_du_dimanche}}`` avant rendu."""
    out = body or ""
    for old in _CLES_LECTURE_LEGACY_TEMPLATE_PHRASES:
        out = out.replace(old, _CLES_LECTURE_CANONICAL_TEMPLATE_PHRASE)
    return out


def fix_rendered_email_cles_lecture_phrase(body: str, *, date_label: str) -> str:
    """Corrige les corps déjà rendus (ex. « de ce Sainte Trinité ») — templates ETPL hérités."""
    if not body or not str(date_label or "").strip():
        return body
    canonical = email_cles_lecture_celebration_phrase(date_label=date_label)
    return _CLES_LECTURE_RENDERED_RE.sub(canonical, body, count=1)


def render_weekly_email_template(tpl: EmailTemplate, *, values: dict[str, str]) -> EmailTemplate:
    """
    Rendu e-mail hebdo : normalise la phrase « clés de lecture » et injecte ``cles_lecture_celebration``.
    """
    date_label = str(values.get("date_dimanche") or "").strip()
    vals = dict(values)
    vals.setdefault(
        "cles_lecture_celebration",
        email_cles_lecture_celebration_phrase(date_label=date_label),
    )
    body_norm = normalize_email_body_liturgy_clause(tpl.body or "")
    rendered = render_template(EmailTemplate(subject=tpl.subject, body=body_norm), values=vals)
    fixed_body = fix_rendered_email_cles_lecture_phrase(rendered.body or "", date_label=date_label)
    return EmailTemplate(subject=rendered.subject, body=fixed_body)


def supported_tags() -> tuple[str, ...]:
    return (
        "prenom",
        "nom",
        "date_dimanche",
        "nom_du_dimanche",
        "cles_lecture_celebration",
        "url_pdf",
        "url_audio",
        "url_audio_readings",
        "url_illustration",
        "illustration_description",
        "url_app",
        "optout_url",
    )


def normalize_email_template_text(text: str) -> str:
    """Normalise objet/corps pour comparer formulaire UI vs ligne Sheets (espaces, fins de ligne)."""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


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


def email_sunday_date_fallback_label(date_str: str) -> str:
    """
    Repli ``{{nom_du_dimanche}}`` : même format que ``{{date_dimanche}}``, préfixé par
    « dimanche » lorsque la date tombe un dimanche.
    """
    ds = str(date_str or "").strip()[:10]
    if len(ds) != 10:
        return "—"
    try:
        d = date.fromisoformat(ds)
    except ValueError:
        return "—"
    label = french_day_month_year(d)
    if d.weekday() == 6:
        return f"dimanche {label}"
    return label


def resolve_email_nom_du_dimanche(
    *,
    identity: object | None,
    date_str: str,
    gspread_client: object | None = None,
    spreadsheet_id: str | None = None,
) -> str:
    """
    Valeur de ``{{nom_du_dimanche}}`` pour un envoi e-mail.

    1. Identité AELF du dimanche ciblé (fête + semaine du Psautier).
    2. Secours : dernière ligne ``readings_cache`` (RDC) pour cette date.
    3. Repli : « dimanche 25 mai 2026 » (ou date seule si ce n’est pas un dimanche).
    """
    from core.liturgy_display_helpers import email_sunday_liturgy_label, is_weak_liturgy_title
    from core.sheets_db import fetch_records, sheet_row_status_is_live

    def _ok(label: str) -> bool:
        return bool(label and label != "—" and not is_weak_liturgy_title(label))

    label = email_sunday_liturgy_label(identity)
    if _ok(label):
        return label

    sid = str(spreadsheet_id or "").strip()
    ds = str(date_str or "")[:10]
    if gspread_client and sid and ds:
        try:
            rows = fetch_records(
                gspread_client=gspread_client,
                spreadsheet_id=sid,
                table="readings_cache",
                limit=0,
                use_cache=True,
            )
        except Exception:
            rows = []
        else:
            candidates = [
                r
                for r in rows
                if str(r.get("date") or "")[:10] == ds
                and sheet_row_status_is_live(r.get("status"))
                and not str(r.get("error") or "").strip()
            ]
            if candidates:
                candidates.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
                r0 = candidates[0]

                class _LiturgyRow:
                    fete = r0.get("fete")
                    semaine = r0.get("semaine")
                    jour_liturgique_nom = r0.get("jour_liturgique_nom")
                    periode = r0.get("periode")

                label = email_sunday_liturgy_label(_LiturgyRow())
                if _ok(label):
                    return label

    return email_sunday_date_fallback_label(date_str)

