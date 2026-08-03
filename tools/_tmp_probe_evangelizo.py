"""Probe real Evangelizo Reader Feed + Gemini-suggested fake REST URLs."""

from __future__ import annotations

import requests

UA = {"User-Agent": "LumenViaProbe/1.0", "Accept": "*/*"}
BASE = "https://feed.evangelizo.org/v2/reader.php"
D = "20260809"  # Sunday


def _snip(s: str, n: int = 280) -> str:
    return (s or "").replace("\n", " ")[:n]


def probe_gemini_fake() -> None:
    print("=== Gemini suggested REST (expect HTML SPA / 404) ===")
    for lg in ("fr", "en", "de", "es", "it"):
        url = f"https://levangileauquotidien.org/api/v1/{lg}/reading/2026-08-09"
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        print(f"{lg} -> {r.status_code} ct={ct} body={_snip(r.text, 120)!r}")
    r = requests.get(
        "https://www.ciudadredonda.org/api/v1/evangelio/2026-08-09",
        headers=UA,
        timeout=25,
        allow_redirects=True,
    )
    print(f"ciudadredonda -> {r.status_code} body={_snip(r.text, 120)!r}")
    r = requests.get(
        "https://api.evangelizo.org/v1/en/readings/2026-08-09",
        headers=UA,
        timeout=25,
        allow_redirects=True,
    )
    print(f"api.evangelizo.org -> {r.status_code} body={_snip(r.text, 120)!r}")


def probe_reader() -> None:
    print("=== Official Reader Evangelizo feed.evangelizo.org ===")
    for lang in ("FR", "EN", "DE", "ES", "IT"):
        for typ in ("liturgic_t", "all", "xml"):
            url = f"{BASE}?date={D}&type={typ}&lang={lang}"
            r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
            ct = r.headers.get("Content-Type", "")
            print(
                f"{lang} type={typ} -> {r.status_code} ct={ct} "
                f"len={len(r.text or '')} body={_snip(r.text)!r}"
            )
            print("---")

    print("=== reading + content= (FR) ===")
    for content in ("", "GSP", "FR", "LT", "PS", "G", "EP", "1", "2"):
        if content:
            url = f"{BASE}?date={D}&type=reading&lang=FR&content={content}"
        else:
            url = f"{BASE}?date={D}&type=reading&lang=FR"
        r = requests.get(url, headers=UA, timeout=25)
        print(f"content={content!r} -> {r.status_code} len={len(r.text or '')} body={_snip(r.text, 160)!r}")

    print("=== DE/EN/ES/IT type=all sample ===")
    for lang in ("DE", "EN", "ES", "IT"):
        url = f"{BASE}?date={D}&type=all&lang={lang}"
        r = requests.get(url, headers=UA, timeout=25)
        print(f"{lang} all -> {r.status_code} len={len(r.text or '')}")
        print(_snip(r.text, 500))
        print("---")


if __name__ == "__main__":
    probe_gemini_fake()
    probe_reader()
