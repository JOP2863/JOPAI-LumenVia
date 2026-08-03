"""Sondes HTTP / AELF pour le lab multi-langues (textes complets uniquement)."""

from __future__ import annotations

import json
import re
import time
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
    raw_kind: str = ""  # json | jsonp | html | text | aelf | excluded
    # --- debug API ---
    url: str = ""
    elapsed_ms: int | None = None
    content_type: str = ""
    content_length: int | None = None
    final_url: str = ""
    redirect_count: int = 0
    encoding: str = ""
    server: str = ""
    cache_control: str = ""
    top_keys: str = ""
    """Clés JSON de premier niveau (ou chemin utile), séparées par virgule."""
    headers_debug: str = ""
    """Sous-ensemble d’en-têtes HTTP utiles (texte compact)."""
    body_sha_prefix: str = ""
    """Préfixe court d’empreinte pour comparer les réponses (pas un hash crypto affiché entier)."""


def _strip_jsonp(raw: str) -> str:
    s = (raw or "").strip()
    m = re.search(r"\{[\s\S]*\}\s*;?\s*$", s)
    if m:
        return m.group(0).rstrip().rstrip(";")
    return s


def _top_keys_from_payload(payload: Any, *, limit: int = 24) -> str:
    try:
        if isinstance(payload, dict):
            keys = list(payload.keys())[:limit]
            return ", ".join(str(k) for k in keys)
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                keys = list(first.keys())[:limit]
                return f"[0].{{{', '.join(str(k) for k in keys)}}}"
            return f"list[{len(payload)}]"
    except Exception:
        pass
    return ""


def _headers_debug(headers: Any) -> str:
    if headers is None:
        return ""
    interesting = (
        "content-type",
        "content-length",
        "server",
        "cache-control",
        "content-encoding",
        "x-request-id",
        "x-powered-by",
        "access-control-allow-origin",
        "location",
        "date",
    )
    parts: list[str] = []
    try:
        # Case-insensitive lookup
        lower_map = {str(k).lower(): str(v) for k, v in headers.items()}
        for name in interesting:
            val = lower_map.get(name)
            if val:
                parts.append(f"{name}={val[:120]}")
    except Exception:
        return ""
    return " | ".join(parts)


def _body_prefix_fingerprint(raw: str) -> str:
    s = (raw or "")[:800]
    if not s:
        return ""
    # Empreinte légère non crypto : longueur + quelques codes
    mix = sum(ord(c) for c in s[::17]) % 9973
    return f"len={len(raw or '')};p={mix:04d}"


