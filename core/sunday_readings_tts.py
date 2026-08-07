"""Préparation texte pour TTS des lectures et de la synthèse (Vertex + Gemini API)."""

from __future__ import annotations

import hashlib
import re
from datetime import date

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
from core.prompt_locale import (
    DEFAULT_PREF_LANGUE,
    all_tts_reading_section_titles,
    canonical_reading_section_key,
    coerce_aip_langue,
    detect_pref_langue_from_section_title,
    fr_canonical_reading_title,
    pdf_titles,
    tts_fallback_intro,
)
from core.tts_pronunciation import apply_tts_pronunciation
from core.voix_audio import norm_slug

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

# Titres injectés par ``plain_readings_for_tts`` (FR + DE/EN/ES/IT/PT).
_READINGS_TTS_SECTION_MARKERS: tuple[str, ...] = all_tts_reading_section_titles()

_LITURGY_SECTION_TITLE_ALT = "|".join(
    re.escape(t) for t in sorted(_READINGS_TTS_SECTION_MARKERS, key=len, reverse=True)
)
_LITURGY_SECTION_LINE_RE = re.compile(
    rf"^({_LITURGY_SECTION_TITLE_ALT})\.\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_LITURGY_FIRST_SECTION_RE = re.compile(
    r"(?i)\b("
    + "|".join(
        re.escape(t)
        for t in (
            "Première lecture",
            "Premiere lecture",
            "Erste Lesung",
            "First reading",
            "Primera lectura",
            "Prima lettura",
            "Primeira leitura",
        )
    )
    + r")\.",
)

# Annonce de repli FR (rétrocompat imports).
PREMIERE_LECTURE_TTS_INTRO_FALLBACK = tts_fallback_intro("premiere_lecture", "FR")


def premiere_lecture_tts_intro(
    intro_lue: str | None = None,
    *,
    pref_langue: object | None = None,
) -> str:
    lg = coerce_aip_langue(pref_langue)
    return oral_reading_intro_phrase(
        fr_canonical_reading_title("premiere_lecture")
        if lg == "FR"
        else pdf_titles(lg)["premiere_lecture"],
        intro_lue=intro_lue,
        pref_langue=lg,
    )


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
    """Titre FR canonique (branchements internes) — conserve le libellé localisé si inconnu."""
    key = canonical_reading_section_key(title)
    if key:
        return fr_canonical_reading_title(key)
    return (title or "").strip()


