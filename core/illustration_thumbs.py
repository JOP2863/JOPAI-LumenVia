"""
Vignettes WebP dans GCS sous ``Images/thumbs/{année}/{date}.webp``,
dérivées des fichiers ``Images/illustrations/...`` pour accélérer l’affichage dans l’app.
"""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud import storage

THUMB_GCS_PREFIX = "Images/thumbs"

_ILLUSTRATIONS_MARKER = "/illustrations/"


def gcs_thumb_path_from_source_blob(source_blob_path: str) -> str:
    """
    ``Images/illustrations/2026/2026-01-04.png`` → ``Images/thumbs/2026/2026-01-04.webp``
    """
    s = (source_blob_path or "").strip().replace("\\", "/")
    if _ILLUSTRATIONS_MARKER in s:
        tail = s.split(_ILLUSTRATIONS_MARKER, 1)[1]
    else:
        tail = s.split("/", 1)[-1] if "/" in s else s
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    return f"{THUMB_GCS_PREFIX}/{base}.webp"


def build_thumbnail_webp(image_bytes: bytes, *, max_side: int = 420, quality: int = 82) -> bytes:
    """Redimensionne (max côté) et encode en WebP."""
    from PIL import Image

    if not image_bytes:
        return b""
    im = Image.open(io.BytesIO(image_bytes))
    if im.mode in ("P", "PA"):
        im = im.convert("RGBA")
    elif im.mode == "RGB":
        pass
    elif im.mode == "RGBA":
        pass
    else:
        im = im.convert("RGB")
    w, h = im.size
    if w <= 0 or h <= 0:
        return b""
    scale = min(float(max_side) / float(w), float(max_side) / float(h), 1.0)
    if scale < 1.0:
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=int(quality), method=6)
    return buf.getvalue()


def thumb_blob_exists(
    *,
    gcs: storage.Client,
    bucket_name: str,
    source_blob_path: str,
) -> bool:
    from core.storage import blob_exists

    path = gcs_thumb_path_from_source_blob(source_blob_path)
    return blob_exists(gcs=gcs, bucket_name=bucket_name, path=path)


def generate_thumb_from_source_and_upload(
    *,
    gcs: storage.Client,
    bucket_name: str,
    source_blob_path: str,
    download_bytes_fn,
    upload_bytes_fn,
    max_side: int = 420,
) -> str:
    """Télécharge la source, produit la vignette, upload. Retourne le chemin thumb GCS."""
    data = download_bytes_fn(gcs=gcs, bucket_name=bucket_name, path=source_blob_path)
    thumb = build_thumbnail_webp(data, max_side=max_side)
    dest = gcs_thumb_path_from_source_blob(source_blob_path)
    upload_bytes_fn(
        gcs=gcs,
        bucket_name=bucket_name,
        path=dest,
        data=thumb,
        content_type="image/webp",
    )
    return dest


