from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4

import gspread
from gspread.exceptions import APIError as GspreadAPIError
from google.oauth2 import service_account


# Acronymes par défaut (alignés sur ``tools/init_sheets_db.py``) si AliasTables est illisible.
_DEFAULT_TABLE_ACRONYMS: dict[str, str] = {
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
}

# Noms logiques autorisés dans le code applicatif (jamais d’acronyme seul en dur).
KNOWN_LOGICAL_TABLES: frozenset[str] = frozenset(_DEFAULT_TABLE_ACRONYMS.keys())

_ACRONYM_TO_LOGICAL: dict[str, str] = {acr: logical for logical, acr in _DEFAULT_TABLE_ACRONYMS.items()}


@dataclass(frozen=True)
class AliasAuditIssue:
    severity: str  # "error" | "warning"
    logical_table: str
    message: str


def describe_gspread_api_error(
    ex: BaseException,
    *,
    spreadsheet_id: str = "",
    service_account_email: str | None = None,
) -> str:
    """Message lisible pour admin (Streamlit Cloud masque souvent le détail brut de gspread)."""
    code: int | None = None
    resp = getattr(ex, "response", None)
    if resp is not None:
        try:
            code = int(getattr(resp, "status_code", None))
        except (TypeError, ValueError):
            code = None

    sid = str(spreadsheet_id or "").strip()
    sa = str(service_account_email or "").strip()
    sa_hint = f" `{sa}`" if sa else " (compte de service des secrets)"

    if code == 403:
        return (
            f"Accès refusé au Google Sheet (HTTP 403). "
            f"Partagez le fichier `{sid}` en **Éditeur** avec{sa_hint}, "
            "puis vérifiez que les API Google Sheets et Drive sont activées sur le projet GCP."
        )
    if code == 404:
        return (
            f"Google Sheet introuvable (HTTP 404) pour `gsheet_id={sid}`. "
            "Vérifiez l’identifiant dans les secrets Streamlit Cloud (URL : `/d/<ID>/edit`)."
        )
    if code == 429:
        return (
            "Quota Google Sheets dépassé (HTTP 429). Attendez une minute et réessayez "
            "(trop d’écritures / lectures en peu de temps)."
        )
    if code:
        return (
            f"Erreur Google Sheets (HTTP {code}). Vérifiez `gsheet_id`, le partage du fichier "
            f"et les credentials GCP.{f' Détail : {ex}' if str(ex) else ''}"
        )
    return (
        f"Impossible d’ouvrir le Google Sheet `{sid}`. Vérifiez `gsheet_id`, le partage "
        f"avec{sa_hint} et les secrets Streamlit Cloud."
    )


_SPREADSHEET_CACHE: dict[str, tuple[float, gspread.Spreadsheet]] = {}
_FETCH_RECORDS_CACHE: dict[tuple[str, str, int | None], tuple[float, list[dict[str, Any]]]] = {}
_SHEETS_CACHE_TTL_S = 90.0


def _is_gspread_quota_error(ex: BaseException) -> bool:
    msg = str(ex)
    if "429" in msg or "Quota exceeded" in msg:
        return True
    resp = getattr(ex, "response", None)
    if resp is not None:
        try:
            return int(getattr(resp, "status_code", 0)) == 429
        except (TypeError, ValueError):
            pass
    return False


def _gspread_call_with_retry(
    fn,
    *,
    max_retries: int = 5,
    base_sleep_s: float = 1.5,
):
    last_ex: BaseException | None = None
    for i in range(max(1, int(max_retries))):
        try:
            return fn()
        except GspreadAPIError as ex:
            last_ex = ex
            if not _is_gspread_quota_error(ex):
                raise
            time.sleep(base_sleep_s * (2**i))
        except Exception as ex:
            last_ex = ex
            if not _is_gspread_quota_error(ex):
                raise
            time.sleep(base_sleep_s * (2**i))
    if last_ex:
        raise last_ex
    raise RuntimeError("Appel Google Sheets échoué après plusieurs tentatives.")


def invalidate_fetch_records_cache(
    *,
    spreadsheet_id: str | None = None,
    table: str | None = None,
) -> None:
    """Invalide le cache court TTL de ``fetch_records`` (après écriture admin)."""
    sid = str(spreadsheet_id or "").strip()
    tbl = str(table or "").strip()
    if not sid and not tbl:
        _FETCH_RECORDS_CACHE.clear()
        _SPREADSHEET_CACHE.clear()
        return
    drop_keys = [
        k
        for k in _FETCH_RECORDS_CACHE
        if (not sid or k[0] == sid) and (not tbl or k[1] == tbl)
    ]
    for k in drop_keys:
        _FETCH_RECORDS_CACHE.pop(k, None)
    if sid:
        _SPREADSHEET_CACHE.pop(sid, None)


