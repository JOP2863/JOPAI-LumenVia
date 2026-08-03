"""Chemins GCS localisés par ``pref_langue`` (ISO 639-1, majuscules en feuille).

Structure cible ::

  Syntheses/{lang}/{date}/{entity}.txt
  Audio/{lang}/{date}/{entity}.{ext}
  AudioLectures/{lang}/{date}/{entity}.{ext}
  Fascicules/{lang}/{date}/lumenvia_dimanche_{date}.pdf
  Images/illustrations/{lang}/{year}/{date}.{ext}

Lecture : chemins localisés (MAJ + min) puis chemins historiques sans langue.
"""

from __future__ import annotations

from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue


def _lang(pref_langue: object | None) -> str:
    return normalize_pref_langue(pref_langue)


def _lang_path_variants(lang: str) -> list[str]:
    """Majuscules (canonique) + minuscules (chemins déjà publiés éventuels)."""
    u = normalize_pref_langue(lang)
    low = u.lower()
    out = [u]
    if low != u:
        out.append(low)
    return out


def _uniq(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def synthesis_text_path(date_str: str, gen_entity_id: str, *, pref_langue: object | None = None) -> str:
    lang = _lang(pref_langue)
    return f"Syntheses/{lang}/{date_str}/{gen_entity_id}.txt"


def synthesis_text_path_candidates(
    date_str: str, gen_entity_id: str, *, pref_langue: object | None = None
) -> list[str]:
    lang = _lang(pref_langue)
    out: list[str] = []
    for lg in _lang_path_variants(lang):
        out.append(f"Syntheses/{lg}/{date_str}/{gen_entity_id}.txt")
    out.append(f"Syntheses/{date_str}/{gen_entity_id}.txt")
    if lang != DEFAULT_PREF_LANGUE:
        for lg in _lang_path_variants(DEFAULT_PREF_LANGUE):
            out.append(f"Syntheses/{lg}/{date_str}/{gen_entity_id}.txt")
    return _uniq(out)


def audio_synth_path(
    date_str: str, gen_entity_id: str, ext: str, *, pref_langue: object | None = None
) -> str:
    lang = _lang(pref_langue)
    e = (ext or "wav").lstrip(".")
    return f"Audio/{lang}/{date_str}/{gen_entity_id}.{e}"


def audio_synth_path_candidates(
    date_str: str, gen_entity_id: str, ext: str, *, pref_langue: object | None = None
) -> list[str]:
    lang = _lang(pref_langue)
    e = (ext or "wav").lstrip(".")
    out: list[str] = []
    for lg in _lang_path_variants(lang):
        out.append(f"Audio/{lg}/{date_str}/{gen_entity_id}.{e}")
    out.append(f"Audio/{date_str}/{gen_entity_id}.{e}")
    if lang != DEFAULT_PREF_LANGUE:
        for lg in _lang_path_variants(DEFAULT_PREF_LANGUE):
            out.append(f"Audio/{lg}/{date_str}/{gen_entity_id}.{e}")
    return _uniq(out)


def audio_readings_path(
    date_str: str, gen_entity_id: str, ext: str, *, pref_langue: object | None = None
) -> str:
    lang = _lang(pref_langue)
    e = (ext or "wav").lstrip(".")
    return f"AudioLectures/{lang}/{date_str}/{gen_entity_id}.{e}"


def audio_readings_path_candidates(
    date_str: str, gen_entity_id: str, ext: str, *, pref_langue: object | None = None
) -> list[str]:
    lang = _lang(pref_langue)
    e = (ext or "wav").lstrip(".")
    out: list[str] = []
    for lg in _lang_path_variants(lang):
        out.append(f"AudioLectures/{lg}/{date_str}/{gen_entity_id}.{e}")
    out.append(f"AudioLectures/{date_str}/{gen_entity_id}.{e}")
    if lang != DEFAULT_PREF_LANGUE:
        for lg in _lang_path_variants(DEFAULT_PREF_LANGUE):
            out.append(f"AudioLectures/{lg}/{date_str}/{gen_entity_id}.{e}")
    return _uniq(out)


def fascicule_pdf_path(date_str: str, *, pref_langue: object | None = None) -> str:
    lang = _lang(pref_langue)
    return f"Fascicules/{lang}/{date_str}/lumenvia_dimanche_{date_str}.pdf"


def fascicule_pdf_path_candidates(date_str: str, *, pref_langue: object | None = None) -> list[str]:
    lang = _lang(pref_langue)
    out: list[str] = []
    for lg in _lang_path_variants(lang):
        out.append(f"Fascicules/{lg}/{date_str}/lumenvia_dimanche_{date_str}.pdf")
    out.append(f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf")
    if lang != DEFAULT_PREF_LANGUE:
        for lg in _lang_path_variants(DEFAULT_PREF_LANGUE):
            out.append(f"Fascicules/{lg}/{date_str}/lumenvia_dimanche_{date_str}.pdf")
    return _uniq(out)


def illustration_path_candidates(
    date_str: str, *, pref_langue: object | None = None, exts: tuple[str, ...] = (".webp", ".png", ".jpg", ".jpeg")
) -> list[str]:
    lang = _lang(pref_langue)
    year = date_str[:4]
    out: list[str] = []
    for lg in _lang_path_variants(lang):
        for ext in exts:
            out.append(f"Images/illustrations/{lg}/{year}/{date_str}{ext}")
    for ext in exts:
        out.append(f"Images/illustrations/{year}/{date_str}{ext}")
    if lang != DEFAULT_PREF_LANGUE:
        for lg in _lang_path_variants(DEFAULT_PREF_LANGUE):
            for ext in exts:
                out.append(f"Images/illustrations/{lg}/{year}/{date_str}{ext}")
    return _uniq(out)


def illustration_primary_path(
    date_str: str, ext: str = ".png", *, pref_langue: object | None = None
) -> str:
    lang = _lang(pref_langue)
    year = date_str[:4]
    e = ext if str(ext).startswith(".") else f".{ext}"
    return f"Images/illustrations/{lang}/{year}/{date_str}{e}"
