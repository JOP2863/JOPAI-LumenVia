from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping

from google.cloud import storage, vision


def existing_illustration_blob_path(
    *,
    gcs: storage.Client,
    bucket_name: str,
    target: Mapping[str, Any],
) -> str | None:
    """Premier chemin manifeste (primary puis alternates) qui existe sur le bucket."""
    from core.storage import blob_exists

    cand: list[str] = []
    p0 = str(target.get("gcs_path_primary") or "").strip()
    if p0:
        cand.append(p0)
    for a in target.get("alternates") or []:
        s = str(a or "").strip()
        if s:
            cand.append(s)
    for path in cand:
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                return path
        except Exception:
            continue
    return None


def detect_text_in_image_bytes(
    *,
    image_bytes: bytes,
    client: vision.ImageAnnotatorClient,
) -> str:
    """
    Texte détecté dans l'image (Vision TEXT_DETECTION).
    Chaîne vide si aucun glyphe exploitable.
    """
    if not image_bytes:
        return ""
    img = vision.Image(content=image_bytes)
    resp = client.text_detection(image=img)
    if resp.error.message:
        raise RuntimeError(resp.error.message)
    anns = resp.text_annotations
    if not anns:
        return ""
    # Premier élément = bloc complet ; les suivants sont des fragments avec bounding boxes.
    return str(anns[0].description or "").strip()


_WS_RE = re.compile(r"\s+")


def _normalize_for_len(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip())


def image_has_meaningful_text(*, raw_text: str, min_chars: int) -> bool:
    t = _normalize_for_len(raw_text)
    return len(t) >= max(1, int(min_chars))


def audit_targets_for_text(
    *,
    gcs: storage.Client,
    bucket_name: str,
    targets: list[dict],
    vision_client: vision.ImageAnnotatorClient,
    max_workers: int = 8,
    min_chars: int = 2,
    download_bytes_fn=None,
) -> list[dict]:
    """
    Pour chaque cible avec fichier sur GCS : télécharge l'image et appelle Vision.

    Retourne une liste de dicts :
      date, gcs_path, has_text, detected_text, error

    `download_bytes_fn` injectable pour les tests (sinon `core.storage.download_bytes`).
    """
    from core.storage import download_bytes as _dl

    dl = download_bytes_fn or _dl

    rows_in: list[tuple[str, str, dict]] = []
    for t in targets:
        ds = str(t.get("date") or "").strip()[:10]
        bp = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
        if not bp:
            continue
        rows_in.append((ds, bp, t))

    out: list[dict] = []

    def job(ds: str, path: str, target: dict) -> dict:
        err: str | None = None
        txt = ""
        try:
            data = dl(gcs=gcs, bucket_name=bucket_name, path=path)
            txt = detect_text_in_image_bytes(image_bytes=data, client=vision_client)
        except Exception as ex:
            err = str(ex)
        ht = bool(not err and image_has_meaningful_text(raw_text=txt, min_chars=min_chars))
        return {
            "date": ds,
            "gcs_path": path,
            "target": target,
            "has_text": ht,
            "detected_text": txt if ht else "",
            "error": err,
        }

    if not rows_in:
        return []

    workers = max(1, min(int(max_workers), len(rows_in)))
    if workers == 1:
        for ds, bp, t in rows_in:
            out.append(job(ds, bp, t))
        return out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {ex.submit(job, ds, bp, t): (ds, bp) for ds, bp, t in rows_in}
        for fut in as_completed(fut_map):
            out.append(fut.result())

    out.sort(key=lambda r: str(r.get("date") or ""))
    return out


def filter_rows_with_text(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("has_text")]


_ACTIVATION_URL_RE = re.compile(
    r"https://console\.(?:developers\.google\.com|cloud\.google\.com)[^\s\"'<>]+",
    re.I,
)


def is_vision_service_disabled_error(message: str | None) -> bool:
    """403 API désactivée / jamais activée sur le projet GCP."""
    if not message:
        return False
    m = message.lower()
    if "service_disabled" in m:
        return True
    if "403" in message and "vision" in m and ("disabled" in m or "not been used" in m):
        return True
    return False


def extract_console_url_from_error(message: str | None) -> str | None:
    if not message:
        return None
    mm = _ACTIVATION_URL_RE.search(message)
    return mm.group(0).rstrip(").',") if mm else None


def shorten_audit_error_message(message: str | None, max_len: int = 240) -> str:
    """Évite les tableaux illisibles avec des protobuf géants."""
    if not message:
        return ""
    if is_vision_service_disabled_error(message):
        return (
            "API Cloud Vision désactivée sur ce projet GCP. Active « Cloud Vision API » dans Google Cloud Console, "
            "attends quelques minutes, puis relance l’analyse."
        )
    one = " ".join(str(message).split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def all_errors_are_vision_service_disabled(rows: list[dict]) -> bool:
    errs = [r for r in rows if r.get("error")]
    if not errs:
        return False
    return all(is_vision_service_disabled_error(str(r.get("error") or "")) for r in errs)
