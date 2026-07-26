"""Réécrit la table Voix_Audio (pools lectures / synthèse) + assure les colonnes readings_cache."""

from __future__ import annotations

import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.sheets_db import (
    build_gspread_client,
    ensure_table,
    get_table_spec,
    open_spreadsheet,
)


def _concat(parts: list[str]) -> str:
    return " | ".join(p.strip() for p in parts if str(p).strip())


VOIX_SPECS: list[tuple[str, str, str, str, str]] = [
    ("lectures", "*", "*", "Charon", "Pool lectures — claire / pédagogique"),
    ("lectures", "*", "*", "Kore", "Pool lectures — neutre / posée"),
    ("lectures", "*", "*", "Vindemiatrix", "Pool lectures — douce"),
    ("lectures", "*", "*", "Zephyr", "Pool lectures — lumineuse"),
    ("lectures", "*", "*", "Aoede", "Pool lectures — légère"),
    ("synthese", "*", "*", "Sulafat", "Pool synthèse — douce chaleur"),
    ("synthese", "*", "*", "Laomedeia", "Pool synthèse — tonique"),
    ("synthese", "*", "*", "Achird", "Pool synthèse — chaleureuse"),
    ("synthese", "*", "*", "Sadachbia", "Pool synthèse — vive"),
    ("synthese", "*", "*", "Puck", "Pool synthèse — espiègle"),
    ("synthese", "*", "pascal", "Laomedeia", "Override — temps pascal"),
    ("synthese", "*", "careme", "Vindemiatrix", "Override — Carême"),
    ("synthese", "violet", "*", "Sulafat", "Override — liturgie violette"),
    ("synthese", "rouge", "*", "Sadachbia", "Override — liturgie rouge"),
]


def main() -> int:
    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        print("ERREUR: gsheet_id / gcp_service_account manquants.")
        return 1

    gs = build_gspread_client(cfg.gcp_service_account)
    sid = str(cfg.gsheet_id).strip()

    # 1) Colonnes meta lectures (intro / ref) si absentes
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=sid,
        table=get_table_spec("readings_cache"),
    )
    print("OK: readings_cache — header aligné (intro/ref inclus).")

    # 2) Remplace Voix_Audio
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=sid,
        table=get_table_spec("Voix_Audio"),
    )
    sh = open_spreadsheet(gs, sid, use_cache=False)
    from core import sheets_db as sheets_db_mod

    ws_name = sheets_db_mod._resolve_table_name(sh=sh, table="Voix_Audio")  # noqa: SLF001
    ws = sh.worksheet(ws_name)
    headers = get_table_spec("Voix_Audio").columns
    ws.clear()
    ws.update(values=[headers], range_name="A1", value_input_option="RAW")

    today = date.today().isoformat()
    ver = "2"
    statut = "Actif"
    bulk: list[list[str]] = []
    for cible, couleur, temps, voix, description in VOIX_SPECS:
        rid = sha256(
            f"voix|rot|{cible}|{couleur}|{temps}|{voix}|{ver}".encode("utf-8")
        ).hexdigest()[:18]
        concat = _concat([rid, statut, ver, today, cible, couleur, temps, voix, description])
        bulk.append(
            [rid, statut, ver, today, cible, couleur, temps, voix, description, concat]
        )

    ws.append_rows(bulk, value_input_option="RAW")
    print(f"OK: Voix_Audio réécrit — {len(bulk)} règle(s) (pools + overrides).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
