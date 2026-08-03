#!/usr/bin/env python3
"""
Ajoute ``pref_langue`` sur USR (si besoin), crée ``langues_pays`` (LGP),
ensemence les codes ISO, initialise ``pref_langue=FR`` (majuscules).

La nationalité reste dans ``country`` (pas de colonne ``nationalite``).

Usage (racine du dépôt) ::
  python tools/add_user_locale_columns.py
"""

from __future__ import annotations

import argparse
import sys
import time
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.locale_codes import (
    DEFAULT_PREF_LANGUE,
    DOMAINE_LANGUE,
    DOMAINE_PAYS,
    SEED_LANGUES,
    SEED_PAYS,
    normalize_pref_langue,
)
from core.sheets_db import (
    SHEETS_ROW_STATUS_ACTIVE,
    append_immutable_rows_bulk,
    build_gspread_client,
    ensure_table,
    fetch_records,
    get_table_spec,
    langues_pays_table_spec,
    open_spreadsheet,
    utc_now_iso,
    _resolve_table_name,
)


def _migrate_alias_tables_and_rename(*, gc, gsheet_id: str) -> None:
    import importlib.util

    path = REPO_ROOT / "tools" / "init_sheets_db.py"
    spec = importlib.util.spec_from_file_location("lumenvia_init_sheets_db", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossible de charger {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.migrate_alias_tables_and_rename(gc=gc, gsheet_id=gsheet_id)


def _col_letter(idx1: int) -> str:
    n = int(idx1)
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _seed_langues_pays(*, gc, gsheet_id: str, dry_run: bool) -> int:
    if dry_run:
        print(
            f"[dry-run] langues_pays : jusqu'a {len(SEED_LANGUES) + len(SEED_PAYS)} "
            "ligne(s) d'ensemencement (si absentes)."
        )
        return 0
    existing = fetch_records(
        gspread_client=gc,
        spreadsheet_id=gsheet_id,
        table="langues_pays",
        limit=0,
        use_cache=False,
    )
    have: set[tuple[str, str]] = set()
    for r in existing:
        dom = str(r.get("domaine") or "").strip().lower()
        code = str(r.get("code") or "").strip()
        if dom == DOMAINE_LANGUE:
            code = normalize_pref_langue(code)
        else:
            code = code.upper()
        if dom and code:
            have.add((dom, code))

    to_add: list[dict[str, str]] = []
    now = utc_now_iso()
    for code, libelle, bcp47 in SEED_LANGUES:
        key = (DOMAINE_LANGUE, code)
        if key in have:
            continue
        ent = sha256(f"lgp|langue|{code}|{now}".encode("utf-8")).hexdigest()[:24]
        to_add.append(
            {
                "entity_id": ent,
                "code": code,
                "domaine": DOMAINE_LANGUE,
                "libelle": libelle,
                "norme": "ISO-639-1",
                "bcp47": bcp47,
                "status": SHEETS_ROW_STATUS_ACTIVE,
                "version": 1,
            }
        )
        have.add(key)

    for code, libelle in SEED_PAYS:
        key = (DOMAINE_PAYS, code)
        if key in have:
            continue
        ent = sha256(f"lgp|pays|{code}|{now}".encode("utf-8")).hexdigest()[:24]
        to_add.append(
            {
                "entity_id": ent,
                "code": code,
                "domaine": DOMAINE_PAYS,
                "libelle": libelle,
                "norme": "ISO-3166-1",
                "bcp47": "",
                "status": SHEETS_ROW_STATUS_ACTIVE,
                "version": 1,
            }
        )
        have.add(key)

    if not to_add:
        print("langues_pays : deja ensemence (rien a ajouter).")
        return 0
    n = append_immutable_rows_bulk(
        gspread_client=gc,
        spreadsheet_id=gsheet_id,
        table="langues_pays",
        values_by_col_list=to_add,
        chunk_size=80,
    )
    print(f"langues_pays : {n} ligne(s) ajoutee(s).")
    return int(n or 0)


def _ensure_users_pref_langue(*, gc, gsheet_id: str, dry_run: bool) -> int:
    if dry_run:
        print(f"[dry-run] USR : ensure pref_langue={DEFAULT_PREF_LANGUE} (concat non recalcule).")
        return 0
    ensure_table(gspread_client=gc, spreadsheet_id=gsheet_id, table=get_table_spec("users"))

    sh = open_spreadsheet(gc, gsheet_id, service_account_email=None)
    ws_name = _resolve_table_name(sh=sh, table="users")
    ws = sh.worksheet(ws_name)
    header = [str(h or "").strip() for h in ws.row_values(1)]
    if "pref_langue" not in header:
        raise RuntimeError(f"Colonne pref_langue absente apres ensure_table (header={header!r}).")
    # Ne pas recreer nationalite si elle a ete retiree.
    idx_lang = header.index("pref_langue") + 1
    letter_lang = _col_letter(idx_lang)

    all_vals = ws.get_all_values()
    if len(all_vals) <= 1:
        print("USR : aucune ligne utilisateur.")
        return 0

    updates: list[dict[str, str]] = []
    filled = 0
    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")
        cur_lang = str(row[idx_lang - 1] if idx_lang - 1 < len(row) else "").strip()
        new_lang = normalize_pref_langue(cur_lang or DEFAULT_PREF_LANGUE)
        if new_lang != cur_lang:
            filled += 1
            updates.append({"range": f"{letter_lang}{i}", "values": [[new_lang]]})

    print(f"USR : {filled} ligne(s) a initialiser/normaliser (pref_langue={DEFAULT_PREF_LANGUE}).")
    if not updates:
        return 0

    chunk = 80
    for start in range(0, len(updates), chunk):
        batch = updates[start : start + chunk]
        ws.batch_update(batch, value_input_option="RAW")
        if start + chunk < len(updates):
            time.sleep(1.2)
    print(f"USR : {filled} ligne(s) mises a jour (concat non recalcule).")
    return filled


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Colonne pref_langue + table langues_pays (LGP).")
    p.add_argument("--gsheet-id", default=None)
    p.add_argument("--secrets", default=str(Path(".streamlit") / "secrets.toml"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    gsheet_id = (args.gsheet_id or cfg.gsheet_id or "").strip()
    if not gsheet_id:
        print("Erreur: gsheet_id manquant.", file=sys.stderr)
        return 2
    if not cfg.gcp_service_account:
        print("Erreur: gcp_service_account manquant dans secrets.toml.", file=sys.stderr)
        return 3

    gc = build_gspread_client(cfg.gcp_service_account)
    try:
        if not args.dry_run:
            ensure_table(
                gspread_client=gc,
                spreadsheet_id=gsheet_id,
                table=langues_pays_table_spec(),
            )
            _migrate_alias_tables_and_rename(gc=gc, gsheet_id=gsheet_id)
        else:
            print("[dry-run] ensure_table langues_pays + AliasTables ignores.")

        _seed_langues_pays(gc=gc, gsheet_id=gsheet_id, dry_run=bool(args.dry_run))
        _ensure_users_pref_langue(gc=gc, gsheet_id=gsheet_id, dry_run=bool(args.dry_run))
    except PermissionError:
        sa_email = str(cfg.gcp_service_account.get("client_email", "") or "").strip()
        print("\nERREUR: acces refuse au Google Sheet.\n", file=sys.stderr)
        if sa_email:
            print(f"Partage le fichier avec : {sa_email} (Editeur).", file=sys.stderr)
        return 4

    print(
        "OK : USR.pref_langue (majuscules) + table langues_pays (LGP). "
        "Nationalite = colonne country."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
