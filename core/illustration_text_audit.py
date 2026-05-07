from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping

from google.cloud import storage, vision


def existing_illustration_blob_path(
    *,
    gcs: storage.Client,
    bucket_name: str,
    target: Mapping[str, Any],
) -> str | None:
    """Premier chemin manifeste (primary puis alternates) qui existe sur le bucket."""
    from core.storage import blob_exists

    cand: list[str] = []
    p0 = str(target.get("gcs_path_primary") or "").strip()
    if p0:
        cand.append(p0)
    for a in target.get("alternates") or []:
        s = str(a or "").strip()
        if s:
            cand.append(s)
    for path in cand:
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                return path
        except Exception:
            continue
    return None


def detect_text_in_image_bytes(
    *,
    image_bytes: bytes,
    client: vision.ImageAnnotatorClient,
) -> str:
    """
    Texte détecté dans l'image (Vision TEXT_DETECTION).
    Chaîne vide si aucun glyphe exploitable.
    """
    if not image_bytes:
        return ""
    img = vision.Image(content=image_bytes)
    resp = client.text_detection(image=img)
    if resp.error.message:
        raise RuntimeError(resp.error.message)
    anns = resp.text_annotations
    if not anns:
        return ""
    # Premier élément = bloc complet ; les suivants sont des fragments avec bounding boxes.
    return str(anns[0].description or "").strip()


_WS_RE = re.compile(r"\s+")


def _normalize_for_len(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip())


_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[A-Za-zÀ-ÖØ-öø-ÿ]+)?", re.UNICODE)


def _bbox_wh(ann: object) -> tuple[int, int] | None:
    try:
        poly = getattr(ann, "bounding_poly", None)
        verts = getattr(poly, "vertices", None) if poly is not None else None
        if not verts:
            return None
        xs = [int(getattr(v, "x", 0) or 0) for v in verts]
        ys = [int(getattr(v, "y", 0) or 0) for v in verts]
        if not xs or not ys:
            return None
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if w <= 0 or h <= 0:
            return None
        return w, h
    except Exception:
        return None


def _looks_like_decor_noise(
    *,
    image_bytes: bytes,
    annotations: list,
    min_boxes: int = 3,
    max_boxes: int = 50,
    max_p90_height_ratio: float = 0.028,
    max_p90_area_ratio: float = 0.0012,
) -> tuple[bool, dict[str, Any]]:
    """
    Heuristique anti-faux-positifs :
    si Vision ne renvoie que des micro-boîtes (motifs / traits / décor) on considère que ce n'est pas du texte humain.
    """
    diag: dict[str, Any] = {}
    try:
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(image_bytes))
        w_img, h_img = im.size
    except Exception:
        return False, diag

    if not w_img or not h_img:
        return False, diag

    anns = list(annotations or [])
    if len(anns) <= 1:
        return False, diag

    frags = anns[1 : 1 + max_boxes]
    whs: list[tuple[int, int]] = []
    for a in frags:
        wh = _bbox_wh(a)
        if wh:
            whs.append(wh)
    if len(whs) < min_boxes:
        return False, diag

    heights = sorted([h for (_w, h) in whs])
    areas = sorted([int(w * h) for (w, h) in whs])
    p90_i = max(0, int(round(0.9 * (len(heights) - 1))))
    p90_h = heights[p90_i]
    p90_a = areas[p90_i]
    img_area = int(w_img * h_img)
    hr = float(p90_h) / float(h_img)
    ar = float(p90_a) / float(max(1, img_area))

    diag.update(
        {
            "img_w": int(w_img),
            "img_h": int(h_img),
            "boxes_n": int(len(whs)),
            "p90_box_h": int(p90_h),
            "p90_box_area": int(p90_a),
            "p90_h_ratio": round(hr, 6),
            "p90_area_ratio": round(ar, 6),
        }
    )

    # Si 90% des boxes sont très petites => bruit décoratif probable.
    if hr <= float(max_p90_height_ratio) and ar <= float(max_p90_area_ratio):
        return True, diag
    return False, diag


def _spellcheck_misspelled_words_fr(text: str) -> tuple[list[str], int]:
    """
    Retourne (mots_mal_orthographies, nb_mots_analyses).
    Si la dépendance n'est pas dispo, renvoie ([], 0) et laisse les heuristiques classiques faire le job.
    """
    t = (text or "").strip()
    if not t:
        return [], 0
    try:
        from spellchecker import SpellChecker  # type: ignore
    except Exception:
        return [], 0

    sc = SpellChecker(language="fr")
    words = [w.lower() for w in _WORD_RE.findall(t) if len(w) >= 3]
    if not words:
        return [], 0
    miss = sorted(set(sc.unknown(words)))
    return miss, len(words)


