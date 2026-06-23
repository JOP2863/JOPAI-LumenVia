"""Préparation texte pour TTS des lectures et de la synthèse (Vertex + Gemini API)."""

from __future__ import annotations

import re

from core.aelf_reading_meta import (
    encode_readings_tts_meta_line,
    liturgy_tts_sections_from_texts,
    oral_reading_intro_phrase,
    split_readings_tts_body_meta,
)
from core.aelf_text_cleanup import clean_aelf_text_for_display
from core.catechese_section_strip import (
    CATECHESE_SECTION_TITLE,
    CATECHESE_SECTION_TITLES,
    find_catechese_section_index,
    strip_catechese_title_prefix,
)
from core.tts_pronunciation import apply_tts_pronunciation

# Clés ``Paramètres_IA`` (Levier B) : documentation admin / choix de voix — jamais lues à voix haute.
AUDIO_STYLE_TEMPLATE_KEYS = frozenset(
    {
        "audio_style_default",
        "audio_style_paques",
        "audio_style_careme",
        "audio_style_lectures",
    }
)

# Débuts typiques des consignes ``audio_style_*`` (anciennes versions les concaténaient au TTS).
_TTS_ADMIN_PREAMBLE_PREFIXES: tuple[str, ...] = (
    "tu es lecteur du lectionnaire",
    "tu es la voix de lumenvia",
    "lis le texte suivant en français",
    "accent léger de joie",
    "garde une gravité paisible",
)

# Titres injectés par ``plain_readings_for_tts`` — point de départ du contenu parlé.
_READINGS_TTS_SECTION_MARKERS: tuple[str, ...] = (
    "Première lecture",
    "Premiere lecture",
    "Psaume",
    "Deuxième lecture",
    "Deuxieme lecture",
    "Évangile",
    "Evangile",
)

_LITURGY_SECTION_LINE_RE = re.compile(
    r"^(Première lecture|Premiere lecture|Psaume|Deuxième lecture|Deuxieme lecture|Évangile|Evangile)\.\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


# Annonce de repli si l'API AELF ne fournit pas ``intro_lue`` (cache ancien).
PREMIERE_LECTURE_TTS_INTRO_FALLBACK = "Première lecture. Écoutez la première lecture de la Parole."


def premiere_lecture_tts_intro(intro_lue: str | None = None) -> str:
    return oral_reading_intro_phrase("Première lecture", intro_lue=intro_lue)


_SYNTHESIS_TTS_HEADINGS: tuple[str, ...] = (
    "Le Psaume",
    "À retenir",
    *CATECHESE_SECTION_TITLES,
)

_SYNTHESIS_HEADING_SPLIT_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:#{1,3}\s*|\*\*)?\s*("
    + "|".join(re.escape(h) for h in _SYNTHESIS_TTS_HEADINGS)
    + r")(?:\*\*)?\s*(?=\n|$)"
)


def normalize_liturgy_section_title(title: str) -> str:
    """Libellé oral canonique pour annoncer une section du lectionnaire."""
    low = (title or "").strip().lower()
    if low.startswith("première") or low.startswith("premiere"):
        return "Première lecture"
    if low.startswith("deuxième") or low.startswith("deuxieme"):
        return "Deuxième lecture"
    if low.startswith("psaume"):
        return "Psaume"
    if low.startswith("évangile") or low.startswith("evangile"):
        return "Évangile"
    return (title or "").strip()


def liturgy_section_oral_announcement(
    title: str,
    *,
    intro_lue: str | None = None,
    ref: str | None = None,
) -> str:
    """Annonce orale d'une césure liturgique ou d'une sous-section de synthèse."""
    raw = (title or "").strip()
    if raw.lower().startswith("à retenir"):
        return "À retenir."
    norm = normalize_liturgy_section_title(raw)
    if norm in ("Première lecture", "Deuxième lecture", "Psaume", "Évangile") or raw.lower() == "le psaume":
        return oral_reading_intro_phrase(norm or raw, intro_lue=intro_lue, ref=ref)
    return f"{raw}." if raw and not raw.endswith(".") else raw


