"""
Vignettes WebP dans GCS sous ``Images/thumbs/{année}/{date}.webp``,
dérivées des fichiers ``Images/illustrations/...`` pour accélérer l’affichage dans l’app.
"""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import storage

THUMB_GCS_PREFIX = "Images/thumbs"

_ILLUSTRATIONS_MARKER = "/illustrations/"


def gcs_thumb_path_from_source_blob(source_blob_path: str) -> str:
    """
    ``Images/illustrations/2026/2026-01-04.png`` → ``Images/thumbs/2026/2026-01-04.webp``
    """
    s = (source_blob_path or "").strip().replace("\\", "/")
    if _ILLUSTRATIONS_MARKER in s:
        tail = s.split(_ILLUSTRATIONS_MARKER, 1)[1]
    else:
        tail = s.split("/", 1)[-1] if "/" in s else s
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{THUMB_GCS_PREFIX}/{base}.webp"


def build_thumbnail_webp(image_bytes: bytes, *, max_side: int = 420, quality: int = 82) -> bytes:
    """Redimensionne (max côté) et encode en WebP."""
    from PIL import Image

    if not image_bytes:
        return b""
    im = Image.open(io.BytesIO(image_bytes))
    if im.mode in ("P", "PA"):
        im = im.convert("RGBA")
    elif im.mode == "RGB":
        pass
    elif im.mode == "RGBA":
        pass
    else:
        im = im.convert("RGB")
    w, h = im.size
    if w <= 0 or h <= 0:
        return b""
    scale = min(float(max_side) / float(w), float(max_side) / float(h), 1.0)
    if scale < 1.0:
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=int(quality), method=6)
    return buf.getvalue()


def thumb_blob_exists(
    *,
    gcs: storage.Client,
    bucket_name: str,
    source_blob_path: str,
) -> bool:
    from core.storage import blob_exists

    path = gcs_thumb_path_from_source_blob(source_blob_path)
    return blob_exists(gcs=gcs, bucket_name=bucket_name, path=path)


def generate_thumb_from_source_and_upload(
    *,
    gcs: storage.Client,
    bucket_name: str,
    source_blob_path: str,
    download_bytes_fn,
    upload_bytes_fn,
    max_side: int = 420,
) -> str:
    """Télécharge la source, produit la vignette, upload. Retourne le chemin thumb GCS."""
    data = download_bytes_fn(gcs=gcs, bucket_name=bucket_name, path=source_blob_path)
    thumb = build_thumbnail_webp(data, max_side=max_side)
    dest = gcs_thumb_path_from_source_blob(source_blob_path)
    upload_bytes_fn(
        gcs=gcs,
        bucket_name=bucket_name,
        path=dest,
        data=thumb,
        content_type="image/webp",
    )
    return dest


_PROJECT_RE = re.compile(r"project[=/](\d+)", re.I)


def extract_gcp_project_id_from_error(message: str | None) -> str | None:
    if not message:
        return None
    m = _PROJECT_RE.search(message)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{10,})\b", message)
    return m.group(1) if m else None


def vision_console_activation_url(project_id: str | None) -> str:
    pid = (project_id or "").strip()
    if not pid:
        pid = ""
    return f"https://console.cloud.google.com/apis/library/vision.googleapis.com?project={pid}"
