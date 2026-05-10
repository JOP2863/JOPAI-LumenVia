"""URL signées GCS (V4) — partagé par l’e-mail hebdo, la page Dimanche, etc."""

from __future__ import annotations


def gcs_signed_url(
    *,
    gcs: object,
    bucket_name: str,
    path: str,
    expires_s: int = 7 * 24 * 3600,
) -> str | None:
    """URL signée (V4) pour accès anonyme temporaire à un objet privé."""
    try:
        bucket = gcs.bucket(bucket_name)
        blob = bucket.blob(path)
        if not blob.exists():
            return None
        return blob.generate_signed_url(
            version="v4",
            expiration=int(expires_s),
            method="GET",
        )
    except Exception:
        return None


def gcs_first_signed_url(
    *,
    gcs: object,
    bucket_name: str,
    candidate_paths: list[str],
    expires_s: int = 7 * 24 * 3600,
) -> str | None:
    for p in candidate_paths:
        path = str(p or "").strip()
        if not path:
            continue
        u = gcs_signed_url(gcs=gcs, bucket_name=bucket_name, path=path, expires_s=expires_s)
        if u:
            return u
    return None
