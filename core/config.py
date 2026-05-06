from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib  # py311+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

try:  # py<311
    import tomli  # type: ignore
except Exception:  # pragma: no cover
    tomli = None  # type: ignore[assignment]

import streamlit as st


@dataclass(frozen=True)
class AppConfig:
    gsheet_id: str
    gcs_bucket_name: str
    gemini_api_key: str | None
    openai_api_key: str | None
    gcp_service_account: Mapping[str, Any]


def load_config() -> AppConfig:
    s = st.secrets
    return AppConfig(
        gsheet_id=str(s.get("gsheet_id", "")).strip(),
        gcs_bucket_name=str(s.get("gcs_bucket_name", "")).strip(),
        gemini_api_key=(str(s.get("GEMINI_API_KEY")).strip() if s.get("GEMINI_API_KEY") else None),
        openai_api_key=(str(s.get("OPENAI_API_KEY")).strip() if s.get("OPENAI_API_KEY") else None),
        gcp_service_account=dict(s.get("gcp_service_account", {})),
    )


def load_config_from_secrets_toml(secrets_toml_path: str | Path) -> AppConfig:
    p = Path(secrets_toml_path)
    raw = p.read_bytes()
    if tomllib is not None:
        data = tomllib.loads(raw.decode("utf-8"))
    elif tomli is not None:
        data = tomli.loads(raw.decode("utf-8"))
    else:
        raise RuntimeError("Aucun parseur TOML disponible (installe tomli ou utilise Python >= 3.11).")
    return AppConfig(
        gsheet_id=str(data.get("gsheet_id", "")).strip(),
        gcs_bucket_name=str(data.get("gcs_bucket_name", "")).strip(),
        gemini_api_key=(str(data.get("GEMINI_API_KEY")).strip() if data.get("GEMINI_API_KEY") else None),
        openai_api_key=(str(data.get("OPENAI_API_KEY")).strip() if data.get("OPENAI_API_KEY") else None),
        gcp_service_account=dict(data.get("gcp_service_account", {}) or {}),
    )