def _heuristic_full_mass_from_obj(obj: Any) -> tuple[bool, dict[str, bool], int, str]:
    """Cherche des champs typiques sans prétendre mapper toutes les APIs."""
    blob = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    low = blob.lower()
    text_len = len(blob)

    def _has(*needles: str) -> bool:
        return any(n in low for n in needles)

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
        "mass_r1",
        '"mass_r1"',
    )
    has_ps = _has(
        "psaume",
        "psalm",
        "antwortpsalm",
        "salmo",
        "salmo responsorial",
        "mass_ps",
        '"mass_ps"',
    )
    has_ev = _has(
        "evangile",
        "gospel",
        "evangelium",
        "vangelo",
        "evangelio",
        "mass_g",
        '"mass_g"',
    )
    has_l2 = _has(
        "deuxieme_lecture",
        "deuxième lecture",
        "second reading",
        "zweite lesung",
        "seconda lettura",
        "segunda lectura",
        "mass_r2",
        '"mass_r2"',
    )

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
    url = format_endpoint(spec, date_iso=date_iso)

    if spec.status == "excluded":
        return LiturgyProbeResult(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            ok=False,
            error="Source exclue (règle : textes complets uniquement / pas calendrier seul).",
            raw_kind="excluded",
            url=url,
        )

    if spec.id == "aelf_france":
        t0 = time.perf_counter()
        try:
            _ident, texts = fetch_aelf_day(date_iso, zone="france")
            elapsed = int((time.perf_counter() - t0) * 1000)
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
                url=url,
                elapsed_ms=elapsed,
                content_type="application/json (via client AELF)",
                content_length=chars,
                final_url=url,
                top_keys="informations, messes → lectures (client métier)",
                body_sha_prefix=_body_prefix_fingerprint(excerpt),
            )
        except Exception as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            if is_aelf_not_found_error(ex):
                return LiturgyProbeResult(
                    source_id=spec.id,
                    lang=spec.lang,
                    date=date_iso,
                    ok=False,
                    http_status=404,
                    error="AELF 404 — date non publiée",
                    raw_kind="aelf",
                    url=url,
                    elapsed_ms=elapsed,
                    final_url=url,
                )
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=f"{type(ex).__name__}: {ex}"[:240],
                raw_kind="aelf",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )

    if spec.id == "universalis_mass":
        from core.universalis import (
            UniversalisError,
            UniversalisHorizonError,
            copyright_notice,
            fetch_universalis_mass,
            is_full_mass,
        )

        t0 = time.perf_counter()
        try:
            _ident, texts, payload = fetch_universalis_mass(date_iso)
            elapsed = int((time.perf_counter() - t0) * 1000)
            full, blocks, chars, excerpt = _heuristic_full_mass_from_aelf(texts)
            # Affiner avec le contrat adapter
            if is_full_mass(texts):
                full = True
                blocks = {**blocks, "lecture_1": True, "psaume": True, "evangile": True}
            keys = ", ".join(sorted(str(k) for k in payload.keys()))
            notice = copyright_notice(payload)[:160]
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=True,
                http_status=200,
                full_mass_heuristic=full,
                blocks_found=blocks,
                chars_total=chars,
                excerpt=(excerpt + (" | © " + notice if notice else ""))[:400],
                raw_kind="universalis",
                url=url,
                elapsed_ms=elapsed,
                content_type="application/javascript (JSONP)",
                content_length=chars,
                final_url=url,
                top_keys=keys,
                body_sha_prefix=_body_prefix_fingerprint(excerpt),
            )
        except UniversalisHorizonError as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                http_status=200,
                error=str(ex)[:240],
                raw_kind="universalis_horizon",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )
        except UniversalisError as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=str(ex)[:240],
                raw_kind="universalis",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )
        except Exception as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=f"{type(ex).__name__}: {ex}"[:240],
                raw_kind="universalis",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )

    if spec.id.startswith("evangelizo_"):
        from core.evangelizo import (
            SOURCE_ID_TO_EVANGELIZO_LANG,
            EvangelizoError,
            EvangelizoHorizonError,
            fetch_evangelizo_mass,
            is_full_mass as evangelizo_is_full_mass,
        )

        e_lang = SOURCE_ID_TO_EVANGELIZO_LANG.get(spec.id)
        if not e_lang:
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=f"Source Evangelizo non mappée : {spec.id}",
                raw_kind="evangelizo",
                url=url,
            )
        t0 = time.perf_counter()
        try:
            _ident, texts, payload = fetch_evangelizo_mass(date_iso, evangelizo_lang=e_lang)
            elapsed = int((time.perf_counter() - t0) * 1000)
            full, blocks, chars, excerpt = _heuristic_full_mass_from_aelf(texts)
            if evangelizo_is_full_mass(texts):
                full = True
                blocks = {
                    **blocks,
                    "lecture_1": True,
                    "psaume": True,
                    "evangile": True,
                    "lecture_2": bool(str(texts.deuxieme_lecture or "").strip()),
                }
            keys = ", ".join(sorted(str(k) for k in payload.keys() if payload.get(k)))
            title = str(payload.get("liturgic_t") or "")[:120]
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=True,
                http_status=200,
                full_mass_heuristic=full,
                blocks_found=blocks,
                chars_total=chars,
                excerpt=((title + " · " if title else "") + excerpt)[:400],
                raw_kind="evangelizo_xml",
                url=url,
                elapsed_ms=elapsed,
                content_type="application/xml (Reader Feed)",
                content_length=chars,
                final_url=url,
                top_keys=keys,
                body_sha_prefix=_body_prefix_fingerprint(excerpt),
            )
        except EvangelizoHorizonError as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                http_status=200,
                error=str(ex)[:240],
                raw_kind="evangelizo_horizon",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )
        except EvangelizoError as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=str(ex)[:240],
                raw_kind="evangelizo",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )
        except Exception as ex:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return LiturgyProbeResult(
                source_id=spec.id,
                lang=spec.lang,
                date=date_iso,
                ok=False,
                error=f"{type(ex).__name__}: {ex}"[:240],
                raw_kind="evangelizo",
                url=url,
                elapsed_ms=elapsed,
                final_url=url,
            )

    t0 = time.perf_counter()
    try:
        r = requests.get(
            url,
            timeout=timeout_s,
            headers={
                "User-Agent": "JOPAI-LumenVia-LiturgyLab/0.1",
                "Accept": "application/json, text/*, */*",
            },
            allow_redirects=True,
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        status = int(r.status_code)
        raw = r.text or ""
        ctype = (r.headers.get("content-type") or "").strip()
        server = (r.headers.get("server") or "").strip()
        cache = (r.headers.get("cache-control") or "").strip()
        encoding = str(getattr(r, "encoding", "") or "")
        final_url = str(r.url or url)
        redirect_count = max(0, len(getattr(r, "history", None) or []))
        try:
            clen_hdr = r.headers.get("content-length")
            content_length = int(clen_hdr) if clen_hdr else len(raw.encode(encoding or "utf-8", errors="replace"))
        except Exception:
            content_length = len(raw)

        common_kw = dict(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            url=url,
            elapsed_ms=elapsed,
            content_type=ctype,
            content_length=content_length,
            final_url=final_url,
            redirect_count=redirect_count,
            encoding=encoding,
            server=server,
            cache_control=cache,
            headers_debug=_headers_debug(r.headers),
            body_sha_prefix=_body_prefix_fingerprint(raw),
        )

        if status >= 400:
            return LiturgyProbeResult(
                ok=False,
                http_status=status,
                error=f"HTTP {status}",
                excerpt=raw[:240],
                raw_kind="http",
                **common_kw,
            )

        kind = "text"
        payload: Any = raw
        if "json" in ctype.lower() or raw.strip().startswith("{") or raw.strip().startswith("["):
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
            ok=True,
            http_status=status,
            full_mass_heuristic=full,
            blocks_found=blocks,
            chars_total=chars,
            excerpt=excerpt,
            raw_kind=kind,
            top_keys=_top_keys_from_payload(payload),
            **common_kw,
        )
    except Exception as ex:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return LiturgyProbeResult(
            source_id=spec.id,
            lang=spec.lang,
            date=date_iso,
            ok=False,
            error=f"{type(ex).__name__}: {ex}"[:240],
            raw_kind="http",
            url=url,
            elapsed_ms=elapsed,
            final_url=url,
        )
