from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from core.sheets_db import sheet_row_status_is_live


def _email_tpl_status_cell(row: Mapping[str, Any]) -> str:
    """Lit la cellule « statut ligne » (synonymes possibles dans l’onglet Sheets)."""

    for key in ("status", "Statut", "Status"):
        s = row.get(key)
        if str(s or "").strip():
            return str(s or "").strip()
    return ""


def email_template_row_is_live(row: Mapping[str, Any]) -> bool:
    """Indique si une ligne de l’onglet **templates e-mail** est la version **courante** du point de vue métier.

    **Seul** ``status`` / ``Statut`` / ``Status`` compte : la valeur doit être **Actif** (ou équivalent
    accepté par ``sheet_row_status_is_live``). Dès qu’elle est **Inactif** (ou équivalent hors service),
    la ligne est ignorée pour l’édition, l’**envoi manuel** et le **choix du template par le scheduler**.

    La colonne ``active`` (si elle existe encore sur la feuille) **n’est pas lue** ici : elle peut servir à
    d’autres besoins de pilotage « campagne planifiée » dans la feuille, mais ne participe pas à cette décision.

    Une ligne **sans** statut renseigné n’est pas considérée comme version courante (évite les lignes ambiguës).
    """

    st_raw = _email_tpl_status_cell(row)
    if not st_raw:
        return False
    return sheet_row_status_is_live(st_raw)


def pick_latest_live_email_template(
    rows: Iterable[dict[str, Any]],
    *,
    template_key: str,
    channel: str = "email",
    language_in: tuple[str, ...] | None = ("fr", "fr-fr", "france", "FR", ""),
) -> dict[str, Any] | None:
    """Parmi les lignes dont le **statut** est encore **Actif** (voir ``email_template_row_is_live``), retourne
    la plus récente selon ``version`` puis ``created_at`` (la colonne ``active`` n’intervient pas).

    Retour ``None`` s’il n’y a aucune ligne pertinente."""

    tk = str(template_key or "").strip()
    ch_l = str(channel or "email").strip().lower()

    pool: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("template_key") or "").strip() != tk:
            continue
        if str(r.get("channel") or "").strip().lower() != ch_l:
            continue
        if language_in is not None:
            lang = str(r.get("language") or "").strip().lower()
            if lang not in language_in:
                continue
        if not email_template_row_is_live(r):
            continue
        pool.append(r)

    if not pool:
        return None

    def _key(rep: dict[str, Any]) -> tuple[int, str, str]:
        v_raw = str(rep.get("version") or "").strip()
        vn = int(v_raw) if v_raw.isdigit() else -1
        return (
            vn,
            str(rep.get("created_at") or ""),
            str(rep.get("row_id") or rep.get("entity_id") or ""),
        )

    return max(pool, key=_key)


def language_filter_for_pref_langue(pref_langue: object | None) -> tuple[str, ...]:
    """Valeurs acceptées dans ``email_templates.language`` pour une ``pref_langue`` produit."""
    from core.liturgy_day import coerce_liturgy_pref_langue

    lg = coerce_liturgy_pref_langue(pref_langue)
    # FR : chaîne vide = historique (templates sans langue = français).
    if lg == "FR":
        return ("fr", "fr-fr", "france", "FR", "")
    aliases: dict[str, tuple[str, ...]] = {
        "DE": ("de", "de-de", "deutsch", "germany", "allemagne", "DE"),
        "EN": ("en", "en-gb", "en-us", "english", "uk", "gb", "EN"),
        "ES": ("es", "es-es", "español", "espanol", "spain", "ES"),
        "IT": ("it", "it-it", "italiano", "italy", "italia", "IT"),
        "PT": ("pt", "pt-pt", "português", "portugues", "portugal", "PT"),
    }
    return aliases.get(lg, (lg.lower(), lg))


