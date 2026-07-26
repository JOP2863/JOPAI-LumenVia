"""
Purge en place les rubriques AELF (OU LECTURE BREVE, OU BIEN, …) dans readings_cache / RDC.

  python tools/scrub_rdc_lectionary_rubrics.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.aelf_text_cleanup import (  # noqa: E402
    aelf_text_still_has_lectionary_rubric,
    strip_aelf_lectionary_rubrics,
)
from core.config import load_config_from_secrets_toml  # noqa: E402
from core.sheets_db import (  # noqa: E402
    build_gspread_client,
    compute_concat,
    open_spreadsheet,
    _resolve_table_name,
)

_READING_BODY_COLS = (
    "premiere_lecture",
    "psaume",
    "deuxieme_lecture",
    "evangile",
)


def main() -> int:
    cfg = load_config_from_secrets_toml(REPO_ROOT / ".streamlit" / "secrets.toml")
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        print("ERROR: gcp_service_account / gsheet_id manquants dans secrets.toml")
        return 1

    gs = build_gspread_client(cfg.gcp_service_account)
    sh = open_spreadsheet(gs, cfg.gsheet_id)
    ws = sh.worksheet(_resolve_table_name(sh=sh, table="readings_cache"))
    values = ws.get_all_values()
    if not values:
        print("RDC vide.")
        return 0

    header = [str(c or "").strip() for c in values[0]]
    col_idx = {h: i for i, h in enumerate(header) if h}
    body_cols = [c for c in _READING_BODY_COLS if c in col_idx]
    concat_i = col_idx.get("concat")
    if not body_cols:
        print("Colonnes lectures introuvables dans l'en-tête RDC.")
        return 1

    from gspread.utils import rowcol_to_a1

    updates: list[dict] = []
    rows_touched = 0
    for r_i, row_vals in enumerate(values[1:], start=2):
        cells = list(row_vals) + [""] * max(0, len(header) - len(row_vals))
        changed = False
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
            updates.append({"range": rowcol_to_a1(r_i, j + 1), "values": [[cleaned]]})
        if changed:
            rows_touched += 1
            if concat_i is not None:
                row_map = {header[k]: cells[k] for k in range(len(header))}
                new_concat = compute_concat(row_map, header=header)
                updates.append({"range": rowcol_to_a1(r_i, concat_i + 1), "values": [[new_concat]]})

    print(f"Lignes à nettoyer : {rows_touched} · cellules : {len(updates)}")
    if not updates:
        print("Rien à faire.")
        return 0

    chunk = 80
    for i in range(0, len(updates), chunk):
        ws.batch_update(updates[i : i + chunk], value_input_option="RAW")
        print(f"  batch {i // chunk + 1}: {min(chunk, len(updates) - i)} update(s)")

    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