def open_spreadsheet(
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    *,
    service_account_email: str | None = None,
    use_cache: bool = True,
) -> gspread.Spreadsheet:
    sid = str(spreadsheet_id or "").strip()
    if not sid:
        raise ValueError("gsheet_id vide — renseignez `gsheet_id` dans les secrets Streamlit.")
    now = time.time()
    if use_cache:
        cached = _SPREADSHEET_CACHE.get(sid)
        if cached and now - cached[0] < _SHEETS_CACHE_TTL_S:
            return cached[1]
    try:
        sh = _gspread_call_with_retry(lambda: gspread_client.open_by_key(sid))
    except GspreadAPIError as ex:
        raise RuntimeError(
            describe_gspread_api_error(
                ex,
                spreadsheet_id=sid,
                service_account_email=service_account_email,
            )
        ) from ex
    if use_cache:
        _SPREADSHEET_CACHE[sid] = (now, sh)
    return sh


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: list[str]


def _row_cell(row: Mapping[str, Any], *keys: str) -> str:
    """Lit une cellule d’enregistrement Sheets en essayant plusieurs noms de colonne."""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    lower_map = {str(h).strip().lower(): v for h, v in row.items()}
    for k in keys:
        v = lower_map.get(k.strip().lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _alias_table_map(sh: gspread.Spreadsheet) -> dict[str, str]:
    """
    Lit AliasTables : nom logique (ou acronyme) → onglet physique canonique.
    Ex. ``email_templates`` → ``ETPL`` (même si un ancien onglet ``email_templates`` existe encore).
    """
    out: dict[str, str] = {}
    try:
        ws_alias = sh.worksheet("AliasTables")
        rows = ws_alias.get_all_records(numericise_ignore=["all"])
        for r in rows:
            stat = _row_cell(r, "Statut", "status", "Status").lower()
            if stat and (stat.startswith("inactif") or stat.startswith("inactive")):
                continue
            full = _row_cell(
                r,
                "Nom Complet Table",
                "nom_complet_table",
                "Nom complet table",
            )
            acr = _row_cell(
                r,
                "Acronyme Table",
                "acronyme_table",
                "Acronyme table",
            ).upper()
            if not full or not acr:
                continue
            if len(acr) not in (3, 4):
                continue
            out[full] = acr
            out[acr] = acr
    except Exception:
        pass
    return out


def audit_alias_tables(*, sh: gspread.Spreadsheet) -> list[AliasAuditIssue]:
    """
    Contrôle AliasTables ↔ onglets physiques ↔ registre dépôt (``KNOWN_LOGICAL_TABLES``).
    À lancer après ``init_sheets_db`` ou depuis l’admin (Test ressources).
    """
    issues: list[AliasAuditIssue] = []
    try:
        worksheets = sh.worksheets()
        titles = {ws.title for ws in worksheets}
    except Exception as ex:
        return [
            AliasAuditIssue(
                severity="error",
                logical_table="AliasTables",
                message=f"Impossible de lister les onglets : {ex}",
            )
        ]

    if "AliasTables" not in titles:
        issues.append(
            AliasAuditIssue(
                severity="error",
                logical_table="AliasTables",
                message="Onglet maître **AliasTables** absent — lancez `python tools/init_sheets_db.py`.",
            )
        )

    alias_map = _alias_table_map(sh)
    alias_logical: dict[str, str] = {}
    for logical in KNOWN_LOGICAL_TABLES:
        mapped = alias_map.get(logical)
        if mapped:
            alias_logical[logical] = mapped.upper()

    if "AliasTables" in titles and not alias_logical:
        issues.append(
            AliasAuditIssue(
                severity="error",
                logical_table="AliasTables",
                message="AliasTables illisible ou sans ligne **Actif** — les alias ne peuvent pas être appliqués.",
            )
        )

    for logical in sorted(KNOWN_LOGICAL_TABLES):
        expected_acr = _DEFAULT_TABLE_ACRONYMS[logical]
        mapped = alias_logical.get(logical)
        physical = mapped or expected_acr

        if not mapped:
            issues.append(
                AliasAuditIssue(
                    severity="warning",
                    logical_table=logical,
                    message=f"Absent de AliasTables — repli dépôt : onglet `{expected_acr}`.",
                )
            )
        elif mapped != expected_acr:
            issues.append(
                AliasAuditIssue(
                    severity="error",
                    logical_table=logical,
                    message=f"Acronyme AliasTables `{mapped}` ≠ registre dépôt `{expected_acr}`.",
                )
            )

        ghost = logical in titles and logical != physical
        physical_ok = physical in titles
        if not physical_ok:
            if ghost:
                issues.append(
                    AliasAuditIssue(
                        severity="error",
                        logical_table=logical,
                        message=(
                            f"Onglet canonique `{physical}` absent ; seul `{logical}` existe "
                            f"(fantôme pré-migration) — l’app écrirait au mauvais endroit sans AliasTables."
                        ),
                    )
                )
            else:
                issues.append(
                    AliasAuditIssue(
                        severity="error",
                        logical_table=logical,
                        message=f"Onglet `{physical}` introuvable (alias logique `{logical}`).",
                    )
                )
        elif ghost:
            issues.append(
                AliasAuditIssue(
                    severity="warning",
                    logical_table=logical,
                    message=(
                        f"Doublon `{logical}` + `{physical}` — l’app cible `{physical}` ; "
                        f"supprimez `{logical}` s’il est vide ou obsolète."
                    ),
                )
            )

    known_acrs = set(_DEFAULT_TABLE_ACRONYMS.values())
    for title in sorted(titles):
        if title == "AliasTables":
            continue
        if len(title) in (3, 4) and title.isupper() and title not in known_acrs:
            issues.append(
                AliasAuditIssue(
                    severity="warning",
                    logical_table=title,
                    message=f"Onglet acronyme `{title}` sans entrée dans le registre dépôt.",
                )
            )

    return issues


def format_alias_audit_report(issues: list[AliasAuditIssue]) -> str:
    if not issues:
        return "AliasTables OK — toutes les tables connues sont résolues."
    lines: list[str] = []
    for it in issues:
        tag = "ERREUR" if it.severity == "error" else "AVERT"
        lines.append(f"[{tag}] {it.logical_table}: {it.message}")
    return "\n".join(lines)


def _resolve_table_name(*, sh: gspread.Spreadsheet, table: str) -> str:
    """
    Résout un nom logique (ex: 'users', 'email_templates') vers le nom physique d'onglet.
    Supporte la convention AliasTables (acronymes 3–4 lettres).

    **Priorité AliasTables** : si ``email_templates`` et ``ETPL`` coexistent (onglet fantôme créé
    avant migration), on utilise toujours l'acronyme mappé (``ETPL``), pas le nom complet.

    Un acronyme passé seul (ex. ``CMPG``) est normalisé vers le nom logique du registre dépôt
    avant résolution — le code applicatif doit préférer le nom logique.
    """
    t = str(table or "").strip()
    if not t:
        return t
    if len(t) in (3, 4) and t.isupper() and t in _ACRONYM_TO_LOGICAL:
        t = _ACRONYM_TO_LOGICAL[t]
    try:
        titles = {ws.title for ws in sh.worksheets()}
    except Exception:
        titles = set()

    alias_map = _alias_table_map(sh)
    if t in alias_map:
        return alias_map[t]

    # AliasTables absent ou incomplet : préférer l’acronyme s’il existe (ex. ETPL, pas email_templates).
    default_acr = _DEFAULT_TABLE_ACRONYMS.get(t)
    if default_acr and default_acr in titles:
        return default_acr

    if t in titles:
        return t
    # Dernier recours : acronyme attendu même si l’onglet n’existe pas encore (ensure_table le créera).
    if default_acr := _DEFAULT_TABLE_ACRONYMS.get(t):
        return default_acr
    return t


def prune_stale_fullname_table_duplicates(*, sh: gspread.Spreadsheet) -> list[str]:
    """
    Supprime les onglets « nom complet » vides lorsque l'acronyme canonique existe déjà
    (ex. ``email_templates`` vide + ``ETPL`` peuplé). Retourne les messages d'action.
    """
    messages: list[str] = []
    try:
        existing = {ws.title: ws for ws in sh.worksheets()}
    except Exception:
        return messages

    for full, acr in _alias_table_map(sh).items():
        if full == acr or full not in existing or acr not in existing:
            continue
        ws_full = existing[full]
        ws_acr = existing[acr]
        try:
            n_full = max(0, len(ws_full.get_all_values()) - 1)
            n_acr = max(0, len(ws_acr.get_all_values()) - 1)
        except Exception:
            continue
        if n_full <= 0:
            try:
                sh.del_worksheet(ws_full)
                messages.append(
                    f"Onglet doublon vide supprimé : {full!r} (canonique : {acr!r}, {n_acr} ligne(s))."
                )
                existing.pop(full, None)
            except Exception as ex:
                messages.append(f"Impossible de supprimer {full!r} : {ex}")
        elif n_acr > 0:
            messages.append(
                f"Attention : {full!r} ({n_full} ligne(s)) et {acr!r} ({n_acr} ligne(s)) coexistent. "
                f"L'application utilise {acr!r} — vérifiez puis supprimez {full!r} si obsolète."
            )
    return messages


BASE_COLUMNS = [
    "row_id",
    "entity_id",
    "version",
    "status",
    "created_at",
]

# Valeurs affichées dans les onglets Sheets (alignement produit)
SHEETS_ROW_STATUS_ACTIVE = "Actif"
SHEETS_ROW_STATUS_INACTIVE = "Inactif"

_SHEETS_STATUS_INACTIVE_ALIASES: frozenset[str] = frozenset(
    {
        "inactif",
        "inactive",
        "deleted",
        "supprimé",
        "supprime",
        "obsolete",
        "obsolète",
        "archived",
        "archivé",
        "archive",
    }
)


def normalize_row_status_for_write(raw: object, *, default: str = SHEETS_ROW_STATUS_ACTIVE) -> str:
    """
    Canonicalise une valeur de colonne ``status`` (BASE_COLUMNS) avant écriture : **Actif** ou **Inactif**.
    Accepte encore les anciennes formes (`active`, `inactive`, vide, etc.).
    """

    def _nz(s: object) -> str:
        return str(s or "").strip().lower()

    s = _nz(raw)
    if not s:
        return default
    if s.startswith("inactif") or s.startswith("inactive"):
        return SHEETS_ROW_STATUS_INACTIVE
    if s in _SHEETS_STATUS_INACTIVE_ALIASES:
        return SHEETS_ROW_STATUS_INACTIVE
    if s in ("actif", "active", "true", "1", "oui", "yes", "enabled", "on", "ok"):
        return SHEETS_ROW_STATUS_ACTIVE
    # Valeur inhabituelle : on conserve **Actif** par défaut (moins cassant pour des typos légers)
    return SHEETS_ROW_STATUS_ACTIVE


def sheet_row_status_is_live(raw: object) -> bool:
    """
    Une ligne Sheets est utilisée tant que ``status`` (ou colonne équivalente **Statut**)
    n’indique pas inactif / supprimé. Vide = actif (lignes historiques avant ``Actif``/``Inactif``).
    """

    s = str(raw or "").strip().lower()
    if not s:
        return True
    if s.startswith("inactif") or s.startswith("inactive"):
        return False
    return s not in _SHEETS_STATUS_INACTIVE_ALIASES and s not in ("false", "0", "no", "non", "off")


def with_concat(columns: list[str]) -> list[str]:
    # Contrainte utilisateur: une colonne concat qui concatène tous les champs d'avant.
    return [*columns, "concat"]


def liturgy_illustrations_table_spec() -> TableSpec:
    """Métadonnées et légendes des visuels dominicaux (alias logique ``liturgy_illustrations`` / ILUS)."""
    return TableSpec(
        name="liturgy_illustrations",
        columns=with_concat(
            [
                *BASE_COLUMNS,
                "date",
                "zone",
                "gcs_path",
                "description_illustration",
                "gen_entity_id",
                "caption_source",
                "caption_model",
            ]
        ),
    )


def default_tables() -> list[TableSpec]:
    return [
        TableSpec(
            name="AliasTables",
            columns=[
                "#ID",
                "Statut",
                "Version",
                "Nom Complet Table",
                "Acronyme Table",
                "Description",
            ],
        ),
        TableSpec(
            name="audiences",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "audience_key",
                    "libelle",
                    "description",
                    "spec_aide",
                ]
            ),
        ),
        TableSpec(
            name="users",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "email",
                    "first_name",
                    "last_name",
                    "phone_e164",
                    "country",
                    "source",
                    "password_salt_b64",
                    "password_hash_b64",
                ]
            ),
        ),
        TableSpec(
            name="subscriptions",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "user_entity_id",
                    "type",
                    "zone",
                    "length_pref",
                    "opt_in",
                    "active",
                ]
            ),
        ),
        TableSpec(
            name="password_resets",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "email",
                    "token_hash",
                    "expires_at",
                    "used",
                ]
            ),
        ),
        TableSpec(
            name="email_templates",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "template_key",
                    "channel",
                    "language",
                    "subject",
                    "body",
                    "active",  # colonne facultative ; le choix de version template côté app repose uniquement sur `status`
                    "status_note",
                ]
            ),
        ),
        # Scheduler (noms logiques → CMPG / RUNS via AliasTables)
        TableSpec(
            name="scheduler_campaigns",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "name",
                    "enabled",
                    "timezone",
                    "schedule_kind",  # manual|weekly|daily
                    "schedule_spec",  # ex: "fri 19:00"
                    "audience_kind",  # ex: "weekly_friday_optin"
                    "audience_spec",  # json/text (futur)
                    "send_email",
                    "send_sms",
                    "email_template_key",
                    "sms_template_key",
                    "content_pdf",
                    "content_audio",
                    "content_illustration",
                    "content_app_link",
                ]
            ),
        ),
        TableSpec(
            name="scheduler_runs",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "run_kind",  # manual|scheduled
                    "status_detail",
                    "started_at",
                    "finished_at",
                    "recipients_ok",
                    "recipients_err",
                    "error",
                ]
            ),
        ),
        TableSpec(
            name="experience_feedback",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "submitter_email",
                    "emotion_global",
                    "rating_illustration",
                    "rating_synthesis",
                    "rating_audio",
                    "utility_liturgy",
                    "touch_memorable",
                    "wish_improve_one",
                    "campaign_hint",
                    "date_dimanche_hint",
                    "source_route",
                ]
            ),
        ),
        TableSpec(
            name="feedback_insights",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "n_sample",
                    "bundle_sha256",
                    "model_used",
                    "synthesis_text",
                ]
            ),
        ),
        TableSpec(
            name="outbound_messages",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "channel",
                    "template_key",
                    "user_entity_id",
                    "email",
                    "phone_e164",
                    "date_dimanche",
                    "status_detail",
                    "scheduled_at",
                    "sent_at",
                    "error",
                ]
            ),
        ),
        TableSpec(
            name="liturgy_fetches",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "date",
                    "zone",
                    "endpoint",
                    "response_hash",
                    "raw_gcs_path",
                ]
            ),
        ),
        TableSpec(
            name="generations",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "date",
                    "zone",
                    "cycle",
                    "season",
                    "length",
                    "prompt_version",
                    "model",
                    "source_hash",
                    "text_gcs_path",
                    "duration_text_s",
                    "duration_text_retry_s",
                    "duration_upload_text_s",
                    "text_words",
                ]
            ),
        ),
        TableSpec(
            name="Paramètres_IA",
            columns=[
                "#ID",
                "Clé_Prompt",
                "Description",
                "Version",
                "Statut",
                "Date_Effet",
                "Contenu_Markdown",
                "Concaténation",
            ],
        ),
        TableSpec(
            name="Voix_Audio",
            columns=[
                "#ID",
                "Statut",
                "Version",
                "Date_Effet",
                "Cible",
                "Couleur",
                "Temps_Liturgique",
                "Voix",
                "Description",
                "Concaténation",
            ],
        ),
        TableSpec(
            name="audio",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "gen_entity_id",
                    "voice",
                    "format",
                    "gcs_path",
                    "kind",
                    "duration_tts_s",
                    "duration_upload_s",
                    "tts_route",
                ]
            ),
        ),
        liturgy_illustrations_table_spec(),
        TableSpec(
            name="pdf_exports",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "range_start",
                    "range_end",
                    "zone",
                    "gcs_path",
                    "date_semaine_liturgique",
                    "gen_entity_id",
                    "kind",
                    "duration_build_s",
                ]
            ),
        ),
        TableSpec(
            name="admin_changelog",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "title",
                    "detail",
                ]
            ),
        ),
        TableSpec(
            name="memos",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "user_entity_id",
                    "date",
                    "zone",
                    "title",
                    "resolution",
                    "memo_gcs_path",
                    "gen_entity_id",
                ]
            ),
        ),
        TableSpec(
            name="vision_text_audit",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "run_id",
                    "date",
                    "gcs_path",
                    "min_chars",
                    "detected_text",
                    "detected_text_chars",
                    "detected_text_alpha_chars",
                    "has_meaningful_text",
                    "error",
                ]
            ),
        ),
        TableSpec(
            name="vision_text_corrections",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "audit_entity_id",
                    "run_id",
                    "date",
                    "gcs_path",
                    "replace_from",
                    "replace_to",
                    "status_detail",
                    "vertex_model",
                    "result_mime",
                    "result_gcs_path",
                    "thumb_gcs_path",
                    "error",
                ]
            ),
        ),
        TableSpec(
            name="vision_text_whitelist",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "date",
                    "gcs_path",
                    "reason",
                ]
            ),
        ),
        TableSpec(
            name="readings_cache",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "date",
                    "zone",
                    "periode",
                    "semaine",
                    "annee",
                    "couleur",
                    "fete",
                    "jour_liturgique_nom",
                    "premiere_lecture",
                    "psaume",
                    "deuxieme_lecture",
                    "evangile",
                    "source",
                    "error",
                ]
            ),
        ),
    ]