def pick_latest_live_email_template_for_pref_langue(
    rows: Iterable[dict[str, Any]],
    *,
    template_key: str,
    pref_langue: object | None,
    channel: str = "email",
    fallback_fr: bool = True,
) -> dict[str, Any] | None:
    """Template Actif pour ``pref_langue`` ; repli FR si ``fallback_fr`` et aucune ligne native."""
    from core.liturgy_day import coerce_liturgy_pref_langue

    lg = coerce_liturgy_pref_langue(pref_langue)
    tpl = pick_latest_live_email_template(
        rows,
        template_key=template_key,
        channel=channel,
        language_in=language_filter_for_pref_langue(lg),
    )
    if tpl is None and fallback_fr and lg != "FR":
        tpl = pick_latest_live_email_template(
            rows,
            template_key=template_key,
            channel=channel,
            language_in=language_filter_for_pref_langue("FR"),
        )
    return tpl


_TAG_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@dataclass(frozen=True)
class EmailTemplate:
    subject: str
    body: str


def render_template(tpl: EmailTemplate, *, values: dict[str, str]) -> EmailTemplate:
    """
    Remplace les balises {{tag}} par des valeurs.
    Les balises non résolues restent visibles (pour détecter un oubli).
    """

    def _sub(s: str) -> str:
        def repl(m: re.Match) -> str:
            k = str(m.group(1) or "").strip()
            if not k:
                return m.group(0)
            return str(values.get(k, m.group(0)))

        return re.sub(_TAG_RE, repl, s or "")

    return EmailTemplate(subject=_sub(tpl.subject), body=_sub(tpl.body))


_CLES_LECTURE_LEGACY_TEMPLATE_PHRASES: tuple[str, ...] = (
    "les clés de lecture de ce dimanche {{nom_du_dimanche}} ({{date_dimanche}})",
    "les clés de lecture de ce dimanche {{nom_du_dimanche}}",
    "les clés de lecture de ce {{nom_du_dimanche}}",
)

_CLES_LECTURE_CANONICAL_TEMPLATE_PHRASE = (
    "les clés de lecture de la célébration de ce dimanche {{date_dimanche}}"
)

_CLES_LECTURE_RENDERED_RE = re.compile(
    r"les clés de lecture de ce(?:\s+dimanche)?(?:\s*\([^)]+\))?(?:\s+[^.\n]+)?",
    re.IGNORECASE,
)

# Ancre FR (templates mal traduits) — remplacée par la phrase localisée à l’envoi.
_CLES_LECTURE_FR_BASE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"les\s+cl[eé]s\s+de\s+lecture\s+de\s+la\s+c[eé]l[eé]bration\s+de\s+ce\s+dimanche",
        re.IGNORECASE,
    ),
    re.compile(
        r"les\s+cl[eé]s\s+de\s+lecture\s+de\s+ce\s+dimanche",
        re.IGNORECASE,
    ),
)

_CLES_LECTURE_BASE_BY_LANG: dict[str, str] = {
    "FR": "les clés de lecture de la célébration de ce dimanche",
    "DE": "die Leseschlüssel für die Feier dieses Sonntags",
    "EN": "the keys to reading this Sunday's celebration",
    "ES": "las claves de lectura de la celebración de este domingo",
    "IT": "le chiavi di lettura della celebrazione di questa domenica",
    "PT": "as chaves de leitura da celebração deste domingo",
}


def email_cles_lecture_celebration_phrase(
    *, date_label: str, pref_langue: object | None = None
) -> str:
    """Phrase canonique après « Nous avons préparé pour vous … » (date = ``{{date_dimanche}}`` résolu)."""
    from core.prompt_locale import coerce_aip_langue

    lg = coerce_aip_langue(pref_langue)
    base = _CLES_LECTURE_BASE_BY_LANG.get(lg) or _CLES_LECTURE_BASE_BY_LANG["FR"]
    d = str(date_label or "").strip()
    if not d:
        return base
    return f"{base} {d}"


