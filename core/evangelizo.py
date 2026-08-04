"""Evangelizo — Reader Feed v2 (textes messe multi-langues).

Source officielle documentée : ``https://feed.evangelizo.org/v2/reader.php``
(pas l’URL REST inventée ``levangileauquotidien.org/api/v1/...`` qui renvoie le shell HTML SPA).

Codes langue Evangelizo (≠ ISO 639-1) :
- FR, DE, IT
- AM = anglais US (pas ``EN``)
- SP = espagnol (pas ``ES``)

Horizon Reader : |date − aujourd’hui| ≤ ``EVANGELIZO_HORIZON_DAYS`` (30 j inclus).
Hors fenêtre → HTML « Error : wrong param « date » ».
Licence / ToS : à valider avant e-mail / TTS / PDF / prod.
"""

from __future__ import annotations

import html as html_lib
import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any
from xml.etree.ElementTree import Element

import requests

from core.aelf import AelfDayIdentity, AelfTexts

EVANGELIZO_READER_URL = "https://feed.evangelizo.org/v2/reader.php"
EVANGELIZO_HOME_URL = "https://levangileauquotidien.org/"
EVANGELIZO_DOC_URL = "https://feed.evangelizo.org/v2/reader.php"
_UA = "JOPAI-LumenVia-Evangelizo/0.1"

# Inclusive : delta −30…+30 OK ; −31 / +31 → wrong param date (probes 2026-08-03).
EVANGELIZO_HORIZON_DAYS = 30

# Product ISO 639-1 → code Reader Evangelizo
PRODUCT_LANG_TO_EVANGELIZO: dict[str, str] = {
    "FR": "FR",
    "DE": "DE",
    "IT": "IT",
    "EN": "AM",  # American English calendar
    "ES": "SP",  # Spanish
}

# Source registry id → Evangelizo lang code
SOURCE_ID_TO_EVANGELIZO_LANG: dict[str, str] = {
    "evangelizo_de": "DE",
    "evangelizo_en_am": "AM",
    "evangelizo_es_sp": "SP",
    "evangelizo_it": "IT",
}


class EvangelizoError(RuntimeError):
    """Erreur d’accès ou de parsing Evangelizo."""


class EvangelizoHorizonError(EvangelizoError):
    """Date hors horizon Reader (±30 j) ou paramètre invalide."""


def _parse_iso_date(date_iso: object) -> date | None:
    s = str(date_iso or "").strip()[:10]
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return None
    try:
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except Exception:
        return None


def is_within_evangelizo_horizon(
    date_iso: object,
    *,
    today: date | None = None,
    horizon_days: int = EVANGELIZO_HORIZON_DAYS,
) -> bool:
    """True si la date est dans la fenêtre Reader (± ``horizon_days`` autour d’aujourd’hui)."""
    d = date_iso if isinstance(date_iso, date) else _parse_iso_date(date_iso)
    if d is None:
        return False
    ref = today or date.today()
    return abs((d - ref).days) <= int(horizon_days)


def evangelizo_horizon_bounds(*, today: date | None = None) -> tuple[date, date]:
    ref = today or date.today()
    from datetime import timedelta

    return ref - timedelta(days=EVANGELIZO_HORIZON_DAYS), ref + timedelta(days=EVANGELIZO_HORIZON_DAYS)


def _date_compact(date_iso: str) -> str:
    return str(date_iso or "").strip()[:10].replace("-", "")


