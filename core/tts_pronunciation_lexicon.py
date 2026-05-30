"""Construction du dictionnaire TTS à partir du cache lectures (RDC / ``readings_cache``)."""

from __future__ import annotations

import json
import re
from collections import Counter

from core.aelf_text_cleanup import clean_aelf_text_for_display

# Colonnes analysées (demande produit + évangile pour couvrir tout le lectionnaire).
READINGS_CACHE_TEXT_COLUMNS: tuple[str, ...] = (
    "jour_liturgique_nom",
    "premiere_lecture",
    "psaume",
    "deuxieme_lecture",
    "evangile",
)

# Formes TTS validées manuellement (prioritaires).
MANUAL_SPEAK_FORMS: dict[str, str] = {
    "Moïse": "Mo-ïse",
    "Ésaïe": "Ésa-ïe",
    "Caïn": "Ca-ïn",
    "Éphraïm": "Éphra-ïm",
    "Sinaï": "Si-naï",
    "Naïm": "Na-ïm",
    "Israël": "Isra-ël",
    "Bethléem": "Beth-léem",
    "Galilée": "Galil-ée",
    "Égypte": "É-gypte",
    "Élisée": "É-li-sée",
    "Ézéchiel": "É-zé-chiel",
    "Élisabeth": "É-li-sa-beth",
    "Gethsémani": "Geth-sé-mani",
    "Capharnaüm": "Caphar-na-üm",
    "Emmaüs": "Em-ma-üs",
    "Abel": "Abel",
    "Noé": "Noé",
    "Élie": "Élie",
    "Jésus": "Jésus",
    "Jérusalem": "Jérusalem",
    "Nazareth": "Nazareth",
    "Pharaon": "Pharaon",
    "Abraham": "Abraham",
    "Isaac": "Isaac",
    "Jacob": "Jacob",
    "Joseph": "Joseph",
    "Marie": "Marie",
    "David": "David",
    "Salomon": "Salomon",
    "Samuel": "Samuel",
    "Saül": "Saül",
    "Jean-Baptiste": "Jean-Baptiste",
    "Béthanie": "Bé-tha-nie",
    "Jéricho": "Jéricho",
    "Jourdain": "Jourdain",
    "Antioche": "Antioche",
    "Corinthe": "Corinthe",
    "Éphèse": "Éphèse",
    "Thessalonique": "Thessalonique",
    "Pharisiens": "Pharisiens",
    "Sadducéens": "Sadducéens",
    "Babylone": "Babylone",
    "Assyrie": "Assyrie",
    "Samarie": "Samarie",
    "Golgotha": "Golgotha",
    "Pilate": "Pilate",
    "César": "César",
}

# Mots capitalisés fréquents dans la Bible / liturgie mais sans correction TTS dédiée.
_KEEP_AS_IS_IF_FOUND: frozenset[str] = frozenset(
    {
        "Seigneur",
        "Dieu",
        "Esprit",
        "Père",
        "Fils",
        "Saint",
        "Sainte",
        "Saints",
        "Saintes",
        "Alleluia",
        "Amen",
        "Christ",
        "Apôtre",
        "Apôtres",
        "Prophète",
        "Prophètes",
        "Roi",
        "Reine",
        "Temple",
        "Synagogue",
        "Évangile",
        "Lecture",
        "Psaume",
        "Israélite",
        "Israélites",
        "Jérusalem",
        "Jésus",
    }
)