def get_table_spec(name: str) -> TableSpec:
    """Retourne le ``TableSpec`` du registre ``default_tables()`` (nom logique)."""
    n = str(name or "").strip()
    for t in default_tables():
        if t.name == n:
            return t
    raise KeyError(f"TableSpec introuvable: {name!r}")


def build_gspread_client(service_account_info: Mapping[str, Any]) -> gspread.Client:
    info = dict(service_account_info or {})
    email = str(info.get("client_email") or "").strip()
    pk = str(info.get("private_key") or "").strip()
    if not email or not pk:
        raise ValueError(
            "Secrets `gcp_service_account` incomplets (`client_email` ou `private_key` manquant). "
            "Sur Streamlit Cloud, vérifiez la section `[gcp_service_account]` dans les secrets "
            "(clé privée multiligne entre triples guillemets)."
        )
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def ensure_database(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    tables: Iterable[TableSpec],
) -> None:
    sh = open_spreadsheet(gspread_client, spreadsheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}

    for t in tables:
        name = _resolve_table_name(sh=sh, table=t.name)
        ws = existing.get(name) or sh.add_worksheet(title=name, rows=2000, cols=max(10, len(t.columns) + 2))
        _ensure_header(ws, t.columns)


def ensure_table(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    table: TableSpec,
) -> None:
    """Crée l'onglet si absent et pose le header. Utile en runtime (admin) sans relancer init_sheets_db."""
    sh = open_spreadsheet(gspread_client, spreadsheet_id)
    name = _resolve_table_name(sh=sh, table=table.name)
    try:
        ws = sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=2000, cols=max(10, len(table.columns) + 2))
    _ensure_header(ws, table.columns)


