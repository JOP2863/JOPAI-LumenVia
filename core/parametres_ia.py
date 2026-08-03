from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from core.locale_codes import DEFAULT_PREF_LANGUE
from core.prompt_locale import coerce_aip_langue
from core.sheets_db import sheet_row_status_is_live


@dataclass(frozen=True)
class ParamIaRow:
    id: str
    key: str
    version: int
    statut: str
    date_effet: date | None
    content_md: str
    langue: str = DEFAULT_PREF_LANGUE


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
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return None


def _is_active(statut: str) -> bool:
    return sheet_row_status_is_live(statut)


def row_aip_langue(r: dict[str, Any]) -> str:
    """Langue d’une ligne AIP ; cellule / colonne absente → FR."""
    raw = r.get("Langue")
    if raw is None:
        raw = r.get("langue") or r.get("language") or r.get("pref_langue")
    return coerce_aip_langue(raw)


def pick_effective_templates(
    rows: Iterable[dict[str, Any]],
    *,
    today: date | None = None,
    allowed_keys: set[str] | None = None,
    pref_langue: object | None = None,
    fallback_fr: bool = True,
) -> dict[str, ParamIaRow]:
    """
    Pivot de vérité Sheets (append-only).
    Sélectionne la meilleure ligne par Clé_Prompt selon:
    - Langue (= ``pref_langue``, défaut FR ; lignes sans Langue = FR)
    - Statut Actif
    - Date_Effet <= aujourd'hui (si fournie)
    - Version la plus haute (puis Date_Effet la plus récente)

    Si ``fallback_fr`` et qu’une clé manque hors FR, complète avec le gagnant FR.
    """
    t = today or date.today()
    want = coerce_aip_langue(pref_langue)
    best: dict[str, ParamIaRow] = {}

    def _consider(r: dict[str, Any], *, lang_filter: str) -> None:
        if row_aip_langue(r) != lang_filter:
            return
        key = _norm(r.get("Clé_Prompt") or r.get("Cle_Prompt") or r.get("cle_prompt"))
        if not key:
            return
        if allowed_keys is not None and key not in allowed_keys:
            return

        statut = _norm(r.get("Statut"))
        if not _is_active(statut):
            return

        de = _parse_date_effet(r.get("Date_Effet"))
        if de is not None and de > t:
            return

        row = ParamIaRow(
            id=_norm(r.get("#ID") or r.get("ID") or r.get("id")),
            key=key,
            version=_to_int(r.get("Version"), default=0),
            statut=statut,
            date_effet=de,
            content_md=_norm(r.get("Contenu_Markdown")),
            langue=lang_filter,
        )

        cur = best.get(key)
        if cur is None:
            best[key] = row
            return

        cur_de = cur.date_effet or date.min
        row_de = row.date_effet or date.min
        if (row.version, row_de) >= (cur.version, cur_de):
            best[key] = row

    for r in rows:
        _consider(r, lang_filter=want)

    if fallback_fr and want != DEFAULT_PREF_LANGUE:
        missing = None
        if allowed_keys is not None:
            missing = [k for k in allowed_keys if k not in best]
        else:
            missing = []
        if missing or allowed_keys is None:
            # Complète uniquement les clés absentes avec le FR Actif.
            fr_best: dict[str, ParamIaRow] = {}
            saved = best
            best = fr_best
            for r in rows:
                _consider(r, lang_filter=DEFAULT_PREF_LANGUE)
            for k, row in fr_best.items():
                if k not in saved:
                    saved[k] = row
            best = saved

    return best
