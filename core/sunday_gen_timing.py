"""Journal de timings pour la génération dominicale (overlay / message final)."""

from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_ENTRIES: list[dict[str, Any]] = []
_RUN_T0: float | None = None

# Ancrage diagnostic 2026-11-15 (FR+DE) : Vertex ~8 s, TTS synth FR ~111 s,
# TTS lectures FR ~185 s, puis DE séquentiel → ~11 min wall. Cible : paralléliser.


def timing_reset() -> None:
    global _RUN_T0
    with _LOCK:
        _ENTRIES.clear()
        _RUN_T0 = time.perf_counter()


def timing_add(*, lang: str, step: str, seconds: float, note: str = "") -> None:
    with _LOCK:
        _ENTRIES.append(
            {
                "lang": str(lang or "").strip().upper() or "?",
                "step": str(step or "").strip() or "?",
                "seconds": round(float(seconds), 3),
                "note": str(note or "").strip(),
            }
        )


def timing_entries() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(x) for x in _ENTRIES]


def timing_wall_s() -> float | None:
    with _LOCK:
        if _RUN_T0 is None:
            return None
        return round(time.perf_counter() - _RUN_T0, 3)


def format_timing_recap(*, max_lines: int = 24) -> str:
    """Texte markdown court pour le message de fin de génération."""
    rows = timing_entries()
    wall = timing_wall_s()
    if not rows and wall is None:
        return ""
    lines: list[str] = ["**Chrono génération**"]
    if wall is not None:
        lines.append(f"- Total wall : **{wall:.1f} s** ({wall / 60.0:.1f} min)")
    for e in rows[: max(1, int(max_lines))]:
        note = f" ({e['note']})" if e.get("note") else ""
        lines.append(
            f"- {e['lang']} · {e['step']} : **{e['seconds']:.1f} s**{note}"
        )
    if len(rows) > max_lines:
        lines.append(f"- … +{len(rows) - max_lines} étape(s)")
    return "\n".join(lines)