def dedupe_tts_section_body(
    section_title: str,
    body: str,
    *,
    intro_lue: str | None = None,
) -> str:
    """
    Retire un début de corps redondant avec l'annonce de section.

    Ex. annonce « Le Psaume. » + corps « Le psaume exprime… » → « Il exprime… ».
    """
    text = " ".join((body or "").split())
    if not text:
        return text

    norm = normalize_liturgy_section_title(section_title)
    raw = (section_title or "").strip()
    stems: list[tuple[str, str | None]] = []

    if norm == "Psaume" or raw.lower() == "le psaume":
        stems = [
            ("le psaume", "Il"),
            ("psaume", "Il"),
            ("ce psaume", "Il"),
        ]
    elif norm == "Première lecture":
        stems = [
            ("la première lecture", "Elle"),
            ("première lecture", "Elle"),
            ("premiere lecture", "Elle"),
        ]
    elif norm == "Deuxième lecture":
        stems = [
            ("la deuxième lecture", "Elle"),
            ("deuxième lecture", "Elle"),
            ("deuxieme lecture", "Elle"),
        ]
    elif norm == "Évangile":
        stems = [
            ("l'évangile", "Il"),
            ("l'evangile", "Il"),
            ("évangile", "Il"),
            ("evangile", "Il"),
        ]
    elif raw.lower().startswith("à retenir"):
        stems = [("à retenir", None), ("a retenir", None)]

    low = text.lower()
    for stem, pronoun in stems:
        if not low.startswith(stem):
            continue
        rest = text[len(stem) :].lstrip(" ,:;.-")
        if not rest:
            return text
        if pronoun:
            return f"{pronoun} {rest}"
        return rest

    intro = (intro_lue or "").strip()
    if intro:
        intro_low = intro.lower().rstrip(".")
        if low.startswith(intro_low):
            rest = text[len(intro_low) :].lstrip(" ,:;.-")
            if rest:
                return rest
    return text


def _canonical_synthesis_section_title(raw: str) -> str:
    t = (raw or "").strip()
    low = t.lower()
    if low == "le psaume":
        return "Le Psaume"
    if low.startswith("à retenir"):
        return "À retenir"
    for cate in CATECHESE_SECTION_TITLES:
        if low.startswith(cate.lower()):
            return CATECHESE_SECTION_TITLE
    return t


def parse_synthesis_tts_sections(text: str) -> list[tuple[str, str]] | None:
    """
    Découpe une synthèse en sections pour TTS (« Le Psaume », « À retenir », passerelle…).

    Retourne ``None`` si aucune sous-section détectée (texte continu).
    """
    t = (text or "").strip()
    if not t:
        return None

    matches = list(_SYNTHESIS_HEADING_SPLIT_RE.finditer(t))
    if not matches:
        idx = find_catechese_section_index(t)
        if idx < 0:
            return None
        before = t[:idx].strip()
        cate_body = strip_catechese_title_prefix(t[idx:].strip())
        out: list[tuple[str, str]] = []
        if before:
            out.append(("", before))
        if cate_body:
            out.append((CATECHESE_SECTION_TITLE, cate_body))
        return out if out else None

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        lead = t[: matches[0].start()].strip()
        if lead:
            sections.append(("", lead))

    for i, match in enumerate(matches):
        title = _canonical_synthesis_section_title(match.group(1))
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        body = t[start:end].strip()
        if title == CATECHESE_SECTION_TITLE:
            body = strip_catechese_title_prefix(f"{match.group(1)}\n{body}")
        if body or title == CATECHESE_SECTION_TITLE:
            sections.append((title, body))

    if not sections:
        return None
    if len(sections) == 1 and not sections[0][0]:
        return None
    return sections


def _trim_to_first_liturgy_section(text: str) -> str:
    """Coupe tout texte parasite avant « Première lecture. » (consignes / morceaux orphelins)."""
    t = (text or "").strip()
    if not t:
        return t
    m = re.search(r"(?i)\b(Première lecture|Premiere lecture)\.", t)
    if m and m.start() > 0:
        return t[m.start() :].strip()
    return t


def is_liturgy_readings_tts_text(text: str) -> bool:
    """True si le texte provient de ``plain_readings_for_tts`` (lectionnaire dominical)."""
    return bool(re.match(r"(?i)^(?:Première|Premiere) lecture\b", (text or "").strip()))


def parse_liturgy_reading_sections(text: str) -> list[tuple[str, str]]:
    """
    Découpe le texte ``plain_readings_for_tts`` en sections ``(titre, corps)``.

    Chaque paragraphe commence par « Première lecture. », « Psaume. », etc.
    """
    out: list[tuple[str, str]] = []
    for para in (text or "").split("\n\n"):
        p = " ".join(para.split())
        if not p:
            continue
        m = _LITURGY_SECTION_LINE_RE.match(p)
        if m:
            out.append(
                (
                    normalize_liturgy_section_title(m.group(1)),
                    (m.group(2) or "").strip(),
                )
            )
        else:
            out.append(("", p))
    return out