def _reader_html_error_message(raw: str) -> str | None:
    """Extrait le message rouge « Error : wrong param … » de la page doc Reader."""
    m = re.search(
        r"Error\s*:\s*wrong\s+param[^<]{0,240}",
        raw or "",
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return html_lib.unescape(re.sub(r"\s+", " ", m.group(0))).strip()


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _cdata_text(el: Element | None) -> str:
    if el is None:
        return ""
    return _strip_html("".join(el.itertext()))


def _find_child(root: Element, *names: str) -> Element | None:
    """Cherche un enfant direct (typos doc : litugic_t)."""
    wanted = {n.lower() for n in names}
    for child in list(root):
        if str(child.tag or "").lower() in wanted:
            return child
    # nested <evangelizo>
    for child in root.iter():
        if child is root:
            continue
        if str(child.tag or "").lower() in wanted:
            return child
    return None


def parse_evangelizo_xml(raw: str) -> dict[str, Any]:
    """Parse ``type=xml`` → dict plat (textes + refs + titre liturgique)."""
    s = (raw or "").strip()
    if not s:
        raise EvangelizoError("Réponse Evangelizo vide")
    low = s[:800].lower()
    html_err = _reader_html_error_message(s)
    if html_err:
        # Doc Reader : souvent « wrong param « date » !! Must be less than 30 days… »
        # (pas le code lang — AM/SP/DE/IT sont valides).
        raise EvangelizoHorizonError(html_err)
    if "error : wrong param" in low:
        raise EvangelizoHorizonError(
            "Paramètre Reader invalide (date hors horizon ±30 j, ou code lang)."
        )
    if s.startswith("<!DOCTYPE") or (low.startswith("<html") and "<?xml" not in low):
        raise EvangelizoHorizonError(
            "Réponse HTML (doc / erreur / hors horizon ±30 j), pas de XML messe."
        )
    i = s.find("<?xml")
    if i > 0:
        s = s[i:]
    try:
        root = ET.fromstring(s)
    except ET.ParseError as ex:
        raise EvangelizoError(f"XML illisible : {ex}") from ex

    # Conteneur <evangelizo> ou racine
    container = root
    for child in list(root):
        if str(child.tag or "").lower() == "evangelizo":
            container = child
            break

    def g(*tags: str) -> str:
        return _cdata_text(_find_child(container, *tags))

    return {
        "liturgic_t": g("liturgic_t", "litugic_t"),
        "saint": g("saint"),
        "date": g("date"),
        "reading_text1": g("reading_text1"),
        "reading_text1_lt": g("reading_text1_lt"),
        "reading_text1_st": g("reading_text1_st"),
        # Chez Evangelizo, reading_text2 = psaume
        "reading_text2": g("reading_text2"),
        "reading_text2_lt": g("reading_text2_lt"),
        "reading_text2_st": g("reading_text2_st"),
        "reading_text3": g("reading_text3"),
        "reading_text3_lt": g("reading_text3_lt"),
        "reading_text3_st": g("reading_text3_st"),
        "reading_gospel": g("reading_gospel"),
        "reading_gospel_lt": g("reading_gospel_lt"),
        "reading_gospel_st": g("reading_gospel_st"),
        "comment_t": g("comment_t"),
        "comment": g("comment"),
    }


def payload_to_aelf_texts(payload: dict[str, Any]) -> AelfTexts:
    # ``*_lt`` = titre long (intro lue) ; ``*_st`` = référence courte.
    return AelfTexts(
        premiere_lecture=payload.get("reading_text1") or None,
        psaume=payload.get("reading_text2") or None,
        deuxieme_lecture=payload.get("reading_text3") or None,
        evangile=payload.get("reading_gospel") or None,
        premiere_lecture_intro=payload.get("reading_text1_lt") or None,
        premiere_lecture_ref=payload.get("reading_text1_st") or None,
        psaume_intro=payload.get("reading_text2_lt") or None,
        psaume_ref=payload.get("reading_text2_st") or None,
        deuxieme_lecture_intro=payload.get("reading_text3_lt") or None,
        deuxieme_lecture_ref=payload.get("reading_text3_st") or None,
        evangile_intro=payload.get("reading_gospel_lt") or None,
        evangile_ref=payload.get("reading_gospel_st") or None,
    )


_ORDINAL_EN: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty-first": 21,
    "twenty-second": 22,
    "twenty-third": 23,
    "twenty-fourth": 24,
    "twenty-fifth": 25,
    "twenty-sixth": 26,
    "twenty-seventh": 27,
    "twenty-eighth": 28,
    "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty-first": 31,
    "thirty-second": 32,
    "thirty-third": 33,
    "thirty-fourth": 34,
}

