"""
Construit le dictionnaire TTS depuis ``readings_cache`` (RDC) et le publie dans Sheets + fichier dépôt.

Usage :
  python .\\tools\\seed_tts_pronunciation_from_readings_cache.py
  python .\\tools\\seed_tts_pronunciation_from_readings_cache.py --publish
  python .\\tools\\seed_tts_pronunciation_from_readings_cache.py --publish --force
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import build_gspread_client, fetch_records, sheet_row_status_is_live
from core.tts_pronunciation_lexicon import (
    build_pronunciation_dict_from_readings_rows,
    pronunciation_dict_to_json_text,
)
import core.sheets_db as sheets_db_mod
import tools.init_sheets_db as init_sheets_db


def _live_readings_cache_rows(*, gc: object, gsheet_id: str) -> list[dict]:
    rows = fetch_records(gspread_client=gc, spreadsheet_id=gsheet_id, table="readings_cache", limit=0)
    out: list[dict] = []
    for r in rows:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        if str(r.get("error") or "").strip():
            continue
        out.append(r)
    return out


def _parametres_ia_has_active_tts_pronunciation(*, gc: object, gsheet_id: str) -> bool:
    sh = gc.open_by_key(gsheet_id)
    ws_name = sheets_db_mod._resolve_table_name(sh=sh, table="Paramètres_IA")  # noqa: SLF001
    ws = sh.worksheet(ws_name)
    try:
        records = ws.get_all_records(numericise_ignore=["all"])
    except Exception:
        records = []
    return init_sheets_db._prompt_has_active(records, "tts_pronunciation")


def _append_parametres_ia_tts_pronunciation(*, gc: object, gsheet_id: str, body: str) -> None:
    sh = gc.open_by_key(gsheet_id)
    ws_name = sheets_db_mod._resolve_table_name(sh=sh, table="Paramètres_IA")  # noqa: SLF001
    ws = sh.worksheet(ws_name)
    header = ws.row_values(1)
    if not header:
        raise RuntimeError("Paramètres_IA: header vide.")
    try:
        records = ws.get_all_records(numericise_ignore=["all"])
    except Exception:
        records = []
    today = date.today().isoformat()
    key = "tts_pronunciation"
    ver = str(init_sheets_db._max_prompt_version_for_key(records, key) + 1)
    rid = sha256(f"ia|{key}|{ver}|{body}".encode("utf-8")).hexdigest()[:18]
    row_map = {
        "#ID": rid,
        "Clé_Prompt": key,
        "Description": "TTS — dictionnaire de prononciation (JSON, voix seulement)",
        "Version": ver,
        "Statut": "Actif",
        "Date_Effet": today,
        "Contenu_Markdown": body,
        "Concaténation": init_sheets_db._concat_ia([rid, key, ver, "Actif", today]),
    }
    ws.append_rows([[row_map.get(c, "") for c in header]], value_input_option="RAW")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Seed dictionnaire TTS depuis readings_cache (RDC).")
    p.add_argument("--gsheet-id", default=None)
    p.add_argument("--secrets", default=str(Path(".streamlit") / "secrets.toml"))
    p.add_argument(
        "--publish",
        action="store_true",
        help="Écrit aussi une ligne Actif dans Paramètres_IA (Sheets).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Avec --publish : ajoute une nouvelle version même si une ligne Actif existe déjà.",
    )
    p.add_argument(
        "--json-out",
        default=str(ROOT / "data" / "tts_pronunciation_fr.json"),
        help="Fichier JSON dépôt à mettre à jour.",
    )
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    gsheet_id = (args.gsheet_id or cfg.gsheet_id or "").strip()
    if not gsheet_id or not cfg.gcp_service_account:
        print("Erreur: gsheet_id et gcp_service_account requis.", file=sys.stderr)
        return 2

    gc = build_gspread_client(cfg.gcp_service_account)
    rc_rows = _live_readings_cache_rows(gc=gc, gsheet_id=gsheet_id)
    rules = build_pronunciation_dict_from_readings_rows(rc_rows, include_manual_always=True)
    body = pronunciation_dict_to_json_text(rules)

    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(body, encoding="utf-8")
    print(f"OK: {len(rules)} entree(s) -> {json_out}")
    print(f"    readings_cache analysees : {len(rc_rows)} ligne(s) Actif")

    if args.publish:
        if _parametres_ia_has_active_tts_pronunciation(gc=gc, gsheet_id=gsheet_id) and not args.force:
            print(
                "Sheets: ligne Actif `tts_pronunciation` déjà présente — "
                "relancez avec --publish --force pour ajouter une nouvelle version."
            )
        else:
            _append_parametres_ia_tts_pronunciation(gc=gc, gsheet_id=gsheet_id, body=body)
            print("OK: ligne `tts_pronunciation` publiée dans Paramètres_IA (Statut = Actif).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
