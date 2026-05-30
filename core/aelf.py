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
class AelfTexts:
    premiere_lecture: str | None
    psaume: str | None
    deuxieme_lecture: str | None
    evangile: str | None


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

        return AelfTexts(
            premiere_lecture=_extract_reading(lectures, ["lecture_1", "premiere_lecture", "lecture1"]),
            psaume=_extract_reading(lectures, ["psaume", "psalm"]),
            deuxieme_lecture=_extract_reading(lectures, ["lecture_2", "deuxieme_lecture", "lecture2"]),
            evangile=_extract_reading(lectures, ["evangile", "evangel"]),
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


def _extract_reading(lectures: Any, keys: list[str]) -> str | None:
    # Cas 1: format dict (ancien / alternatif)
    if isinstance(lectures, dict):
        for k in keys:
            v = lectures.get(k)
            if isinstance(v, dict):
                txt = v.get("texte") or v.get("text") or v.get("contenu")
                if txt:
                    return _clean_aelf_html(str(txt).strip()) or None
            elif isinstance(v, str) and v.strip():
                return _clean_aelf_html(v.strip()) or None
        return None

    # Cas 2: format list (AELF courant): [{"type":"lecture_1", "contenu":"<p>...</p>", ...}, ...]
    if isinstance(lectures, list):
        wanted = {k.lower() for k in keys}
        for item in lectures:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type", "")).strip().lower()
            if t in wanted:
                txt = item.get("texte") or item.get("text") or item.get("contenu")
                if txt:
                    return _clean_aelf_html(str(txt).strip()) or None
        return None

    return None


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