_ROMAN: dict[str, int] = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "XI": 11,
    "XII": 12,
    "XIII": 13,
    "XIV": 14,
    "XV": 15,
    "XVI": 16,
    "XVII": 17,
    "XVIII": 18,
    "XIX": 19,
    "XX": 20,
    "XXI": 21,
    "XXII": 22,
    "XXIII": 23,
    "XXIV": 24,
    "XXV": 25,
    "XXVI": 26,
    "XXVII": 27,
    "XXVIII": 28,
    "XXIX": 29,
    "XXX": 30,
    "XXXI": 31,
    "XXXII": 32,
    "XXXIII": 33,
    "XXXIV": 34,
}


def _advent_sunday(year: int) -> date:
    """1er dimanche de l’Avent = dimanche entre le 27 nov. et le 3 déc."""
    from datetime import timedelta

    # Dimanche le plus proche de / sur le 30 novembre, dans [27 nov ; 3 déc].
    for day in range(27, 34):
        if day <= 30:
            d = date(year, 11, day)
        else:
            d = date(year, 12, day - 30)
        if d.weekday() == 6:  # Sunday
            return d
    return date(year, 11, 30)


def gospel_cycle_letter(date_iso: object) -> str | None:
    """Cycle A/B/C du lectionnaire dominical (année de Noël de l’année liturgique)."""
    d = date_iso if isinstance(date_iso, date) else _parse_iso_date(date_iso)
    if d is None:
        return None
    advent = _advent_sunday(d.year)
    christmas_year = d.year if d >= advent else d.year - 1
    # Advent 2022 → année A ; 2023 → B ; 2024 → C ; 2025 → A…
    return {0: "A", 1: "B", 2: "C"}.get(christmas_year % 3)