def _ensure_header(ws: gspread.Worksheet, header: list[str]) -> None:
    first_row = ws.row_values(1)
    if first_row and [c.strip() for c in first_row if c.strip()]:
        # Si déjà initialisé : ajoute les colonnes manquantes sans écraser l’en-tête existant.
        missing = [c for c in header if c not in first_row]
        if missing:
            ws.update([first_row + missing], "A1")
        return
    ws.update([header], "A1")


def make_row(values_by_col: Mapping[str, Any], *, status: str = SHEETS_ROW_STATUS_ACTIVE, version: int = 1) -> dict[str, Any]:
    row_id = str(uuid4())
    entity_id = str(values_by_col.get("entity_id") or uuid4())
    created_at = utc_now_iso()

    raw_stat = values_by_col.get("status")
    eff_status = status if raw_stat is None or str(raw_stat).strip() == "" else raw_stat
    status_default = normalize_row_status_for_write(status)

    row: dict[str, Any] = {
        "row_id": row_id,
        "entity_id": entity_id,
        "version": int(values_by_col.get("version") or version),
        "status": normalize_row_status_for_write(eff_status, default=status_default),
        "created_at": str(values_by_col.get("created_at") or created_at),
    }
    for k, v in values_by_col.items():
        if k in row:
            continue
        row[k] = v

    return row