def normalize_email_body_liturgy_clause(body: str) -> str:
    """Réécrit les anciennes formulations ``… de ce {{nom_du_dimanche}}`` avant rendu."""
    out = body or ""
    for old in _CLES_LECTURE_LEGACY_TEMPLATE_PHRASES:
        out = out.replace(old, _CLES_LECTURE_CANONICAL_TEMPLATE_PHRASE)
    return out


def fix_rendered_email_cles_lecture_phrase(
    body: str, *, date_label: str, pref_langue: object | None = None
) -> str:
    """Corrige restes FR / formulations héritées — templates ETPL, y compris hors FR."""
    from core.prompt_locale import coerce_aip_langue

    if not body:
        return body
    lg = coerce_aip_langue(pref_langue)
    d = str(date_label or "").strip()
    canonical = email_cles_lecture_celebration_phrase(date_label=d, pref_langue=lg)
    out = body
    for base_re in _CLES_LECTURE_FR_BASE_RES:
        if d:
            pat = re.compile(base_re.pattern + r"(?:\s+" + re.escape(d) + r")?", re.I)
        else:
            pat = base_re
        if pat.search(out):
            return pat.sub(canonical, out, count=1)
    if lg == "FR" and d:
        return _CLES_LECTURE_RENDERED_RE.sub(canonical, out, count=1)
    return out


def render_weekly_email_template(tpl: EmailTemplate, *, values: dict[str, str]) -> EmailTemplate:
    """
    Rendu e-mail hebdo : normalise la phrase « clés de lecture » et injecte ``cles_lecture_celebration``.
    """
    date_label = str(values.get("date_dimanche") or "").strip()
    pref = values.get("pref_langue")
    vals = dict(values)
    vals.setdefault(
        "cles_lecture_celebration",
        email_cles_lecture_celebration_phrase(date_label=date_label, pref_langue=pref),
    )
    body_norm = normalize_email_body_liturgy_clause(tpl.body or "")
    rendered = render_template(EmailTemplate(subject=tpl.subject, body=body_norm), values=vals)
    fixed_body = fix_rendered_email_cles_lecture_phrase(
        rendered.body or "", date_label=date_label, pref_langue=pref
    )
    return EmailTemplate(subject=rendered.subject, body=fixed_body)


WEEKLY_ACTUALITE_LEAD = "À noter cette semaine dans l'actualité de LumenVia : "

WEEKLY_ACTUALITE_LEAD_BY_LANG: dict[str, str] = {
    "FR": WEEKLY_ACTUALITE_LEAD,
    "DE": "Aktuelles von LumenVia in dieser Woche: ",
    "EN": "This week in LumenVia news: ",
    "ES": "Novedades de LumenVia esta semana: ",
    "IT": "Novità di LumenVia di questa settimana: ",
    "PT": "Novidades do LumenVia esta semana: ",
}

# Texte proposé pour l’envoi hebdo (UI Emailing + défaut si ETPL.status_note vide).
# Ne pas commencer par « Cette semaine » : le préfixe WEEKLY_ACTUALITE_LEAD l’indique déjà.
PROPOSED_WEEKLY_ACTUALITE_MESSAGE = (
    "LumenVia s’ouvre davantage : vous pouvez préparer le dimanche en français, "
    "anglais, espagnol, allemand ou italien — lectures, synthèse, PDF et audio suivent "
    "votre langue. Les audios s’habillent aussi d’une légère ambiance (cloche, orgue…) "
    "autour de la voix, pour une écoute plus recueillie. "
    "Comme chaque semaine, illustration, liens pour écouter ou relire, et l’essentiel "
    "pour entrer dans la célébration sont dans ce message."
)

# Alias historique : même contenu que la proposition courante.
DEFAULT_WEEKLY_ACTUALITE_MESSAGE = PROPOSED_WEEKLY_ACTUALITE_MESSAGE

