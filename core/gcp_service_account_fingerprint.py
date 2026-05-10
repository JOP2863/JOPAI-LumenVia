"""Empreinte stable du compte de service GCP (invalidation de caches)."""

from __future__ import annotations

from core.prompt_templates import compute_sha256_text


def service_account_fingerprint(sa: object) -> str:
    try:
        d = dict(sa or {})
        stable = "|".join(
            [
                str(d.get("project_id") or ""),
                str(d.get("client_email") or ""),
                str(d.get("private_key_id") or ""),
            ]
        )
        return compute_sha256_text(stable)[:16]
    except Exception:
        return "na"