def build_thumbs_montage_png(
    thumbs: list[tuple[str, bytes]],
    *,
    cols: int = 4,
    rows: int = 13,
    cell: int = 200,
    pad: int = 10,
    bg: tuple[int, int, int] = (253, 251, 247),
    title_cell_text: str | None = None,
) -> bytes:
    """Monte toutes les vignettes en grille dans un PNG (calendrier de l’année)."""
    from PIL import Image, ImageDraw, ImageFont

    # On conserve l’ordre d’entrée.
    w = cols * cell + (cols + 1) * pad
    h = rows * cell + (rows + 1) * pad
    canvas = Image.new("RGB", (w, h), color=bg)

    # Cellule titre (en haut à gauche) : décale le reste (51 -> 52).
    start_i = 0
    if title_cell_text:
        x0 = pad
        y0 = pad
        draw = ImageDraw.Draw(canvas)
        # Bordure or légère
        draw.rectangle([x0, y0, x0 + cell, y0 + cell], outline=(212, 175, 55), width=3)
        txt = str(title_cell_text).strip()

        def _load_font(px: int):
            try:
                return ImageFont.truetype("arial.ttf", int(px))
            except Exception:
                return ImageFont.load_default()

        def _wrap_lines(text: str, font, max_w: int) -> list[str]:
            raw_lines = text.splitlines() if "\n" in text else [text]
            out: list[str] = []
            for raw in raw_lines:
                words = [w for w in str(raw).split(" ") if w]
                if not words:
                    continue
                cur = words[0]
                for w in words[1:]:
                    cand = f"{cur} {w}"
                    bb = draw.textbbox((0, 0), cand, font=font)
                    if (bb[2] - bb[0]) <= max_w:
                        cur = cand
                    else:
                        out.append(cur)
                        cur = w
                out.append(cur)
            return out

        margin = 14
        max_w = max(10, cell - 2 * margin)
        max_h = max(10, cell - 2 * margin)
        chosen_font = _load_font(26)
        chosen_lines = _wrap_lines(txt, chosen_font, max_w)
        chosen_line_h = 20

        # Auto-fit : réduit la police jusqu’à ce que tout rentre (largeur + hauteur).
        for px in (26, 24, 22, 20, 18, 17, 16, 15, 14, 13, 12):
            font = _load_font(px)
            lines = _wrap_lines(txt, font, max_w)
            if not lines:
                continue
            bbs = [draw.textbbox((0, 0), ln, font=font) for ln in lines]
            line_h = max((bb[3] - bb[1] for bb in bbs), default=px)
            total_h = line_h * len(lines) + 6 * (len(lines) - 1)
            if total_h <= max_h and all((bb[2] - bb[0]) <= max_w for bb in bbs):
                chosen_font = font
                chosen_lines = lines
                chosen_line_h = line_h
                break

        # Centrage multi-ligne
        total_h = chosen_line_h * len(chosen_lines) + 6 * (len(chosen_lines) - 1)
        ty = y0 + (cell - total_h) // 2
        for ln in chosen_lines:
            bb = draw.textbbox((0, 0), ln, font=chosen_font)
            tw = bb[2] - bb[0]
            tx = x0 + (cell - tw) // 2
            draw.text((tx, ty), ln, fill=(52, 46, 41), font=chosen_font)
            ty += chosen_line_h + 6
        start_i = 1

    max_n = cols * rows
    for j, (_name, b) in enumerate(thumbs[: max_n - start_i]):
        i = j + start_i
        try:
            im = Image.open(io.BytesIO(b))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            # Fit dans la cellule.
            im.thumbnail((cell, cell), Image.Resampling.LANCZOS)
            x0 = pad + (i % cols) * (cell + pad) + (cell - im.size[0]) // 2
            y0 = pad + (i // cols) * (cell + pad) + (cell - im.size[1]) // 2
            if im.mode == "RGBA":
                canvas.paste(im, (x0, y0), im)
            else:
                canvas.paste(im, (x0, y0))
        except Exception:
            continue

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def pastelize_png(png_bytes: bytes, *, alpha: float = 0.55) -> bytes:
    """Version “pastel” : éclaircit + désature légèrement."""
    from PIL import Image, ImageEnhance

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im = ImageEnhance.Color(im).enhance(0.75)
    im = ImageEnhance.Brightness(im).enhance(1.15)
    # Voile crème
    veil = Image.new("RGB", im.size, (253, 251, 247))
    im = Image.blend(im, veil, float(min(0.9, max(0.0, alpha))))
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue()


_PROJECT_RE = re.compile(r"project[=/](\d+)", re.I)


def extract_gcp_project_id_from_error(message: str | None) -> str | None:
    if not message:
        return None
    m = _PROJECT_RE.search(message)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{10,})\b", message)
    return m.group(1) if m else None


def vision_console_activation_url(project_id: str | None) -> str:
    pid = (project_id or "").strip()
    if not pid:
        pid = ""
    return f"https://console.cloud.google.com/apis/library/vision.googleapis.com?project={pid}"