_CETTE_SEMAINE_LEAD_RE = re.compile(r"(?is)^cette\s+semaine\s*[,:\-–—]?\s*")

_EMAIL_GREETING_LINE_RE = re.compile(
    r"(?i)^\s*(bonjour|hallo|guten\s+tag|hello|hi|hola|buenos\s+d[ií]as|"
    r"buenas\s+tardes|ciao|buongiorno|ol[áa]|bom\s+dia|boa\s+tarde|"
    r"lieber?\b|dear\b)\b"
)


def weekly_actualite_lead_for_lang(pref_langue: object | None = None) -> str:
    from core.prompt_locale import coerce_aip_langue

    lg = coerce_aip_langue(pref_langue)
    return WEEKLY_ACTUALITE_LEAD_BY_LANG.get(lg) or WEEKLY_ACTUALITE_LEAD


# Citation signature LumenVia (corps e-mail / encadré HTML).
LUMENVIA_MISSION_QUOTE_FR = (
    "LumenVia n'est pas là pour remplacer la rencontre, mais pour la préparer, "
    "afin que chaque messe devienne une rencontre plus consciente avec le Christ."
)

LUMENVIA_MISSION_QUOTE_BY_LANG: dict[str, str] = {
    "FR": LUMENVIA_MISSION_QUOTE_FR,
    "DE": (
        "LumenVia ist nicht dazu da, die Begegnung zu ersetzen, sondern sie vorzubereiten, "
        "damit jede Messe zu einer bewussteren Begegnung mit Christus wird."
    ),
    "EN": (
        "LumenVia is not here to replace the encounter, but to prepare for it, "
        "so that each Mass may become a more conscious encounter with Christ."
    ),
    "ES": (
        "LumenVia no está ahí para reemplazar el encuentro, sino para prepararlo, "
        "a fin de que cada misa se convierta en un encuentro más consciente con Cristo."
    ),
    "IT": (
        "LumenVia non è qui per sostituire l'incontro, ma per prepararlo, "
        "affinché ogni messa diventi un incontro più consapevole con Cristo."
    ),
    "PT": (
        "O LumenVia não está aqui para substituir o encontro, mas para o preparar, "
        "para que cada missa se torne um encontro mais consciente com Cristo."
    ),
}


def lumenvia_mission_quote_for_lang(pref_langue: object | None = None) -> str:
    from core.prompt_locale import coerce_aip_langue

    lg = coerce_aip_langue(pref_langue)
    return LUMENVIA_MISSION_QUOTE_BY_LANG.get(lg) or LUMENVIA_MISSION_QUOTE_FR


def replace_mission_quote_in_text(text: str, *, pref_langue: object | None) -> str:
    """Remplace toute variante connue de la citation signature par la version localisée."""
    out = text or ""
    target = lumenvia_mission_quote_for_lang(pref_langue)
    variants: list[str] = []
    for q in LUMENVIA_MISSION_QUOTE_BY_LANG.values():
        if q and q not in variants:
            variants.append(q)
        # Apostrophe typographique vs ASCII
        q2 = (q or "").replace("'", "’")
        if q2 and q2 not in variants:
            variants.append(q2)
        q3 = (q or "").replace("’", "'")
        if q3 and q3 not in variants:
            variants.append(q3)
    for q in variants:
        if q and q != target and q in out:
            out = out.replace(q, target)
    return out


_ILLU_DESC_EMAIL_CACHE: dict[tuple[str, str], str] = {}