def liturgy_section_oral_announcement(
    title: str,
    *,
    intro_lue: str | None = None,
    ref: str | None = None,
    pref_langue: object | None = None,
) -> str:
    """Annonce orale d'une césure liturgique ou d'une sous-section de synthèse."""
    raw = (title or "").strip()
    if raw.lower().startswith("à retenir"):
        return "À retenir."
    key = canonical_reading_section_key(raw)
    if key or raw.lower() in ("le psaume", "der psalm", "the psalm", "el salmo", "il salmo", "o salmo"):
        return oral_reading_intro_phrase(
            raw,
            intro_lue=intro_lue,
            ref=ref,
            pref_langue=pref_langue,
        )
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

    key = canonical_reading_section_key(section_title)
    norm = normalize_liturgy_section_title(section_title)
    raw = (section_title or "").strip()
    stems: list[tuple[str, str | None]] = []

    if key == "psaume" or norm == "Psaume" or raw.lower() in (
        "le psaume",
        "der psalm",
        "the psalm",
        "el salmo",
        "il salmo",
        "o salmo",
    ):
        stems = [
            ("le psaume", "Il"),
            ("der psalm", None),
            ("the psalm", None),
            ("el salmo", None),
            ("il salmo", None),
            ("o salmo", None),
            ("antwortpsalm", None),
            ("responsorial psalm", None),
            ("salmo responsorial", None),
            ("salmo responsoriale", None),
            ("psaume", "Il"),
            ("ce psaume", "Il"),
        ]
    elif key == "premiere_lecture":
        stems = [
            ("la première lecture", "Elle"),
            ("première lecture", "Elle"),
            ("premiere lecture", "Elle"),
            ("erste lesung", None),
            ("first reading", None),
            ("primera lectura", None),
            ("prima lettura", None),
            ("primeira leitura", None),
        ]
    elif key == "deuxieme_lecture":
        stems = [
            ("la deuxième lecture", "Elle"),
            ("deuxième lecture", "Elle"),
            ("deuxieme lecture", "Elle"),
            ("zweite lesung", None),
            ("second reading", None),
            ("segunda lectura", None),
            ("seconda lettura", None),
            ("segunda leitura", None),
        ]
    elif key == "evangile":
        stems = [
            ("l'évangile", "Il"),
            ("l'evangile", "Il"),
            ("évangile", "Il"),
            ("evangile", "Il"),
            ("evangelium", None),
            ("gospel", None),
            ("evangelio", None),
            ("vangelo", None),
            ("evangelho", None),
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
    """Coupe tout texte parasite avant la première section lectures (toute langue)."""
    t = (text or "").strip()
    if not t:
        return t
    m = _LITURGY_FIRST_SECTION_RE.search(t)
    if m and m.start() > 0:
        return t[m.start() :].strip()
    return t


def is_liturgy_readings_tts_text(text: str) -> bool:
    """True si le texte provient de ``plain_readings_for_tts`` (lectionnaire dominical)."""
    head = (text or "").strip()
    if not head:
        return False
    return bool(_LITURGY_FIRST_SECTION_RE.match(head)) or bool(
        canonical_reading_section_key(head.split(".", 1)[0]) == "premiere_lecture"
        and head.lstrip().find(".") > 0
    )


def parse_liturgy_reading_sections(text: str) -> list[tuple[str, str]]:
    """
    Découpe le texte ``plain_readings_for_tts`` en sections ``(titre, corps)``.

    Conserve le titre localisé injecté (Erste Lesung, First reading, …).
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
                    (m.group(1) or "").strip(),
                    (m.group(2) or "").strip(),
                )
            )
        else:
            out.append(("", p))
    return out


def default_first_reading_tts_title(text: str) -> str:
    """Titre de repli pour un corps orphelin en tête — suit la langue du reste du texte."""
    for title, _body in parse_liturgy_reading_sections(text):
        lg = detect_pref_langue_from_section_title(title)
        if lg:
            return pdf_titles(lg)["premiere_lecture"]
    return pdf_titles(DEFAULT_PREF_LANGUE)["premiere_lecture"]


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
    default_first = default_first_reading_tts_title(text)

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
        merged.insert(0, (default_first, pending))
    elif pending:
        merged.insert(0, (default_first, pending))

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


def spoken_text_for_tts(
    body: str,
    *,
    liturgy_readings: bool = False,
    pref_langue: object | None = None,
) -> str:
    """
    Texte parlé nettoyé (prononciation, préambules admin).

    Le wrapping director / ``# Transcript`` est appliqué juste avant l'appel API
    via ``strict_verbatim_tts_prompt`` (Vertex + Gemini) — pas ici, pour que le
    découpage sections / morceaux reste sur le texte oral seul.

    ``liturgy_readings=True`` : retire les préambules admin et coupe avant la 1re lecture.
    Ne pas activer pour la synthèse (sinon un « Première lecture. » en milieu de texte
    tronque tout le début de la synthèse).

    Le lexique de prononciation FR n'est appliqué que pour ``pref_langue=FR``
    (sinon les scories FR polluent DE/EN/ES/IT).
    """
    cleaned = (body or "").strip()
    if liturgy_readings:
        cleaned = strip_tts_admin_preamble(cleaned)
        cleaned = _trim_to_first_liturgy_section(cleaned)
    else:
        # Synthèse : retirer seulement une consigne admin en tête, sans trim liturgique.
        cleaned = strip_tts_admin_preamble(cleaned)
        if is_liturgy_readings_tts_text(cleaned):
            # Garde-fou : un corps de synthèse ne doit pas être traité comme lectionnaire.
            pass
    lg = coerce_aip_langue(pref_langue) if pref_langue is not None else DEFAULT_PREF_LANGUE
    if pref_langue is not None and lg != "FR":
        return cleaned
    return apply_tts_pronunciation(cleaned)


_TTS_VERBATIM_TRANSCRIPT_MARKER = "# Transcript"

# Accents francophones variés — rotation déterministe (dimanche + cible + voix).
# Paire : consigne director (EN, pour Gemini) + libellé UI (FR).
FRENCH_TTS_ACCENT_SPECS: tuple[tuple[str, str], ...] = (
    (
        "Metropolitan French from France (standard liturgical diction from Paris / Île-de-France)",
        "France (métropolitain)",
    ),
    (
        "Belgian French (Belgium), clear liturgical diction",
        "Belgique",
    ),
    (
        "Swiss French (Romandy / Suisse romande), clear liturgical diction",
        "Suisse romande",
    ),
    (
        "Quebec French (Canada), clear liturgical diction",
        "Québec",
    ),
    (
        "West African French (e.g. Senegal or Côte d'Ivoire), clear liturgical diction",
        "Afrique de l’Ouest",
    ),
    (
        "Maghrebi French (North Africa), clear liturgical diction",
        "Maghreb",
    ),
    (
        "Southern France French (Midi), clear liturgical diction",
        "Sud de la France (Midi)",
    ),
)

FRENCH_TTS_ACCENT_POOL: tuple[str, ...] = tuple(spec[0] for spec in FRENCH_TTS_ACCENT_SPECS)

# Consignes director hors FR — diction liturgique dans la langue cible (pas d'accent français).
NON_FR_TTS_SPEECH_SPECS: dict[str, str] = {
    "DE": (
        "Standard German (Germany), clear liturgical diction. "
        "Speak entirely in German. Pronounce numbers, psalm references and verse lists in German. "
        "Do not use French pronunciation or French number words."
    ),
    "EN": (
        "Clear liturgical English diction. "
        "Speak entirely in English. Pronounce numbers, psalm references and verse lists in English. "
        "Do not use French pronunciation or French number words."
    ),
    "ES": (
        "Clear liturgical Spanish diction (Castilian or Latin American). "
        "Speak entirely in Spanish. Pronounce numbers, psalm references and verse lists in Spanish. "
        "Do not use French pronunciation or French number words."
    ),
    "IT": (
        "Standard Italian, clear liturgical diction. "
        "Speak entirely in Italian. Pronounce numbers, psalm references and verse lists in Italian. "
        "Do not use French pronunciation or French number words."
    ),
    "PT": (
        "European Portuguese (Portugal), clear liturgical diction — not Brazilian Portuguese. "
        "Speak entirely in European Portuguese. Pronounce numbers, psalm references and verse lists "
        "in European Portuguese. Do not use French pronunciation or French number words, "
        "and do not switch to Brazilian Portuguese pronunciation."
    ),
}


def _tts_french_accent_index(
    *,
    sunday_date: date | None = None,
    cible: str = "synthese",
    voice_name: str | None = None,
) -> int:
    day = (sunday_date or date.today()).isoformat()
    key = f"{day}|{norm_slug(cible)}|{norm_slug(voice_name)}|accent-fr-v1"
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    return h % len(FRENCH_TTS_ACCENT_SPECS)


def pick_tts_french_accent(
    *,
    sunday_date: date | None = None,
    cible: str = "synthese",
    voice_name: str | None = None,
) -> str:
    """
    Accent francophone pseudo-aléatoire mais stable pour un (dimanche, cible, voix).

    Même job TTS (tous les morceaux) → même accent ; dimanches / cibles différents → variété.
    """
    return FRENCH_TTS_ACCENT_SPECS[
        _tts_french_accent_index(
            sunday_date=sunday_date, cible=cible, voice_name=voice_name
        )
    ][0]


def pick_tts_speech_direction(
    *,
    pref_langue: object | None = None,
    sunday_date: date | None = None,
    cible: str = "synthese",
    voice_name: str | None = None,
) -> str:
    """
    Consigne director TTS selon ``pref_langue``.

    FR : rotation d'accents francophones. Autres langues : diction liturgique
    dans la langue cible (évite l'énumération française des numéros de psaumes).
    """
    lg = coerce_aip_langue(pref_langue)
    if lg == "FR":
        return pick_tts_french_accent(
            sunday_date=sunday_date, cible=cible, voice_name=voice_name
        )
    return NON_FR_TTS_SPEECH_SPECS.get(lg) or NON_FR_TTS_SPEECH_SPECS["EN"]


def tts_french_accent_label_fr(
    *,
    sunday_date: date | None = None,
    cible: str = "synthese",
    voice_name: str | None = None,
) -> str:
    """Libellé court pour l’UI (ex. « Québec », « France (métropolitain) »)."""
    return FRENCH_TTS_ACCENT_SPECS[
        _tts_french_accent_index(
            sunday_date=sunday_date, cible=cible, voice_name=voice_name
        )
    ][1]


def strict_verbatim_tts_prompt(
    spoken: str,
    *,
    french_accent: str | None = None,
    pref_langue: object | None = None,
) -> str:
    """
    Enveloppe le texte oral d'un prompt director Gemini TTS (non lu à voix haute).

    Empêche le modèle d'inventer / prolonger le texte quand un morceau est court
    (annonce seule, titre, etc.). Format recommandé Google : préambule TTS +
    Director's Notes + marqueur ``# Transcript`` avant le contenu à lire.
    """
    t = (spoken or "").strip()
    if not t:
        return t
    # Déjà enveloppé (double appel spoken_text → generate_audio).
    if t.lstrip().startswith("# TTS") and _TTS_VERBATIM_TRANSCRIPT_MARKER in t:
        return t
    lg = coerce_aip_langue(pref_langue) if pref_langue is not None else DEFAULT_PREF_LANGUE
    direction = (french_accent or "").strip()
    if not direction:
        direction = pick_tts_speech_direction(pref_langue=lg)
    if lg == "FR":
        direction_notes = (
            f"Accent: {direction}. "
            "Keep this regional French accent consistently for the whole transcript. "
        )
    else:
        direction_notes = (
            f"Speech: {direction}. "
            "Do not switch to French. Keep the target language for numbers and biblical references. "
        )
    return (
        "# TTS\n"
        "Synthesize speech for the transcript below. "
        "Speak ONLY the words of the Transcript. "
        "Do not add, invent, paraphrase, summarize, introduce, conclude, "
        "continue, comment, or improvise any words beyond the Transcript. "
        "Do not invent biblical verses, liturgical formulas, or pastoral commentary. "
        "Do not read these instructions or the director notes aloud.\n\n"
        "# Director's Notes\n"
        f"{direction_notes}"
        "Liturgical / pastoral reading: clear, neutral delivery, unhurried pacing. "
        "Verbatim only — zero extra words before, during, or after the Transcript.\n\n"
        f"{_TTS_VERBATIM_TRANSCRIPT_MARKER}\n"
        f"{t}"
    )


def plain_readings_for_tts(texts: object, *, pref_langue: object | None = None) -> str:
    """Texte continu pour TTS des quatre lectures (sans HTML), titres selon ``pref_langue``."""
    lg = coerce_aip_langue(pref_langue)
    parts: list[str] = []
    for sec in liturgy_tts_sections_from_texts(texts, pref_langue=lg):
        raw = clean_aelf_text_for_display(sec.body or "")
        raw = re.sub(r"<[^>]+>", " ", raw)
        is_psalm = canonical_reading_section_key(sec.title) == "psaume"
        if is_psalm:
            raw = re.sub(r"[ \t]+\n", "\n", raw)
            raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
        else:
            raw = " ".join(raw.split())
        raw = strip_tts_admin_preamble(raw)
        if not raw.strip():
            continue
        if len(raw.strip()) < 12 and is_psalm:
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
    return spoken_text_for_tts(body, liturgy_readings=False)


def compose_readings_tts_text(*, body: str, templates: dict[str, str] | None = None) -> str:
    """Texte lu pour l’audio des lectures intégrales (sans préfixe ``audio_style_lectures``)."""
    del templates
    return spoken_text_for_tts(body, liturgy_readings=True)
