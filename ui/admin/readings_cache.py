"""Admin — Préchargement du cache lectures (AELF FR + Evangelizo DE/EN/ES/IT) vers Sheets."""

from __future__ import annotations

import re
from datetime import date, timedelta
from hashlib import sha256

import streamlit as st

from core.aelf import is_aelf_not_found_error
from core.aelf_text_cleanup import (
    aelf_text_still_has_lectionary_rubric,
    strip_aelf_lectionary_rubrics,
)
from core.config import load_config
from core.evangelizo import (
    EVANGELIZO_HORIZON_DAYS,
    EvangelizoHorizonError,
    evangelizo_horizon_bounds,
    is_within_evangelizo_horizon,
)
from core.liturgy_day import coerce_liturgy_pref_langue, fetch_liturgy_day, supported_liturgy_langs
from core.readings_cache_loader import (
    rdc_zone_for_pref_langue,
    readings_cache_row_from_texts,
)
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

_LANG_LABELS = {
    "FR": "FR — AELF (france)",
    "DE": "DE — Evangelizo",
    "EN": "EN — Evangelizo (AM)",
    "ES": "ES — Evangelizo (SP)",
    "IT": "IT — Evangelizo",
}


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
            from gspread.utils import rowcol_to_a1

            updates.append({"range": rowcol_to_a1(r_i, j + 1), "values": [[cleaned]]})
        if changed and concat_i is not None:
            row_map = {header[k]: cells[k] for k in range(len(header))}
            new_concat = compute_concat(row_map, header=header)
            from gspread.utils import rowcol_to_a1

            updates.append({"range": rowcol_to_a1(r_i, concat_i + 1), "values": [[new_concat]]})

    if not updates:
        return 0
    chunk = 80
    for i in range(0, len(updates), chunk):
        ws.batch_update(updates[i : i + chunk], value_input_option="RAW")
    return len(updates)


def _readings_cache_row(
    *,
    ds: str,
    zone: str,
    identity,
    texts,
    source: str,
) -> dict[str, str]:
    row = readings_cache_row_from_texts(
        ds=ds, zone=zone, identity=identity, texts=texts, source=source
    )
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


def _readings_row_is_source_unavailable(r: dict, *, zone: str, year: int) -> bool:
    """Date déjà tentée : source absente de façon permanente (ex. AELF 404).

    Ne pas y mettre les erreurs d’horizon Evangelizo (±30 j) : la date redevient
    récupérable quand elle entre dans la fenêtre.
    """
    if str(r.get("zone") or "").strip() != zone:
        return False
    if not sheet_row_status_is_live(r.get("status")):
        return False
    ds = str(r.get("date") or "").strip()
    if not ds.startswith(str(year)):
        return False
    err = str(r.get("error") or "")
    if not err.strip():
        return False
    # Horizon Evangelizo = temporaire → re-tenter quand la date entre dans ±30 j.
    if "30 day" in err.lower() or "horizon" in err.lower() or "wrong param" in err.lower():
        return False
    return is_aelf_not_found_error(Exception(err)) or ("404" in err and "aelf" in err.lower())


