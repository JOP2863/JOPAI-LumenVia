"""Synchronisation immuable des templates e-mail localisés depuis le pivot FR.

À chaque enregistrement FR : pour DE/EN/ES/IT/PT —
1) inactiver les lignes Actives (même template_key / canal / langue)
2) append une nouvelle ligne Actif (traduction programme)
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Callable, Iterable

from core.email_template_localize import (
    email_localize_target_langs,
    localize_email_template_from_fr,
)
from core.emailing import (
    email_template_row_is_live,
    language_filter_for_pref_langue,
)
from core.prompt_locale import coerce_aip_langue
from core.sheets_db import (
    SHEETS_ROW_STATUS_ACTIVE,
    SHEETS_ROW_STATUS_INACTIVE,
    append_immutable_row,
    compute_concat,
    fetch_records,
    invalidate_fetch_records_cache,
    open_spreadsheet,
    utc_now_iso,
    _resolve_table_name,
)


ProgressCb = Callable[[str], None]


def _lang_cell_matches(lang_raw: object, pref_langue: str) -> bool:
    cell = str(lang_raw or "").strip().lower()
    allowed = {x.lower() for x in language_filter_for_pref_langue(pref_langue)}
    # Hors FR : ne pas matcher la chaîne vide (réservée au FR historique).
    if pref_langue != "FR" and not cell:
        return False
    return cell in allowed


def _inactivate_live_templates(
    *,
    gs: object,
    spreadsheet_id: str,
    service_account_email: str | None,
    template_key: str,
    channel: str,
    pref_langue: str,
) -> int:
    """Passe en Inactif les lignes Actives pour (clé, canal, langue). Retourne le nombre touché."""
    sh = open_spreadsheet(
        gs, spreadsheet_id, service_account_email=service_account_email or None
    )
    ws = sh.worksheet(_resolve_table_name(sh=sh, table="email_templates"))
    header = ws.row_values(1)
    if not header:
        raise RuntimeError("Onglet templates e-mail sans en-tête.")
    col_status = header.index("status") + 1 if "status" in header else 0
    col_concat = header.index("concat") + 1 if "concat" in header else 0
    if not col_status:
        raise RuntimeError("Colonne `status` absente sur l’onglet templates e-mail.")
    try:
        recs = ws.get_all_records(numericise_ignore=["all"])
    except Exception:
        recs = []
    n = 0
    tk = str(template_key or "").strip()
    ch = str(channel or "email").strip().lower()
    for ix, rr in enumerate(recs):
        if str(rr.get("template_key") or "").strip() != tk:
            continue
        if str(rr.get("channel") or "").strip().lower() != ch:
            continue
        if not _lang_cell_matches(rr.get("language"), pref_langue):
            continue
        if not email_template_row_is_live(rr):
            continue
        merged = dict(rr)
        merged["status"] = SHEETS_ROW_STATUS_INACTIVE
        row_num = ix + 2
        ws.update_cell(row_num, col_status, SHEETS_ROW_STATUS_INACTIVE)
        if col_concat:
            ws.update_cell(row_num, col_concat, compute_concat(merged, header=header))
        n += 1
    return n


def _next_version_for_lang(
    rows: Iterable[dict[str, Any]],
    *,
    template_key: str,
    channel: str,
    pref_langue: str,
) -> int:
    tk = str(template_key or "").strip()
    ch = str(channel or "email").strip().lower()
    max_ver = 0
    for r0 in rows:
        if str(r0.get("template_key") or "").strip() != tk:
            continue
        if str(r0.get("channel") or "").strip().lower() != ch:
            continue
        if not _lang_cell_matches(r0.get("language"), pref_langue):
            continue
        vtxt = str(r0.get("version") or "").strip()
        if vtxt.isdigit():
            max_ver = max(max_ver, int(vtxt))
    return int(max_ver + 1)


def sync_localized_email_templates_from_fr(
    *,
    gs: object,
    spreadsheet_id: str,
    service_account_email: str | None = None,
    template_key: str,
    subject_fr: str,
    body_fr: str,
    status_note_fr: str = "",
    channel: str = "email",
    target_langs: tuple[str, ...] | None = None,
    progress: ProgressCb | None = None,
    vertex_client: object | None = None,
) -> dict[str, str]:
    """
    Pour chaque langue cible : inactiver l’Actif courant, append une version traduite.

    Retourne ``{lang: "ok"|"error: …"}``.
    """
    langs = target_langs or email_localize_target_langs()
    sid = str(spreadsheet_id or "").strip()
    tk = str(template_key or "").strip()
    results: dict[str, str] = {}
    if not sid or not tk or not (subject_fr or "").strip() or not (body_fr or "").strip():
        return {lg: "error: paramètres incomplets" for lg in langs}

    vx = vertex_client
    if vx is None:
        try:
            from core.config import load_config
            from core.vertex_gemini import VertexGeminiClient

            cfg = load_config()
            sa = getattr(cfg, "gcp_service_account", None)
            if sa:
                vx = VertexGeminiClient(service_account_info=sa)
        except Exception:
            vx = None

    try:
        rows_all = fetch_records(
            gspread_client=gs,
            spreadsheet_id=sid,
            table="email_templates",
            limit=0,
            use_cache=False,
        )
    except Exception as ex:
        return {lg: f"error: lecture ETPL ({type(ex).__name__})" for lg in langs}

    for lg0 in langs:
        lg = coerce_aip_langue(lg0)
        if lg == "FR":
            continue
        try:
            if progress:
                progress(f"Traduction Vertex template e-mail → {lg}…")
            subj_l, body_l, note_l = localize_email_template_from_fr(
                subject_fr=subject_fr,
                body_fr=body_fr,
                status_note_fr=status_note_fr,
                target_lang=lg,
                vertex_client=vx,
            )
            if progress:
                progress(f"Écriture ETPL {lg} (immuabilité)…")
            _inactivate_live_templates(
                gs=gs,
                spreadsheet_id=sid,
                service_account_email=service_account_email,
                template_key=tk,
                channel=channel,
                pref_langue=lg,
            )
            # Relecture légère pour version (rows_all peut être périmé après FR append).
            try:
                rows_now = fetch_records(
                    gspread_client=gs,
                    spreadsheet_id=sid,
                    table="email_templates",
                    limit=0,
                    use_cache=False,
                )
            except Exception:
                rows_now = list(rows_all)
            next_ver = _next_version_for_lang(
                rows_now, template_key=tk, channel=channel, pref_langue=lg
            )
            lang_cell = lg.lower()
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=sid,
                table="email_templates",
                values_by_col={
                    "entity_id": sha256(
                        f"tpl|{tk}|{lang_cell}|{next_ver}|{subj_l}|{body_l}|{utc_now_iso()}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:24],
                    "template_key": tk,
                    "channel": channel,
                    "language": lang_cell,
                    "subject": subj_l,
                    "body": body_l,
                    "version": next_ver,
                    "status": SHEETS_ROW_STATUS_ACTIVE,
                    "status_note": note_l,
                },
                version=next_ver,
            )
            results[lg] = "ok"
            rows_all = list(rows_now)  # best-effort
        except Exception as ex:
            results[lg] = f"error: {type(ex).__name__}: {ex}"

    invalidate_fetch_records_cache(spreadsheet_id=sid, table="email_templates")
    return results


__all__ = [
    "sync_localized_email_templates_from_fr",
]
