"""Lien public « Écouter » (PDF / exports) à partir d’une base d’URL connue."""

from __future__ import annotations


def public_app_listen_url(
    *, date_str: str, base_public_app_url: str | None, lang: str | None = None
) -> tuple[str | None, str | None]:
    """
    URL optionnelle pour le lien « Écouter » dans le PDF.
    Ajoute ``?sunday=YYYY-MM-DD`` ou ``&sunday=...`` selon la présence d’un query existant,
    puis ``&lang=`` si fourni (langue de consultation du destinataire).
    """
    base = (base_public_app_url or "").strip().rstrip("/")
    if not base:
        return None, None
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}sunday={date_str[:10]}"
    lg = str(lang or "").strip().upper()
    if lg:
        url += f"&lang={lg}"
    return url, None