def localize_illustration_description_for_email(
    text_fr: str,
    *,
    pref_langue: object | None,
    cfg: object | None = None,
) -> str:
    """Traduit la légende ILUS (FR) pour l’e-mail selon ``pref_langue`` (cache process)."""
    from core.prompt_locale import coerce_aip_langue
    from core.prompt_translate import translate_plain_fr_to

    src = (text_fr or "").strip()
    if not src:
        return ""
    lg = coerce_aip_langue(pref_langue)
    if lg == "FR":
        return src
    key = (lg, src)
    cached = _ILLU_DESC_EMAIL_CACHE.get(key)
    if cached is not None:
        return cached
    vertex = None
    try:
        sa = getattr(cfg, "gcp_service_account", None) if cfg is not None else None
        if not sa:
            from core.config import load_config

            sa = getattr(load_config(), "gcp_service_account", None)
        if sa:
            from core.vertex_gemini import VertexGeminiClient

            vertex = VertexGeminiClient(service_account_info=sa)
    except Exception:
        vertex = None
    try:
        out = translate_plain_fr_to(
            src,
            target_lang=lg,
            vertex_client=vertex,
            context="short liturgical illustration caption for a Sunday email newsletter",
        )
        out = (out or src).strip() or src
    except Exception:
        out = src
    _ILLU_DESC_EMAIL_CACHE[key] = out
    return out


def strip_redundant_cette_semaine_lead(message: str) -> str:
    """Retire un « Cette semaine, » en tête (déjà présent dans ``WEEKLY_ACTUALITE_LEAD``)."""
    return _CETTE_SEMAINE_LEAD_RE.sub("", (message or "").strip()).strip()


def format_weekly_actualite_paragraph(
    message: str, *, pref_langue: object | None = None
) -> str:
    """
    Paragraphe éditorial optionnel (actualité LumenVia).
    Préfixe automatique selon ``pref_langue`` sauf si le message commence déjà par un lead connu.
    """
    from core.prompt_locale import coerce_aip_langue

    msg = (message or "").strip()
    if not msg:
        return ""
    lg = coerce_aip_langue(pref_langue)
    lead = weekly_actualite_lead_for_lang(lg)
    low = msg.lower().replace("’", "'")
    # Déjà préfixé (FR ou autre langue).
    if low.startswith("à noter cette semaine") or low.startswith("a noter cette semaine"):
        return msg
    for other in WEEKLY_ACTUALITE_LEAD_BY_LANG.values():
        o = (other or "").strip().lower().replace("’", "'")
        if o and low.startswith(o.rstrip(" :").lower()):
            return msg
    msg = strip_redundant_cette_semaine_lead(msg)
    if not msg:
        return ""
    return f"{lead}{msg}"


def inject_weekly_actualite_into_email_body(
    body: str, *, message: str, pref_langue: object | None = None
) -> str:
    """Insère le paragraphe d’actualité juste après la ligne de salutation, sinon en tête."""
    para = format_weekly_actualite_paragraph(message, pref_langue=pref_langue)
    if not para:
        return body or ""
    text = (body or "").replace("\r\n", "\n")
    lines = text.split("\n")
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and _EMAIL_GREETING_LINE_RE.match((ln or "").strip()):
            out.append("")
            out.append(para)
            inserted = True
    if not inserted:
        out = [para, ""] + out
    return "\n".join(out).strip()


def append_weekly_email_run(
    *,
    gspread_client: object,
    spreadsheet_id: str,
    date_dimanche: str,
    message_actualite: str,
    recipients_ok: int,
    recipients_err: int,
    run_kind: str = "manual_broadcast",
    campaign_key: str = "weekly_friday_lumenvia",
    template_key: str = "weekly_friday_lumenvia",
    status_detail: str = "done",
    started_at: str | None = None,
    error: str = "",
) -> dict:
    """
    Une ligne RUNS par envoi hebdo (mention + date) — pas de duplication OUTM.
    Les colonnes manquantes sont ajoutées à l’en-tête via ``append_immutable_row``.
    """
    from hashlib import sha256

    from core.sheets_db import append_immutable_row, utc_now_iso

    day = str(date_dimanche or "").strip()[:10]
    finished = utc_now_iso()
    started = str(started_at or "").strip() or finished
    mention = str(message_actualite or "").strip()
    run_id = sha256(
        f"runs|{campaign_key}|{day}|{run_kind}|{started}".encode("utf-8")
    ).hexdigest()[:24]
    return append_immutable_row(
        gspread_client=gspread_client,
        spreadsheet_id=spreadsheet_id,
        table="scheduler_runs",
        values_by_col={
            "entity_id": run_id,
            "campaign_key": campaign_key,
            "run_kind": run_kind,
            "status_detail": status_detail,
            "started_at": started,
            "finished_at": finished,
            "recipients_ok": str(int(recipients_ok)),
            "recipients_err": str(int(recipients_err)),
            "error": str(error or "")[:900],
            "date_dimanche": day,
            "message_actualite": mention,
            "template_key": str(template_key or "").strip(),
        },
    )


