"""Helpers purs pour les lignes d’abonnement (Sheets), sans Streamlit."""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256

from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue


def latest_subscription_record(subs: list[dict], user_entity_id: str, sub_type: str) -> dict | None:
    rows = [
        s
        for s in subs
        if str(s.get("user_entity_id", "")).strip() == user_entity_id and str(s.get("type", "")).strip() == sub_type
    ]
    if not rows:
        return None
    return sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]


def subscription_is_active(sub: dict | None) -> bool:
    if not sub:
        return False
    return str(sub.get("active", "")).strip().lower() in ("true", "1", "oui", "yes", "active")


def weekly_subscription_langs(
    subs: list[dict],
    user_entity_id: str,
    *,
    default_lang: object | None = None,
) -> list[str]:
    """Langues hebdo actives d'un utilisateur, une ligne ``subscriptions`` par langue."""
    uid = str(user_entity_id or "").strip()
    fallback_lang = normalize_pref_langue(default_lang or DEFAULT_PREF_LANGUE)
    latest_by_lang: dict[str, dict] = {}
    legacy_rows: list[dict] = []
    for s in subs:
        if str(s.get("user_entity_id") or "").strip() != uid:
            continue
        if str(s.get("type") or "").strip() != "weekly_friday":
            continue
        st = str(s.get("status") or "").strip().lower()
        if st.startswith("inactif") or st.startswith("inactive"):
            continue
        raw_lg = str(s.get("pref_langue") or "").strip()
        if raw_lg:
            lg = normalize_pref_langue(raw_lg)
            prev = latest_by_lang.get(lg)
            if not prev or str(s.get("created_at") or "") > str(prev.get("created_at") or ""):
                latest_by_lang[lg] = s
        else:
            legacy_rows.append(s)
    if legacy_rows:
        legacy_latest = sorted(
            legacy_rows,
            key=lambda r: str(r.get("created_at") or ""),
            reverse=True,
        )[0]
        lg = fallback_lang
        prev = latest_by_lang.get(lg)
        if not prev or str(legacy_latest.get("created_at") or "") > str(prev.get("created_at") or ""):
            latest_by_lang[lg] = legacy_latest
    out = [
        lg
        for lg, row in latest_by_lang.items()
        if str((row or {}).get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes")
        and subscription_is_active(row)
    ]
    return sorted(set(out))


def hashed_email_user_entity_id(email: object) -> str:
    em = str(email or "").strip().lower()
    if not em:
        return ""
    return sha256(em.encode("utf-8")).hexdigest()[:24]


def entity_ids_for_email(
    users_rows: list[dict] | None,
    email: object,
    *,
    extra_ids: Iterable[str] | None = None,
) -> list[str]:
    """Tous les ``entity_id`` déjà utilisés pour un e-mail (fiches live et historiques)."""
    em = str(email or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()

    def _add(uid: object) -> None:
        u = str(uid or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    hashed = hashed_email_user_entity_id(em)
    _add(hashed)
    for uid in extra_ids or []:
        _add(uid)
    for row in users_rows or []:
        if str(row.get("email") or "").strip().lower() != em:
            continue
        _add(row.get("entity_id"))
    return out


def weekly_subscription_langs_for_user(
    subs: list[dict],
    user: dict | None,
    *,
    users_rows: list[dict] | None = None,
) -> list[str]:
    """Langues hebdo actives, en fusionnant toutes les fiches du même e-mail."""
    u = user or {}
    account_lang = normalize_pref_langue(u.get("pref_langue") or DEFAULT_PREF_LANGUE)
    uids = entity_ids_for_email(
        users_rows,
        u.get("email"),
        extra_ids=[str(u.get("entity_id") or "").strip()],
    )
    found: list[str] = []
    for uid in uids:
        found.extend(weekly_subscription_langs(subs, uid, default_lang=account_lang))
    return list(dict.fromkeys(found))


def ordered_account_weekly_langs(account_lang: object, weekly_langs: list[str]) -> list[str]:
    """Langue compte en premier, puis les autres langues newsletter actives."""
    primary = normalize_pref_langue(account_lang or DEFAULT_PREF_LANGUE)
    langs = [normalize_pref_langue(x) for x in (weekly_langs or []) if str(x or "").strip()]
    if not langs:
        return [primary] if primary else []
    if primary in langs:
        return [primary] + [x for x in langs if x != primary]
    return langs


def cap_weekly_langs(
    langs: list[object],
    *,
    account_lang: object | None = None,
    max_count: int = 2,
) -> list[str]:
    """Ordre stable + plafond pour les multiselect Streamlit (max 2 langues)."""
    normalized = [
        normalize_pref_langue(x)
        for x in (langs or [])
        if str(x or "").strip()
    ]
    normalized = list(dict.fromkeys(normalized))
    ordered = (
        ordered_account_weekly_langs(account_lang or DEFAULT_PREF_LANGUE, normalized)
        if account_lang is not None
        else normalized
    )
    return ordered[: max(0, int(max_count))]
