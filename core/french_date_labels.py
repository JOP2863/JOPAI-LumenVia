"""Formats de dates en français pour l’UI et les légendes."""

from __future__ import annotations

from datetime import date, datetime


def fmt_cached_at_human(iso_s: str) -> str:
    s = (iso_s or "").strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
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
        return f"{dt.day} {mois[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d} UTC"
    except Exception:
        return s[:19]


def offline_cache_caption(cached_at: str) -> str:
    return f"Consultation hors-ligne (mise en cache le {fmt_cached_at_human(cached_at)})."


def french_long_date_label(date_str: str) -> str:
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
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
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    return f"{jours[d.weekday()].capitalize()} {d.day} {mois[d.month - 1]} {d.year}"


def french_day_month_year(date_str: str) -> str:
    """Date courte : jour + mois + année (sans jour de semaine)."""
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
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


def french_weekday_day_month_year(date_str: str) -> str:
    """Pour une phrase comme « … la célébration du dimanche 10 mai 2026 »."""
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
    jours_sem = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    return f"{jours_sem[d.weekday()]} {french_day_month_year(date_str)}"
