from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import default_tables, ensure_database, build_gspread_client


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Initialise la base Google Sheets (onglets + headers).")
    p.add_argument("--gsheet-id", default=None, help="Override gsheet_id (sinon lit .streamlit/secrets.toml)")
    p.add_argument(
        "--secrets",
        default=str(Path(".streamlit") / "secrets.toml"),
        help="Chemin vers secrets.toml (défaut: .streamlit/secrets.toml)",
    )
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    gsheet_id = (args.gsheet_id or cfg.gsheet_id).strip()
    if not gsheet_id:
        print("Erreur: gsheet_id manquant (secrets.toml ou --gsheet-id).", file=sys.stderr)
        return 2

    if not cfg.gcp_service_account:
        print("Erreur: gcp_service_account manquant dans secrets.toml.", file=sys.stderr)
        return 3

    gc = build_gspread_client(cfg.gcp_service_account)
    sa_email = str(cfg.gcp_service_account.get("client_email", "")).strip()

    try:
        ensure_database(gspread_client=gc, spreadsheet_id=gsheet_id, tables=default_tables())
    except PermissionError:
        print("\nERREUR: accès refusé (403) au Google Sheet.\n", file=sys.stderr)
        print("Actions à faire (dans l’ordre) :", file=sys.stderr)
        if sa_email:
            print(
                f"1) Ouvre le Google Sheet et partage-le avec le compte de service : {sa_email} (au moins Éditeur).",
                file=sys.stderr,
            )
        else:
            print(
                "1) Ouvre le Google Sheet et partage-le avec le compte de service (client_email dans secrets.toml).",
                file=sys.stderr,
            )
        print(
            "2) Vérifie que l’ID du fichier est correct (dans l’URL : /spreadsheets/d/<ID>/edit).",
            file=sys.stderr,
        )
        print(
            "3) Dans GCP (projet vernal-day-484816-n4), active si besoin : Google Sheets API + Google Drive API.",
            file=sys.stderr,
        )
        print(
            "4) Relance : python .\\tools\\init_sheets_db.py",
            file=sys.stderr,
        )
        return 4

    print("OK: base Google Sheets initialisée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

