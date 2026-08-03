#!/usr/bin/env python3
"""
Corrige le schéma locale USR :
- supprime la colonne ``nationalite`` (on garde ``country``)
- force ``pref_langue`` en majuscules (ex. FR)
- aligne les codes langue de ``langues_pays`` (LGP) en majuscules

Usage ::
  python tools/fix_user_locale_country_pref.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.locale_codes import DEFAULT_PREF_LANGUE, DOMAINE_LANGUE, normalize_pref_langue
from core.sheets_db import (
    build_gspread_client,
    open_spreadsheet,
    _resolve_table_name,
)


def _col_letter(idx1: int) -> str:
    n = int(idx1)
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _batch_write(ws, updates: list[dict[str, str]], *, chunk: int = 80) -> None:
    for start in range(0, len(updates), chunk):
        batch = updates[start : start + chunk]
        if not batch:
            continue
        ws.batch_update(batch, value_input_option="RAW")
        if start + chunk < len(updates):
            time.sleep(1.1)


def fix_usr(*, gc, gsheet_id: str, dry_run: bool) -> None:
    sh = open_spreadsheet(gc, gsheet_id, service_account_email=None)
    ws_name = _resolve_table_name(sh=sh, table="users")
    ws = sh.worksheet(ws_name)
    header = [str(h or "").strip() for h in ws.row_values(1)]
    if "pref_langue" not in header:
        raise RuntimeError(f"Colonne pref_langue absente (header={header!r}).")

    idx_lang = header.index("pref_langue") + 1
    letter_lang = _col_letter(idx_lang)
    idx_nat = header.index("nationalite") + 1 if "nationalite" in header else 0

    all_vals = ws.get_all_values()
    updates: list[dict[str, str]] = []
    n_lang = 0
    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")
        cur = str(row[idx_lang - 1] if idx_lang - 1 < len(row) else "").strip()
        new = normalize_pref_langue(cur or DEFAULT_PREF_LANGUE)
        if new != cur:
            n_lang += 1
            updates.append({"range": f"{letter_lang}{i}", "values": [[new]]})

    print(f"USR pref_langue -> majuscules : {n_lang} cellule(s).")
    if not dry_run and updates:
        _batch_write(ws, updates)

    if idx_nat:
        print(f"USR : suppression colonne nationalite (index {idx_nat}).")
        if not dry_run:
            # gspread delete_columns is 1-based inclusive
            ws.delete_columns(idx_nat)
            print("USR : colonne nationalite supprimee.")
    else:
        print("USR : pas de colonne nationalite (deja absente).")


def fix_lgp_lang_codes(*, gc, gsheet_id: str, dry_run: bool) -> None:
    sh = open_spreadsheet(gc, gsheet_id, service_account_email=None)
    try:
        ws_name = _resolve_table_name(sh=sh, table="langues_pays")
        ws = sh.worksheet(ws_name)
    except Exception as ex:
        print(f"langues_pays : onglet inaccessible ({ex}) — ignore.")
        return

    header = [str(h or "").strip() for h in ws.row_values(1)]
    if "code" not in header or "domaine" not in header:
        print("langues_pays : colonnes code/domaine manquantes — ignore.")
        return
    idx_code = header.index("code") + 1
    idx_dom = header.index("domaine") + 1
    letter_code = _col_letter(idx_code)

    all_vals = ws.get_all_values()
    updates: list[dict[str, str]] = []
    n = 0
    for i, row in enumerate(all_vals[1:], start=2):
        while len(row) < len(header):
            row.append("")
        dom = str(row[idx_dom - 1] if idx_dom - 1 < len(row) else "").strip().lower()
        if dom != DOMAINE_LANGUE:
            continue
        cur = str(row[idx_code - 1] if idx_code - 1 < len(row) else "").strip()
        new = normalize_pref_langue(cur)
        if new and new != cur:
            n += 1
            updates.append({"range": f"{letter_code}{i}", "values": [[new]]})

    print(f"LGP codes langue -> majuscules : {n} cellule(s).")
    if not dry_run and updates:
        _batch_write(ws, updates)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Supprime nationalite ; pref_langue en majuscules.")
    p.add_argument("--gsheet-id", default=None)
    p.add_argument("--secrets", default=str(Path(".streamlit") / "secrets.toml"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    gsheet_id = (args.gsheet_id or cfg.gsheet_id or "").strip()
    if not gsheet_id or not cfg.gcp_service_account:
        print("Erreur: gsheet_id / gcp_service_account manquant.", file=sys.stderr)
        return 2

    gc = build_gspread_client(cfg.gcp_service_account)
    # Ordre important : majuscules pref_langue AVANT delete nationalite
    # (les indices de colonnes changent apres delete).
    fix_usr(gc=gc, gsheet_id=gsheet_id, dry_run=bool(args.dry_run))
    fix_lgp_lang_codes(gc=gc, gsheet_id=gsheet_id, dry_run=bool(args.dry_run))
    print("OK : country conserve, nationalite retire, pref_langue en majuscules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
