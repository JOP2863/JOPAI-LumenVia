"""Probe Evangelizo AM/SP + XML structure for Sunday mass."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import requests

UA = {"User-Agent": "LumenViaProbe/1.0", "Accept": "*/*"}
BASE = "https://feed.evangelizo.org/v2/reader.php"
D = "20260809"


def main() -> None:
    for lang in ("AM", "SP", "EN", "ES"):
        url = f"{BASE}?date={D}&type=all&lang={lang}"
        r = requests.get(url, headers=UA, timeout=25)
        print(f"lang={lang} all -> {r.status_code} len={len(r.text or '')} body={(r.text or '')[:200]!r}")

    print("\n=== XML FR tags ===")
    url = f"{BASE}?date={D}&type=xml&lang=FR"
    r = requests.get(url, headers=UA, timeout=25)
    raw = r.text or ""
    # fix common typo litugic
    tags = sorted(set(re.findall(r"<([a-zA-Z0-9_]+)", raw)))
    print("tags:", tags)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print("parse err", e)
        # try strip BOM / junk
        i = raw.find("<?xml")
        root = ET.fromstring(raw[i:] if i >= 0 else raw)
    for el in root.iter():
        if el.text and el.text.strip() and len(el.text.strip()) > 40:
            print(f"  {el.tag}: {el.text.strip()[:100]!r} ... len={len(el.text.strip())}")
        elif el.text and el.text.strip():
            print(f"  {el.tag}: {el.text.strip()[:80]!r}")

    print("\n=== per-content AM/DE/IT/SP ===")
    for lang in ("AM", "DE", "IT", "SP"):
        ok = []
        for content in ("FR", "PS", "SR", "GSP"):
            url = f"{BASE}?date={D}&type=reading&lang={lang}&content={content}"
            r = requests.get(url, headers=UA, timeout=25)
            body = r.text or ""
            bad = "Error : wrong param" in body or body.strip().startswith("<!DOCTYPE")
            ok.append((content, r.status_code, len(body), not bad))
        print(lang, ok)


if __name__ == "__main__":
    main()
