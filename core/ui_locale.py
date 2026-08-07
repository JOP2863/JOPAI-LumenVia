"""Catalogue UI multilingue — **chrome public** (FR/DE/EN/ES/IT/PT).

``core/sunday_view_locale.py`` / ``core/pdf_locale.py`` localisent déjà le **contenu**
liturgique (lectures, PDF). Ce module couvre le **chrome** du site public : navigation,
confort de lecture, À propos, chrome de « La Lumière du Dimanche » (titres / spinners /
erreurs non déjà couverts par ``sunday_view_locale``), inscription / compte, mémo,
questionnaire d’avis.

L’administration reste exclusivement en français : ``t()`` ne doit **jamais** être
appelé depuis ``ui/admin/*``.

Pas de traduction IA à la volée : catalogues Python figés, repli FR si clé ou langue
absente (même politique que ``sunday_view_locale`` / ``pdf_locale``). Aucune lecture
Sheets supplémentaire pour résoudre la langue.
"""

from __future__ import annotations

import streamlit as st

from core.liturgy_day import coerce_liturgy_pref_langue, supported_liturgy_langs
from core.locale_codes import DEFAULT_PREF_LANGUE

SESSION_KEY = "ui_lang"

# Mêmes 6 langues que le contenu liturgique (FR, DE, EN, ES, IT, PT).
SUPPORTED_UI_LANGS: tuple[str, ...] = supported_liturgy_langs()

LANG_NATIVE_LABEL: dict[str, str] = {
    "FR": "Français",
    "DE": "Deutsch",
    "EN": "English",
    "ES": "Español",
    "IT": "Italiano",
    "PT": "Português",
}


def coerce_ui_lang(code: object | None) -> str:
    """ISO 639-1 majuscule parmi les langues chrome supportées ; repli ``FR``."""
    return coerce_liturgy_pref_langue(code)


def get_ui_lang() -> str:
    """Langue chrome courante (session) — résolue une fois puis stable en session.

    Repli : langue du compte connecté (``pref_langue`` déjà en session, sans lecture
    Sheets) si connue, sinon ``FR``. Ne change plus ensuite sans action explicite
    (``?lang=``, sélecteur, ou nouvelle connexion).
    """
    raw = st.session_state.get(SESSION_KEY)
    if raw:
        lg = coerce_ui_lang(raw)
        st.session_state[SESSION_KEY] = lg
        return lg
    acct_pref = st.session_state.get("pref_langue")
    lg = coerce_ui_lang(acct_pref) if acct_pref else DEFAULT_PREF_LANGUE
    st.session_state[SESSION_KEY] = lg
    return lg


def set_ui_lang(code: object | None) -> None:
    st.session_state[SESSION_KEY] = coerce_ui_lang(code)


def switch_public_lang(code: object | None, *, sync_sunday: bool = True) -> str:
    """Change la langue chrome (+ alignement Dimanche) et renvoie le code appliqué."""
    lg = coerce_ui_lang(code)
    set_ui_lang(lg)
    if sync_sunday:
        st.session_state["sunday_view_pref_langue"] = lg
    return lg


def render_public_lang_flags() -> None:
    """Barre de drapeaux (haut droite) — boutons Streamlit (même fenêtre, pas de lien).

    L’admin reste rédigé en français ; changer la langue ici n’affecte que le chrome
    public (nav, titres pages publiques, etc.).
    """
    try:
        from core.sunday_view_locale import LANG_FLAG_CDN
    except Exception:
        LANG_FLAG_CDN = {
            "FR": "fr",
            "DE": "de",
            "EN": "gb",
            "ES": "es",
            "IT": "it",
            "PT": "pt",
        }

    cur = get_ui_lang()
    # Boutons taille fixe (ratio drapeau ~4:3) — pas use_container_width (étire l’image).
    css_rules: list[str] = [
        """
div[class*="st-key-lv_pub_lang_flags"] {
  display: flex;
  justify-content: flex-end;
  width: 100%;
}
div[class*="st-key-lv_pub_lang_flags"] [data-testid="stHorizontalBlock"] {
  gap: 0.32rem !important;
  justify-content: flex-end !important;
  width: auto !important;
  margin-left: auto !important;
}
div[class*="st-key-lv_pub_lang_flags"] [data-testid="column"] {
  flex: 0 0 1.65rem !important;
  width: 1.65rem !important;
  min-width: 1.65rem !important;
  max-width: 1.65rem !important;
}
div[class*="st-key-lv_pub_lang_flags"] button {
  width: 1.65rem !important;
  min-width: 1.65rem !important;
  max-width: 1.65rem !important;
  height: 1.2rem !important;
  min-height: 1.2rem !important;
  max-height: 1.2rem !important;
  padding: 0 !important;
  border-radius: 3px !important;
  border: 1.5px solid rgba(52, 46, 41, 0.22) !important;
  background-color: transparent !important;
  background-size: 1.65rem 1.2rem !important;
  background-position: center !important;
  background-repeat: no-repeat !important;
  box-shadow: none !important;
  opacity: 0.82;
}
div[class*="st-key-lv_pub_lang_flags"] button p,
div[class*="st-key-lv_pub_lang_flags"] button span {
  font-size: 0 !important;
  line-height: 0 !important;
  color: transparent !important;
}
div[class*="st-key-lv_pub_lang_flags"] button:hover {
  opacity: 1 !important;
  border-color: rgba(95, 79, 58, 0.5) !important;
  background-color: transparent !important;
}
""".strip()
    ]
    for lg in SUPPORTED_UI_LANGS:
        code = LANG_FLAG_CDN.get(lg, lg.lower()[:2])
        active = lg == cur
        border = "rgba(95, 79, 58, 0.7)" if active else "rgba(52, 46, 41, 0.22)"
        opacity = "1" if active else "0.82"
        css_rules.append(
            f'div[class*="st-key-lv_pub_lang_{lg}"] button {{'
            f' background-image: url("https://flagcdn.com/h20/{code}.png") !important;'
            f" border-color: {border} !important;"
            f" opacity: {opacity} !important;"
            f" }}"
        )
    st.markdown("<style>\n" + "\n".join(css_rules) + "\n</style>", unsafe_allow_html=True)

    with st.container(key="lv_pub_lang_flags"):
        cols = st.columns(len(SUPPORTED_UI_LANGS), gap="small")
        for col, lg in zip(cols, SUPPORTED_UI_LANGS):
            with col:
                if st.button(
                    "\u200b",
                    key=f"lv_pub_lang_{lg}",
                    help=LANG_NATIVE_LABEL.get(lg, lg),
                    use_container_width=False,
                ):
                    if lg != cur:
                        switch_public_lang(lg, sync_sunday=True)
                        st.rerun()


