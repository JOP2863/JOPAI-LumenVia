"""Caches Streamlit (@st.cache_data) pour données distantes (évite import circulaire app ↔ ui)."""

from __future__ import annotations

import json

import streamlit as st

from core.aelf import AelfClient
from core.config import load_config
from core.parametres_ia import pick_effective_templates
from core.prompt_template_keys import PROMPT_TEMPLATE_KEYS
from core.sheets_db import build_gspread_client, fetch_records


@st.cache_data(ttl=75, max_entries=40, show_spinner=False)
def adm_feedback_sheet_fetch_cached(
    spreadsheet_id: str,
    table: str,
    limit: int,
    service_account_json: str,
) -> list[dict]:
    """Court TTL : les reruns Streamlit (expanders, widgets) ne refont pas un aller-retour Sheets à chaque fois."""
    info = json.loads(service_account_json)
    gs = build_gspread_client(info)
    return fetch_records(
        gspread_client=gs,
        spreadsheet_id=spreadsheet_id,
        table=table,
        limit=limit,
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_prompt_templates_cached(*, gsheet_id: str, service_account_fingerprint: str) -> dict[str, str]:
    """
    Charge les prompts IA depuis Google Sheets (onglet `Paramètres_IA`, standard MARPA).
    Cache court pour éviter de relire Sheets à chaque run Streamlit.
    """
    if not gsheet_id:
        return {}
    _ = service_account_fingerprint

    cfg = load_config()
    if not cfg.gcp_service_account:
        return {}

    gs = build_gspread_client(cfg.gcp_service_account)
    rows = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="Paramètres_IA", limit=5000)
    latest = pick_effective_templates(rows, allowed_keys=set(PROMPT_TEMPLATE_KEYS))
    out = {k: v.content_md for k, v in latest.items() if k in PROMPT_TEMPLATE_KEYS and v.content_md}
    try:
        from core.tts_pronunciation import refresh_tts_pronunciation_overrides_from_templates

        refresh_tts_pronunciation_overrides_from_templates(out)
    except Exception:
        pass
    return out


@st.cache_data(ttl=300, show_spinner=False)
def load_voix_rules_cached(*, gsheet_id: str, service_account_fingerprint: str) -> list[dict]:
    """Règles de voix TTS (`Voix_Audio` / VOIX)."""
    if not gsheet_id:
        return []
    _ = service_account_fingerprint
    cfg = load_config()
    if not cfg.gcp_service_account:
        return []
    gs = build_gspread_client(cfg.gcp_service_account)
    try:
        return fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="Voix_Audio", limit=0)
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=3600)
def cached_aelf(date_str: str, zone: str = "france", *, _identity_schema: int = 4):
    """_identity_schema invalide le cache quand le dataclass AelfDayIdentity évolue."""
    c = AelfClient()
    identity = c.informations(date_str, zone=zone)
    texts = c.messes(date_str, zone=zone)
    return identity, texts
