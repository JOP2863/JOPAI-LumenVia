import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: extract_docx_text.py <input.docx> <output.txt>", file=sys.stderr)
        return 2

    in_path, out_path = sys.argv[1], sys.argv[2]

    try:
        from docx import Document  # type: ignore
    except Exception as e:
        print(
            "Missing dependency: python-docx. Install with: python -m pip install python-docx",
            file=sys.stderr,
        )
        print(str(e), file=sys.stderr)
        return 3

    doc = Document(in_path)
    with open(out_path, "w", encoding="utf-8") as f:
        for para in doc.paragraphs:
            t = (para.text or "").strip()
            if t:
                f.write(t + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

