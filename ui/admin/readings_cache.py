"""Admin — Préchargement du cache lectures AELF vers Sheets."""

from __future__ import annotations

import re
from datetime import date, timedelta
from hashlib import sha256

import streamlit as st

from core.aelf import fetch_aelf_day
from core.config import load_config
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_rows_bulk,
    build_gspread_client,
    ensure_table,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from ui.components import loading_overlay


def _normalize_aelf_text_for_cache(s: str | None) -> str:
    raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _readings_row_is_usable(r: dict, *, zone: str, year: int) -> bool:
    if str(r.get("zone") or "").strip() != zone:
        return False
    if not sheet_row_status_is_live(r.get("status")):
        return False
    if str(r.get("error") or "").strip():
        return False
    ds = str(r.get("date") or "").strip()
    if not ds.startswith(str(year)):
        return False
    return any(str(r.get(k) or "").strip() for k in ("premiere_lecture", "psaume", "evangile"))


def _readings_cache_row_from_aelf(*, ds: str, zone: str, identity, texts) -> dict[str, str]:
    return {
        "entity_id": sha256(f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
        "date": ds,
        "zone": zone,
        "periode": getattr(identity, "periode", None) or "",
        "semaine": getattr(identity, "semaine", None) or "",
        "annee": getattr(identity, "annee", None) or "",
        "couleur": getattr(identity, "couleur", None) or "",
        "fete": getattr(identity, "fete", None) or "",
        "jour_liturgique_nom": getattr(identity, "jour_liturgique_nom", None) or "",
        "premiere_lecture": _normalize_aelf_text_for_cache(getattr(texts, "premiere_lecture", None)),
        "psaume": _normalize_aelf_text_for_cache(getattr(texts, "psaume", None)),
        "deuxieme_lecture": _normalize_aelf_text_for_cache(getattr(texts, "deuxieme_lecture", None)),
        "evangile": _normalize_aelf_text_for_cache(getattr(texts, "evangile", None)),
        "source": "aelf_api_prefetch",
        "error": "",
    }


def render_admin_readings_cache() -> None:
    st.title("Cache lectures (AELF → Sheets)")
    st.caption(
        "Cette page permet de précharger les lectures liturgiques (AELF) dans la table `readings_cache`, "
        "sans doublons. Utile pour accélérer l’usage et stabiliser le rendu (web/PDF)."
    )
    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configure `gcp_service_account` et `gsheet_id` dans `.streamlit/secrets.toml`.")
        return

    zone = "france"
    today = date.today()
    year = st.number_input("Année", min_value=2020, max_value=2100, value=int(today.year), step=1)
    month = st.selectbox(
        "Mois (optionnel)",
        options=[("all", "Toute l’année")] + [(f"{i:02d}", f"{i:02d}") for i in range(1, 13)],
        format_func=lambda x: x[1],
        index=0,
        key="adm_readings_cache_month",
    )[0]

    def _sundays_in_year(y: int) -> list[date]:
        d = date(int(y), 1, 1)
        days_to_sun = (6 - d.weekday()) % 7
        d = d + timedelta(days=days_to_sun)
        out: list[date] = []
        while d.year == int(y):
            out.append(d)
            d = d + timedelta(days=7)
        return out

    def _sundays_in_month(y: int, m: int) -> list[date]:
        return [d for d in _sundays_in_year(y) if d.month == int(m)]

    targets = _sundays_in_year(year) if month == "all" else _sundays_in_month(year, int(month))
    st.metric("Dimanches à vérifier", len(targets))

    if st.button("Précharger dans `readings_cache`", type="primary", key="adm_readings_cache_run"):
        ov = loading_overlay("Préchargement des lectures…")
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=TableSpec(
                    name="readings_cache",
                    columns=with_concat(
                        [
                            *BASE_COLUMNS,
                            "date",
                            "zone",
                            "periode",
                            "semaine",
                            "annee",
                            "couleur",
                            "fete",
                            "jour_liturgique_nom",
                            "premiere_lecture",
                            "psaume",
                            "deuxieme_lecture",
                            "evangile",
                            "source",
                            "error",
                        ]
                    ),
                ),
            )

            existing = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=6000)
            existing_dates = {
                str(r.get("date") or "").strip()
                for r in existing
                if _readings_row_is_usable(r, zone=zone, year=int(year))
            }

            to_fetch = [d for d in targets if d.isoformat() not in existing_dates]
            skipped = len(targets) - len(to_fetch)
            st.write(f"Déjà en base (lectures OK) : **{skipped}** · À récupérer : **{len(to_fetch)}** dimanche(s).")
            if not to_fetch:
                st.success("Rien à faire : tout est déjà en base pour cette sélection.")
                return

            rows: list[dict[str, str]] = []
            ok_count = 0
            err_count = 0
            errors_preview: list[str] = []

            for d in to_fetch:
                ds = d.isoformat()
                try:
                    identity, texts = fetch_aelf_day(ds, zone=zone)
                    rows.append(_readings_cache_row_from_aelf(ds=ds, zone=zone, identity=identity, texts=texts))
                    ok_count += 1
                except Exception as ex:
                    err_count += 1
                    msg = str(ex)[:900]
                    if len(errors_preview) < 5:
                        errors_preview.append(f"{ds} : {msg[:200]}")
                    rows.append(
                        {
                            "entity_id": sha256(f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                            "date": ds,
                            "zone": zone,
                            "source": "aelf_api_prefetch",
                            "error": msg,
                        }
                    )

            added = append_immutable_rows_bulk(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="readings_cache",
                values_by_col_list=rows,
                chunk_size=120,
            )
            st.success(
                f"Préchargement terminé : **{added}** ligne(s) ajoutée(s) "
                f"({ok_count} succès, {err_count} échec(s))."
            )
            if errors_preview:
                st.warning("Aperçu des erreurs :")
                for line in errors_preview:
                    st.caption(line)
            if err_count:
                st.info(
                    "Les dimanches en échec (lignes avec `error` rempli ou sans lectures) "
                    "seront re-tentés au prochain préchargement."
                )
        finally:
            ov.empty()
