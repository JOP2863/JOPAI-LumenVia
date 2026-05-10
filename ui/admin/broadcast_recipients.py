"""Sélection des destinataires pour expédition manuelle (users + subscriptions)."""

from __future__ import annotations

from core.sheets_db import sheet_row_status_is_live



def lumenvia_manual_broadcast_users(
    *,
    users_rows: list[dict],
    subs_rows: list[dict],
    send_to_all: bool,
) -> list[dict]:
    """Feuilles `users` + `subscriptions` → liste des utilisateurs potentiels pour l’expédition manuelle (dry-run inclus)."""

    def _latest_sub_by_uid() -> dict[str, dict]:
        by: dict[str, dict] = {}
        for r in subs_rows:
            if str(r.get("type") or "").strip() != "weekly_friday":
                continue
            uid0 = str(r.get("user_entity_id") or "").strip()
            if not uid0:
                continue
            prev = by.get(uid0)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[uid0] = r
        return by

    latest_sub = _latest_sub_by_uid()
    by_uid_user: dict[str, dict] = {}
    for u in users_rows:
        uid0 = str(u.get("entity_id") or "").strip()
        if not uid0:
            continue
        prev = by_uid_user.get(uid0)
        if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
            by_uid_user[uid0] = u

    if send_to_all:
        ordered: list[dict] = []
        for uid0, subr in latest_sub.items():
            if not sheet_row_status_is_live(subr.get("status")):
                continue
            if str(subr.get("opt_in") or "").strip().lower() not in ("true", "1", "oui", "yes"):
                continue
            if str(subr.get("active") or "").strip().lower() not in ("true", "1", "oui", "yes", "active"):
                continue
            urec = by_uid_user.get(uid0) or {}
            if urec:
                ordered.append(urec)
        return ordered

    dry_users = [
        u for u in users_rows if str(u.get("source") or "").strip().lower() == "dry_run" and str(u.get("email") or "").strip()
    ]
    dry_users.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    u0 = dry_users[0] if dry_users else {}
    return [u0] if u0 else []


