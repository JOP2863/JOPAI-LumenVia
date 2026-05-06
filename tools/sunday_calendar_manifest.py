#!/usr/bin/env python3
"""
Étape 2 — Manifeste liturgique des dimanches (année civile).

Usage (à la racine du repo) :
  python tools/sunday_calendar_manifest.py --year 2026 --zone france --out data/manifests/sundays_liturgy.json

Pour chaque dimanche : date ISO, cycle A/B/C (API AELF), temps liturgique, couleur,
libellé du jour, et mots-clés illustratifs selon le cycle (pour recherche d’images /
pipeline Midjourney ou Open Bible).
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

# Permet "python tools/..." depuis la racine du repo
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.aelf import AelfClient  # noqa: E402


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_key(s: str | None) -> str:
    t = _strip_accents((s or "").strip().lower())
    return "".join(ch if ch.isalnum() else "_" for ch in t).strip("_")


KEYWORDS_BY_CYCLE: dict[str, list[str]] = {
    "A": ["Matthieu", "Royaume des cieux", "Unité", "Disciples"],
    "B": ["Marc", "Mission", "Conversion", "Chemin"],
    "C": ["Luc", "Miséricorde", "Accueil", "Pardon"],
    "I": ["Temps ordinaire", "Année I", "Psautier", "Fidélité"],
    "II": ["Temps ordinaire", "Année II", "Psautier", "Fidélité"],
}


def sundays_in_year(year: int) -> list[date]:
    out: list[date] = []
    d = date(year, 1, 1)
    last = date(year, 12, 31)
    while d <= last:
        if d.weekday() == 6:
            out.append(d)
        d += timedelta(days=1)
    return out


def cycle_keywords(annee_raw: str | None) -> list[str]:
    if not annee_raw:
        return ["Liturgie", "Dimanche"]
    a = annee_raw.strip().upper()
    if a in KEYWORDS_BY_CYCLE:
        return KEYWORDS_BY_CYCLE[a]
    nk = _norm_key(annee_raw)
    if nk in ("annee_i", "i"):
        return KEYWORDS_BY_CYCLE["I"]
    if nk in ("annee_ii", "ii"):
        return KEYWORDS_BY_CYCLE["II"]
    return KEYWORDS_BY_CYCLE.get(a[:1], ["Liturgie", "Dimanche"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Manifeste des dimanches + métadonnées AELF.")
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument("--zone", default="france")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/manifests/sundays_liturgy.json"),
        help="Chemin JSON de sortie (relatif à la racine du repo).",
    )
    args = parser.parse_args()
    out_path = args.out
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = AelfClient()
    entries: list[dict] = []
    for d in sundays_in_year(args.year):
        ds = d.isoformat()
        try:
            info = client.informations(ds, zone=args.zone)
        except Exception as e:
            entries.append(
                {
                    "date": ds,
                    "zone": args.zone,
                    "error": str(e),
                }
            )
            continue
        kw = cycle_keywords(info.annee)
        tempo = (info.periode or "").strip()
        merged_kw = list(dict.fromkeys([tempo, info.couleur or "", *kw]))
        merged_kw = [k for k in merged_kw if k]
        entries.append(
            {
                "date": ds,
                "zone": args.zone,
                "cycle": info.annee,
                "temps_liturgique": info.periode,
                "semaine": info.semaine,
                "couleur": info.couleur,
                "jour_liturgique_nom": info.jour_liturgique_nom,
                "keywords_illustration": merged_kw[:12],
            }
        )

    payload = {
        "year": args.year,
        "zone": args.zone,
        "source_liturgy": "AELF API /v1/informations",
        "entries": entries,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Écrit {len(entries)} dimanches -> {out_path}")


if __name__ == "__main__":
    main()
