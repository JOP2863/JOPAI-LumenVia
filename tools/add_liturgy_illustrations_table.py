#!/usr/bin/env python3
"""
Crée l’onglet **ILUS** pour les illustrations dominicales + entrée dans **AliasTables**.

Colonnes (base obligatoire + concat) :
  row_id, entity_id, version, status, created_at,
  date (dimanche ISO), zone, gcs_path,
  description_illustration (légende courte),
  gen_entity_id (optionnel — lien generations.entity_id comme pour AUD),
  caption_source (ex. vertex | manual | import),
  caption_model (ex. gemini-2.5-flash si source vertex),
  concat

Usage (racine du dépôt) ::
  python tools/add_liturgy_illustrations_table.py
  python tools/add_liturgy_illustrations_table.py --secrets .streamlit/secrets.toml --gsheet-id VOTRE_ID

Ré-appliquer uniquement les alias : ``python tools/init_sheets_db.py`` synchronise aussi cette table
si elle est déjà dans ``default_tables()`` (équivalent après coup).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import build_gspread_client, ensure_table, liturgy_illustrations_table_spec


def _migrate_alias_tables_and_rename(*, gc, gsheet_id: str) -> None:
    """Charge ``tools/init_sheets_db.py`` sans exiger ``tools`` comme package."""
    import importlib.util

    path = REPO_ROOT / "tools" / "init_sheets_db.py"
    spec = importlib.util.spec_from_file_location("lumenvia_init_sheets_db", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossible de charger {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.migrate_alias_tables_and_rename(gc=gc, gsheet_id=gsheet_id)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Ajoute la table liturgy_illustrations (ILUS) + alias.")
    p.add_argument("--gsheet-id", default=None, help="Override gsheet_id (sinon secrets.toml)")
    p.add_argument(
        "--secrets",
        default=str(Path(".streamlit") / "secrets.toml"),
        help="Chemin vers secrets.toml",
    )
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
        ensure_table(
            gspread_client=gc,
            spreadsheet_id=gsheet_id,
            table=liturgy_illustrations_table_spec(),
        )
        _migrate_alias_tables_and_rename(gc=gc, gsheet_id=gsheet_id)
    except PermissionError:
        sa_email = str(cfg.gcp_service_account.get("client_email", "") or "").strip()
        print("\nERREUR: accès refusé au Google Sheet.\n", file=sys.stderr)
        if sa_email:
            print(f"Partage le fichier avec : {sa_email} (Éditeur).", file=sys.stderr)
        return 4

    print(
        "OK : table `liturgy_illustrations` (onglet physique **ILUS** selon AliasTables) "
        "+ ligne d’alias mise à jour."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