def append_lang_query(url: str, *, lang: object | None) -> str:
    """Ajoute ``?lang=`` (ou ``&lang=``) à ``url`` — liens e-mail (``url_app``, ``optout_url``, …).

    Utilisé côté envoi (``ui/admin/emailing_manual_broadcast.py``, ``ui/admin/scheduler.py``)
    pour que le lien atterrisse dans la langue de consultation du destinataire (``pref_langue``).
    Ne modifie pas ``url`` si elle est vide ou si ``lang`` est absent.
    """
    u = (url or "").strip()
    if not u:
        return u
    lg = coerce_ui_lang(lang) if lang else ""
    if not lg:
        return u
    sep = "&" if "?" in u else "?"
    return f"{u}{sep}lang={lg}"


def apply_lang_query_param() -> None:
    """Applique ``?lang=DE`` (etc.) à la session, puis consomme le paramètre.

    À appeler tôt dans ``app.py`` (avant ``top_nav``). Aligné aussi sur Dimanche
    (bandeau drapeaux et liens e-mail ``?lang=`` / ``?sunday=``).
    """
    try:
        raw = str(st.query_params.get("lang") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return
    # Alignement Dimanche + chrome (bandeau drapeaux et liens e-mail ?lang=).
    switch_public_lang(raw, sync_sunday=True)
    try:
        del st.query_params["lang"]
    except Exception:
        pass


def t(key: str, **fmt: object) -> str:
    """Chaîne UI localisée (chrome public). Repli FR puis clé brute si introuvable."""
    lg = get_ui_lang()
    table = _CATALOG.get(lg) or _CATALOG[DEFAULT_PREF_LANGUE]
    raw = table.get(key)
    if raw is None:
        raw = _CATALOG[DEFAULT_PREF_LANGUE].get(key, key)
    if fmt:
        try:
            return raw.format(**fmt)
        except Exception:
            return raw
    return raw


_CATALOG: dict[str, dict[str, str]] = {
    "FR": {
        # --- Navigation ---
        # Clés composées (2 lignes bouton) — utilisées si un appelant assemble lui-même le libellé.
        "nav.about_suffix": "c'est quoi ?",
        "nav.sunday_line1": "La lumière",
        "nav.sunday_line2": "du dimanche",
        "nav.memo_line1": "Mon",
        "nav.memo_line2": "Aide‑Mémoire",
        "nav.join_line1": "S'inscrire à",
        "nav.join_line2": "la Newsletter",
        "nav.account": "Mon Compte",
        "nav.feedback_line1": "Donner",
        "nav.feedback_line2": "Votre avis",
        "nav.menu": "Menu",
        "nav.connected_as": "🟢 Connecté · {email}",
        "nav.session_active": "session active",
        "nav.logout": "Déconnexion",
        # Clés simples (libellé complet prêt à l'emploi) — utilisées par ``ui/navigation.py``.
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\nc'est quoi ?",
        "nav.sunday": "La lumière\ndu dimanche",
        "nav.memo": "Mon\nAide‑Mémoire",
        "nav.join": "S'inscrire à la Newsletter",
        "nav.feedback": "Donner\nVotre avis",
        "nav.connected": "🟢 Connecté · {email}",
        # --- Confort de lecture ---
        "comfort.title": "Confort de lecture — taille du texte",
        "comfort.caption": "Agrandit les textes des pages et des lectures pour un meilleur confort visuel.",
        "comfort.size_label": "Taille du texte",
        "comfort.standard": "Standard",
        "comfort.large": "Grand",
        "comfort.xlarge": "Très grand",
        "comfort.language_label": "Langue de l'interface",
        "comfort.language_caption": "Change la langue des menus et des pages (FR/DE/EN/ES/IT/PT).",
        # --- À propos ---
        "about.references_title": "Références & sources",
        "about.readings_heading": "Lectures liturgiques",
        "about.table_lang": "Langue",
        "about.table_source": "Source",
        "about.table_usage": "Usage LumenVia",
        "about.fr_usage": (
            "Source de production — textes de la messe via l’API publique (pas de clé)."
        ),
        "about.other_usage": (
            "Complément multi-langues : textes natifs complets (pas de traduction maison "
            "depuis l’AELF). Affichage dans l’app + cache RDC ; redistribution e-mail / TTS / "
            "PDF des textes hors FR soumise à confirmation des conditions Evangelizo."
        ),
        "about.audio_heading": "Audios",
        "about.voice_label": "Voix",
        "about.voice_desc": (
            "synthèse vocale Google (Vertex / Gemini TTS) — lecture des textes déjà disponibles."
        ),
        "about.ambiance_label": "Ambiance",
        "about.ambiance_desc": (
            "(intro / outro / fond) : clips **libres de droits** déposés par l’équipe dans "
            "l’Atelier audio — licences **CC0**, **domaine public** ou **CC-BY** (attribution). "
            "Aucune musique commerciale non licenciée."
        ),
        "about.footer_note": (
            "Les illustrations et contenus générés par IA sont des aides à la méditation ; "
            "les textes liturgiques restent ceux des sources ci-dessus."
        ),
        # --- Dimanche (chrome uniquement) ---
        "sunday.title": "La Lumière du Dimanche",
        "sunday.date_label": "Date (dimanche de la semaine)",
        "sunday.source_expander": "Source des lectures",
        "sunday.zone_caption": "Zone : **{zone}** · langue **{lang}**",
        "sunday.loading_readings": "Récupération des lectures…",
        "sunday.error_fetch_readings": "Impossible de récupérer les lectures en **{lang}** pour le {date}.",
        "sunday.error_retry_hint": (
            "Réessaie avec du réseau, ou choisis une date déjà consultée récemment dans "
            "cette langue sur cet appareil."
        ),
        "sunday.calendar_expander": "Voir les contenus déjà disponibles — {month} {year}",
        "sunday.calendar_legend": "Dimanche avec contenu",
        "sunday.preview_readings": "Lectures **{lang}** (`{source}`) — aperçu : _{preview}_",
        # --- Inscription / compte ---
        "join.title_account": "Mon compte",
        "join.title_newsletter": "S'inscrire à la newsletter",
        "join.section_login": "Connexion",
        "join.btn_login": "Se connecter",
        "join.btn_logout": "Se déconnecter",
        "join.password_label": "Mot de passe",
        "join.btn_signup_mode": "Créer / activer un compte",
        "join.field_first_name": "Prénom",
        "join.field_last_name": "Nom",
        "join.field_phone": "Téléphone (optionnel, format international)",
        "join.field_country": "Pays",
        "join.field_pref_langue": "Préférence langue",
        "join.consent": "J’accepte de recevoir ces e-mails (désinscription possible à tout moment).",
        "join.btn_subscribe": "S’abonner",
        "join.btn_unsubscribe": "Se désinscrire",
        "join.section_my_info": "Mes informations",
        "join.btn_save_profile": "Enregistrer mes informations",
        "join.section_newsletter": "Newsletter",
        # --- Mémo ---
        "memo.title": "Mon Aide-Mémoire",
        "memo.subtitle": "Espace réservé aux utilisateurs connectés.",
        "memo.login_required": "Pour accéder à **Mon Aide‑Mémoire**, il faut être connecté.",
        "memo.btn_go_account": "Aller à Mon compte",
        "memo.existing_memos": "Mes mémos existants",
        "memo.no_memo": "Aucun mémo pour le moment.",
        "memo.new_memo_heading": "Créer un nouveau mémo",
        "memo.field_title": "Titre",
        "memo.field_date": "Date (dimanche)",
        "memo.field_body": "Ton mémo",
        "memo.field_resolution": "Ma résolution (cette semaine)",
        "memo.btn_save": "Enregistrer le mémo",
        "memo.saved": "OK — mémo enregistré.",
        "memo.export_heading": "Export PDF — Graine de Parole",
        # --- Avis / feedback ---
        "feedback.title": "Donner votre avis",
        "feedback.login_required": (
            "Pour répondre au questionnaire, connecte-toi (**Mon compte**) ou ouvre le lien "
            "reçu dans ton e-mail LumenVia — il préremplit ton adresse et permet de participer "
            "sans compte."
        ),
        "feedback.btn_go_account": "Aller à Mon compte",
        "feedback.btn_go_join": "S'inscrire à la newsletter",
        "feedback.section_first_steps": "Vos premiers pas avec LumenVia",
        "feedback.field_illustration": "L'illustration",
        "feedback.field_synthesis": "Le pdf de synthèse",
        "feedback.field_audio": "L'audio",
        "feedback.field_utility": (
            "Ce contenu vous aide-t-il réellement à vous préparer pour la célébration de dimanche ?"
        ),
        "feedback.field_standout": (
            "Qu'est-ce qui vous a le plus touché ou semblé le plus utile dans cet envoi ?"
        ),
        "feedback.field_wish": (
            "Une seule chose à améliorer ou à ajouter (musique d'ambiance, texte plus court, …) ?"
        ),
        "feedback.btn_submit": "Envoyer mon avis",
        "feedback.thanks": "Merci infiniment : ton avis nous aide à faire grandir LumenVia.",
        # --- Commun ---
        "common.save": "Enregistrer",
        "common.cancel": "Annuler",
    },
    "DE": {
        "nav.about_suffix": "was ist das?",
        "nav.sunday_line1": "Das Licht",
        "nav.sunday_line2": "des Sonntags",
        "nav.memo_line1": "Meine",
        "nav.memo_line2": "Gedächtnisstütze",
        "nav.join_line1": "Newsletter",
        "nav.join_line2": "abonnieren",
        "nav.account": "Mein Konto",
        "nav.feedback_line1": "Feedback",
        "nav.feedback_line2": "geben",
        "nav.menu": "Menü",
        "nav.connected_as": "🟢 Angemeldet · {email}",
        "nav.session_active": "aktive Sitzung",
        "nav.logout": "Abmelden",
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\nwas ist das?",
        "nav.sunday": "Das Licht\ndes Sonntags",
        "nav.memo": "Meine\nGedächtnisstütze",
        "nav.join": "Newsletter abonnieren",
        "nav.feedback": "Feedback\ngeben",
        "nav.connected": "🟢 Angemeldet · {email}",
        "comfort.title": "Lesekomfort — Textgröße",
        "comfort.caption": "Vergrößert die Texte der Seiten und Lesungen für mehr visuellen Komfort.",
        "comfort.size_label": "Textgröße",
        "comfort.standard": "Standard",
        "comfort.large": "Groß",
        "comfort.xlarge": "Sehr groß",
        "comfort.language_label": "Oberflächensprache",
        "comfort.language_caption": "Ändert die Sprache der Menüs und Seiten (FR/DE/EN/ES/IT/PT).",
        "about.references_title": "Quellen & Referenzen",
        "about.readings_heading": "Liturgische Lesungen",
        "about.table_lang": "Sprache",
        "about.table_source": "Quelle",
        "about.table_usage": "Verwendung in LumenVia",
        "about.fr_usage": (
            "Produktionsquelle — Messtexte über die öffentliche API (kein Schlüssel nötig)."
        ),
        "about.other_usage": (
            "Mehrsprachige Ergänzung: vollständige Originaltexte (keine eigene Übersetzung "
            "der AELF-Texte). Anzeige in der App + RDC-Cache; Weiterverbreitung per E-Mail / "
            "TTS / PDF von Texten außerhalb FR vorbehaltlich Bestätigung der Evangelizo-Bedingungen."
        ),
        "about.audio_heading": "Audiodateien",
        "about.voice_label": "Stimme",
        "about.voice_desc": (
            "Google-Sprachsynthese (Vertex / Gemini TTS) — Vorlesen der bereits verfügbaren Texte."
        ),
        "about.ambiance_label": "Klangkulisse",
        "about.ambiance_desc": (
            "(Intro / Outro / Hintergrund): **lizenzfreie** Clips, vom Team im Audio-Atelier "
            "hinterlegt — Lizenzen **CC0**, **gemeinfrei** oder **CC-BY** (Namensnennung). "
            "Keine unlizenzierte kommerzielle Musik."
        ),
        "about.footer_note": (
            "Die von KI erzeugten Illustrationen und Inhalte sind Hilfen zur Meditation; die "
            "liturgischen Texte bleiben die der oben genannten Quellen."
        ),
        "sunday.title": "Das Licht des Sonntags",
        "sunday.date_label": "Datum (Sonntag der Woche)",
        "sunday.source_expander": "Quelle der Lesungen",
        "sunday.zone_caption": "Zone: **{zone}** · Sprache **{lang}**",
        "sunday.loading_readings": "Lesungen werden abgerufen…",
        "sunday.error_fetch_readings": "Die Lesungen auf **{lang}** für den {date} konnten nicht abgerufen werden.",
        "sunday.error_retry_hint": (
            "Versuche es erneut mit Netzverbindung, oder wähle ein Datum, das auf diesem Gerät "
            "kürzlich in dieser Sprache aufgerufen wurde."
        ),
        "sunday.calendar_expander": "Bereits verfügbare Inhalte anzeigen — {month} {year}",
        "sunday.calendar_legend": "Sonntag mit Inhalt",
        "sunday.preview_readings": "Lesungen **{lang}** (`{source}`) — Vorschau: _{preview}_",
        "join.title_account": "Mein Konto",
        "join.title_newsletter": "Newsletter abonnieren",
        "join.section_login": "Anmeldung",
        "join.btn_login": "Anmelden",
        "join.btn_logout": "Abmelden",
        "join.password_label": "Passwort",
        "join.btn_signup_mode": "Konto erstellen / aktivieren",
        "join.field_first_name": "Vorname",
        "join.field_last_name": "Nachname",
        "join.field_phone": "Telefon (optional, internationales Format)",
        "join.field_country": "Land",
        "join.field_pref_langue": "Sprachpräferenz",
        "join.consent": "Ich stimme zu, diese E-Mails zu erhalten (jederzeit abbestellbar).",
        "join.btn_subscribe": "Abonnieren",
        "join.btn_unsubscribe": "Abbestellen",
        "join.section_my_info": "Meine Daten",
        "join.btn_save_profile": "Meine Daten speichern",
        "join.section_newsletter": "Newsletter",
        "memo.title": "Meine Gedächtnisstütze",
        "memo.subtitle": "Bereich für angemeldete Nutzer.",
        "memo.login_required": "Um auf **Meine Gedächtnisstütze** zuzugreifen, musst du angemeldet sein.",
        "memo.btn_go_account": "Zu Mein Konto",
        "memo.existing_memos": "Meine bestehenden Notizen",
        "memo.no_memo": "Noch keine Notiz vorhanden.",
        "memo.new_memo_heading": "Neue Notiz erstellen",
        "memo.field_title": "Titel",
        "memo.field_date": "Datum (Sonntag)",
        "memo.field_body": "Deine Notiz",
        "memo.field_resolution": "Mein Vorsatz (diese Woche)",
        "memo.btn_save": "Notiz speichern",
        "memo.saved": "OK — Notiz gespeichert.",
        "memo.export_heading": "PDF-Export — Graine de Parole",
        "feedback.title": "Feedback geben",
        "feedback.login_required": (
            "Um am Fragebogen teilzunehmen, melde dich an (**Mein Konto**) oder öffne den Link "
            "aus deiner LumenVia-E-Mail — er füllt deine Adresse automatisch aus und erlaubt "
            "die Teilnahme ohne Konto."
        ),
        "feedback.btn_go_account": "Zu Mein Konto",
        "feedback.btn_go_join": "Newsletter abonnieren",
        "feedback.section_first_steps": "Deine ersten Schritte mit LumenVia",
        "feedback.field_illustration": "Die Illustration",
        "feedback.field_synthesis": "Das PDF der Zusammenfassung",
        "feedback.field_audio": "Das Audio",
        "feedback.field_utility": (
            "Hilft dir dieser Inhalt wirklich, dich auf die Sonntagsfeier vorzubereiten?"
        ),
        "feedback.field_standout": (
            "Was hat dich an dieser Sendung am meisten berührt oder erschien dir am nützlichsten?"
        ),
        "feedback.field_wish": (
            "Eine einzige Sache zu verbessern oder hinzuzufügen (Hintergrundmusik, kürzerer Text, …)?"
        ),
        "feedback.btn_submit": "Feedback senden",
        "feedback.thanks": "Herzlichen Dank: dein Feedback hilft LumenVia zu wachsen.",
        "common.save": "Speichern",
        "common.cancel": "Abbrechen",
    },
    "EN": {
        "nav.about_suffix": "what is it?",
        "nav.sunday_line1": "The Light",
        "nav.sunday_line2": "of Sunday",
        "nav.memo_line1": "My",
        "nav.memo_line2": "Memo",
        "nav.join_line1": "Subscribe to",
        "nav.join_line2": "the Newsletter",
        "nav.account": "My Account",
        "nav.feedback_line1": "Give",
        "nav.feedback_line2": "Your Feedback",
        "nav.menu": "Menu",
        "nav.connected_as": "🟢 Signed in · {email}",
        "nav.session_active": "active session",
        "nav.logout": "Log out",
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\nwhat is it?",
        "nav.sunday": "The Light\nof Sunday",
        "nav.memo": "My\nMemo",
        "nav.join": "Subscribe to the Newsletter",
        "nav.feedback": "Give\nYour Feedback",
        "nav.connected": "🟢 Signed in · {email}",
        "comfort.title": "Reading comfort — text size",
        "comfort.caption": "Enlarges page and reading texts for greater visual comfort.",
        "comfort.size_label": "Text size",
        "comfort.standard": "Standard",
        "comfort.large": "Large",
        "comfort.xlarge": "Extra large",
        "comfort.language_label": "Interface language",
        "comfort.language_caption": "Changes the language of menus and pages (FR/DE/EN/ES/IT/PT).",
        "about.references_title": "References & sources",
        "about.readings_heading": "Liturgical readings",
        "about.table_lang": "Language",
        "about.table_source": "Source",
        "about.table_usage": "Use in LumenVia",
        "about.fr_usage": "Production source — Mass texts via the public API (no key required).",
        "about.other_usage": (
            "Multi-language complement: complete native texts (no in-house translation from "
            "AELF). Displayed in the app + RDC cache; redistribution by e-mail / TTS / PDF of "
            "non-FR texts subject to confirmation of Evangelizo's terms."
        ),
        "about.audio_heading": "Audio",
        "about.voice_label": "Voice",
        "about.voice_desc": "Google voice synthesis (Vertex / Gemini TTS) — reads texts already available.",
        "about.ambiance_label": "Ambiance",
        "about.ambiance_desc": (
            "(intro / outro / background): **royalty-free** clips uploaded by the team in the "
            "Audio Workshop — **CC0**, **public domain** or **CC-BY** (attribution) licences. "
            "No unlicensed commercial music."
        ),
        "about.footer_note": (
            "AI-generated illustrations and content are aids to meditation; the liturgical "
            "texts remain those of the sources above."
        ),
        "sunday.title": "The Light of Sunday",
        "sunday.date_label": "Date (Sunday of the week)",
        "sunday.source_expander": "Readings source",
        "sunday.zone_caption": "Zone: **{zone}** · language **{lang}**",
        "sunday.loading_readings": "Fetching the readings…",
        "sunday.error_fetch_readings": "Unable to fetch the readings in **{lang}** for {date}.",
        "sunday.error_retry_hint": (
            "Try again with a network connection, or pick a date already viewed recently in "
            "this language on this device."
        ),
        "sunday.calendar_expander": "See content already available — {month} {year}",
        "sunday.calendar_legend": "Sunday with content",
        "sunday.preview_readings": "Readings **{lang}** (`{source}`) — preview: _{preview}_",
        "join.title_account": "My account",
        "join.title_newsletter": "Subscribe to the newsletter",
        "join.section_login": "Login",
        "join.btn_login": "Log in",
        "join.btn_logout": "Log out",
        "join.password_label": "Password",
        "join.btn_signup_mode": "Create / activate an account",
        "join.field_first_name": "First name",
        "join.field_last_name": "Last name",
        "join.field_phone": "Phone (optional, international format)",
        "join.field_country": "Country",
        "join.field_pref_langue": "Language preference",
        "join.consent": "I agree to receive these e-mails (unsubscribe at any time).",
        "join.btn_subscribe": "Subscribe",
        "join.btn_unsubscribe": "Unsubscribe",
        "join.section_my_info": "My information",
        "join.btn_save_profile": "Save my information",
        "join.section_newsletter": "Newsletter",
        "memo.title": "My Memo",
        "memo.subtitle": "Area reserved for signed-in users.",
        "memo.login_required": "You must be signed in to access **My Memo**.",
        "memo.btn_go_account": "Go to My account",
        "memo.existing_memos": "My existing memos",
        "memo.no_memo": "No memo yet.",
        "memo.new_memo_heading": "Create a new memo",
        "memo.field_title": "Title",
        "memo.field_date": "Date (Sunday)",
        "memo.field_body": "Your memo",
        "memo.field_resolution": "My resolution (this week)",
        "memo.btn_save": "Save memo",
        "memo.saved": "OK — memo saved.",
        "memo.export_heading": "PDF export — Graine de Parole",
        "feedback.title": "Give your feedback",
        "feedback.login_required": (
            "To answer the questionnaire, sign in (**My account**) or open the link received "
            "in your LumenVia e-mail — it pre-fills your address and lets you take part "
            "without an account."
        ),
        "feedback.btn_go_account": "Go to My account",
        "feedback.btn_go_join": "Subscribe to the newsletter",
        "feedback.section_first_steps": "Your first steps with LumenVia",
        "feedback.field_illustration": "The illustration",
        "feedback.field_synthesis": "The summary PDF",
        "feedback.field_audio": "The audio",
        "feedback.field_utility": "Does this content really help you prepare for Sunday Mass?",
        "feedback.field_standout": "What touched you the most or seemed the most useful in this e-mail?",
        "feedback.field_wish": (
            "One single thing to improve or add (background music, shorter text, …)?"
        ),
        "feedback.btn_submit": "Send my feedback",
        "feedback.thanks": "Thank you so much: your feedback helps LumenVia grow.",
        "common.save": "Save",
        "common.cancel": "Cancel",
    },
    "ES": {
        "nav.about_suffix": "¿qué es?",
        "nav.sunday_line1": "La Luz",
        "nav.sunday_line2": "del Domingo",
        "nav.memo_line1": "Mi",
        "nav.memo_line2": "Ayuda-Memoria",
        "nav.join_line1": "Suscribirse al",
        "nav.join_line2": "boletín",
        "nav.account": "Mi Cuenta",
        "nav.feedback_line1": "Danos tu",
        "nav.feedback_line2": "opinión",
        "nav.menu": "Menú",
        "nav.connected_as": "🟢 Conectado · {email}",
        "nav.session_active": "sesión activa",
        "nav.logout": "Cerrar sesión",
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\n¿qué es?",
        "nav.sunday": "La Luz\ndel Domingo",
        "nav.memo": "Mi\nAyuda-Memoria",
        "nav.join": "Suscribirse al boletín",
        "nav.feedback": "Danos tu\nopinión",
        "nav.connected": "🟢 Conectado · {email}",
        "comfort.title": "Comodidad de lectura — tamaño del texto",
        "comfort.caption": "Agranda los textos de las páginas y lecturas para mayor comodidad visual.",
        "comfort.size_label": "Tamaño del texto",
        "comfort.standard": "Estándar",
        "comfort.large": "Grande",
        "comfort.xlarge": "Muy grande",
        "comfort.language_label": "Idioma de la interfaz",
        "comfort.language_caption": "Cambia el idioma de los menús y las páginas (FR/DE/EN/ES/IT/PT).",
        "about.references_title": "Referencias y fuentes",
        "about.readings_heading": "Lecturas litúrgicas",
        "about.table_lang": "Idioma",
        "about.table_source": "Fuente",
        "about.table_usage": "Uso en LumenVia",
        "about.fr_usage": "Fuente de producción — textos de la misa a través de la API pública (sin clave).",
        "about.other_usage": (
            "Complemento multilingüe: textos nativos completos (sin traducción propia desde "
            "AELF). Se muestran en la app + caché RDC; la redistribución por e-mail / TTS / PDF "
            "de textos fuera de FR está sujeta a la confirmación de las condiciones de Evangelizo."
        ),
        "about.audio_heading": "Audios",
        "about.voice_label": "Voz",
        "about.voice_desc": "síntesis de voz de Google (Vertex / Gemini TTS) — lectura de los textos ya disponibles.",
        "about.ambiance_label": "Ambiente",
        "about.ambiance_desc": (
            "(intro / outro / fondo): clips **libres de derechos** subidos por el equipo en el "
            "Taller de audio — licencias **CC0**, **dominio público** o **CC-BY** (atribución). "
            "Ninguna música comercial sin licencia."
        ),
        "about.footer_note": (
            "Las ilustraciones y contenidos generados por IA son ayudas para la meditación; los "
            "textos litúrgicos siguen siendo los de las fuentes anteriores."
        ),
        "sunday.title": "La Luz del Domingo",
        "sunday.date_label": "Fecha (domingo de la semana)",
        "sunday.source_expander": "Fuente de las lecturas",
        "sunday.zone_caption": "Zona: **{zone}** · idioma **{lang}**",
        "sunday.loading_readings": "Recuperando las lecturas…",
        "sunday.error_fetch_readings": "No se pudieron recuperar las lecturas en **{lang}** para el {date}.",
        "sunday.error_retry_hint": (
            "Vuelve a intentarlo con conexión de red, o elige una fecha ya consultada "
            "recientemente en este idioma en este dispositivo."
        ),
        "sunday.calendar_expander": "Ver los contenidos ya disponibles — {month} {year}",
        "sunday.calendar_legend": "Domingo con contenido",
        "sunday.preview_readings": "Lecturas **{lang}** (`{source}`) — vista previa: _{preview}_",
        "join.title_account": "Mi cuenta",
        "join.title_newsletter": "Suscribirse al boletín",
        "join.section_login": "Iniciar sesión",
        "join.btn_login": "Iniciar sesión",
        "join.btn_logout": "Cerrar sesión",
        "join.password_label": "Contraseña",
        "join.btn_signup_mode": "Crear / activar una cuenta",
        "join.field_first_name": "Nombre",
        "join.field_last_name": "Apellido",
        "join.field_phone": "Teléfono (opcional, formato internacional)",
        "join.field_country": "País",
        "join.field_pref_langue": "Preferencia de idioma",
        "join.consent": "Acepto recibir estos correos (puedo darme de baja en cualquier momento).",
        "join.btn_subscribe": "Suscribirse",
        "join.btn_unsubscribe": "Darse de baja",
        "join.section_my_info": "Mis datos",
        "join.btn_save_profile": "Guardar mis datos",
        "join.section_newsletter": "Boletín",
        "memo.title": "Mi Ayuda-Memoria",
        "memo.subtitle": "Espacio reservado a usuarios conectados.",
        "memo.login_required": "Para acceder a **Mi Ayuda-Memoria**, debes estar conectado.",
        "memo.btn_go_account": "Ir a Mi cuenta",
        "memo.existing_memos": "Mis notas existentes",
        "memo.no_memo": "Ninguna nota por el momento.",
        "memo.new_memo_heading": "Crear una nueva nota",
        "memo.field_title": "Título",
        "memo.field_date": "Fecha (domingo)",
        "memo.field_body": "Tu nota",
        "memo.field_resolution": "Mi propósito (esta semana)",
        "memo.btn_save": "Guardar la nota",
        "memo.saved": "OK — nota guardada.",
        "memo.export_heading": "Exportar PDF — Graine de Parole",
        "feedback.title": "Danos tu opinión",
        "feedback.login_required": (
            "Para responder al cuestionario, conéctate (**Mi cuenta**) o abre el enlace "
            "recibido en tu correo de LumenVia — rellena automáticamente tu dirección y "
            "permite participar sin cuenta."
        ),
        "feedback.btn_go_account": "Ir a Mi cuenta",
        "feedback.btn_go_join": "Suscribirse al boletín",
        "feedback.section_first_steps": "Tus primeros pasos con LumenVia",
        "feedback.field_illustration": "La ilustración",
        "feedback.field_synthesis": "El PDF de síntesis",
        "feedback.field_audio": "El audio",
        "feedback.field_utility": "¿Este contenido te ayuda realmente a prepararte para la misa dominical?",
        "feedback.field_standout": "¿Qué es lo que más te ha tocado o te ha parecido más útil en este envío?",
        "feedback.field_wish": (
            "¿Una sola cosa que mejorar o añadir (música ambiental, texto más corto, …)?"
        ),
        "feedback.btn_submit": "Enviar mi opinión",
        "feedback.thanks": "Muchas gracias: tu opinión ayuda a LumenVia a crecer.",
        "common.save": "Guardar",
        "common.cancel": "Cancelar",
    },
    "IT": {
        "nav.about_suffix": "cos'è?",
        "nav.sunday_line1": "La Luce",
        "nav.sunday_line2": "della Domenica",
        "nav.memo_line1": "Il mio",
        "nav.memo_line2": "Promemoria",
        "nav.join_line1": "Iscriviti alla",
        "nav.join_line2": "newsletter",
        "nav.account": "Il mio Account",
        "nav.feedback_line1": "Lascia un",
        "nav.feedback_line2": "parere",
        "nav.menu": "Menu",
        "nav.connected_as": "🟢 Connesso · {email}",
        "nav.session_active": "sessione attiva",
        "nav.logout": "Disconnetti",
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\ncos'è?",
        "nav.sunday": "La Luce\ndella Domenica",
        "nav.memo": "Il mio\nPromemoria",
        "nav.join": "Iscriviti alla newsletter",
        "nav.feedback": "Lascia un\nparere",
        "nav.connected": "🟢 Connesso · {email}",
        "comfort.title": "Comfort di lettura — dimensione del testo",
        "comfort.caption": "Ingrandisce i testi delle pagine e delle letture per un maggiore comfort visivo.",
        "comfort.size_label": "Dimensione del testo",
        "comfort.standard": "Standard",
        "comfort.large": "Grande",
        "comfort.xlarge": "Molto grande",
        "comfort.language_label": "Lingua dell'interfaccia",
        "comfort.language_caption": "Cambia la lingua dei menu e delle pagine (FR/DE/EN/ES/IT/PT).",
        "about.references_title": "Riferimenti e fonti",
        "about.readings_heading": "Letture liturgiche",
        "about.table_lang": "Lingua",
        "about.table_source": "Fonte",
        "about.table_usage": "Uso in LumenVia",
        "about.fr_usage": "Fonte di produzione — testi della messa tramite l’API pubblica (nessuna chiave).",
        "about.other_usage": (
            "Complemento multilingue: testi nativi completi (nessuna traduzione interna "
            "dall’AELF). Visualizzati nell’app + cache RDC; ridistribuzione via e-mail / TTS / "
            "PDF dei testi non-FR soggetta a conferma delle condizioni Evangelizo."
        ),
        "about.audio_heading": "Audio",
        "about.voice_label": "Voce",
        "about.voice_desc": "sintesi vocale Google (Vertex / Gemini TTS) — lettura dei testi già disponibili.",
        "about.ambiance_label": "Atmosfera",
        "about.ambiance_desc": (
            "(intro / outro / sottofondo): clip **libere da diritti** caricate dal team "
            "nell’Atelier audio — licenze **CC0**, **dominio pubblico** o **CC-BY** "
            "(attribuzione). Nessuna musica commerciale non concessa in licenza."
        ),
        "about.footer_note": (
            "Le illustrazioni e i contenuti generati dall’IA sono aiuti alla meditazione; i "
            "testi liturgici restano quelli delle fonti sopra indicate."
        ),
        "sunday.title": "La Luce della Domenica",
        "sunday.date_label": "Data (domenica della settimana)",
        "sunday.source_expander": "Fonte delle letture",
        "sunday.zone_caption": "Zona: **{zone}** · lingua **{lang}**",
        "sunday.loading_readings": "Recupero delle letture…",
        "sunday.error_fetch_readings": "Impossibile recuperare le letture in **{lang}** per il {date}.",
        "sunday.error_retry_hint": (
            "Riprova con la rete, oppure scegli una data già consultata di recente in questa "
            "lingua su questo dispositivo."
        ),
        "sunday.calendar_expander": "Vedi i contenuti già disponibili — {month} {year}",
        "sunday.calendar_legend": "Domenica con contenuto",
        "sunday.preview_readings": "Letture **{lang}** (`{source}`) — anteprima: _{preview}_",
        "join.title_account": "Il mio account",
        "join.title_newsletter": "Iscriviti alla newsletter",
        "join.section_login": "Accesso",
        "join.btn_login": "Accedi",
        "join.btn_logout": "Disconnetti",
        "join.password_label": "Password",
        "join.btn_signup_mode": "Crea / attiva un account",
        "join.field_first_name": "Nome",
        "join.field_last_name": "Cognome",
        "join.field_phone": "Telefono (facoltativo, formato internazionale)",
        "join.field_country": "Paese",
        "join.field_pref_langue": "Preferenza linguistica",
        "join.consent": "Accetto di ricevere queste e-mail (disiscrizione possibile in qualsiasi momento).",
        "join.btn_subscribe": "Iscriviti",
        "join.btn_unsubscribe": "Annulla iscrizione",
        "join.section_my_info": "Le mie informazioni",
        "join.btn_save_profile": "Salva le mie informazioni",
        "join.section_newsletter": "Newsletter",
        "memo.title": "Il mio Promemoria",
        "memo.subtitle": "Spazio riservato agli utenti connessi.",
        "memo.login_required": "Per accedere a **Il mio Promemoria** devi essere connesso.",
        "memo.btn_go_account": "Vai a Il mio account",
        "memo.existing_memos": "I miei promemoria esistenti",
        "memo.no_memo": "Nessun promemoria per il momento.",
        "memo.new_memo_heading": "Crea un nuovo promemoria",
        "memo.field_title": "Titolo",
        "memo.field_date": "Data (domenica)",
        "memo.field_body": "Il tuo promemoria",
        "memo.field_resolution": "Il mio proposito (questa settimana)",
        "memo.btn_save": "Salva il promemoria",
        "memo.saved": "OK — promemoria salvato.",
        "memo.export_heading": "Esporta PDF — Graine de Parole",
        "feedback.title": "Lascia un parere",
        "feedback.login_required": (
            "Per rispondere al questionario, connettiti (**Il mio account**) oppure apri il "
            "link ricevuto nella tua e-mail LumenVia — precompila il tuo indirizzo e consente "
            "di partecipare senza account."
        ),
        "feedback.btn_go_account": "Vai a Il mio account",
        "feedback.btn_go_join": "Iscriviti alla newsletter",
        "feedback.section_first_steps": "I tuoi primi passi con LumenVia",
        "feedback.field_illustration": "L'illustrazione",
        "feedback.field_synthesis": "Il PDF di sintesi",
        "feedback.field_audio": "L'audio",
        "feedback.field_utility": "Questo contenuto ti aiuta davvero a prepararti per la celebrazione domenicale?",
        "feedback.field_standout": "Cosa ti ha colpito di più o ti è sembrato più utile in questo invio?",
        "feedback.field_wish": (
            "Una sola cosa da migliorare o aggiungere (musica d'ambiente, testo più breve, …)?"
        ),
        "feedback.btn_submit": "Invia il mio parere",
        "feedback.thanks": "Grazie infinite: il tuo parere aiuta LumenVia a crescere.",
        "common.save": "Salva",
        "common.cancel": "Annulla",
    },
    "PT": {
        "nav.about_suffix": "o que é?",
        "nav.sunday_line1": "A Luz",
        "nav.sunday_line2": "do Domingo",
        "nav.memo_line1": "O Meu",
        "nav.memo_line2": "Memo",
        "nav.join_line1": "Subscrever a",
        "nav.join_line2": "Newsletter",
        "nav.account": "A Minha Conta",
        "nav.feedback_line1": "Dar a tua",
        "nav.feedback_line2": "Opinião",
        "nav.menu": "Menu",
        "nav.connected_as": "🟢 Sessão iniciada · {email}",
        "nav.session_active": "sessão ativa",
        "nav.logout": "Terminar sessão",
        "nav.about": "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮\u00a0:\no que é?",
        "nav.sunday": "A Luz\ndo Domingo",
        "nav.memo": "O Meu\nMemo",
        "nav.join": "Subscrever a Newsletter",
        "nav.feedback": "Dar a tua\nOpinião",
        "nav.connected": "🟢 Sessão iniciada · {email}",
        "comfort.title": "Conforto de leitura — tamanho do texto",
        "comfort.caption": "Aumenta os textos das páginas e das leituras para maior conforto visual.",
        "comfort.size_label": "Tamanho do texto",
        "comfort.standard": "Padrão",
        "comfort.large": "Grande",
        "comfort.xlarge": "Muito grande",
        "comfort.language_label": "Língua da interface",
        "comfort.language_caption": "Muda a língua dos menus e das páginas (FR/DE/EN/ES/IT/PT).",
        "about.references_title": "Referências e fontes",
        "about.readings_heading": "Leituras litúrgicas",
        "about.table_lang": "Língua",
        "about.table_source": "Fonte",
        "about.table_usage": "Utilização no LumenVia",
        "about.fr_usage": (
            "Fonte de produção — textos da missa através da API pública (sem chave)."
        ),
        "about.other_usage": (
            "Complemento multilingue: textos nativos completos (sem tradução própria a partir "
            "da AELF). Apresentados na aplicação + cache RDC; a redistribuição por e-mail / TTS / "
            "PDF dos textos fora do FR está sujeita à confirmação das condições da Evangelizo."
        ),
        "about.audio_heading": "Áudios",
        "about.voice_label": "Voz",
        "about.voice_desc": (
            "síntese de voz Google (Vertex / Gemini TTS) — leitura dos textos já disponíveis."
        ),
        "about.ambiance_label": "Ambiente",
        "about.ambiance_desc": (
            "(intro / outro / fundo): clipes **livres de direitos** carregados pela equipa no "
            "Atelier áudio — licenças **CC0**, **domínio público** ou **CC-BY** (atribuição). "
            "Nenhuma música comercial sem licença."
        ),
        "about.footer_note": (
            "As ilustrações e conteúdos gerados por IA são ajudas à meditação; os textos "
            "litúrgicos continuam a ser os das fontes acima."
        ),
        "sunday.title": "A Luz do Domingo",
        "sunday.date_label": "Data (domingo da semana)",
        "sunday.source_expander": "Fonte das leituras",
        "sunday.zone_caption": "Zona: **{zone}** · língua **{lang}**",
        "sunday.loading_readings": "A obter as leituras…",
        "sunday.error_fetch_readings": "Não foi possível obter as leituras em **{lang}** para {date}.",
        "sunday.error_retry_hint": (
            "Tenta novamente com ligação à rede, ou escolhe uma data já consultada "
            "recentemente nesta língua neste dispositivo."
        ),
        "sunday.calendar_expander": "Ver os conteúdos já disponíveis — {month} {year}",
        "sunday.calendar_legend": "Domingo com conteúdo",
        "sunday.preview_readings": "Leituras **{lang}** (`{source}`) — pré-visualização: _{preview}_",
        "join.title_account": "A minha conta",
        "join.title_newsletter": "Subscrever a newsletter",
        "join.section_login": "Iniciar sessão",
        "join.btn_login": "Iniciar sessão",
        "join.btn_logout": "Terminar sessão",
        "join.password_label": "Palavra-passe",
        "join.btn_signup_mode": "Criar / ativar uma conta",
        "join.field_first_name": "Primeiro nome",
        "join.field_last_name": "Apelido",
        "join.field_phone": "Telefone (opcional, formato internacional)",
        "join.field_country": "País",
        "join.field_pref_langue": "Preferência de língua",
        "join.consent": "Aceito receber estes e-mails (é possível cancelar a subscrição em qualquer momento).",
        "join.btn_subscribe": "Subscrever",
        "join.btn_unsubscribe": "Cancelar subscrição",
        "join.section_my_info": "As minhas informações",
        "join.btn_save_profile": "Guardar as minhas informações",
        "join.section_newsletter": "Newsletter",
        "memo.title": "O Meu Memo",
        "memo.subtitle": "Espaço reservado aos utilizadores com sessão iniciada.",
        "memo.login_required": "Para acederes a **O Meu Memo**, tens de ter sessão iniciada.",
        "memo.btn_go_account": "Ir para A Minha Conta",
        "memo.existing_memos": "Os meus memos existentes",
        "memo.no_memo": "Ainda não há nenhum memo.",
        "memo.new_memo_heading": "Criar um novo memo",
        "memo.field_title": "Título",
        "memo.field_date": "Data (domingo)",
        "memo.field_body": "O teu memo",
        "memo.field_resolution": "A minha resolução (esta semana)",
        "memo.btn_save": "Guardar o memo",
        "memo.saved": "OK — memo guardado.",
        "memo.export_heading": "Exportar PDF — Graine de Parole",
        "feedback.title": "Dar a tua opinião",
        "feedback.login_required": (
            "Para responder ao questionário, inicia sessão (**A Minha Conta**) ou abre a "
            "hiperligação recebida no teu e-mail LumenVia — esta preenche automaticamente o "
            "teu endereço e permite participar sem conta."
        ),
        "feedback.btn_go_account": "Ir para A Minha Conta",
        "feedback.btn_go_join": "Subscrever a newsletter",
        "feedback.section_first_steps": "Os teus primeiros passos com o LumenVia",
        "feedback.field_illustration": "A ilustração",
        "feedback.field_synthesis": "O PDF de síntese",
        "feedback.field_audio": "O áudio",
        "feedback.field_utility": (
            "Este conteúdo ajuda-te realmente a preparar a celebração de domingo?"
        ),
        "feedback.field_standout": (
            "O que é que mais te tocou ou te pareceu mais útil neste envio?"
        ),
        "feedback.field_wish": (
            "Uma única coisa a melhorar ou a acrescentar (música de ambiente, texto mais curto, …)?"
        ),
        "feedback.btn_submit": "Enviar a minha opinião",
        "feedback.thanks": "Muito obrigado: a tua opinião ajuda o LumenVia a crescer.",
        "common.save": "Guardar",
        "common.cancel": "Cancelar",
    },
}
