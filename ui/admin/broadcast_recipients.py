"""Sélection des destinataires pour expédition manuelle (users + subscriptions)."""

from __future__ import annotations

import re

from core.sheets_db import sheet_row_status_is_live


def is_broadcast_email_ok(email: str) -> bool:
    email_lc = (email or "").strip().lower()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_lc)) if email_lc else False


def is_broadcast_phone_ok(phone: str) -> bool:
    ph = (phone or "").strip()
    return bool(re.match(r"^\+[1-9]\d{6,14}$", ph)) if ph else False


def _weekly_subscriptions_by_uid(subs_rows: list[dict]) -> dict[str, dict]:
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


def _latest_users_by_uid(users_rows: list[dict]) -> dict[str, dict]:
    by_uid_user: dict[str, dict] = {}
    for u in users_rows:
        uid0 = str(u.get("entity_id") or "").strip()
        if not uid0:
            continue
        prev = by_uid_user.get(uid0)
        if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
            by_uid_user[uid0] = u
    return by_uid_user


def _subscription_is_mailable(subr: dict) -> bool:
    if not sheet_row_status_is_live(subr.get("status")):
        return False
    if str(subr.get("opt_in") or "").strip().lower() not in ("true", "1", "oui", "yes"):
        return False
    if str(subr.get("active") or "").strip().lower() not in ("true", "1", "oui", "yes", "active"):
        return False
    return True


def lumenvia_manual_broadcast_recipient_pairs(
    *,
    users_rows: list[dict],
    subs_rows: list[dict],
    send_to_all: bool,
    for_email: bool = False,
    for_sms: bool = False,
) -> list[tuple[str, dict]]:
    """
    Abonnements hebdo opt-in → ``(user_entity_id, fiche users)``.

    Exclut les abonnements sans fiche ``users``, sans e-mail valide (si ``for_email``),
    ou sans téléphone E.164 (si ``for_sms`` seul).
    """
    if not send_to_all:
        dry_users = [
            u
            for u in users_rows
            if str(u.get("source") or "").strip().lower() == "dry_run"
            and is_broadcast_email_ok(str(u.get("email") or ""))
        ]
        dry_users.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        u0 = dry_users[0] if dry_users else {}
        if not u0:
            return []
        uid0 = str(u0.get("entity_id") or "").strip() or "dry_run"
        return [(uid0, u0)]

    latest_sub = _weekly_subscriptions_by_uid(subs_rows)
    by_uid_user = _latest_users_by_uid(users_rows)
    out: list[tuple[str, dict]] = []

    for uid0, subr in latest_sub.items():
        if not _subscription_is_mailable(subr):
            continue
        urec = by_uid_user.get(uid0)
        if not urec:
            continue
        em = str(urec.get("email") or "").strip()
        ph = str(urec.get("phone_e164") or "").strip()
        has_em = is_broadcast_email_ok(em)
        has_ph = is_broadcast_phone_ok(ph)
        if for_email and for_sms:
            if not has_em and not has_ph:
                continue
        elif for_email and not has_em:
            continue
        elif for_sms and not has_ph:
            continue
        out.append((uid0, urec))
    return out


def lumenvia_manual_broadcast_users(
    *,
    users_rows: list[dict],
    subs_rows: list[dict],
    send_to_all: bool,
    for_email: bool = False,
    for_sms: bool = False,
) -> list[dict]:
    """Feuilles ``users`` + ``subscriptions`` → fiches utilisateurs pour l’expédition manuelle."""
    return [
        u
        for _, u in lumenvia_manual_broadcast_recipient_pairs(
            users_rows=users_rows,
            subs_rows=subs_rows,
            send_to_all=send_to_all,
            for_email=for_email,
            for_sms=for_sms,
        )
    ]


def count_skipped_weekly_broadcast_recipients(
    *,
    users_rows: list[dict],
    subs_rows: list[dict],
    for_email: bool = False,
    for_sms: bool = False,
) -> dict[str, int]:
    """Compte les abonnements hebdo opt-in exclus (aperçu / message admin)."""
    latest_sub = _weekly_subscriptions_by_uid(subs_rows)
    by_uid_user = _latest_users_by_uid(users_rows)
    stats = {"no_user": 0, "no_email": 0, "no_phone": 0, "eligible": 0}
    for uid0, subr in latest_sub.items():
        if not _subscription_is_mailable(subr):
            continue
        urec = by_uid_user.get(uid0)
        if not urec:
            stats["no_user"] += 1
            continue
        em = str(urec.get("email") or "").strip()
        ph = str(urec.get("phone_e164") or "").strip()
        has_em = is_broadcast_email_ok(em)
        has_ph = is_broadcast_phone_ok(ph)
        if for_email and for_sms:
            ok = has_em or has_ph
        elif for_email:
            ok = has_em
        elif for_sms:
            ok = has_ph
        else:
            ok = True
        if ok:
            stats["eligible"] += 1
        else:
            if for_email and not has_em:
                stats["no_email"] += 1
            if for_sms and not has_ph:
                stats["no_phone"] += 1
    return stats
