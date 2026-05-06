#!/usr/bin/env python3
"""
Étape 3 — Pipeline illustrations dominicales (chemins GCS + prompts pour Midjourney / banques d’images).

Lit `data/manifests/sundays_liturgy.json` (étape 2) et produit
`data/manifests/illustration_pipeline.json` : pour chaque dimanche sans erreur,
chemins cibles dans le bucket (`Images/illustrations/{année}/{date}.webp`) et une
proposition de prompt homogène (style charte LumenVia).

Usage (racine du repo) :
  python tools/step3_illustration_pipeline.py
  python tools/step3_illustration_pipeline.py --manifest data/manifests/sundays_liturgy.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manifeste pipeline illustrations (étape 3).")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manifests/sundays_liturgy.json"),
        help="JSON produit par sunday_calendar_manifest.py",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/manifests/illustration_pipeline.json"),
        help="Sortie : liste des cibles GCS + prompts",
    )
    args = parser.parse_args()
    src = args.manifest if args.manifest.is_absolute() else _REPO_ROOT / args.manifest
    out = args.out if args.out.is_absolute() else _REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    raw = json.loads(src.read_text(encoding="utf-8"))
    entries = raw.get("entries") or []
    targets: list[dict] = []

    for e in entries:
        if "error" in e:
            continue
        ds = str(e.get("date") or "").strip()
        if len(ds) < 10:
            continue
        year = ds[:4]
        kw = e.get("keywords_illustration") or []
        kw_txt = ", ".join(str(x) for x in kw[:8] if x)
        gcs_primary = f"Images/illustrations/{year}/{ds}.webp"
        prompt = (
            "Minimalist Catholic liturgical illustration, woodcut-inspired line art, "
            f"gold accent #D4AF37 on cream, serene, mood and symbols only (theme hints: {kw_txt or 'Sunday liturgy'}). "
            "CRITICAL: zero text in the image — no letters, names, French words, banners, books with lines, "
            "subtitles, or typography; words would be misspelled; purely wordless imagery."
        )
        targets.append(
            {
                "date": ds,
                "zone": e.get("zone"),
                "temps_liturgique": e.get("temps_liturgique"),
                "couleur": e.get("couleur"),
                "gcs_path_primary": gcs_primary,
                "alternates": [
                    f"Images/illustrations/{year}/{ds}.png",
                    f"Images/illustrations/{year}/{ds}.jpg",
                ],
                "keywords": kw,
                "prompt_midjourney_style": prompt,
            }
        )

    try:
        src_rel = src.relative_to(_REPO_ROOT)
    except ValueError:
        src_rel = src
    payload = {
        "source_manifest": str(src_rel),
        "upload_note": "Uploader une image vers gcs_path_primary (ou alternate) pour affichage automatique sur « La Lumière du Dimanche ».",
        "targets": targets,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Étape 3 — {len(targets)} cibles écrites -> {out}")


if __name__ == "__main__":
    main()
