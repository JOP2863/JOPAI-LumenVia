from __future__ import annotations

import argparse
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import core.sheets_db as sheets_db_mod
from core.config import load_config_from_secrets_toml
from core.sheets_db import (
    build_gspread_client,
    default_tables,
    ensure_database,
    fetch_records,
    sheet_row_status_is_live,
)


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
        "Voix_Audio": "VOIX",
        "liturgy_illustrations": "ILUS",
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
        "Voix_Audio": "Règles de voix TTS Gemini (synthèse / lectures)",
        "liturgy_illustrations": "Visuels dominicaux (GCS, descriptions / légendes IA ou manuel)",
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


def _concat_ia(parts: list[str]) -> str:
    return " | ".join(p.strip() for p in parts if str(p).strip())


def _concat_voix(parts: list[str]) -> str:
    return " | ".join(p.strip() for p in parts if str(p).strip())


def _seed_voix_audio_defaults(*, gc, gsheet_id: str) -> int:
    """Ajoute les règles VOIX par défaut uniquement si la table est vide (aucune ligne de données)."""
    rows = fetch_records(gspread_client=gc, spreadsheet_id=gsheet_id, table="Voix_Audio", limit=0)
    if rows:
        return 0

    sh = gc.open_by_key(gsheet_id)
    ws_name = sheets_db_mod._resolve_table_name(sh=sh, table="Voix_Audio")  # noqa: SLF001
    ws = sh.worksheet(ws_name)
    if not ws.row_values(1):
        raise RuntimeError("Voix_Audio: header vide après ensure_database.")

    today = date.today().isoformat()
    specs: list[tuple[str, str, str, str, str]] = [
        ("*", "*", "*", "Achird", "Défaut — toutes cibles / tous temps"),
        ("synthese", "*", "pascal", "Laomedeia", "Synthèse — temps pascal (tonique)"),
        ("synthese", "*", "careme", "Vindemiatrix", "Synthèse — Carême (douce)"),
        ("synthese", "violet", "*", "Sulafat", "Synthèse — liturgie violette"),
        ("synthese", "rouge", "*", "Sadachbia", "Synthèse — liturgie rouge"),
        ("lectures", "*", "*", "Charon", "Lectures AELF — voix claire / lecteur"),
    ]

    bulk: list[list[str]] = []
    ver = "1"
    statut = "Actif"
    for cible, couleur, temps, voix, description in specs:
        rid = sha256(f"voix|{cible}|{couleur}|{temps}|{voix}|{ver}".encode("utf-8")).hexdigest()[:18]
        concat = _concat_voix([rid, statut, ver, today, cible, couleur, temps, voix, description])
        bulk.append([rid, statut, ver, today, cible, couleur, temps, voix, description, concat])

    ws.append_rows(bulk, value_input_option="RAW")
    return len(bulk)


_AUDIO_PROMPT_SPECS: list[tuple[str, str, str]] = [
    (
        "audio_style_default",
        "TTS — style oral par défaut (synthèse)",
        (
            "Tu es la voix de LumenVia. Lis le texte suivant en français avec un ton chaleureux et posé, "
            "comme un accompagnateur qui partage la Parole du dimanche.\n"
            "Rythme : débit moyen ; courte pause après chaque titre de section ; un peu plus lent sur les citations "
            "ou répliques entre guillemets.\n"
            "Ne reformule pas le texte : lis-le tel quel."
        ),
    ),
    (
        "audio_style_paques",
        "TTS — surcouche temps pascal (synthèse)",
        (
            "Accent léger de joie et de clarté : comme une bonne nouvelle qui se déploie, sans emphase théâtrale."
        ),
    ),
    (
        "audio_style_careme",
        "TTS — surcouche Carême (synthèse)",
        (
            "Garde une gravité paisible : registre un peu plus bas, silences un peu plus longs entre les paragraphes ; "
            "pas de dramatisation."
        ),
    ),
    (
        "audio_style_lectures",
        "TTS — style lectures du lectionnaire",
        (
            "Tu es lecteur du lectionnaire dominical : lis avec sobriété liturgique ; marque clairement le passage "
            "d'une lecture à l'autre par une courte pause ; pour l'Évangile, une légère élévation respectueuse."
        ),
    ),
]


def _max_prompt_version_for_key(records: list[dict], key: str) -> int:
    mx = 0
    for r in records:
        if str(r.get("Clé_Prompt") or "").strip() != key:
            continue
        try:
            mx = max(mx, int(str(r.get("Version") or "0").strip()))
        except Exception:
            pass
    return mx


def _prompt_has_active(records: list[dict], key: str) -> bool:
    for r in records:
        if str(r.get("Clé_Prompt") or "").strip() != key:
            continue
        if sheet_row_status_is_live(r.get("Statut")):
            return True
    return False


def _seed_parametres_ia_audio_styles(*, gc, gsheet_id: str) -> int:
    """Ajoute les clés TTS (Levier B) si aucune ligne Actif n'existe pour ces Clé_Prompt."""
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
    appended = 0
    for key, description, body in _AUDIO_PROMPT_SPECS:
        if _prompt_has_active(records, key):
            continue
        ver = str(_max_prompt_version_for_key(records, key) + 1)
        rid = sha256(f"ia|{key}|{ver}|{body}".encode("utf-8")).hexdigest()[:18]
        statut = "Actif"
        concat = _concat_ia([rid, key, ver, statut, today])
        row_map = {
            "#ID": rid,
            "Clé_Prompt": key,
            "Description": description,
            "Version": ver,
            "Statut": statut,
            "Date_Effet": today,
            "Contenu_Markdown": body,
            "Concaténation": concat,
        }
        ws.append_rows([[row_map.get(c, "") for c in header]], value_input_option="RAW")
        records.append(dict(row_map))
        appended += 1
    return appended


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
        n_voix = _seed_voix_audio_defaults(gc=gc, gsheet_id=gsheet_id)
        n_aud = _seed_parametres_ia_audio_styles(gc=gc, gsheet_id=gsheet_id)
        if n_voix:
            print(f"OK: seed Voix_Audio — {n_voix} ligne(s).")
        if n_aud:
            print(f"OK: seed Paramètres_IA (prompts TTS) — {n_aud} ligne(s).")
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

