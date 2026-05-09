"""Extract https URLs from Outlook .msg (rough scan) and flag localhost-like refs."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/inspect_msg_urls.py <path.msg|folder>", file=sys.stderr)
        return 2
    p = Path(sys.argv[1])
    if p.is_dir():
        msgs = sorted(p.glob("*.msg"), key=lambda x: x.stat().st_mtime, reverse=True)
        if not msgs:
            print(f"No .msg in {p}", file=sys.stderr)
            return 1
        p = msgs[0]
        print(f"(auto-picked newest .msg: {p.name})", file=sys.stderr)
    if not p.is_file():
        print(f"Not found: {p}", file=sys.stderr)
        return 1
    b = p.read_bytes()
    # MSG is OLE; URLs often appear as UTF-16LE or ASCII fragments.
    s = b.decode("utf-16-le", errors="ignore") + "\n" + b.decode("latin-1", errors="ignore")

    suspects: list[str] = []
    for label, pat in (
        ("localhost", r"localhost"),
        ("127.0.0.1", r"127\.0\.0\.1"),
        ("file URL", r"file://"),
        ("0.0.0.0", r"0\.0\.0\.0"),
    ):
        if re.search(pat, s, re.I):
            suspects.append(label)

    urls = sorted(set(re.findall(r"https?://[^\s\x00<>\")]+", s)))
    # Trim trailing punctuation sometimes captured
    cleaned: list[str] = []
    for u in urls:
        u = u.rstrip(").,;")
        cleaned.append(u)
    urls = sorted(set(cleaned))

    try:
        print(f"File: {p.name}")
    except UnicodeEncodeError:
        print(f"File: {p.name.encode('ascii', 'backslashreplace').decode()}")
    print("Suspect patterns:", ", ".join(suspects) if suspects else "none")
    print(f"Unique http(s) URLs found: {len(urls)}")
    for u in urls:
        print(u)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
