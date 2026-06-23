from __future__ import annotations

from dataclasses import dataclass
import html as _html
import re
from typing import Any, Literal

import requests

from core.config import DEFAULT_AELF_BASE_URL, resolve_aelf_base_url


AelfZone = Literal["france"]


@dataclass(frozen=True)
class AelfDayIdentity:
    date: str
    zone: str
    periode: str | None
    semaine: str | None
    annee: str | None
    couleur: str | None
    fete: str | None
    jour_liturgique_nom: str | None


@dataclass(frozen=True)
class AelfReadingBlock:
    """Corps + métadonnées d'une lecture (champs AELF ``intro_lue``, ``ref``, etc.)."""

    body: str | None
    intro_lue: str | None = None
    ref: str | None = None
    titre: str | None = None
    refrain: str | None = None
    ref_refrain: str | None = None


@dataclass(frozen=True)
class AelfTexts:
    premiere_lecture: str | None
    psaume: str | None
    deuxieme_lecture: str | None
    evangile: str | None
    premiere_lecture_intro: str | None = None
    premiere_lecture_ref: str | None = None
    psaume_intro: str | None = None
    psaume_ref: str | None = None
    psaume_refrain: str | None = None
    psaume_ref_refrain: str | None = None
    deuxieme_lecture_intro: str | None = None
    deuxieme_lecture_ref: str | None = None
    evangile_intro: str | None = None
    evangile_ref: str | None = None


class AelfClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or resolve_aelf_base_url()).rstrip("/") or DEFAULT_AELF_BASE_URL
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "JOPAI-LumenVia/0.1"})

    def informations(self, date: str, zone: str = "france") -> AelfDayIdentity:
        url = f"{self.base_url}/v1/informations/{date}/{zone}"
        r = self._session.get(url, timeout=20)
        r.raise_for_status()
        payload: dict[str, Any] = r.json()
        data: dict[str, Any] = payload.get("informations", payload)
        return AelfDayIdentity(
            date=date,
            zone=zone,
            periode=_get_nested_str(data, ["temps_liturgique", "nom"])
            or _get_nested_str(data, ["periode", "nom"])
            or _get_str(data, "temps_liturgique")
            or _get_str(data, "periode"),
            semaine=_get_str(data, "semaine"),
            annee=_get_str(data, "annee") or _get_str(data, "annee_liturgique"),
            couleur=_get_str(data, "couleur"),
            fete=_get_str(data, "fete") or _get_str(data, "celebration"),
            jour_liturgique_nom=_get_str(data, "jour_liturgique_nom") or _get_str(data, "ligne1"),
        )

    def messes(self, date: str, zone: str = "france") -> AelfTexts:
        url = f"{self.base_url}/v1/messes/{date}/{zone}"
        r = self._session.get(url, timeout=20)
        r.raise_for_status()
        payload: dict[str, Any] = r.json()

        messes = payload.get("messes") or []
        first_messe = messes[0] if messes else payload

        lectures = (first_messe or {}).get("lectures") or []

        b1 = _extract_reading_block(lectures, ["lecture_1", "premiere_lecture", "lecture1"])
        bp = _extract_reading_block(lectures, ["psaume", "psalm"])
        b2 = _extract_reading_block(lectures, ["lecture_2", "deuxieme_lecture", "lecture2"])
        be = _extract_reading_block(lectures, ["evangile", "evangel"])
        return AelfTexts(
            premiere_lecture=b1.body,
            psaume=bp.body,
            deuxieme_lecture=b2.body,
            evangile=be.body,
            premiere_lecture_intro=b1.intro_lue,
            premiere_lecture_ref=b1.ref,
            psaume_intro=bp.intro_lue,
            psaume_ref=bp.ref,
            psaume_refrain=bp.refrain,
            psaume_ref_refrain=bp.ref_refrain,
            deuxieme_lecture_intro=b2.intro_lue,
            deuxieme_lecture_ref=b2.ref,
            evangile_intro=be.intro_lue,
            evangile_ref=be.ref,
        )


def fetch_aelf_day(date_str: str, zone: str = "france") -> tuple[AelfDayIdentity, AelfTexts]:
    """Appel AELF direct (sans cache Streamlit) — préféré pour les boucles bulk."""
    client = AelfClient()
    return client.informations(date_str, zone=zone), client.messes(date_str, zone=zone)


def is_aelf_not_found_error(exc: BaseException) -> bool:
    """True si l'API AELF ne publie pas encore cette date (HTTP 404)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 404
    msg = str(exc)
    return "404" in msg and "Not Found" in msg


def _get_str(d: dict[str, Any], k: str) -> str | None:
    v = d.get(k)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _get_nested_str(d: dict[str, Any], path: list[str]) -> str | None:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    if cur is None:
        return None
    s = str(cur).strip()
    return s or None


def _aelf_plain_field(value: Any) -> str | None:
    if value is None:
        return None
    s = _clean_aelf_html(str(value).strip())
    return s or None


def _reading_block_from_item(item: dict[str, Any]) -> AelfReadingBlock:
    txt = item.get("texte") or item.get("text") or item.get("contenu")
    body = _aelf_plain_field(txt)
    refrain_raw = item.get("refrain_psalmique")
    return AelfReadingBlock(
        body=body,
        intro_lue=_aelf_plain_field(item.get("intro_lue")),
        ref=_aelf_plain_field(item.get("ref")),
        titre=_aelf_plain_field(item.get("titre")),
        refrain=_aelf_plain_field(refrain_raw),
        ref_refrain=_aelf_plain_field(item.get("ref_refrain")),
    )


def _extract_reading_block(lectures: Any, keys: list[str]) -> AelfReadingBlock:
    empty = AelfReadingBlock(body=None)
    if isinstance(lectures, dict):
        for k in keys:
            v = lectures.get(k)
            if isinstance(v, dict):
                block = _reading_block_from_item(v)
                if block.body or block.intro_lue or block.ref:
                    return block
            elif isinstance(v, str) and v.strip():
                return AelfReadingBlock(body=_clean_aelf_html(v.strip()) or None)
        return empty

    if isinstance(lectures, list):
        wanted = {k.lower() for k in keys}
        for item in lectures:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type", "")).strip().lower()
            if t in wanted:
                block = _reading_block_from_item(item)
                if block.body or block.intro_lue or block.ref or block.refrain:
                    return block
        return empty

    return empty


def _extract_reading(lectures: Any, keys: list[str]) -> str | None:
    return _extract_reading_block(lectures, keys).body


_BR_RE = re.compile(r"(?i)<br\s*/?>")
_P_CLOSE_RE = re.compile(r"(?i)</p\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _clean_aelf_html(s: str) -> str:
    """
    L’API AELF renvoie souvent du HTML (p, br, strong, em…).
    On convertit en texte lisible (conserve les sauts de ligne principaux).
    """
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    # Le contenu AELF est souvent une suite de <p> très courts (1 ligne).
    # On évite donc les doubles sauts de ligne pour ne pas "aérer" excessivement.
    s = _P_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = s.replace("\u00a0", " ")
    # Normalise espaces et lignes.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Nettoie indentation "liturgique" (espaces multiples en début de ligne)
    s = "\n".join([line.lstrip() for line in s.split("\n")])
    s = re.sub(r"[ \t]+\n", "\n", s)
    # Pas plus d'une ligne vide consécutive
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"\n{2,}", "\n\n", s)
    return s.strip()