def render_admin_readings_cache() -> None:
    st.title("Cache lectures (Sheets)")
    st.caption(
        "Précharge les lectures dans `readings_cache` (RDC) **sans doublons**, "
        "comme pour le français : **FR = AELF** (`zone=france`) · "
        "**DE / EN / ES / IT = Evangelizo** (`zone=evangelizo_*`). "
        "La page Dimanche lit d’abord ce cache, puis l’API."
    )
    st.caption(
        f"**Evangelizo** : horizon Reader **±{EVANGELIZO_HORIZON_DAYS} jours** autour d’aujourd’hui "
        "(pas toute l’année). Hors fenêtre → pas d’appel API, pas de ligne d’échec écrite. "
        "Reviens chaque mois pour glisser la fenêtre."
    )
    st.caption(
        "Nettoyage auto (FR) : rubriques lectionnaire AELF (`OU LECTURE BREVE`, …) retirées à l’écriture."
    )
    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configure `gcp_service_account` et `gsheet_id` dans `.streamlit/secrets.toml`.")
        return

    langs = st.multiselect(
        "Langues à précharger",
        options=list(supported_liturgy_langs()),
        default=["FR"],
        format_func=lambda lg: _LANG_LABELS.get(lg, lg),
        key="adm_readings_cache_langs",
    )
    if not langs:
        st.warning("Choisis au moins une langue.")
        return

    today = date.today()
    e_lo, e_hi = evangelizo_horizon_bounds(today=today)
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
    non_fr = [lg for lg in langs if coerce_liturgy_pref_langue(lg) != "FR"]
    st.metric("Dimanches à vérifier", len(targets))
    st.caption(
        "Zones RDC : "
        + " · ".join(f"{lg}→`{rdc_zone_for_pref_langue(lg)}`" for lg in langs)
    )
    if non_fr:
        in_h = sum(1 for d in targets if is_within_evangelizo_horizon(d, today=today))
        st.info(
            f"Fenêtre Evangelizo aujourd’hui : **{e_lo.isoformat()} → {e_hi.isoformat()}** "
            f"(±{EVANGELIZO_HORIZON_DAYS} j) — **{in_h}/{len(targets)}** dimanche(s) de la sélection "
            f"dans la fenêtre pour {', '.join(non_fr)}."
        )

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

            existing = fetch_records(
                gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=6000
            )
            target_iso = {d.isoformat() for d in targets}
            rows: list[dict[str, str]] = []
            ok_count = 0
            err_count = 0
            unavailable_count = 0
            skipped_ok = 0
            skipped_horizon = 0
            errors_preview: list[str] = []

            for lg0 in langs:
                lg = coerce_liturgy_pref_langue(lg0)
                zone = rdc_zone_for_pref_langue(lg)
                source_tag = "aelf_api_prefetch" if lg == "FR" else f"evangelizo_prefetch_{lg}"
                existing_dates = {
                    str(r.get("date") or "").strip()
                    for r in existing
                    if _readings_row_is_usable(r, zone=zone, year=int(year))
                }
                unavailable_dates = {
                    str(r.get("date") or "").strip()
                    for r in existing
                    if _readings_row_is_source_unavailable(r, zone=zone, year=int(year))
                }
                candidates = [
                    d
                    for d in targets
                    if d.isoformat() not in existing_dates and d.isoformat() not in unavailable_dates
                ]
                if lg == "FR":
                    to_fetch = candidates
                    horizon_skip_n = 0
                else:
                    to_fetch = [
                        d for d in candidates if is_within_evangelizo_horizon(d, today=today)
                    ]
                    horizon_skip_n = len(candidates) - len(to_fetch)
                    skipped_horizon += horizon_skip_n
                skipped_ok += len(existing_dates & target_iso)
                st.write(
                    f"**{lg}** (`{zone}`) — déjà OK : **{len(existing_dates & target_iso)}** · "
                    f"indispo. : **{len(unavailable_dates & target_iso)}** · "
                    + (
                        f"hors horizon Evangelizo : **{horizon_skip_n}** · "
                        if lg != "FR"
                        else ""
                    )
                    + f"à récupérer : **{len(to_fetch)}**."
                )
                for d in to_fetch:
                    ds = d.isoformat()
                    try:
                        identity, texts, source_id = fetch_liturgy_day(ds, pref_langue=lg)
                        z_write = str(getattr(identity, "zone", None) or zone)
                        row = _readings_cache_row(
                            ds=ds,
                            zone=z_write,
                            identity=identity,
                            texts=texts,
                            source=source_tag if lg == "FR" else f"{source_id}_prefetch",
                        )
                        if lg == "FR":
                            dirty = [
                                k
                                for k in _READING_BODY_COLS
                                if aelf_text_still_has_lectionary_rubric(row.get(k))
                            ]
                            if dirty:
                                raise RuntimeError(
                                    f"Rubrique AELF non retirée après nettoyage ({', '.join(dirty)})"
                                )
                        rows.append(row)
                        ok_count += 1
                    except EvangelizoHorizonError as ex:
                        # Ne pas polluer RDC : la date sortira / entrera dans la fenêtre plus tard.
                        skipped_horizon += 1
                        if len(errors_preview) < 12:
                            errors_preview.append(f"{lg} {ds} : hors horizon — {str(ex)[:140]}")
                    except Exception as ex:
                        if lg == "FR" and is_aelf_not_found_error(ex):
                            unavailable_count += 1
                            if len(errors_preview) < 12:
                                errors_preview.append(f"{lg} {ds} : AELF 404 (non publié)")
                            continue
                        err_count += 1
                        msg = str(ex)[:900]
                        if len(errors_preview) < 12:
                            errors_preview.append(f"{lg} {ds} : {msg[:180]}")
                        # Échecs non-horizon : journaliser en RDC pour audit (pas pour FR 404).
                        rows.append(
                            {
                                "entity_id": sha256(
                                    f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")
                                ).hexdigest()[:24],
                                "date": ds,
                                "zone": zone,
                                "source": source_tag,
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
                invalidate_fetch_records_cache(spreadsheet_id=cfg.gsheet_id, table="readings_cache")
                invalidate_adm_sheets_fetch_cache()
            st.success(
                f"Préchargement terminé : **{added}** ligne(s) ajoutée(s) "
                f"({ok_count} succès, {unavailable_count} indisponible(s), "
                f"{err_count} échec(s), {skipped_ok} déjà OK, "
                f"{skipped_horizon} hors horizon Evangelizo)."
            )
            if errors_preview:
                st.warning("Dates non récupérées :")
                for line in errors_preview:
                    st.caption(line)
            if skipped_horizon:
                st.info(
                    f"Evangelizo ne livre que ±{EVANGELIZO_HORIZON_DAYS} j autour d’aujourd’hui "
                    f"({e_lo.isoformat()} → {e_hi.isoformat()}). "
                    "Précharge à nouveau chaque mois pour glisser la fenêtre ; "
                    "les lignes RDC déjà remplies restent valides."
                )
            if err_count:
                st.info("Les échecs (hors 404 AELF / hors horizon) seront re-tentés au prochain préchargement.")
        finally:
            ov.empty()
