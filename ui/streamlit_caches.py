"""Caches Streamlit (@st.cache_data) pour données distantes (évite import circulaire app ↔ ui)."""

from __future__ import annotations

import json

import streamlit as st

from dataclasses import asdict

from core.aelf import AelfDayIdentity, AelfTexts, fetch_aelf_day
from core.config import load_config
from core.parametres_ia import pick_effective_templates
from core.prompt_template_keys import PROMPT_TEMPLATE_KEYS
from core.sheets_db import build_gspread_client, fetch_records


def service_account_json_fingerprint(service_account_info: dict | None) -> str:
    """Clé stable pour @st.cache_data (ne jamais passer le dict brut à cache_data)."""
    if not service_account_info:
        return ""
    return json.dumps(service_account_info, sort_keys=True, ensure_ascii=False)


@st.cache_data(ttl=90, max_entries=64, show_spinner=False)
def adm_sheets_fetch_cached(
    spreadsheet_id: str,
    table: str,
    limit: int,
    service_account_json: str,
) -> list[dict]:
    """
    Court TTL : les reruns Streamlit (widgets, expanders) ne relisent pas Sheets à chaque fois.
    ``limit <= 0`` = onglet entier (comme ``fetch_records``).
    """
    if not spreadsheet_id or not service_account_json:
        return []
    info = json.loads(service_account_json)
    gs = build_gspread_client(info)
    return fetch_records(
        gspread_client=gs,
        spreadsheet_id=spreadsheet_id,
        table=table,
        limit=limit,
        use_cache=True,
    )


def invalidate_adm_sheets_fetch_cache() -> None:
    """À appeler après une écriture Sheets depuis l’admin (template, etc.)."""
    adm_sheets_fetch_cached.clear()


@st.cache_data(ttl=75, max_entries=40, show_spinner=False)
def adm_feedback_sheet_fetch_cached(
    spreadsheet_id: str,
    table: str,
    limit: int,
    service_account_json: str,
) -> list[dict]:
    """Court TTL : les reruns Streamlit (expanders, widgets) ne refont pas un aller-retour Sheets à chaque fois."""
    return adm_sheets_fetch_cached(spreadsheet_id, table, limit, service_account_json)


@st.cache_data(ttl=300, show_spinner=False)
def load_prompt_templates_cached(*, gsheet_id: str, service_account_fingerprint: str) -> dict[str, str]:
    """
    Charge les prompts IA depuis Google Sheets (onglet `Paramètres_IA`, standard MARPA).
    Cache court pour éviter de relire Sheets à chaque run Streamlit.
    """
    if not gsheet_id:
        return {}
    _ = service_account_fingerprint

    sa_json = service_account_fingerprint
    if not sa_json:
        cfg = load_config()
        sa_json = service_account_json_fingerprint(cfg.gcp_service_account)
    if not sa_json:
        return {}
    rows = adm_sheets_fetch_cached(gsheet_id, "Paramètres_IA", 5000, sa_json)
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
    sa_json = service_account_fingerprint
    if not sa_json:
        cfg = load_config()
        sa_json = service_account_json_fingerprint(cfg.gcp_service_account)
    if not sa_json:
        return []
    try:
        return adm_sheets_fetch_cached(gsheet_id, "Voix_Audio", 0, sa_json)
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=3600)
def _cached_aelf_raw(date_str: str, zone: str = "france", *, _identity_schema: int = 4) -> tuple[dict, dict]:
    """Retour dict (pickle-safe) ; _identity_schema invalide le cache si le schéma AELF évolue."""
    identity, texts = fetch_aelf_day(date_str, zone=zone)
    return asdict(identity), asdict(texts)


def cached_aelf(date_str: str, zone: str = "france", *, _identity_schema: int = 4) -> tuple[AelfDayIdentity, AelfTexts]:
    id_d, txt_d = _cached_aelf_raw(date_str, zone=zone, _identity_schema=_identity_schema)
    return AelfDayIdentity(**id_d), AelfTexts(**txt_d)