def coalesce_liturgy_reading_sections(text: str) -> list[tuple[str, str]]:
    """
    Fusionne les paragraphes orphelins dans la section liturgique précédente.

    Corrige le cas « Première lecture. » (titre seul) suivi du corps sur le paragraphe
    suivant — sans quoi le TTS lit le corps sans annoncer la section.
    """
    sections = parse_liturgy_reading_sections(text)
    if not sections:
        return []

    merged: list[tuple[str, str]] = []
    pending = ""

    for title, body in sections:
        body = (body or "").strip()
        if title:
            full_body = body
            if pending:
                full_body = (pending + "\n\n" + body).strip() if body else pending
                pending = ""
            merged.append((title, full_body))
        elif merged:
            prev_t, prev_b = merged[-1]
            extra = body
            if pending:
                extra = (pending + "\n\n" + body).strip() if body else pending
                pending = ""
            merged[-1] = (prev_t, (prev_b + "\n\n" + extra).strip() if prev_b else extra)
        else:
            pending = (pending + "\n\n" + body).strip() if pending and body else (body or pending)

    if pending and merged:
        merged.insert(0, ("Première lecture", pending))
    elif pending:
        merged.insert(0, ("Première lecture", pending))

    fixed: list[tuple[str, str]] = []
    i = 0
    while i < len(merged):
        title, body = merged[i]
        if title and not body and i + 1 < len(merged) and not merged[i + 1][0]:
            _, orphan_body = merged[i + 1]
            fixed.append((title, orphan_body))
            i += 2
            continue
        fixed.append((title, body))
        i += 1
    return fixed


def strip_tts_admin_preamble(text: str) -> str:
    """
    Retire une consigne ``audio_style_*`` en tête si elle a été concaténée (régression ou cache).

    Ne modifie pas un texte qui commence déjà par une section liturgique seule.
    """
    t = (text or "").strip()
    if not t:
        return t
    # Consigne collée juste après « Première lecture. » (cache Sheets / ancien pipeline).
    t = re.sub(
        r"(?is)^(Première lecture|Premiere lecture)\.\s*"
        r"tu es lecteur du lectionnaire[^.]*\.\s*",
        r"\1. ",
        t,
    )
    low_head = t[:400].lower()
    has_admin_lead = any(p in low_head for p in _TTS_ADMIN_PREAMBLE_PREFIXES) or low_head.startswith(
        "tu es "
    )
    if not has_admin_lead:
        return t
    trimmed = _trim_to_first_liturgy_section(t)
    if trimmed != t:
        return trimmed
    positions = [t.find(marker) for marker in _READINGS_TTS_SECTION_MARKERS if t.find(marker) >= 0]
    if positions:
        return t[min(positions) :].strip()
    # Synthèse ou autre : retirer le premier paragraphe « consigne » si plusieurs blocs.
    parts = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    while len(parts) > 1:
        head = parts[0].lower()
        if any(head.startswith(p) for p in _TTS_ADMIN_PREAMBLE_PREFIXES):
            parts.pop(0)
            continue
        break
    return "\n\n".join(parts).strip() if parts else t


def spoken_text_for_tts(body: str) -> str:
    """
    Texte envoyé tel quel à Vertex ou Gemini TTS.

    Les modèles ne distinguent pas « consigne » et « contenu » : tout le champ ``text``
    est prononcé. Le style oral est porté par ``Voix_Audio`` (nom de voix), pas par les
    clés ``audio_style_*`` dans Sheets.

    Le dictionnaire ``data/tts_pronunciation_fr.json`` (+ clé ``tts_pronunciation`` dans
    ``Paramètres_IA``) est appliqué ici pour corriger certaines prononciations (ex. Moïse).
    """
    cleaned = strip_tts_admin_preamble((body or "").strip())
    cleaned = _trim_to_first_liturgy_section(cleaned)
    return apply_tts_pronunciation(cleaned)


def plain_readings_for_tts(texts: object) -> str:
    """Texte continu pour TTS des quatre lectures AELF (sans HTML)."""
    parts: list[str] = []
    for sec in liturgy_tts_sections_from_texts(texts):
        raw = clean_aelf_text_for_display(sec.body or "")
        raw = re.sub(r"<[^>]+>", " ", raw)
        if sec.title == "Psaume":
            raw = re.sub(r"[ \t]+\n", "\n", raw)
            raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
        else:
            raw = " ".join(raw.split())
        raw = strip_tts_admin_preamble(raw)
        if not raw.strip():
            continue
        if len(raw.strip()) < 12 and sec.title.lower().startswith("psaume"):
            continue
        block = f"{sec.title}."
        meta = encode_readings_tts_meta_line(intro_lue=sec.intro_lue, ref=sec.ref)
        if meta:
            block = f"{block}\n{meta}"
        block = f"{block}\n\n{raw.strip()}"
        parts.append(block)
    return strip_tts_admin_preamble("\n\n".join(parts).strip())


def compose_synthesis_tts_text(
    *,
    body: str,
    templates: dict[str, str] | None = None,
    periode: str | None = None,
) -> str:
    """Texte lu pour l’audio de la synthèse (sans préfixes ``audio_style_*``)."""
    del templates, periode  # compatibilité des appels existants
    return spoken_text_for_tts(body)


def compose_readings_tts_text(*, body: str, templates: dict[str, str] | None = None) -> str:
    """Texte lu pour l’audio des lectures intégrales (sans préfixe ``audio_style_lectures``)."""
    del templates
    return spoken_text_for_tts(body)
