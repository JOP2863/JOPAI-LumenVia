"""
Copie locale de la dernière synthèse + audio pour un dimanche donné (zone).

Permet de réécouter / relire hors ligne après une première consultation ou génération,
sans joindre Sheets ni GCS (protocole « lectionnaire de poche » étendu).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_CACHE_ROOT = Path(".cache") / "lumenvia"


def _safe_zone(zone: str) -> str:
    return str(zone or "france").replace("/", "_").replace("\\", "_")


def _stem(date_str: str, zone: str) -> str:
    ds = str(date_str).strip()[:10]
    return f"sunday_bundle_{ds}_{_safe_zone(zone)}"


def _audio_extension(mime: str | None) -> str:
    m = (mime or "").lower()
    if "mpeg" in m or "mp3" in m:
        return "mp3"
    if "wav" in m:
        return "wav"
    return "bin"


def persist_sunday_bundle(
    *,
    date_str: str,
    zone: str,
    synth_text: str | None,
    audio_bytes: bytes | None,
    audio_mime: str | None,
) -> None:
    """Enregistre synthèse (.txt) + audio (binaire) + métadonnées JSON."""
    root = _CACHE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    stem = _stem(date_str, zone)
    meta_path = root / f"{stem}.json"
    txt_path = root / f"{stem}.txt"

    text_ok = bool((synth_text or "").strip())
    audio_ok = bool(audio_bytes)

    if not text_ok and not audio_ok:
        return

    cached_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audio_filename: str | None = None
    if audio_ok and audio_bytes is not None:
        ext = _audio_extension(audio_mime)
        audio_filename = f"{stem}.{ext}"
        (root / audio_filename).write_bytes(audio_bytes)

    if text_ok:
        txt_path.write_text((synth_text or "").strip(), encoding="utf-8")

    payload = {
        "cached_at": cached_at,
        "date": str(date_str).strip()[:10],
        "zone": _safe_zone(zone),
        "synth_text_file": f"{stem}.txt" if text_ok else None,
        "audio_file": audio_filename,
        "audio_mime": (audio_mime or "").strip() or None,
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sunday_bundle(
    date_str: str,
    zone: str,
) -> tuple[str | None, bytes | None, str | None, str] | None:
    """
    Retourne (synthèse, audio_bytes, mime audio, cached_at) ou None si absent / illisible.
    """
    root = _CACHE_ROOT
    stem = _stem(date_str, zone)
    meta_path = root / f"{stem}.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    cached_at = str(meta.get("cached_at") or "")
    synth: str | None = None
    tf = meta.get("synth_text_file")
    if tf:
        p = root / str(tf)
        if p.is_file():
            try:
                synth = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                synth = None

    audio_bytes: bytes | None = None
    audio_mime: str | None = meta.get("audio_mime")
    af = meta.get("audio_file")
    if af:
        p = root / str(af)
        if p.is_file():
            try:
                audio_bytes = p.read_bytes()
            except Exception:
                audio_bytes = None

    if not (synth or "").strip() and not audio_bytes:
        return None

    return synth, audio_bytes, audio_mime, cached_at
