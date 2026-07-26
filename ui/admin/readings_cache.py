"""Admin — Préchargement du cache lectures AELF vers Sheets."""

from __future__ import annotations

import re
from datetime import date, timedelta
from hashlib import sha256

import streamlit as st

from core.aelf import fetch_aelf_day, is_aelf_not_found_error
from core.aelf_text_cleanup import (
    aelf_text_still_has_lectionary_rubric,
    strip_aelf_lectionary_rubrics,
)
from core.config import load_config
from core.readings_cache_loader import readings_cache_row_from_aelf_texts
from core.sheets_db import (
    append_immutable_rows_bulk,
    build_gspread_client,
    compute_concat,
    ensure_table,
    fetch_records,
    get_table_spec,
    invalidate_fetch_records_cache,
    open_spreadsheet,
    sheet_row_status_is_live,
    utc_now_iso,
    _resolve_table_name,
)
from ui.components import loading_overlay
from ui.streamlit_caches import invalidate_adm_sheets_fetch_cache

_READING_BODY_COLS = (
    "premiere_lecture",
    "psaume",
    "deuxieme_lecture",
    "evangile",
)


def _scrub_readings_row_rubrics(row: dict[str, str]) -> tuple[dict[str, str], int]:
    """Filet final avant écriture Sheets : retire toute rubrique encore présente."""
    out = dict(row)
    n = 0
    for k in _READING_BODY_COLS:
        raw = str(out.get(k) or "")
        if not raw:
            continue
        cleaned = strip_aelf_lectionary_rubrics(raw)
        if k != "psaume":
            cleaned = strip_aelf_lectionary_rubrics(re.sub(r"\s+", " ", cleaned).strip())
        if cleaned != raw:
            n += 1
        out[k] = cleaned
    return out, n


def _scrub_rdc_rubrics_in_place(*, gs: object, spreadsheet_id: str) -> int:
    """
    Nettoie en place les colonnes lectures + concat encore polluées (ancien build).
    Retourne le nombre de cellules modifiées.
    """
    sh = open_spreadsheet(gs, spreadsheet_id)
    ws = sh.worksheet(_resolve_table_name(sh=sh, table="readings_cache"))
    values = ws.get_all_values()
    if not values:
        return 0
    header = [str(c or "").strip() for c in values[0]]
    col_idx = {h: i for i, h in enumerate(header) if h}
    body_cols = [c for c in _READING_BODY_COLS if c in col_idx]
    concat_i = col_idx.get("concat")
    if not body_cols:
        return 0

    updates: list[dict] = []
    for r_i, row_vals in enumerate(values[1:], start=2):
        changed = False
        # Pad row to header length
        cells = list(row_vals) + [""] * max(0, len(header) - len(row_vals))
        for name in body_cols:
            j = col_idx[name]
            raw = str(cells[j] if j < len(cells) else "")
            if not aelf_text_still_has_lectionary_rubric(raw):
                continue
            cleaned = strip_aelf_lectionary_rubrics(raw)
            if name != "psaume":
                cleaned = strip_aelf_lectionary_rubrics(re.sub(r"\s+", " ", cleaned).strip())
            if cleaned == raw:
                continue
            cells[j] = cleaned
            changed = True
            # A1 notation
            from gspread.utils import rowcol_to_a1

            updates.append({"range": rowcol_to_a1(r_i, j + 1), "values": [[cleaned]]})
        if changed and concat_i is not None:
            row_map = {header[k]: cells[k] for k in range(len(header))}
            new_concat = compute_concat(row_map, header=header)
            from gspread.utils import rowcol_to_a1

            updates.append({"range": rowcol_to_a1(r_i, concat_i + 1), "values": [[new_concat]]})

    if not updates:
        return 0
    # Batch par paquets pour éviter les 429.
    chunk = 80
    for i in range(0, len(updates), chunk):
        ws.batch_update(updates[i : i + chunk], value_input_option="RAW")
    return len(updates)


