"""Audit statique du dépôt — granularité (LOC) sans Streamlit.

Index gaussien (réf. stratégie refactor) : scan `.py`, agrégats sur le « Corps »,
seuil d’alerte type μ + 2σ sur les fichiers du corps (navigation cognitive).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path


_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".streamlit",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "node_modules",
        "dist",
        "build",
        ".eggs",
    }
)


def default_repository_root() -> Path:
    """Racine du dépôt LumenVia (parent de ``core/``)."""
    return Path(__file__).resolve().parent.parent


def _posix_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _zone_for(rel_posix: str) -> str:
    """Sommet | corps | peripherie (spec stratégie §7)."""
    if rel_posix == "app.py":
        return "sommet"
    if rel_posix == "ui/navigation.py":
        return "sommet"
    if (
        rel_posix.startswith("core/")
        or rel_posix.startswith("ui/pages/")
        or rel_posix.startswith("ui/admin/")
    ):
        return "corps"
    return "peripherie"


def _count_physical_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if not text.endswith("\n"):
        text += "\n"
    return text.count("\n")


def _should_skip_path(path: Path) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES for part in path.parts)


def _collect_py_files(repo_root: Path) -> list[Path]:
    found: list[Path] = []
    app = repo_root / "app.py"
    if app.is_file():
        found.append(app.resolve())

    for dirname in ("core", "ui", "tools", "pages", "utils"):
        base = repo_root / dirname
        if not base.is_dir():
            continue
        for fp in base.rglob("*.py"):
            if _should_skip_path(fp):
                continue
            try:
                resolved = fp.resolve()
            except OSError:
                continue
            if resolved not in found:
                found.append(resolved)
    return sorted(found, key=lambda p: _posix_rel(p, repo_root))


@dataclass(frozen=True)
class FileAuditRow:
    """Une entrée de fichier scanné."""

    rel_path: str
    line_count: int
    zone: str


@dataclass(frozen=True)
class GranularityAuditResult:
    """Résultat complet pour l’UI radar."""

    repo_root: str
    rows: tuple[FileAuditRow, ...]
    corps_mean: float
    corps_pstdev: float
    corps_n: int
    threshold_lines: float  # μ + 2σ (corps)
    alert_paths: tuple[tuple[str, int, float], ...]
    """Tri décroissant lignes : (chemin relatif, lignes, écart au seuil)."""


def run_granularity_audit(repo_root: Path | None = None) -> GranularityAuditResult:
    """Scan LOC et alertes « hors-nuage » sur le corps constitutionnel."""
    root = (repo_root or default_repository_root()).resolve()

    rows_list: list[FileAuditRow] = []
    for fp in _collect_py_files(root):
        rel = _posix_rel(fp, root)
        n = _count_physical_lines(fp)
        rows_list.append(FileAuditRow(rel_path=rel, line_count=n, zone=_zone_for(rel)))

    rows = tuple(rows_list)
    corps_lines = [r.line_count for r in rows if r.zone == "corps"]
    n_c = len(corps_lines)

    if n_c == 0:
        mean = 0.0
        pstdev = 0.0
    elif n_c == 1:
        mean = float(corps_lines[0])
        pstdev = 0.0
    else:
        mean = float(statistics.mean(corps_lines))
        pstdev = float(statistics.pstdev(corps_lines))

    threshold = mean + 2.0 * pstdev
    alerts: list[tuple[str, int, float]] = []
    for r in rows:
        if r.zone != "corps":
            continue
        if pstdev == 0.0:
            if r.line_count > mean:
                alerts.append((r.rel_path, r.line_count, float(r.line_count - mean)))
        elif float(r.line_count) > threshold:
            alerts.append((r.rel_path, r.line_count, float(r.line_count - threshold)))

    alerts.sort(key=lambda x: (-x[1], x[0]))

    return GranularityAuditResult(
        repo_root=str(root),
        rows=rows,
        corps_mean=mean,
        corps_pstdev=pstdev,
        corps_n=n_c,
        threshold_lines=threshold,
        alert_paths=tuple(alerts),
    )


def corps_line_counts(result: GranularityAuditResult) -> list[int]:
    return [r.line_count for r in result.rows if r.zone == "corps"]


def bin_histogram(
    values: list[int], num_bins: int = 18
) -> tuple[list[float], list[int], list[float]]:
    """Centres de bins, effectifs observés, arêtes (longueur ``num_bins + 1``)."""
    if not values:
        return [], [], []
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        return [float(vmin)], [len(values)], [float(vmin), float(vmin + 1.0)]

    edges = [vmin + i * (vmax - vmin) / num_bins for i in range(num_bins + 1)]
    edges[-1] = float(vmax) + 1e-9

    counts = [0] * num_bins
    centers: list[float] = []
    for i in range(num_bins):
        lo, hi = edges[i], edges[i + 1]
        centers.append((lo + hi) / 2.0)

    span = float(vmax - vmin)
    for v in values:
        if v >= vmax:
            idx = num_bins - 1
        else:
            idx = int((float(v - vmin) / span) * num_bins)
            idx = min(max(0, idx), num_bins - 1)
        counts[idx] += 1

    return centers, counts, edges


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF Φ pour loi normale."""
    if sigma <= 0.0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * (2.0**0.5))
    return 0.5 * (1.0 + math.erf(z))


def expected_bin_counts(
    edges: list[float], mu: float, sigma: float, n_samples: int
) -> list[float]:
    """Effectifs théoriques par intervalle pour N(μ,σ²)."""
    if len(edges) < 2:
        return []
    out: list[float] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        p = normal_cdf(hi, mu, sigma) - normal_cdf(lo, mu, sigma)
        out.append(max(0.0, float(n_samples) * p))
    return out