def compute_concat(row: Mapping[str, Any], *, header: list[str]) -> str:
    # "concat" = concaténation de toutes les colonnes précédentes dans le header.
    parts: list[str] = []
    for col in header:
        if col == "concat":
            break
        v = row.get(col, "")
        s = str(v).strip() if v is not None else ""
        if s:
            parts.append(s)
    return " | ".join(parts)


def append_immutable_row(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    table: str,
    values_by_col: Mapping[str, Any],
    status: str = SHEETS_ROW_STATUS_ACTIVE,
    version: int = 1,
) -> dict[str, Any]:
    sh = open_spreadsheet(gspread_client, spreadsheet_id)
    ws = sh.worksheet(_resolve_table_name(sh=sh, table=table))
    header = ws.row_values(1)
    if not header:
        raise RuntimeError(f"Table '{table}' non initialisée (header vide).")

    row = make_row(values_by_col, status=status, version=version)
    row = dict(row)
    row["concat"] = compute_concat(row, header=header)

    ordered = [row.get(c, "") for c in header]
    _append_rows_with_retry(ws, [ordered])
    invalidate_fetch_records_cache(spreadsheet_id=spreadsheet_id, table=table)
    return row


def _append_rows_with_retry(
    ws: gspread.Worksheet,
    rows: list[list[Any]],
    *,
    max_retries: int = 6,
    base_sleep_s: float = 1.2,
) -> None:
    """
    Limite Sheets : "Write requests per minute per user" → 429.
    On groupe les écritures et on retry avec backoff.
    """
    last_ex: Exception | None = None
    for i in range(max(1, int(max_retries))):
        try:
            ws.append_rows(rows, value_input_option="RAW")
            return
        except Exception as ex:
            last_ex = ex
            msg = str(ex)
            if "429" not in msg and "Quota exceeded" not in msg:
                raise
            time.sleep(base_sleep_s * (2**i))
    if last_ex:
        raise last_ex


