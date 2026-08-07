"""Localisation de la synthèse FR pivot → autres langues (traduction programme, pas rédaction IA).

Règle produit : une seule rédaction pastorale en français ; les autres langues
reçoivent une **traduction** du texte FR (pas une nouvelle génération Vertex).

Moteur : MyMemory (HTTP, gratuit, non-IA générative). Découpage par paragraphes.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests

from core.prompt_locale import (
    CATECHESE_TITLE_BY_LANG,
    PSALM_SECTION_TITLE,
    TAKEAWAYS_SECTION_TITLE,
    coerce_aip_langue,
)
from core.locale_codes import DEFAULT_PREF_LANGUE

_UA = "JOPAI-LumenVia-SynthesisLocalize/1.0"
_MYMEMORY = "https://api.mymemory.translated.net/get"
_MAX_CHUNK = 420


def _langpair(target: str) -> str:
    lg = coerce_aip_langue(target).lower()
    return f"fr|{lg}"


def _translate_chunk(text: str, *, target_lang: str) -> str:
    src = (text or "").strip()
    if not src:
        return ""
    lg = coerce_aip_langue(target_lang)
    if lg == DEFAULT_PREF_LANGUE:
        return src
    try:
        url = f"{_MYMEMORY}?q={quote(src)}&langpair={_langpair(lg)}"
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=45)
        r.raise_for_status()
        data = r.json() or {}
        out = str(((data.get("responseData") or {}).get("translatedText")) or "").strip()
        # MyMemory renvoie parfois le texte source en cas de quota.
        if not out or out.casefold() == src.casefold():
            return src
        # Détection message d'erreur MyMemory
        if "MYMEMORY WARNING" in out.upper():
            return src
        return out
    except Exception:
        return src


def _split_chunks(text: str) -> list[str]:
    parts = re.split(r"(\n\s*\n)", text or "")
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if len(buf) + len(part) <= _MAX_CHUNK:
            buf += part
            continue
        if buf.strip():
            chunks.append(buf)
        if len(part) <= _MAX_CHUNK:
            buf = part
        else:
            # Coupe dure sur phrases.
            sentences = re.split(r"(?<=[.!?…])\s+", part)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) + 1 <= _MAX_CHUNK:
                    buf = f"{buf} {s}".strip() if buf else s
                else:
                    if buf:
                        chunks.append(buf)
                    buf = s
            if buf:
                chunks.append(buf)
                buf = ""
    if buf.strip():
        chunks.append(buf)
    return chunks or ([text] if text else [])


def _map_known_headings(line: str, *, target_lang: str) -> str | None:
    """Remplace les titres de section FR connus par leur équivalent localisé."""
    lg = coerce_aip_langue(target_lang)
    raw = (line or "").strip()
    if not raw:
        return None
    # Markdown headings
    m = re.match(r"^(#{1,3})\s+(.+)$", raw)
    body = m.group(2).strip() if m else raw
    prefix = (m.group(1) + " ") if m else ""

    fr_take = TAKEAWAYS_SECTION_TITLE.get("FR") or "À retenir"
    fr_psalm = PSALM_SECTION_TITLE.get("FR") or "Le Psaume"
    fr_cat = CATECHESE_TITLE_BY_LANG.get("FR") or ""

    folded = body.casefold().replace("’", "'")
    if folded.startswith(fr_take.casefold()) or folded == "a retenir":
        return prefix + (TAKEAWAYS_SECTION_TITLE.get(lg) or body)
    if folded.startswith(fr_psalm.casefold()):
        return prefix + (PSALM_SECTION_TITLE.get(lg) or body)
    if fr_cat and folded.startswith(fr_cat.casefold().replace("’", "'")):
        return prefix + (CATECHESE_TITLE_BY_LANG.get(lg) or body)
    return None


def localize_plain_from_fr(
    text: str,
    *,
    target_lang: object,
    pause_s: float = 0.25,
) -> str:
    """Traduction programme FR → langue (MyMemory), sans bannière HTML."""
    lg = coerce_aip_langue(target_lang)
    src = text or ""
    if not src.strip() or lg == DEFAULT_PREF_LANGUE:
        return src
    pieces = _split_chunks(src)
    translated: list[str] = []
    for i, ch in enumerate(pieces):
        translated.append(_translate_chunk(ch, target_lang=lg))
        if pause_s and i + 1 < len(pieces):
            time.sleep(pause_s)
    return "\n".join(translated)


def localize_synthesis_from_fr(
    body_fr: str,
    *,
    target_lang: object,
    pause_s: float = 0.35,
) -> str:
    """
    Traduit le corps de synthèse FR vers ``target_lang`` (programme, hors rédaction IA).

    Les titres de sections liturgiques connus sont mappés via ``prompt_locale``.
    """
    lg = coerce_aip_langue(target_lang)
    src = (body_fr or "").strip()
    if not src or lg == DEFAULT_PREF_LANGUE:
        return src

    lines = src.replace("\r\n", "\n").split("\n")
    out_lines: list[str] = []
    para_buf: list[str] = []

    def _flush_para() -> None:
        nonlocal para_buf
        if not para_buf:
            return
        block = "\n".join(para_buf).strip()
        para_buf = []
        if not block:
            out_lines.append("")
            return
        pieces = _split_chunks(block)
        translated: list[str] = []
        for i, ch in enumerate(pieces):
            translated.append(_translate_chunk(ch, target_lang=lg))
            if pause_s and i + 1 < len(pieces):
                time.sleep(pause_s)
        out_lines.append("\n".join(translated).strip())

    for ln in lines:
        mapped = _map_known_headings(ln, target_lang=lg)
        if mapped is not None:
            _flush_para()
            out_lines.append(mapped)
            continue
        if not ln.strip():
            _flush_para()
            out_lines.append("")
            continue
        para_buf.append(ln)
    _flush_para()

    result = "\n".join(out_lines).strip()
    return result if result else src


_LOCALIZED_FROM_BANNER_RE = re.compile(
    r"<!--\s*localized_from_[^>]*-->",
    re.IGNORECASE,
)


def strip_localized_from_banners(text: str) -> str:
    """Retire les balises HTML `<!-- localized_from_… -->` (ne doivent jamais apparaître en PDF)."""
    cleaned = _LOCALIZED_FROM_BANNER_RE.sub("", text or "")
    return cleaned.strip()


__all__ = [
    "localize_plain_from_fr",
    "localize_synthesis_from_fr",
    "strip_localized_from_banners",
]
