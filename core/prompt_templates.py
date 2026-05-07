from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable


@dataclass(frozen=True)
class PromptTemplate:
    template_key: str
    content_md: str
    active: bool
    created_at: str
    version: int


def _to_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on", "vrai")


def _to_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


def _norm_key(v: object) -> str:
    return str(v or "").strip()


def _norm_md(v: object) -> str:
    return str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def pick_latest_active_by_key(rows: Iterable[dict[str, Any]]) -> dict[str, PromptTemplate]:
    """
    Sélectionne, pour chaque template_key, la version active la plus récente.
    Ordre: created_at (lexicographique ISO) puis version.
    """
    best: dict[str, PromptTemplate] = {}
    for r in rows:
        key = _norm_key(r.get("template_key"))
        if not key:
            continue
        if not _to_bool(r.get("active", True)) or str(r.get("status") or "").strip().lower() not in ("", "active"):
            continue

        tpl = PromptTemplate(
            template_key=key,
            content_md=_norm_md(r.get("content_md")),
            active=True,
            created_at=str(r.get("created_at") or ""),
            version=_to_int(r.get("version"), default=0),
        )
        cur = best.get(key)
        if cur is None:
            best[key] = tpl
            continue
        if (tpl.created_at, tpl.version) >= (cur.created_at, cur.version):
            best[key] = tpl
    return best


def compute_sha256_text(text: str) -> str:
    return sha256((text or "").encode("utf-8")).hexdigest()