def append_immutable_rows_bulk(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    table: str,
    values_by_col_list: list[Mapping[str, Any]],
    status: str = SHEETS_ROW_STATUS_ACTIVE,
    version: int = 1,
    chunk_size: int = 120,
) -> int:
    """Append-only en lots : 1 requête / chunk (évite les quotas). Retourne le nombre de lignes ajoutées."""
    if not values_by_col_list:
        return 0
    sh = open_spreadsheet(gspread_client, spreadsheet_id)
    ws = sh.worksheet(_resolve_table_name(sh=sh, table=table))
    header = ws.row_values(1)
    if not header:
        raise RuntimeError(f"Table '{table}' non initialisée (header vide).")

    rows_payload: list[list[Any]] = []
    for values_by_col in values_by_col_list:
        row = make_row(values_by_col, status=status, version=version)
        row = dict(row)
        row["concat"] = compute_concat(row, header=header)
        rows_payload.append([row.get(c, "") for c in header])

    added = 0
    step = max(1, int(chunk_size))
    for i in range(0, len(rows_payload), step):
        chunk = rows_payload[i : i + step]
        _append_rows_with_retry(ws, chunk)
        added += len(chunk)
    invalidate_fetch_records_cache(spreadsheet_id=spreadsheet_id, table=table)
    return added


