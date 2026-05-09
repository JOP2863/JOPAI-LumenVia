from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

# API publique AELF (pas de clé requise) — surcharge possible via secrets / env.
DEFAULT_AELF_BASE_URL = "https://api.aelf.org"


def resolve_aelf_base_url_from_mapping(data: Mapping[str, Any]) -> str:
    """Lit `AELF_BASE_URL` racine ou `[aelf].base_url` depuis un dict TOML déjà parsé."""
    v = str(data.get("AELF_BASE_URL") or "").strip()
    if v:
        return v.rstrip("/")
    sec = data.get("aelf")
    if isinstance(sec, dict):
        v2 = str(sec.get("base_url") or sec.get("BASE_URL") or "").strip()
        if v2:
            return v2.rstrip("/")
    return DEFAULT_AELF_BASE_URL


def resolve_aelf_base_url(*, toml_data: Mapping[str, Any] | None = None) -> str:
    """
    Ordre : variable d'environnement ``AELF_BASE_URL``, puis ``toml_data`` si fourni,
    puis ``st.secrets`` (Streamlit), sinon URL officielle par défaut.
    """
    env = str(os.environ.get("AELF_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    if toml_data is not None:
        return resolve_aelf_base_url_from_mapping(toml_data)
    try:
        import streamlit as st  # type: ignore

        s = st.secrets
        top = str(s.get("AELF_BASE_URL") or "").strip()
        if top:
            return top.rstrip("/")
        block = s.get("aelf")
        if isinstance(block, dict):
            bu = str(block.get("base_url") or block.get("BASE_URL") or "").strip()
            if bu:
                return bu.rstrip("/")
    except Exception:
        pass
    return DEFAULT_AELF_BASE_URL

try:
    import tomllib  # py311+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

try:  # py<311
    import tomli  # type: ignore
except Exception:  # pragma: no cover
    tomli = None  # type: ignore[assignment]


@dataclass(frozen=True)
class AppConfig:
    gsheet_id: str
    gcs_bucket_name: str
    gemini_api_key: str | None
    openai_api_key: str | None
    gcp_service_account: Mapping[str, Any]
    aelf_base_url: str


def load_config() -> AppConfig:
    try:
        import streamlit as st  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "Streamlit n'est pas installé dans cet environnement. "
            "Pour exécuter l'app, installe les dépendances (dont streamlit). "
            "Pour les scripts tools/*, utilise plutôt load_config_from_secrets_toml(...)."
        ) from e

    s = st.secrets
    return AppConfig(
        gsheet_id=str(s.get("gsheet_id", "")).strip(),
        gcs_bucket_name=str(s.get("gcs_bucket_name", "")).strip(),
        gemini_api_key=(str(s.get("GEMINI_API_KEY")).strip() if s.get("GEMINI_API_KEY") else None),
        openai_api_key=(str(s.get("OPENAI_API_KEY")).strip() if s.get("OPENAI_API_KEY") else None),
        gcp_service_account=dict(s.get("gcp_service_account", {})),
        aelf_base_url=resolve_aelf_base_url(),
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
        aelf_base_url=resolve_aelf_base_url(toml_data=data),
    )

