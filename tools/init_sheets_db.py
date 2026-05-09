from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.sheets_db import default_tables, ensure_database, build_gspread_client


def _suggest_aliases() -> dict[str, str]:
    # Mapping initial (nom complet -> acronyme 3-4 lettres).
    # Ajustable ensuite via AliasTables sans casser le code.
    return {
        "users": "USR",
        "subscriptions": "SUB",
        "password_resets": "PWRT",
        "email_templates": "ETPL",
        "outbound_messages": "OUTM",
        "generations": "GEN",
        "audio": "AUD",
        "pdf_exports": "PDFX",
        "memos": "MEM",
        "admin_changelog": "ADLG",
        "readings_cache": "RDC",
        "liturgy_fetches": "LITF",
        "vision_text_audit": "VTA",
        "vision_text_corrections": "VTC",
        "vision_text_whitelist": "VTW",
        "Paramètres_IA": "AIP",
        "scheduler_campaigns": "CMPG",
        "scheduler_runs": "RUNS",
        "audiences": "AUDC",
        "experience_feedback": "RSTN",
        "feedback_insights": "FBIN",
        # AliasTables garde son nom long (table maître)
    }


def migrate_alias_tables_and_rename(*, gc, gsheet_id: str) -> None:
    sh = gc.open_by_key(gsheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}

    # Crée AliasTables si absent
    if "AliasTables" not in existing:
        ws = sh.add_worksheet(title="AliasTables", rows=2000, cols=10)
        ws.update([["#ID", "Statut", "Version", "Nom Complet Table", "Acronyme Table", "Description"]], "A1")
        existing["AliasTables"] = ws

    ws_alias = existing["AliasTables"]
    try:
        alias_rows = ws_alias.get_all_records(numericise_ignore=["all"])
    except Exception:
        alias_rows = []
    existing_full = {str(r.get("Nom Complet Table") or "").strip() for r in alias_rows if str(r.get("Nom Complet Table") or "").strip()}
    existing_acr = {str(r.get("Acronyme Table") or "").strip() for r in alias_rows if str(r.get("Acronyme Table") or "").strip()}

    def _next_id() -> int:
        mx = 0
        for r in alias_rows:
            raw = str(r.get("#ID") or "").strip()
            if raw.isdigit():
                mx = max(mx, int(raw))
        return mx + 1

    next_id = _next_id()

    desc = {
        "users": "Comptes utilisateurs (profil)",
        "subscriptions": "Préférences d’envoi (opt-in/opt-out)",
        "password_resets": "Jetons réinitialisation mot de passe",
        "email_templates": "Templates e-mail (versions actives)",
        "outbound_messages": "Journal des messages sortants (email/sms)",
        "generations": "Synthèses générées (texte)",
        "audio": "Audios générés (TTS)",
        "pdf_exports": "Exports PDF",
        "memos": "Aide-mémoire utilisateurs",
        "admin_changelog": "Journal administration",
        "readings_cache": "Cache lectures AELF",
        "liturgy_fetches": "Journal appels AELF",
        "vision_text_audit": "Audit OCR",
        "vision_text_corrections": "Corrections OCR",
        "vision_text_whitelist": "Whitelist OCR",
        "Paramètres_IA": "Prompts IA (Google Sheets)",
        "scheduler_campaigns": "Campagnes d’envoi (scheduler)",
        "scheduler_runs": "Exécutions (scheduler)",
        "audiences": "Audiences (ciblage)",
        "experience_feedback": "Retours / mini-questionnaires post-envoi",
        "feedback_insights": "Synthèses IA (historique questionnaires)",
        "CMPG": "Campagnes d’envoi (scheduler)",
        "RUNS": "Exécutions (scheduler)",
        "RSTN": "Retours / mini-questionnaires post-envoi",
        "FBIN": "Synthèses IA (historique questionnaires)",
        "PWRT": "Jetons réinitialisation mot de passe",
    }

    aliases = _suggest_aliases()
    # Renomme les onglets existants selon le mapping si besoin
    for full, acr in aliases.items():
        full = str(full).strip()
        acr = str(acr).strip().upper()
        if not acr or len(acr) not in (3, 4):
            continue
        # si mapping déjà présent, ne rien faire
        if full in existing_full or acr in existing_acr:
            continue
        if acr in existing and full not in existing:
            # L'onglet acronyme existe déjà, on documente seulement.
            ws_alias.append_row([str(next_id), "Actif", "1", full, acr, desc.get(full, "")], value_input_option="RAW")
            next_id += 1
            continue
        if full in existing:
            try:
                existing[full].update_title(acr)
                existing[acr] = existing.pop(full)
            except Exception:
                pass
        ws_alias.append_row([str(next_id), "Actif", "1", full, acr, desc.get(full, "")], value_input_option="RAW")
        next_id += 1

    # Nettoyage contrôlé : réécrit AliasTables sans doublons et avec descriptions.
    try:
        all_rows = ws_alias.get_all_records(numericise_ignore=["all"])
    except Exception:
        all_rows = []
    # On garde uniquement les entrées avec Nom Complet Table + Acronyme Table
    uniq: dict[tuple[str, str], dict] = {}
    for r in all_rows:
        full = str(r.get("Nom Complet Table") or "").strip()
        acr = str(r.get("Acronyme Table") or "").strip().upper()
        if not full or not acr:
            continue
        key = (full, acr)
        # Préférence : description non vide
        prev = uniq.get(key)
        if not prev or (not str(prev.get("Description") or "").strip() and str(r.get("Description") or "").strip()):
            uniq[key] = r

    cleaned: list[list[str]] = []
    i = 1
    for (full, acr), r in sorted(uniq.items(), key=lambda x: x[0][0].lower()):
        cleaned.append(
            [
                str(i),
                "Actif",
                "1",
                full,
                acr,
                desc.get(full, str(r.get("Description") or "").strip()),
            ]
        )
        i += 1
    # Réécrit la feuille (header + lignes)
    ws_alias.clear()
    ws_alias.update([["#ID", "Statut", "Version", "Nom Complet Table", "Acronyme Table", "Description"]], "A1")
    if cleaned:
        ws_alias.update(cleaned, "A2")


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
        migrate_alias_tables_and_rename(gc=gc, gsheet_id=gsheet_id)
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

    print("OK: base Google Sheets initialisée + AliasTables/alias appliqués.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

