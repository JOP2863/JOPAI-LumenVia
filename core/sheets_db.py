from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

import gspread
from google.oauth2 import service_account


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: list[str]


BASE_COLUMNS = [
    "row_id",
    "entity_id",
    "version",
    "status",
    "created_at",
]


def with_concat(columns: list[str]) -> list[str]:
    # Contrainte utilisateur: une colonne concat qui concatène tous les champs d'avant.
    return [*columns, "concat"]


def default_tables() -> list[TableSpec]:
    return [
        TableSpec(
            name="users",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "email",
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
                    "active",
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
                ]
            ),
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
                ]
            ),
        ),
        TableSpec(
            name="pdf_exports",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "range_start",
                    "range_end",
                    "zone",
                    "gcs_path",
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
    ]


def build_gspread_client(service_account_info: Mapping[str, Any]) -> gspread.Client:
    creds = service_account.Credentials.from_service_account_info(
        dict(service_account_info),
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
    sh = gspread_client.open_by_key(spreadsheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}

    for t in tables:
        ws = existing.get(t.name) or sh.add_worksheet(title=t.name, rows=2000, cols=max(10, len(t.columns) + 2))
        _ensure_header(ws, t.columns)


def _ensure_header(ws: gspread.Worksheet, header: list[str]) -> None:
    first_row = ws.row_values(1)
    if first_row and [c.strip() for c in first_row if c.strip()]:
        # Si déjà initialisé, on ne casse pas: on vérifie juste que les colonnes attendues existent.
        missing = [c for c in header if c not in first_row]
        if missing:
            ws.update([header], "A1")
        return
    ws.update([header], "A1")


def make_row(values_by_col: Mapping[str, Any], *, status: str = "active", version: int = 1) -> dict[str, Any]:
    row_id = str(uuid4())
    entity_id = str(values_by_col.get("entity_id") or uuid4())
    created_at = utc_now_iso()

    row: dict[str, Any] = {
        "row_id": row_id,
        "entity_id": entity_id,
        "version": int(values_by_col.get("version") or version),
        "status": str(values_by_col.get("status") or status),
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
    status: str = "active",
    version: int = 1,
) -> dict[str, Any]:
    sh = gspread_client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(table)
    header = ws.row_values(1)
    if not header:
        raise RuntimeError(f"Table '{table}' non initialisée (header vide).")

    row = make_row(values_by_col, status=status, version=version)
    row = dict(row)
    row["concat"] = compute_concat(row, header=header)

    ordered = [row.get(c, "") for c in header]
    ws.append_row(ordered, value_input_option="RAW")
    return row


def fetch_records(
    *,
    gspread_client: gspread.Client,
    spreadsheet_id: str,
    table: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sh = gspread_client.open_by_key(spreadsheet_id)
    ws = sh.worksheet(table)
    records = ws.get_all_records()
    if limit and len(records) > limit:
        return records[-limit:]
    return records

