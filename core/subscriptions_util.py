"""Helpers purs pour les lignes d’abonnement (Sheets), sans Streamlit."""

from __future__ import annotations

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
    for s in subs:
        if str(s.get("user_entity_id") or "").strip() != uid:
            continue
        if str(s.get("type") or "").strip() != "weekly_friday":
            continue
        lg = normalize_pref_langue(s.get("pref_langue") or fallback_lang)
        prev = latest_by_lang.get(lg)
        if not prev or str(s.get("created_at") or "") > str(prev.get("created_at") or ""):
            latest_by_lang[lg] = s
    out = [
        lg
        for lg, row in latest_by_lang.items()
        if str((row or {}).get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes")
        and subscription_is_active(row)
    ]
    return sorted(set(out))