def image_has_meaningful_text(
    *,
    raw_text: str,
    min_chars: int,
    spellcheck: bool = True,
    misspelled_ratio_threshold: float = 0.34,
) -> tuple[bool, dict]:
    t = _normalize_for_len(raw_text)
    if len(t) < max(1, int(min_chars)):
        return False, {"alpha_chars": 0, "spell_words": 0, "misspelled_words": [], "misspelled_ratio": 0.0}
    # Réduit les faux positifs : ex. "AM" ou artefacts courts détectés dans du décor.
    # On exige au moins 3 caractères alphabétiques (Unicode) pour considérer le texte comme “significatif”.
    alpha_n = sum(1 for ch in t if ch.isalpha())
    if alpha_n < 3:
        return False, {"alpha_chars": int(alpha_n), "spell_words": 0, "misspelled_words": [], "misspelled_ratio": 0.0}

    miss: list[str] = []
    n_words = 0
    ratio = 0.0
    if spellcheck:
        miss, n_words = _spellcheck_misspelled_words_fr(t)
        ratio = (len(miss) / float(max(1, n_words))) if n_words else 0.0
        # Si tout est bien orthographié (ou presque), on ne considère pas ça comme une anomalie à traiter.
        if n_words and ratio < float(misspelled_ratio_threshold):
            return False, {"alpha_chars": int(alpha_n), "spell_words": int(n_words), "misspelled_words": miss, "misspelled_ratio": float(ratio)}

    # Sans spellcheck (ou si dépendance absente), on retombe sur l'heuristique alpha_n.
    return True, {"alpha_chars": int(alpha_n), "spell_words": int(n_words), "misspelled_words": miss, "misspelled_ratio": float(ratio)}


def audit_targets_for_text(
    *,
    gcs: storage.Client,
    bucket_name: str,
    targets: list[dict],
    vision_client: vision.ImageAnnotatorClient,
    max_workers: int = 8,
    min_chars: int = 2,
    download_bytes_fn=None,
) -> list[dict]:
    """
    Pour chaque cible avec fichier sur GCS : télécharge l'image et appelle Vision.

    Retourne une liste de dicts :
      date, gcs_path, has_text, detected_text, error

    `download_bytes_fn` injectable pour les tests (sinon `core.storage.download_bytes`).
    """
    from core.storage import download_bytes as _dl

    dl = download_bytes_fn or _dl

    rows_in: list[tuple[str, str, dict]] = []
    for t in targets:
        ds = str(t.get("date") or "").strip()[:10]
        bp = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
        if not bp:
            continue
        rows_in.append((ds, bp, t))

    out: list[dict] = []

    def job(ds: str, path: str, target: dict) -> dict:
        err: str | None = None
        txt = ""
        try:
            data = dl(gcs=gcs, bucket_name=bucket_name, path=path)
            # On appelle Vision ici pour pouvoir exploiter les bounding boxes (anti faux positifs décor).
            if not data:
                txt = ""
            else:
                img = vision.Image(content=data)
                resp = vision_client.text_detection(image=img)
                if resp.error.message:
                    raise RuntimeError(resp.error.message)
                anns = resp.text_annotations or []
                txt = str(anns[0].description or "").strip() if anns else ""
                is_noise, noise_diag = _looks_like_decor_noise(image_bytes=data, annotations=list(anns))
                if is_noise:
                    txt = ""
        except Exception as ex:
            err = str(ex)
        ht, diag = (False, {})
        if not err:
            ht, diag = image_has_meaningful_text(
                raw_text=txt,
                min_chars=min_chars,
                spellcheck=True,
                misspelled_ratio_threshold=0.34,
            )
        return {
            "date": ds,
            "gcs_path": path,
            "target": target,
            "has_text": ht,
            "detected_text": txt if ht else "",
            "spell_diag": diag,
            "error": err,
        }

    if not rows_in:
        return []

    workers = max(1, min(int(max_workers), len(rows_in)))
    if workers == 1:
        for ds, bp, t in rows_in:
            out.append(job(ds, bp, t))
        return out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {ex.submit(job, ds, bp, t): (ds, bp) for ds, bp, t in rows_in}
        for fut in as_completed(fut_map):
            out.append(fut.result())

    out.sort(key=lambda r: str(r.get("date") or ""))
    return out


def filter_rows_with_text(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("has_text")]


_ACTIVATION_URL_RE = re.compile(
    r"https://console\.(?:developers\.google\.com|cloud\.google\.com)[^\s\"'<>]+",
    re.I,
)


def is_vision_service_disabled_error(message: str | None) -> bool:
    """403 API désactivée / jamais activée sur le projet GCP."""
    if not message:
        return False
    m = message.lower()
    if "service_disabled" in m:
        return True
    if "403" in message and "vision" in m and ("disabled" in m or "not been used" in m):
        return True
    return False


def extract_console_url_from_error(message: str | None) -> str | None:
    if not message:
        return None
    mm = _ACTIVATION_URL_RE.search(message)
    return mm.group(0).rstrip(").',") if mm else None


def shorten_audit_error_message(message: str | None, max_len: int = 240) -> str:
    """Évite les tableaux illisibles avec des protobuf géants."""
    if not message:
        return ""
    if is_vision_service_disabled_error(message):
        return (
            "API Cloud Vision désactivée sur ce projet GCP. Active « Cloud Vision API » dans Google Cloud Console, "
            "attends quelques minutes, puis relance l’analyse."
        )
    one = " ".join(str(message).split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def all_errors_are_vision_service_disabled(rows: list[dict]) -> bool:
    errs = [r for r in rows if r.get("error")]
    if not errs:
        return False
    return all(is_vision_service_disabled_error(str(r.get("error") or "")) for r in errs)
