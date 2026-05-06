from __future__ import annotations

import argparse
import sys


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Nettoie et formate un texte liturgique (retours ligne, espaces).")
    p.add_argument("input", help="Fichier texte en entrée")
    p.add_argument("-o", "--output", default=None, help="Fichier de sortie (sinon stdout)")
    args = p.parse_args(argv)

    raw = open(args.input, "r", encoding="utf-8").read()
    out = normalize_text(raw)

    if args.output:
        open(args.output, "w", encoding="utf-8").write(out)
    else:
        sys.stdout.write(out)
    return 0


def normalize_text(s: str) -> str:
    s = s.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in s.split("\n")]
    # supprime lignes vides multiples
    out = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip() + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

