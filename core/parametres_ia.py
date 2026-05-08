from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from core.sheets_db import sheet_row_status_is_live


@dataclass(frozen=True)
class ParamIaRow:
    id: str
    key: str
    version: int
    statut: str
    date_effet: date | None
    content_md: str


def _to_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


def _norm(s: object) -> str:
    return str(s or "").strip()


def _parse_date_effet(v: object) -> date | None:
    s = _norm(v)
    if not s:
        return None
    # Accepte: YYYY-MM-DD ou ISO complet
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return None


def _is_active(statut: str) -> bool:
    return sheet_row_status_is_live(statut)


def pick_effective_templates(
    rows: Iterable[dict[str, Any]],
    *,
    today: date | None = None,
    allowed_keys: set[str] | None = None,
) -> dict[str, ParamIaRow]:
    """
    Pivot de vérité MARPA.
    Sélectionne la meilleure ligne par Clé_Prompt selon:
    - Statut Actif
    - Date_Effet <= aujourd'hui (si fournie)
    - Version la plus haute (puis Date_Effet la plus récente)
    """
    t = today or date.today()
    best: dict[str, ParamIaRow] = {}

    for r in rows:
        key = _norm(r.get("Clé_Prompt") or r.get("Cle_Prompt") or r.get("cle_prompt"))
        if not key:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue

        statut = _norm(r.get("Statut"))
        if not _is_active(statut):
            continue

        de = _parse_date_effet(r.get("Date_Effet"))
        if de is not None and de > t:
            continue

        row = ParamIaRow(
            id=_norm(r.get("#ID") or r.get("ID") or r.get("id")),
            key=key,
            version=_to_int(r.get("Version"), default=0),
            statut=statut,
            date_effet=de,
            content_md=_norm(r.get("Contenu_Markdown")),
        )

        cur = best.get(key)
        if cur is None:
            best[key] = row
            continue

        cur_de = cur.date_effet or date.min
        row_de = row.date_effet or date.min

        if (row.version, row_de) >= (cur.version, cur_de):
            best[key] = row

    return best