def infer_meta_from_evangelizo_title(
    title: str,
    *,
    evangelizo_lang: str,
    date_iso: str,
) -> dict[str, str | None]:
    """
    Déduit periode / semaine / couleur / annee depuis ``liturgic_t`` (+ cycle A/B/C calendaire).

    Evangelizo ne fournit pas ces champs : on les infère du titre localisé.
    """
    t = (title or "").strip()
    low = t.lower()
    lang = str(evangelizo_lang or "").strip().upper()

    periode: str | None = None
    couleur: str | None = None
    # Détection saison (libellé dans la langue du feed).
    if any(
        x in low
        for x in (
            "ordinary time",
            "jahreskreis",
            "tiempo ordinario",
            "tempo ordinario",
            "temps ordinaire",
        )
    ):
        periode = {
            "DE": "Jahreskreis",
            "AM": "Ordinary Time",
            "EN": "Ordinary Time",
            "SP": "Tiempo Ordinario",
            "ES": "Tiempo Ordinario",
            "IT": "Tempo Ordinario",
            "FR": "Temps Ordinaire",
        }.get(lang, "Temps Ordinaire")
        couleur = {
            "DE": "Grün",
            "AM": "Green",
            "EN": "Green",
            "SP": "Verde",
            "ES": "Verde",
            "IT": "Verde",
            "FR": "Vert",
        }.get(lang, "Vert")
    elif any(x in low for x in ("advent", "adventzeit", "adviento", "avvento", "avent")):
        periode = {
            "DE": "Advent",
            "AM": "Advent",
            "SP": "Adviento",
            "IT": "Avvento",
            "FR": "Avent",
        }.get(lang, "Avent")
        couleur = {
            "DE": "Violett",
            "AM": "Violet",
            "SP": "Morado",
            "IT": "Viola",
            "FR": "Violet",
        }.get(lang, "Violet")
    elif any(x in low for x in ("lent", "fastenzeit", "cuaresma", "quaresima", "carême", "careme")):
        periode = {
            "DE": "Fastenzeit",
            "AM": "Lent",
            "SP": "Cuaresma",
            "IT": "Quaresima",
            "FR": "Carême",
        }.get(lang, "Carême")
        couleur = {
            "DE": "Violett",
            "AM": "Violet",
            "SP": "Morado",
            "IT": "Viola",
            "FR": "Violet",
        }.get(lang, "Violet")
    elif any(x in low for x in ("easter", "oster", "pascua", "pasqua", "pâques", "paques")):
        periode = {
            "DE": "Osterzeit",
            "AM": "Easter Time",
            "SP": "Tiempo Pascual",
            "IT": "Tempo Pasquale",
            "FR": "Temps Pascal",
        }.get(lang, "Temps Pascal")
        couleur = {
            "DE": "Weiß",
            "AM": "White",
            "SP": "Blanco",
            "IT": "Bianco",
            "FR": "Blanc",
        }.get(lang, "Blanc")
    elif any(x in low for x in ("christmas", "weihnachten", "navidad", "natale", "noël", "noel")):
        periode = {
            "DE": "Weihnachten",
            "AM": "Christmas",
            "SP": "Navidad",
            "IT": "Natale",
            "FR": "Noël",
        }.get(lang, "Noël")
        couleur = {
            "DE": "Weiß",
            "AM": "White",
            "SP": "Blanco",
            "IT": "Bianco",
            "FR": "Blanc",
        }.get(lang, "Blanc")

    semaine: str | None = None
    m = re.search(
        r"\b(\d{1,2})\s*[ºªo°.]?\s*(?:sonntag|domingo|domenica|dimanche|sunday)\b",
        low,
    )
    if not m:
        m = re.search(r"\b(?:sonntag|domingo|domenica|dimanche|sunday)\s+(\d{1,2})\b", low)
    if m:
        semaine = m.group(1)
    if not semaine:
        m = re.search(r"\b([ivxlc]{1,7})\s+(?:domenica|domingo|dimanche)\b", t, flags=re.I)
        if m:
            semaine = str(_ROMAN.get(m.group(1).upper()) or "") or None
    if not semaine:
        for word, num in _ORDINAL_EN.items():
            if word in low:
                semaine = str(num)
                break

    return {
        "periode": periode,
        "semaine": semaine,
        "couleur": couleur,
        "annee": gospel_cycle_letter(date_iso),
    }


def payload_to_identity(
    payload: dict[str, Any],
    *,
    date_iso: str,
    evangelizo_lang: str,
) -> AelfDayIdentity:
    from core.readings_cache_loader import PRODUCT_LANG_TO_RDC_ZONE

    title = str(payload.get("liturgic_t") or "").strip()
    saint = str(payload.get("saint") or "").strip()
    # Code Reader → langue produit pour zone pays
    reader_to_product = {"DE": "DE", "AM": "EN", "SP": "ES", "IT": "IT", "FR": "FR"}
    product = reader_to_product.get(str(evangelizo_lang or "").strip().upper(), "FR")
    zone = PRODUCT_LANG_TO_RDC_ZONE.get(product) or f"evangelizo_{str(evangelizo_lang).lower()}"

    meta = infer_meta_from_evangelizo_title(
        title, evangelizo_lang=evangelizo_lang, date_iso=date_iso
    )
    # Fête = titre liturgique ; jour = saint du jour si distinct, sinon même titre.
    fete = title or saint or None
    jour = saint if saint and saint.lower() != (title or "").lower() else (title or saint or None)

    return AelfDayIdentity(
        date=str(date_iso or "")[:10],
        zone=zone,
        periode=meta.get("periode"),
        semaine=meta.get("semaine"),
        annee=meta.get("annee"),
        couleur=meta.get("couleur"),
        fete=fete,
        jour_liturgique_nom=jour,
    )


