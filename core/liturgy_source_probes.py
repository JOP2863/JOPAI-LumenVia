"""Sondes HTTP / AELF pour le lab multi-langues (textes complets uniquement)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from core.aelf import fetch_aelf_day, is_aelf_not_found_error
from core.liturgy_sources_registry import LiturgySourceSpec, format_endpoint


@dataclass
class LiturgyProbeResult:
    source_id: str
    lang: str
    date: str
    ok: bool
    http_status: int | None = None
    full_mass_heuristic: bool = False
    """Heuristique : présence de blocs lecture1 / psaume / évangile (texte non vide)."""
    blocks_found: dict[str, bool] = field(default_factory=dict)
    chars_total: int = 0
    excerpt: str = ""
    error: str = ""
    raw_kind: str = ""  # json | jsonp | html | text | aelf


def _strip_jsonp(raw: str) -> str:
    s = (raw or "").strip()
    # universalis-style: /**/callback({...});
    m = re.search(r"\{[\s\S]*\}\s*;?\s*$", s)
    if m:
        return m.group(0).rstrip().rstrip(";")
    return s


def _heuristic_full_mass_from_obj(obj: Any) -> tuple[bool, dict[str, bool], int, str]:
    """Cherche des champs typiques sans prétendre mapper toutes les APIs."""
    blob = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    low = blob.lower()
    text_len = len(blob)

    def _has(*needles: str) -> bool:
        return any(n in low for n in needles)

    # Signaux grossiers par langue / fournisseur
    has_l1 = _has(
        "premiere_lecture",
        "première lecture",
        "first reading",
        "firstreading",
        "lesung",
        "prima lettura",
        "primera lectura",
        "reading1",
        "lectures",
    )
    has_ps = _has("psaume", "psalm", "antwortpsalm", "salmo", "salmo responsorial")
    has_ev = _has("evangile", "gospel", "evangelium", "vangelo", "evangelio")
    has_l2 = _has("deuxieme_lecture", "deuxième lecture", "second reading", "zweite lesung", "seconda lettura")

    # Contenu texte : refuser les payloads trop courts (calendrier / refs seules)
    substantial = text_len >= 800
    blocks = {
        "lecture_1": has_l1,
        "psaume": has_ps,
        "lecture_2": has_l2,
        "evangile": has_ev,
        "payload_substantiel": substantial,
    }
    full = bool(has_l1 and has_ps and has_ev and substantial)
    excerpt = blob[:400].replace("\n", " ")
    return full, blocks, text_len, excerpt


def _heuristic_full_mass_from_aelf(texts: Any) -> tuple[bool, dict[str, bool], int, str]:
    def _ok(v: object) -> bool:
        return bool(str(v or "").strip()) and len(str(v).strip()) > 40

    b1 = _ok(getattr(texts, "premiere_lecture", None))
    ps = _ok(getattr(texts, "psaume", None))
    b2 = _ok(getattr(texts, "deuxieme_lecture", None))
    ev = _ok(getattr(texts, "evangile", None))
    parts = [
        str(getattr(texts, "premiere_lecture", "") or ""),
        str(getattr(texts, "psaume", "") or ""),
        str(getattr(texts, "deuxieme_lecture", "") or ""),
        str(getattr(texts, "evangile", "") or ""),
    ]
    total = sum(len(p) for p in parts)
    blocks = {
        "lecture_1": b1,
        "psaume": ps,
        "lecture_2": b2,
        "evangile": ev,
        "payload_substantiel": total >= 800,
    }
    full = b1 and ps and ev and total >= 800
    excerpt = (parts[0][:120] + " … " + parts[-1][:120]) if parts else ""
    return full, blocks, total, excerpt


def probe_liturgy_source(spec: LiturgySourceSpec, *, date_iso: str, timeout_s: float = 18.0) -> LiturgyProbeResult:
    """Sonde une source pour une date ISO. AELF via client métier ; autres via HTTP brut."""
    date_iso = str(date_iso or "").strip()[:10]
    if spec.status == "excluded":
        return LiturgyProbeResult(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            ok=False,
            error="Source exclue (règle : textes complets uniquement / pas calendrier seul).",
            raw_kind="excluded",
        )

    if spec.id == "aelf_france":
        try:
            _ident, texts = fetch_aelf_day(date_iso, zone="france")
            full, blocks, chars, excerpt = _heuristic_full_mass_from_aelf(texts)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=True,
                http_status=200,
                full_mass_heuristic=full,
                blocks_found=blocks,
                chars_total=chars,
                excerpt=excerpt,
                raw_kind="aelf",
            )
        except Exception as ex:
            if is_aelf_not_found_error(ex):
                return LiturgyProbeResult(
                    source_id=spec.id,
                    lang=spec.lang,
                    date=date_iso,
                    ok=False,
                    http_status=404,
                    error="AELF 404 — date non publiée",
                    raw_kind="aelf",
                )
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=f"{type(ex).__name__}: {ex}"[:240],
                raw_kind="aelf",
            )

    url = format_endpoint(spec, date_iso=date_iso)
    try:
        r = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": "JOPAI-LumenVia-LiturgyLab/0.1", "Accept": "application/json, text/*, */*"},
        )
        status = int(r.status_code)
        raw = r.text or ""
        if status >= 400:
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                http_status=status,
                error=f"HTTP {status}",
                excerpt=raw[:200],
                raw_kind="http",
            )
        kind = "text"
        payload: Any = raw
        ctype = (r.headers.get("content-type") or "").lower()
        if "json" in ctype or raw.strip().startswith("{") or raw.strip().startswith("["):
            try:
                payload = r.json()
                kind = "json"
            except Exception:
                try:
                    payload = json.loads(_strip_jsonp(raw))
                    kind = "jsonp"
                except Exception:
                    kind = "text"
        elif "callback" in raw[:80].lower() or raw.strip().startswith("/**/"):
            try:
                payload = json.loads(_strip_jsonp(raw))
                kind = "jsonp"
            except Exception:
                kind = "jsonp"
        elif "<html" in raw[:200].lower():
            kind = "html"

        full, blocks, chars, excerpt = _heuristic_full_mass_from_obj(payload)
        return LiturgyProbeResult(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            ok=True,
            http_status=status,
            full_mass_heuristic=full,
            blocks_found=blocks,
            chars_total=chars,
            excerpt=excerpt,
            raw_kind=kind,
        )
    except Exception as ex:
        return LiturgyProbeResult(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            ok=False,
            error=f"{type(ex).__name__}: {ex}"[:240],
            raw_kind="http",
        )
