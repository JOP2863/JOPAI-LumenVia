"""Audit AliasTables ↔ onglets physiques ↔ registre dépôt (noms logiques / acronymes 3–4 lettres)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import (
    audit_alias_tables,
    build_gspread_client,
    format_alias_audit_report,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Vérifie la cohérence AliasTables (JOPAI LumenVia).")
    p.add_argument(
        "--secrets",
        default=".streamlit/secrets.toml",
        help="Chemin secrets.toml (défaut : .streamlit/secrets.toml)",
    )
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(Path(args.secrets))
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        print("ERREUR : gcp_service_account ou gsheet_id manquant dans les secrets.", file=sys.stderr)
        return 2

    gs = build_gspread_client(cfg.gcp_service_account)
    sh = gs.open_by_key(cfg.gsheet_id)
    issues = audit_alias_tables(sh=sh)
    print(format_alias_audit_report(issues))

    if any(i.severity == "error" for i in issues):
        return 1
    if issues:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
