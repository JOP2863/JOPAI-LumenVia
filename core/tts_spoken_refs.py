"""Oralisation des références bibliques pour le TTS (multi-langues)."""

from __future__ import annotations

import re

from core.prompt_locale import coerce_aip_langue

# Abréviations / formes FR fréquentes → libellé oral selon la langue cible.
_BOOK_ORAL: dict[str, dict[str, str]] = {
    "FR": {
        "Ps": "Psaume",
        "Psaume": "Psaume",
        "Salmo": "Psaume",
        "Psalm": "Psaume",
        "Is": "Isaïe",
        "Rm": "Romains",
        "Mt": "Matthieu",
        "Mc": "Marc",
        "Lc": "Luc",
        "Jn": "Jean",
        "He": "Hébreux",
        "Hb": "Hébreux",
        "Ap": "Apocalypse",
        "Ac": "Actes",
        "1 Co": "première lettre aux Corinthiens",
        "2 Co": "deuxième lettre aux Corinthiens",
        "1 Jn": "première lettre de Jean",
        "2 Jn": "deuxième lettre de Jean",
        "3 Jn": "troisième lettre de Jean",
        "1 P": "première lettre de Pierre",
        "2 P": "deuxième lettre de Pierre",
        "1 Th": "première lettre aux Thessaloniciens",
        "2 Th": "deuxième lettre aux Thessaloniciens",
        "1 Tm": "première lettre à Timothée",
        "2 Tm": "deuxième lettre à Timothée",
    },
    "DE": {
        "Ps": "Psalm",
        "Psaume": "Psalm",
        "Salmo": "Psalm",
        "Psalm": "Psalm",
        "Is": "Jesaja",
        "Rm": "Römer",
        "Mt": "Matthäus",
        "Mc": "Markus",
        "Lc": "Lukas",
        "Jn": "Johannes",
        "He": "Hebräer",
        "Hb": "Hebräer",
        "Ap": "Offenbarung",
        "Ac": "Apostelgeschichte",
        "1 Co": "Erster Korintherbrief",
        "2 Co": "Zweiter Korintherbrief",
        "1 Jn": "Erster Johannesbrief",
        "2 Jn": "Zweiter Johannesbrief",
        "3 Jn": "Dritter Johannesbrief",
        "1 P": "Erster Petrusbrief",
        "2 P": "Zweiter Petrusbrief",
        "1 Th": "Erster Thessalonicherbrief",
        "2 Th": "Zweiter Thessalonicherbrief",
        "1 Tm": "Erster Timotheusbrief",
        "2 Tm": "Zweiter Timotheusbrief",
    },
    "EN": {
        "Ps": "Psalm",
        "Psaume": "Psalm",
        "Salmo": "Psalm",
        "Psalm": "Psalm",
        "Is": "Isaiah",
        "Rm": "Romans",
        "Mt": "Matthew",
        "Mc": "Mark",
        "Lc": "Luke",
        "Jn": "John",
        "He": "Hebrews",
        "Hb": "Hebrews",
        "Ap": "Revelation",
        "Ac": "Acts",
        "1 Co": "First Corinthians",
        "2 Co": "Second Corinthians",
        "1 Jn": "First John",
        "2 Jn": "Second John",
        "3 Jn": "Third John",
        "1 P": "First Peter",
        "2 P": "Second Peter",
        "1 Th": "First Thessalonians",
        "2 Th": "Second Thessalonians",
        "1 Tm": "First Timothy",
        "2 Tm": "Second Timothy",
    },
    "ES": {
        "Ps": "Salmo",
        "Psaume": "Salmo",
        "Salmo": "Salmo",
        "Psalm": "Salmo",
        "Is": "Isaías",
        "Rm": "Romanos",
        "Mt": "Mateo",
        "Mc": "Marcos",
        "Lc": "Lucas",
        "Jn": "Juan",
        "He": "Hebreos",
        "Hb": "Hebreos",
        "Ap": "Apocalipsis",
        "Ac": "Hechos",
        "1 Co": "primera carta a los Corintios",
        "2 Co": "segunda carta a los Corintios",
        "1 Jn": "primera carta de Juan",
        "2 Jn": "segunda carta de Juan",
        "3 Jn": "tercera carta de Juan",
        "1 P": "primera carta de Pedro",
        "2 P": "segunda carta de Pedro",
        "1 Th": "primera carta a los Tesalonicenses",
        "2 Th": "segunda carta a los Tesalonicenses",
        "1 Tm": "primera carta a Timoteo",
        "2 Tm": "segunda carta a Timoteo",
    },
    "IT": {
        "Ps": "Salmo",
        "Psaume": "Salmo",
        "Salmo": "Salmo",
        "Psalm": "Salmo",
        "Is": "Isaia",
        "Rm": "Romani",
        "Mt": "Matteo",
        "Mc": "Marco",
        "Lc": "Luca",
        "Jn": "Giovanni",
        "He": "Ebrei",
        "Hb": "Ebrei",
        "Ap": "Apocalisse",
        "Ac": "Atti",
        "1 Co": "prima lettera ai Corinzi",
        "2 Co": "seconda lettera ai Corinzi",
        "1 Jn": "prima lettera di Giovanni",
        "2 Jn": "seconda lettera di Giovanni",
        "3 Jn": "terza lettera di Giovanni",
        "1 P": "prima lettera di Pietro",
        "2 P": "seconda lettera di Pietro",
        "1 Th": "prima lettera ai Tessalonicesi",
        "2 Th": "seconda lettera ai Tessalonicesi",
        "1 Tm": "prima lettera a Timoteo",
        "2 Tm": "seconda lettera a Timoteo",
    },
}

