from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import build_gspread_client, sheet_row_status_is_live


PARAMS_SHEET = "Paramètres_IA"
HEADER = ["#ID", "Clé_Prompt", "Description", "Version", "Statut", "Date_Effet", "Contenu_Markdown", "Concaténation"]


def _read_app_defaults() -> dict[str, str]:
    """
    Source: app.py contient les defaults des surcouches.
    On ne parse pas l'AST : on reste simple et on seed uniquement ce qui existe ailleurs en fichiers.
    Pour les surcouches, on se base sur des fichiers fallback stables si présents.
    """
    # Base instructions: on retire l'en-tête "fallback local minimal" si présent,
    # pour éviter de polluer le prompt stocké dans Sheets.
    base_raw = Path("data/instructions_ia.md").read_text(encoding="utf-8").strip()
    marker = "## 1. Objectif & Rôle"
    base = base_raw
    if marker in base_raw:
        base = ("# Instructions IA — JOPAI LumenVia (Version Renforcée)\n\n" + base_raw.split(marker, 1)[1]).strip()
        base = base.replace("# Instructions IA — JOPAI LumenVia (Version Renforcée)\n\n\n\n", "# Instructions IA — JOPAI LumenVia (Version Renforcée)\n\n")
        base = base.replace("# Instructions IA — JOPAI LumenVia (Version Renforcée)\n\n\n", "# Instructions IA — JOPAI LumenVia (Version Renforcée)\n\n")
    return {
        "instructions_base_md": base,
        # Les autres clés existent déjà en fallback dans app.py ; on les laissera gérées via l'admin si besoin.
        # On les seed quand même avec des valeurs minimales (tu pourras les remplacer dans Sheets).
        "overlay_takeaways": "",
        "overlay_no_takeaways": "",
        "overlay_catechese_bridge": "",
        "retry_hardened_prefix": "",
    }


def _default_descriptions() -> dict[str, str]:
    return {
        "instructions_base_md": "Socle — consignes générales (structure du prompt)",
        "overlay_takeaways": "Surcouche — inclure « Le Psaume » + « À retenir »",
        "overlay_no_takeaways": "Surcouche — ne pas inclure la section « À retenir »",
        "overlay_catechese_bridge": "Surcouche — ajouter une passerelle catéchèse",
        "retry_hardened_prefix": "Surcouche — préfixe de relance (anti-hallucination renforcée)",
    }


def _ensure_sheet(sh) -> object:
    from core.sheets_db import _resolve_table_name

    ws_name = _resolve_table_name(sh=sh, table=PARAMS_SHEET)
    try:
        ws = sh.worksheet(ws_name)
    except Exception:
        ws = sh.add_worksheet(title=ws_name, rows=4000, cols=len(HEADER) + 2)
    hdr = ws.row_values(1)
    if [x.strip() for x in hdr if x.strip()] != HEADER:
        ws.update([HEADER], "A1")
    return ws


def _existing_versions(ws) -> dict[str, int]:
    # Lecture rapide : on récupère toutes les lignes en dicts via get_all_records.
    # NB: get_all_records() ignore la ligne 1 (header).
    try:
        records = ws.get_all_records()
    except Exception:
        records = []
    out: dict[str, int] = {}
    for r in records:
        k = str(r.get("Clé_Prompt") or "").strip()
        if not k:
            continue
        try:
            v = int(str(r.get("Version") or "0").strip())
        except Exception:
            v = 0
        out[k] = max(out.get(k, 0), v)
    return out


def _concat(parts: list[str]) -> str:
    return " | ".join([p.strip() for p in parts if p and str(p).strip()])

def _norm(s: object) -> str:
    return str(s or "").strip()


def _is_active(statut: object) -> bool:
    return sheet_row_status_is_live(statut)


def _existing_active_rows(ws) -> dict[str, list[dict[str, object]]]:
    """
    Retourne les lignes actuellement Actif par Clé_Prompt.
    Utilisé pour “assainir” : on désactive toutes les Actif existantes lors d’un nouveau seed.
    """
    try:
        records = ws.get_all_records()
    except Exception:
        records = []
    out: dict[str, list[dict[str, object]]] = {}
    for r in records:
        k = _norm(r.get("Clé_Prompt"))
        if not k:
            continue
        if not _is_active(r.get("Statut")):
            continue
        out.setdefault(k, []).append(r)
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Seed Paramètres_IA depuis les prompts actuels (append-only).")
    p.add_argument(
        "--secrets",
        default=str(Path(".streamlit") / "secrets.toml"),
        help="Chemin vers secrets.toml (défaut: .streamlit/secrets.toml)",
    )
    p.add_argument("--force", action="store_true", help="Insère même si une version existe déjà (Version+1).")
    args = p.parse_args(argv)

    cfg = load_config_from_secrets_toml(args.secrets)
    if not cfg.gsheet_id:
        print("Erreur: gsheet_id manquant dans secrets.toml.", file=sys.stderr)
        return 2
    if not cfg.gcp_service_account:
        print("Erreur: gcp_service_account manquant dans secrets.toml.", file=sys.stderr)
        return 3

    gc = build_gspread_client(cfg.gcp_service_account)
    sh = gc.open_by_key(cfg.gsheet_id)
    ws = _ensure_sheet(sh)

    existing = _existing_versions(ws)
    actives = _existing_active_rows(ws)
    today = date.today().isoformat()

    payload = _read_app_defaults()
    descs = _default_descriptions()
    rows_to_append: list[list[str]] = []
    for key, content in payload.items():
        content = (content or "").strip()
        if not content:
            # on ne seed pas les blocs vides par défaut
            continue
        prev = int(existing.get(key, 0))
        if prev > 0 and not args.force:
            continue
        ver = prev + 1

        # Append-only (sans supprimer) : on met à jour EN PLACE les lignes Actif existantes (Statut -> Inactif),
        # puis on append uniquement la nouvelle version.
        if actives.get(key):
            header = ws.row_values(1)
            try:
                col_statut = header.index("Statut") + 1
                col_concat = header.index("Concaténation") + 1
            except Exception:
                col_statut = 0
                col_concat = 0

            if col_statut and col_concat:
                # On doit retrouver les numéros de ligne : get_all_records() correspond aux lignes dès la 2.
                records = ws.get_all_records()
                for i, r in enumerate(records):
                    if _norm(r.get("Clé_Prompt")) != key:
                        continue
                    if not _is_active(r.get("Statut")):
                        continue
                    row_num = i + 2
                    ws.update_cell(row_num, col_statut, "Inactif")
                    row_id = _norm(r.get("#ID") or r.get("ID") or r.get("id"))
                    ver_str = _norm(r.get("Version"))
                    de_str = _norm(r.get("Date_Effet")) or today
                    ws.update_cell(row_num, col_concat, _concat([row_id, key, ver_str, "Inactif", de_str]))

        row_id = sha256(f"ia|{key}|{ver}|{content}".encode("utf-8")).hexdigest()[:18]
        statut = "Actif"
        concat = _concat([row_id, key, str(ver), statut, today])
        rows_to_append.append([row_id, key, descs.get(key, ""), str(ver), statut, today, content, concat])

    if not rows_to_append:
        print("Rien à insérer (déjà seedé ou contenus vides).")
        return 0

    ws.append_rows(rows_to_append, value_input_option="RAW")
    print(f"OK: {len(rows_to_append)} ligne(s) ajoutée(s) dans {PARAMS_SHEET}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