def fetch_records(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    table: str,
    limit: int = 500,
    use_cache: bool = False,
) -> list[dict[str, Any]]:
    sid = str(spreadsheet_id or "").strip()
    tbl = str(table or "").strip()
    lim_key: int | None = None if limit is None or limit <= 0 else int(limit)
    now = time.time()
    cache_key = (sid, tbl, lim_key)
    if use_cache:
        cached = _FETCH_RECORDS_CACHE.get(cache_key)
        if cached and now - cached[0] < _SHEETS_CACHE_TTL_S:
            return list(cached[1])

    sh = open_spreadsheet(gspread_client, sid, use_cache=use_cache)
    ws = sh.worksheet(_resolve_table_name(sh=sh, table=tbl))
    # Important: preserve phone numbers like "+336..." and other identifiers as strings.
    # gspread may "numericise" values (cast to int/float) which would drop leading "+" / zeros.
    records = _gspread_call_with_retry(
        lambda: ws.get_all_records(numericise_ignore=["all"])
    )
    # limit<=0 ou None : conserve tout l’onglet (important pour tables append-only anciennes lignes en tête).
    if limit is None or limit <= 0:
        out = records
    elif len(records) > limit:
        out = records[-limit:]
    else:
        out = records

    if use_cache:
        _FETCH_RECORDS_CACHE[cache_key] = (now, list(out))
    return out