_VERSE_WORDS: dict[str, dict[str, str]] = {
    "FR": {"one": "verset", "many": "versets", "range": "à", "and": "et"},
    "DE": {"one": "Vers", "many": "Verse", "range": "bis", "and": "und"},
    "EN": {"one": "verse", "many": "verses", "range": "to", "and": "and"},
    "ES": {"one": "versículo", "many": "versículos", "range": "a", "and": "y"},
    "IT": {"one": "versetto", "many": "versetti", "range": "a", "and": "e"},
}

# Scories d’intro FR restantes dans un flux non FR.
_FR_INTRO_STEMS: tuple[tuple[str, dict[str, str]], ...] = (
    (
        r"(?i)^lecture du livre d['’]",
        {
            "DE": "Lesung aus dem Buch ",
            "EN": "A reading from the book of ",
            "ES": "Lectura del libro de ",
            "IT": "Lettura del libro di ",
        },
    ),
    (
        r"(?i)^lecture de la lettre (?:de |aux |à )?",
        {
            "DE": "Lesung aus dem Brief ",
            "EN": "A reading from the letter ",
            "ES": "Lectura de la carta ",
            "IT": "Lettura della lettera ",
        },
    ),
    (
        r"(?i)^lecture de l['’]évangile (?:de |selon )?",
        {
            "DE": "Lesung aus dem Evangelium nach ",
            "EN": "A reading from the holy Gospel according to ",
            "ES": "Lectura del santo Evangelio según ",
            "IT": "Lettura del santo Vangelo secondo ",
        },
    ),
)


def _oralize_verse_chunk(chunk: str, *, lg: str) -> str:
    words = _VERSE_WORDS.get(lg) or _VERSE_WORDS["FR"]
    raw = (chunk or "").strip(" .;")
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"[.;]\s*", raw) if p.strip()]
    spoken: list[str] = []
    for p in parts:
        m = re.match(r"^(\d+)\s*[-–—]\s*(\d+[a-zA-Z]?)\s*$", p)
        if m:
            spoken.append(f"{m.group(1)} {words['range']} {m.group(2)}")
            continue
        spoken.append(p)
    if not spoken:
        return ""
    if len(spoken) == 1:
        label = words["one"]
        return f"{label} {spoken[0]}"
    label = words["many"]
    if len(spoken) == 2:
        return f"{label} {spoken[0]} {words['and']} {spoken[1]}"
    return f"{label} {', '.join(spoken[:-1])} {words['and']} {spoken[-1]}"


def _expand_book_prefix(text: str, *, lg: str) -> str:
    books = _BOOK_ORAL.get(lg) or _BOOK_ORAL["FR"]
    t = (text or "").strip()
    for abbr in sorted(books.keys(), key=len, reverse=True):
        oral = books[abbr]
        # « Ps. 23 », « Psaume 67 », « 1 Co 13 »
        pat = re.compile(
            rf"^(?P<book>{re.escape(abbr)})\.?(?=\s|\d|$)",
            re.IGNORECASE,
        )
        m = pat.match(t)
        if m:
            rest = t[m.end() :].lstrip(" .")
            return f"{oral} {rest}".strip() if rest else oral
    return t


def _scrub_french_intro_stems(text: str, *, lg: str) -> str:
    if lg == "FR":
        return text
    t = text
    for pat, repl_by_lang in _FR_INTRO_STEMS:
        repl = repl_by_lang.get(lg)
        if not repl:
            continue
        t2, n = re.subn(pat, repl, t, count=1)
        if n:
            return t2
    return t


def spoken_biblical_ref_for_tts(ref: str, pref_langue: object | None = None) -> str:
    """
    Rend une référence missel plus parlable (livre + versets), dans ``pref_langue``.

    Ex. ``Ps 67(66),2-3.5.6.8.`` → ES ``Salmo 67, versículos 2 a 3, 5, 6 y 8``.
    """
    lg = coerce_aip_langue(pref_langue)
    t = (ref or "").strip()
    if not t:
        return t
    t = _scrub_french_intro_stems(t, lg=lg)
    t = _expand_book_prefix(t, lg=lg)
    # Numérotation double hébraïque / LXX : garder le premier chiffre.
    t = re.sub(r"(\d+)\s*\(\s*\d+\s*\)", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .")

    # Sépare chapitre et liste de versets : « Salmo 67,2-3.5.6.8 »
    m = re.match(
        r"^(?P<head>.+?\d+)\s*[,:]\s*(?P<verses>[\d][\d.\-–—a-zA-Z\s;]*?)\.?$",
        t,
    )
    if m:
        head = m.group("head").strip()
        verses = _oralize_verse_chunk(m.group("verses"), lg=lg)
        if verses:
            return f"{head}, {verses}."
        return f"{head}."

    if not t.endswith("."):
        t += "."
    return t


__all__ = ["spoken_biblical_ref_for_tts"]