def is_full_mass(texts: AelfTexts) -> bool:
    def _ok(v: object) -> bool:
        return bool(str(v or "").strip()) and len(str(v).strip()) > 40

    return _ok(texts.premiere_lecture) and _ok(texts.psaume) and _ok(texts.evangile)


def attribution_html() -> str:
    return (
        'Source: <a href="https://levangileauquotidien.org/" rel="noopener noreferrer">'
        "L’Évangile au Quotidien (Evangelizo)</a> — Reader Feed."
    )


def attribution_plain() -> str:
    return "Source: L’Évangile au Quotidien (Evangelizo) — https://levangileauquotidien.org/"


def format_xml_url(*, date_iso: str, evangelizo_lang: str) -> str:
    lang = str(evangelizo_lang or "").strip().upper()
    return (
        f"{EVANGELIZO_READER_URL}"
        f"?date={_date_compact(date_iso)}"
        f"&type=xml&lang={lang}"
    )


def fetch_evangelizo_mass(
    date_iso: str,
    *,
    evangelizo_lang: str,
    timeout_s: float = 20.0,
    skip_horizon_check: bool = False,
) -> tuple[AelfDayIdentity, AelfTexts, dict[str, Any]]:
    """
    Récupère la messe Evangelizo (XML) pour ``date_iso`` et un code langue Reader.
    """
    lang = str(evangelizo_lang or "").strip().upper()
    if not lang:
        raise EvangelizoError("Code langue Evangelizo manquant")
    if not skip_horizon_check and not is_within_evangelizo_horizon(date_iso):
        lo, hi = evangelizo_horizon_bounds()
        raise EvangelizoHorizonError(
            f"Date {str(date_iso)[:10]} hors horizon Evangelizo "
            f"({lo.isoformat()} … {hi.isoformat()}, ±{EVANGELIZO_HORIZON_DAYS} j)."
        )
    url = format_xml_url(date_iso=date_iso, evangelizo_lang=lang)
    try:
        r = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": _UA, "Accept": "application/xml, text/xml, */*"},
            allow_redirects=True,
        )
    except requests.RequestException as ex:
        raise EvangelizoError(f"HTTP Evangelizo : {ex}") from ex
    if r.status_code == 404:
        raise EvangelizoError(f"Evangelizo 404 pour lang={lang} date={date_iso[:10]}")
    if r.status_code >= 400:
        raise EvangelizoError(f"Evangelizo HTTP {r.status_code} pour {url}")
    # Encoding: responses often mislabeled; prefer UTF-8
    raw = r.content.decode("utf-8", errors="replace")
    payload = parse_evangelizo_xml(raw)
    texts = payload_to_aelf_texts(payload)
    if not is_full_mass(texts):
        # Hors horizon / jour sans texte : souvent corps HTML d’erreur déjà levé ;
        # sinon textes trop courts.
        total = sum(len(str(x or "")) for x in (texts.premiere_lecture, texts.psaume, texts.evangile))
        if total < 200:
            raise EvangelizoHorizonError(
                f"Messe incomplète / hors horizon pour {lang} {date_iso[:10]} "
                f"(L1+Ps+Év ≈ {total} car.)."
            )
    ident = payload_to_identity(payload, date_iso=date_iso, evangelizo_lang=lang)
    return ident, texts, payload


def fetch_evangelizo_for_product_lang(
    date_iso: str,
    *,
    pref_langue: str,
    timeout_s: float = 20.0,
) -> tuple[AelfDayIdentity, AelfTexts, dict[str, Any]]:
    product = str(pref_langue or "").strip().upper()
    e_lang = PRODUCT_LANG_TO_EVANGELIZO.get(product)
    if not e_lang:
        raise EvangelizoError(f"Langue produit non mappée Evangelizo : {product}")
    return fetch_evangelizo_mass(date_iso, evangelizo_lang=e_lang, timeout_s=timeout_s)