def _readings_cache_row_from_aelf(*, ds: str, zone: str, identity, texts) -> dict[str, str]:
    row = readings_cache_row_from_aelf_texts(ds=ds, zone=zone, identity=identity, texts=texts)
    row, _n = _scrub_readings_row_rubrics(row)
    row["entity_id"] = sha256(f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
    return row


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


def _readings_row_is_aelf_unavailable(r: dict, *, zone: str, year: int) -> bool:
    """Date déjà tentée : l'API AELF renvoie 404 (calendrier pas encore publié)."""
    if str(r.get("zone") or "").strip() != zone:
        return False
    if not sheet_row_status_is_live(r.get("status")):
        return False
    ds = str(r.get("date") or "").strip()
    if not ds.startswith(str(year)):
        return False
    return is_aelf_not_found_error(Exception(str(r.get("error") or "")))


def render_admin_readings_cache() -> None:
    st.title("Cache lectures (AELF → Sheets)")
    st.caption(
        "Cette page permet de précharger les lectures liturgiques (AELF) dans la table `readings_cache`, "
        "sans doublons. Utile pour accélérer l’usage et stabiliser le rendu (web/PDF)."
    )
    st.caption(
        "Nettoyage auto : rubriques lectionnaire AELF (`OU LECTURE BREVE`, `OU BIEN`, …) retirées à l’écriture."
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

    if st.button(
        "Purger `OU LECTURE BREVE` dans RDC (lignes existantes)",
        key="adm_readings_cache_scrub_rubrics",
        help="Met à jour en place les cellules encore polluées — utile si un ancien build a réécrit le cache.",
    ):
        ov = loading_overlay("Purge des rubriques AELF dans readings_cache…")
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            n = _scrub_rdc_rubrics_in_place(gs=gs, spreadsheet_id=cfg.gsheet_id)
            invalidate_fetch_records_cache(spreadsheet_id=cfg.gsheet_id, table="readings_cache")
            invalidate_adm_sheets_fetch_cache()
            st.success(f"Purge terminée : **{n}** cellule(s) nettoyée(s).")
        except Exception as ex:
            st.error(f"Purge impossible : {ex}")
        finally:
            ov.empty()

    if st.button("Précharger dans `readings_cache`", type="primary", key="adm_readings_cache_run"):
        ov = loading_overlay("Préchargement des lectures…")
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=get_table_spec("readings_cache"),
            )

            existing = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=6000)
            existing_dates = {
                str(r.get("date") or "").strip()
                for r in existing
                if _readings_row_is_usable(r, zone=zone, year=int(year))
            }
            unavailable_dates = {
                str(r.get("date") or "").strip()
                for r in existing
                if _readings_row_is_aelf_unavailable(r, zone=zone, year=int(year))
            }

            to_fetch = [
                d
                for d in targets
                if d.isoformat() not in existing_dates and d.isoformat() not in unavailable_dates
            ]
            target_iso = {d.isoformat() for d in targets}
            st.write(
                f"Déjà en base (lectures OK) : **{len(existing_dates & target_iso)}** · "
                f"Indisponibles AELF (404) : **{len(unavailable_dates & target_iso)}** · "
                f"À récupérer : **{len(to_fetch)}** dimanche(s)."
            )
            if unavailable_dates & {d.isoformat() for d in targets}:
                st.caption(
                    "Les dates en 404 ne sont pas re-tentées tant que l'API AELF ne les publie pas "
                    "(ex. fin d'année liturgique pas encore en ligne)."
                )
            if not to_fetch:
                st.success("Rien à faire : tout est déjà en base pour cette sélection.")
                return

            rows: list[dict[str, str]] = []
            ok_count = 0
            err_count = 0
            unavailable_count = 0
            errors_preview: list[str] = []

            for d in to_fetch:
                ds = d.isoformat()
                try:
                    identity, texts = fetch_aelf_day(ds, zone=zone)
                    row = _readings_cache_row_from_aelf(ds=ds, zone=zone, identity=identity, texts=texts)
                    dirty = [
                        k
                        for k in _READING_BODY_COLS
                        if aelf_text_still_has_lectionary_rubric(row.get(k))
                    ]
                    if dirty:
                        raise RuntimeError(
                            f"Rubrique AELF non retirée après nettoyage ({', '.join(dirty)}) — "
                            "vérifie le déploiement du module core/aelf_text_cleanup.py"
                        )
                    rows.append(row)
                    ok_count += 1
                except Exception as ex:
                    if is_aelf_not_found_error(ex):
                        unavailable_count += 1
                        if len(errors_preview) < 8:
                            errors_preview.append(
                                f"{ds} : non publié par l'API AELF (404) — réessayez plus tard"
                            )
                        continue
                    err_count += 1
                    msg = str(ex)[:900]
                    if len(errors_preview) < 8:
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

            added = 0
            if rows:
                added = append_immutable_rows_bulk(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="readings_cache",
                    values_by_col_list=rows,
                    chunk_size=120,
                )
            st.success(
                f"Préchargement terminé : **{added}** ligne(s) ajoutée(s) "
                f"({ok_count} succès, {unavailable_count} indisponible(s) AELF, {err_count} échec(s))."
            )
            if errors_preview:
                st.warning("Dates non récupérées :")
                for line in errors_preview:
                    st.caption(line)
            if err_count:
                st.info(
                    "Les autres échecs (hors 404) seront re-tentés au prochain préchargement."
                )
        finally:
            ov.empty()
