"""Import bibliothèque audio (catalogue → téléchargement → GCS → AAMB)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

from core.audio_ambiance import (
    AUDIO_AMBIANCE_TABLE,
    ROLES,
    ambiance_gcs_path,
    wav_duration_seconds,
)
from core.sheets_db import (
    append_immutable_row,
    sheet_row_status_is_live,
    utc_now_iso,
)
from core.storage import upload_bytes

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CATALOG = _REPO_ROOT / "data" / "audio_seed_catalog_v1.json"
_UA = "JOPAI-LumenVia-AudioImport/1.0 (admin curated library)"
ProgressCb = Callable[[str], None]


@dataclass
class CatalogItem:
    id: str
    title: str
    role: str
    cible: str
    langue: str
    licence: str
    attribution: str
    url: str
    temps_liturgique: str = ""
    notes: str = ""
    duration_hint_s: float | None = None


@dataclass
class ImportItemResult:
    catalog_id: str
    title: str
    status: str  # ok | skip | error
    detail: str = ""
    gcs_path: str = ""
    entity_id: str = ""
    duration_s: float | None = None
    source_resolved: str = ""


@dataclass
class ImportReport:
    started_at: str
    finished_at: str = ""
    catalog_path: str = ""
    results: list[ImportItemResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.status == "skip")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")


def load_seed_catalog(path: Path | None = None) -> list[CatalogItem]:
    p = path or _DEFAULT_CATALOG
    data = json.loads(p.read_text(encoding="utf-8"))
    items: list[CatalogItem] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in ROLES:
            continue
        items.append(
            CatalogItem(
                id=str(raw.get("id") or "").strip(),
                title=str(raw.get("title") or "").strip(),
                role=role,
                cible=str(raw.get("cible") or "both").strip().lower() or "both",
                langue=str(raw.get("langue") or "ALL").strip().upper() or "ALL",
                licence=str(raw.get("licence") or "CC0").strip(),
                attribution=str(raw.get("attribution") or "").strip(),
                url=str(raw.get("url") or "").strip(),
                temps_liturgique=str(raw.get("temps_liturgique") or "").strip(),
                notes=str(raw.get("notes") or "").strip(),
                duration_hint_s=_as_float(raw.get("duration_hint_s")),
            )
        )
    return items


def _as_float(v: object) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except Exception:
        return None


def _stable_entity_id(catalog_id: str, url: str) -> str:
    return hashlib.sha256(f"aamb_seed|{catalog_id}|{url}".encode("utf-8")).hexdigest()[:24]


def _existing_source_urls(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in rows or []:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        u = str(r.get("source_url") or "").strip()
        if u:
            out.add(u)
            continue
        notes = str(r.get("notes") or "")
        m = re.search(r"source_url=(\S+)", notes)
        if m:
            out.add(m.group(1).strip())
    return out


def _existing_entity_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(r.get("entity_id") or "").strip()
        for r in (rows or [])
        if sheet_row_status_is_live(r.get("status")) and str(r.get("entity_id") or "").strip()
    }


def resolve_download_url(page_url: str, *, freesound_api_key: str | None = None) -> tuple[str, str]:
    """Retourne ``(media_url, hint_ext)`` à partir d’une page Freesound / Commons."""
    url = str(page_url or "").strip()
    if not url:
        raise ValueError("URL vide")
    low = url.casefold()
    if "commons.wikimedia.org" in low or "wikipedia.org" in low:
        return _resolve_wikimedia(url)
    if "freesound.org" in low:
        return _resolve_freesound(url, api_key=freesound_api_key)
    ext = Path(urlparse(url).path).suffix.lstrip(".").lower() or "bin"
    return url, ext


def _resolve_wikimedia(page_url: str) -> tuple[str, str]:
    path = unquote(urlparse(page_url).path)
    m = re.search(r"/wiki/File:(.+)$", path, flags=re.I)
    if not m:
        raise ValueError(f"Page Wikimedia non reconnue : {page_url}")
    title = "File:" + m.group(1)
    api = "https://commons.wikimedia.org/w/api.php"
    r = requests.get(
        api,
        params={
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "format": "json",
        },
        headers={"User-Agent": _UA},
        timeout=45,
    )
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    for _pid, page in pages.items():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        media = str(infos[0].get("url") or "").strip()
        if not media:
            continue
        ext = Path(urlparse(media).path).suffix.lstrip(".").lower() or "bin"
        return media, ext
    raise RuntimeError(f"Fichier média introuvable pour {title}")


def _freesound_id(url: str) -> str | None:
    m = re.search(r"/s/(\d+)/?", url)
    if m:
        return m.group(1)
    m = re.search(r"/sounds/(\d+)/?", url)
    return m.group(1) if m else None


def _resolve_freesound(page_url: str, *, api_key: str | None) -> tuple[str, str]:
    sid = _freesound_id(page_url)
    if not sid:
        raise ValueError(f"ID Freesound introuvable dans {page_url}")
    key = (api_key or "").strip() or str(os.environ.get("FREESOUND_API_KEY") or "").strip()
    if key:
        meta = requests.get(
            f"https://freesound.org/apiv2/sounds/{sid}/",
            params={"token": key, "fields": "id,name,previews,download,type,filesize"},
            headers={"User-Agent": _UA},
            timeout=45,
        )
        if meta.status_code == 200:
            data = meta.json()
            previews = data.get("previews") or {}
            for k in ("preview-hq-ogg", "preview-hq-mp3", "preview-lq-ogg", "preview-lq-mp3"):
                u = str(previews.get(k) or "").strip()
                if u:
                    ext = "ogg" if "ogg" in k else "mp3"
                    return u, ext
    html = requests.get(page_url, headers={"User-Agent": _UA}, timeout=45)
    html.raise_for_status()
    text = html.text
    for pat in (
        rf"https://cdn\.freesound\.org/previews/\d+/{sid}[^\"'\s]+-hq\.ogg",
        rf"https://cdn\.freesound\.org/previews/\d+/{sid}[^\"'\s]+-hq\.mp3",
        rf"https://cdn\.freesound\.org/previews/\d+/{sid}[^\"'\s]+\.ogg",
        rf"https://cdn\.freesound\.org/previews/\d+/{sid}[^\"'\s]+\.mp3",
    ):
        m = re.search(pat, text, flags=re.I)
        if m:
            u = m.group(0)
            ext = "ogg" if u.lower().endswith(".ogg") else "mp3"
            return u, ext
    raise RuntimeError(
        f"Prévisualisation Freesound introuvable pour #{sid}. "
        "Ajoute `FREESOUND_API_KEY` dans les secrets si besoin."
    )


def download_media_bytes(
    url: str,
    *,
    timeout_s: float = 90.0,
    max_retries: int = 5,
) -> bytes:
    last_err: Exception | None = None
    for attempt in range(max(1, int(max_retries))):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": _UA, "Accept": "*/*"},
                timeout=timeout_s,
                allow_redirects=True,
            )
            if r.status_code == 429:
                wait_s = min(8.0 * (2**attempt), 60.0)
                ra = (r.headers or {}).get("Retry-After")
                try:
                    if ra:
                        wait_s = max(wait_s, float(ra))
                except Exception:
                    pass
                time.sleep(wait_s)
                last_err = requests.HTTPError(f"429 Too Many Requests for {url}")
                continue
            r.raise_for_status()
            data = r.content
            if not data:
                raise RuntimeError("Téléchargement vide")
            if data[:15].lstrip().lower().startswith(b"<!doctype") or data[:6].lower() == b"<html":
                raise RuntimeError("Réponse HTML au lieu d’un fichier audio")
            return data
        except Exception as ex:
            last_err = ex
            if attempt < max_retries - 1:
                time.sleep(min(3.0 * (2**attempt), 30.0))
                continue
            break
    raise RuntimeError(f"Téléchargement échoué : {last_err}")


def _find_ffmpeg() -> str | None:
    w = shutil.which("ffmpeg")
    if w:
        return w
    # WinGet (Gyan.FFmpeg) — utile si Streamlit a démarré avant la mise à jour du PATH.
    local = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if local.is_dir():
        matches = sorted(local.glob("Gyan.FFmpeg*/ffmpeg*/bin/ffmpeg.exe"))
        if matches:
            return str(matches[-1])
    return None


def convert_to_wav_pcm16(raw: bytes, *, src_ext: str) -> bytes:
    """Convertit en WAV PCM 16-bit via ffmpeg si disponible ; passe-plat si déjà WAV."""
    if raw[:4] == b"RIFF" and b"WAVE" in raw[:16]:
        return raw
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg introuvable — requis pour convertir MP3/OGG → WAV. "
            "Installe ffmpeg et redémarre Streamlit (intro/outro/bed exigent du WAV)."
        )
    suffix = "." + (src_ext or "bin").lstrip(".")
    with tempfile.TemporaryDirectory(prefix="lv_aamb_") as td:
        src = Path(td) / f"in{suffix}"
        dst = Path(td) / "out.wav"
        src.write_bytes(raw)
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "44100",
            str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not dst.is_file():
            err = (proc.stderr or proc.stdout or "")[-400:]
            raise RuntimeError(f"ffmpeg a échoué : {err}")
        out = dst.read_bytes()
        if out[:4] != b"RIFF":
            raise RuntimeError("Sortie ffmpeg non-WAV")
        return out


def import_catalog_to_aamb(
    *,
    gs: object,
    gcs: object,
    spreadsheet_id: str,
    bucket_name: str,
    existing_rows: list[dict[str, Any]],
    catalog_path: Path | None = None,
    freesound_api_key: str | None = None,
    force: bool = False,
    progress: ProgressCb | None = None,
) -> ImportReport:
    """
    Télécharge chaque entrée du catalogue, convertit (WAV pour mix TTS), upload GCS, append AAMB.
    Idempotent : saute si ``source_url`` ou ``entity_id`` déjà Actif (sauf ``force``).
    """
    started = utc_now_iso()
    catalog = load_seed_catalog(catalog_path)
    report = ImportReport(started_at=started, catalog_path=str(catalog_path or _DEFAULT_CATALOG))
    known_urls = _existing_source_urls(existing_rows)
    known_eids = _existing_entity_ids(existing_rows)

    def _prog(msg: str) -> None:
        if progress:
            progress(msg)

    for item in catalog:
        _prog(f"Traitement : {item.title}…")
        entity_id = _stable_entity_id(item.id or item.title, item.url)
        if not force and (item.url in known_urls or entity_id in known_eids):
            report.results.append(
                ImportItemResult(
                    catalog_id=item.id,
                    title=item.title,
                    status="skip",
                    detail="Déjà présent (Actif) — ignoré.",
                    entity_id=entity_id,
                )
            )
            continue
        try:
            media_url, ext = resolve_download_url(item.url, freesound_api_key=freesound_api_key)
            # Wikimedia / Freesound : espacer les téléchargements pour limiter les 429.
            time.sleep(1.5)
            raw = download_media_bytes(media_url)
            need_wav = item.role in ("intro", "outro", "bed")
            try:
                wav = convert_to_wav_pcm16(raw, src_ext=ext)
                data, store_ext, ctype = wav, "wav", "audio/wav"
            except Exception as conv_ex:
                if need_wav:
                    raise
                data, store_ext = raw, (ext or "bin")
                ctype = {
                    "ogg": "audio/ogg",
                    "mp3": "audio/mpeg",
                    "wav": "audio/wav",
                }.get(store_ext, "application/octet-stream")
                _prog(
                    f"Conversion WAV impossible ({conv_ex}) — stockage {store_ext} pour écoute."
                )

            path = ambiance_gcs_path(entity_id=entity_id, ext=store_ext)
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=path,
                data=data,
                content_type=ctype,
            )
            duration_s: float | None = None
            if data[:4] == b"RIFF":
                try:
                    duration_s = round(wav_duration_seconds(data), 2)
                except Exception:
                    duration_s = item.duration_hint_s
            else:
                duration_s = item.duration_hint_s

            notes_parts = [
                item.notes,
                f"temps={item.temps_liturgique}" if item.temps_liturgique else "",
                f"source_url={item.url}",
                f"catalog_id={item.id}",
                "seed=audio_seed_catalog_v1",
            ]
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=spreadsheet_id,
                table=AUDIO_AMBIANCE_TABLE,
                values_by_col={
                    "entity_id": entity_id,
                    "title": item.title,
                    "role": item.role,
                    "cible": item.cible,
                    "langue": item.langue,
                    "licence": item.licence,
                    "attribution": item.attribution,
                    "gcs_path": path,
                    "duration_s": str(duration_s) if duration_s is not None else "",
                    "preferred": "",
                    "source_url": item.url,
                    "notes": " | ".join(p for p in notes_parts if p),
                },
            )
            known_urls.add(item.url)
            known_eids.add(entity_id)
            report.results.append(
                ImportItemResult(
                    catalog_id=item.id,
                    title=item.title,
                    status="ok",
                    detail=f"Uploadé ({store_ext}) · {item.role}/{item.cible}",
                    gcs_path=path,
                    entity_id=entity_id,
                    duration_s=duration_s,
                    source_resolved=media_url,
                )
            )
        except Exception as ex:
            report.results.append(
                ImportItemResult(
                    catalog_id=item.id,
                    title=item.title,
                    status="error",
                    detail=f"{type(ex).__name__}: {ex}",
                    entity_id=entity_id,
                )
            )

    report.finished_at = utc_now_iso()
    return report


def report_to_markdown(report: ImportReport) -> str:
    lines = [
        "# Rapport import bibliothèque audio",
        "",
        f"- Catalogue : `{report.catalog_path}`",
        f"- Début : {report.started_at}",
        f"- Fin : {report.finished_at}",
        f"- OK : **{report.ok_count}** · Ignorés : **{report.skip_count}** · Erreurs : **{report.error_count}**",
        "",
        "| Statut | Titre | Détail | GCS |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        gcs = f"`{r.gcs_path}`" if r.gcs_path else "—"
        detail = (r.detail or "").replace("|", "/")
        lines.append(f"| {r.status} | {r.title} | {detail} | {gcs} |")
    return "\n".join(lines)
