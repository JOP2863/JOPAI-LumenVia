#!/usr/bin/env python3
"""Repère et supprime les colonnes en double en ligne 1 (ex. deux ``zone`` sur PDFX)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config_from_secrets_toml  # noqa: E402
from core.sheets_db import (  # noqa: E402
    build_gspread_client,
    header_duplicate_columns,
    open_spreadsheet,
    repair_worksheet_duplicate_headers,
    _resolve_table_name,
)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Répare les en-têtes Sheets dupliqués (ligne 1).")
    p.add_argument(
        "--secrets",
        default=str(Path(".streamlit") / "secrets.toml"),
        help="Chemin secrets.toml",
    )
    p.add_argument(
        "--table",
        default="pdf_exports",
        help="Table logique (défaut: pdf_exports / PDFX)",
    )
    p.add_argument("--all-tables", action="store_true", help="Parcourt tous les onglets du fichier.")
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    if not cfg.gsheet_id or not cfg.gcp_service_account:
        print("Erreur: gsheet_id ou gcp_service_account manquant.", file=sys.stderr)
        return 2

    gc = build_gspread_client(cfg.gcp_service_account)
    sh = open_spreadsheet(gc, cfg.gsheet_id)

    worksheets = list(sh.worksheets()) if args.all_tables else [sh.worksheet(_resolve_table_name(sh=sh, table=args.table))]

    fixed_any = False
    for ws in worksheets:
        header = ws.row_values(1)
        dups = header_duplicate_columns(header)
        if not dups:
            print(f"OK  {ws.title}: pas de doublon")
            continue
        print(f"FIX {ws.title}: doublons {dups} — suppression des colonnes en trop…")
        repair_worksheet_duplicate_headers(ws)
        after = ws.row_values(1)
        still = header_duplicate_columns(after)
        if still:
            print(f"  Échec: doublons restants {still}", file=sys.stderr)
            return 3
        print(f"  En-tête après réparation: {' | '.join(after)}")
        fixed_any = True

    if not fixed_any:
        print("Rien à réparer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