def supported_tags() -> tuple[str, ...]:
    return (
        "prenom",
        "nom",
        "date_dimanche",
        "nom_du_dimanche",
        "cles_lecture_celebration",
        "url_pdf",
        "url_audio",
        "url_audio_readings",
        "url_illustration",
        "illustration_description",
        "url_app",
        "optout_url",
    )


def normalize_email_template_text(text: str) -> str:
    """Normalise objet/corps pour comparer formulaire UI vs ligne Sheets (espaces, fins de ligne)."""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def french_day_month_year(d: date) -> str:
    mois = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    return f"{d.day} {mois[d.month - 1]} {d.year}"


def email_sunday_date_fallback_label(date_str: str) -> str:
    """
    Repli ``{{nom_du_dimanche}}`` : même format que ``{{date_dimanche}}``, préfixé par
    « dimanche » lorsque la date tombe un dimanche.
    """
    ds = str(date_str or "").strip()[:10]
    if len(ds) != 10:
        return "—"
    try:
        d = date.fromisoformat(ds)
    except ValueError:
        return "—"
    label = french_day_month_year(d)
    if d.weekday() == 6:
        return f"dimanche {label}"
    return label


def resolve_email_nom_du_dimanche(
    *,
    identity: object | None,
    date_str: str,
    gspread_client: object | None = None,
    spreadsheet_id: str | None = None,
) -> str:
    """
    Valeur de ``{{nom_du_dimanche}}`` pour un envoi e-mail.

    1. Identité AELF du dimanche ciblé (fête + semaine du Psautier).
    2. Secours : dernière ligne ``readings_cache`` (RDC) pour cette date.
    3. Repli : « dimanche 25 mai 2026 » (ou date seule si ce n’est pas un dimanche).
    """
    from core.liturgy_display_helpers import email_sunday_liturgy_label, is_weak_liturgy_title
    from core.sheets_db import fetch_records, sheet_row_status_is_live

    def _ok(label: str) -> bool:
        return bool(label and label != "—" and not is_weak_liturgy_title(label))

    label = email_sunday_liturgy_label(identity)
    if _ok(label):
        return label

    sid = str(spreadsheet_id or "").strip()
    ds = str(date_str or "")[:10]
    if gspread_client and sid and ds:
        try:
            rows = fetch_records(
                gspread_client=gspread_client,
                spreadsheet_id=sid,
                table="readings_cache",
                limit=0,
                use_cache=True,
            )
        except Exception:
            rows = []
        else:
            candidates = [
                r
                for r in rows
                if str(r.get("date") or "")[:10] == ds
                and sheet_row_status_is_live(r.get("status"))
                and not str(r.get("error") or "").strip()
            ]
            if candidates:
                candidates.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
                r0 = candidates[0]

                class _LiturgyRow:
                    fete = r0.get("fete")
                    semaine = r0.get("semaine")
                    jour_liturgique_nom = r0.get("jour_liturgique_nom")
                    periode = r0.get("periode")

                label = email_sunday_liturgy_label(_LiturgyRow())
                if _ok(label):
                    return label

    return email_sunday_date_fallback_label(date_str)