# Mots français / grammaticaux souvent capitalisés en début de phrase — à ignorer.
_SKIP_TOKENS: frozenset[str] = frozenset(
    {
        "Le",
        "La",
        "Les",
        "Un",
        "Une",
        "Des",
        "Du",
        "De",
        "Et",
        "Ou",
        "Car",
        "Mais",
        "Donc",
        "Or",
        "Ni",
        "Il",
        "Elle",
        "Ils",
        "Elles",
        "Nous",
        "Vous",
        "Car",
        "Ainsi",
        "Alors",
        "Voici",
        "Voilà",
        "Celui",
        "Celle",
        "Ceux",
        "Celles",
        "Cet",
        "Cette",
        "Ces",
        "Mon",
        "Ma",
        "Mes",
        "Ton",
        "Ta",
        "Tes",
        "Son",
        "Sa",
        "Ses",
        "Leur",
        "Leurs",
        "Qui",
        "Que",
        "Quoi",
        "Dont",
        "Où",
        "Car",
        "Parce",
        "Pour",
        "Dans",
        "Sur",
        "Sous",
        "Vers",
        "Chez",
        "Sans",
        "Avec",
        "Contre",
        "Entre",
        "Pendant",
        "Depuis",
        "Jusqu",
        "Comme",
        "Lorsque",
        "Quand",
        "Même",
        "Encore",
        "Tout",
        "Tous",
        "Toute",
        "Toutes",
        "Très",
        "Bien",
        "Peuple",
        "Homme",
        "Femme",
        "Enfant",
        "Enfants",
        "Frère",
        "Frères",
        "Sœur",
        "Soeur",
        "Maison",
        "Terre",
        "Ciel",
        "Cieux",
        "Jour",
        "Nuit",
        "An",
        "Ans",
        "Ans",
        "Voix",
        "Parole",
        "Loi",
        "Alliance",
        "Grâce",
        "Paix",
        "Amour",
        "Vie",
        "Mort",
        "Roi",
        "Rois",
        "Main",
        "Mains",
        "Yeux",
        "Cœur",
        "Coeur",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ''\-]*[A-Za-zÀ-ÖØ-öø-ÿ]|[A-Za-zÀ-ÖØ-öø-ÿ]")
_MARKUP_RE = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    s = _MARKUP_RE.sub(" ", text or "")
    return " ".join(s.split())


def _heuristic_speak_form(word: str) -> str | None:
    """Propose une graphie TTS si elle diffère du mot source."""
    w = (word or "").strip()
    if not w:
        return None
    if w in MANUAL_SPEAK_FORMS:
        return MANUAL_SPEAK_FORMS[w]
    out = w
    if "ï" in out:
        out = out.replace("ï", "-ï")
    if "ë" in out:
        out = out.replace("ë", "-ë")
    if out != w:
        return out
    return None


def extract_word_candidates(text: str) -> Counter[str]:
    """Compte les tokens dignes d'une entrée de dictionnaire."""
    plain = _strip_markup(clean_aelf_text_for_display(text))
    counts: Counter[str] = Counter()
    for m in _TOKEN_RE.finditer(plain):
        tok = m.group(0).strip("-'")
        if len(tok) < 3 or tok in _SKIP_TOKENS:
            continue
        if tok in _KEEP_AS_IS_IF_FOUND:
            continue
        needs = (
            any(ch in tok for ch in "ïëæœ")
            or (tok[0].isupper() and len(tok) >= 4)
            or tok in MANUAL_SPEAK_FORMS
        )
        if needs:
            counts[tok] += 1
    return counts


def corpus_from_readings_rows(rows: list[dict]) -> str:
    parts: list[str] = []
    for row in rows:
        for col in READINGS_CACHE_TEXT_COLUMNS:
            parts.append(str(row.get(col) or ""))
    return "\n\n".join(p for p in parts if p.strip())


def build_pronunciation_dict_from_readings_rows(
    rows: list[dict],
    *,
    min_count: int = 1,
    include_manual_always: bool = True,
) -> dict[str, str]:
    """
    Fusionne :
    - entrées manuelles (si le mot apparaît ou ``include_manual_always``),
    - mots extraits du corpus avec heuristique de syllabation.
    """
    counts: Counter[str] = Counter()
    for row in rows:
        for col in READINGS_CACHE_TEXT_COLUMNS:
            counts.update(extract_word_candidates(str(row.get(col) or "")))

    out: dict[str, str] = {}

    if include_manual_always:
        out.update(MANUAL_SPEAK_FORMS)
    else:
        for word, speak in MANUAL_SPEAK_FORMS.items():
            if counts.get(word, 0) >= min_count:
                out[word] = speak

    for word, n in counts.most_common():
        if n < min_count or word in out:
            continue
        speak = _heuristic_speak_form(word)
        if speak and speak != word:
            out[word] = speak

    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def pronunciation_dict_to_json_text(rules: dict[str, str]) -> str:
    return json.dumps(rules, ensure_ascii=False, indent=2) + "\n"
