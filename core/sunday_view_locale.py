"""Libellés UI « contenu liturgique » localisés (Dimanche) + panneau langue.

Le chrome site reste FR ; cette couche ne couvre que la zone de contenu
(identité, temps liturgique, commentaire image, titres de lectures) et le
bandeau d’édition admin lié à ``pref_langue``.
"""

from __future__ import annotations

from html import escape as html_escape

from core.liturgy_theme import norm_key
from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue
from core.prompt_locale import PDF_READING_TITLES, coerce_aip_langue, output_language_label

LANG_FLAGS: dict[str, str] = {
    "FR": "🇫🇷",
    "DE": "🇩🇪",
    "EN": "🇬🇧",
    "ES": "🇪🇸",
    "IT": "🇮🇹",
    "PT": "🇵🇹",
}

# Codes flagcdn (ISO 3166) — les emoji drapeaux ne s’affichent souvent pas sous Windows / Chrome desktop.
LANG_FLAG_CDN: dict[str, str] = {
    "FR": "fr",
    "DE": "de",
    "EN": "gb",
    "ES": "es",
    "IT": "it",
    "PT": "pt",
}

# Fonds discrets par langue (lisibles sur crème liturgique, distincts les uns des autres).
LANG_PANEL_STYLE: dict[str, dict[str, str]] = {
    "FR": {
        "bg": "rgba(180, 140, 60, 0.10)",
        "border": "rgba(139, 105, 20, 0.45)",
        "accent": "#8B6914",
        "head_bg": "rgba(180, 140, 60, 0.18)",
    },
    "DE": {
        "bg": "rgba(70, 110, 150, 0.11)",
        "border": "rgba(50, 85, 120, 0.40)",
        "accent": "#3a5f7a",
        "head_bg": "rgba(70, 110, 150, 0.18)",
    },
    "EN": {
        "bg": "rgba(90, 120, 90, 0.11)",
        "border": "rgba(55, 90, 60, 0.40)",
        "accent": "#3d6b45",
        "head_bg": "rgba(90, 120, 90, 0.18)",
    },
    "ES": {
        "bg": "rgba(160, 90, 70, 0.11)",
        "border": "rgba(130, 70, 50, 0.40)",
        "accent": "#8a4a35",
        "head_bg": "rgba(160, 90, 70, 0.18)",
    },
    "IT": {
        "bg": "rgba(100, 130, 100, 0.12)",
        "border": "rgba(60, 100, 70, 0.40)",
        "accent": "#3f6b48",
        "head_bg": "rgba(100, 130, 100, 0.18)",
    },
    "PT": {
        "bg": "rgba(150, 60, 60, 0.11)",
        "border": "rgba(120, 45, 45, 0.40)",
        "accent": "#7a3030",
        "head_bg": "rgba(150, 60, 60, 0.18)",
    },
}

_UI: dict[str, dict[str, str]] = {
    "FR": {
        "edit_banner": "Édition du contenu — langue sélectionnée",
        "edit_hint": "Les lectures, l’illustration et la génération ci-dessous suivent cette langue.",
        "content_banner": "Contenu liturgique",
        "identity": "Identité du jour",
        "readings": "Lectures",
        "liturgy_details": "Détails sur le temps liturgique",
        "season": "Temps",
        "cycle": "Cycle",
        "color": "Couleur",
        "feast": "Fête / mémoire",
        "illustration_caption": "Illustration du dimanche {date}",
        "image_comment": "Commentaire de l’image",
        "no_image_comment": "Aucun commentaire d’illustration pour ce dimanche.",
        "source_line": "Lectures · {lang} · source {source}",
    },
    "DE": {
        "edit_banner": "Inhaltsbearbeitung — gewählte Sprache",
        "edit_hint": "Lesungen, Illustration und Generierung folgen dieser Sprache.",
        "content_banner": "Liturgischer Inhalt",
        "identity": "Identität des Tages",
        "readings": "Lesungen",
        "liturgy_details": "Details zur liturgischen Zeit",
        "season": "Zeit",
        "cycle": "Zyklus",
        "color": "Farbe",
        "feast": "Fest / Gedenken",
        "illustration_caption": "Illustration des Sonntags {date}",
        "image_comment": "Bildkommentar",
        "no_image_comment": "Kein Illustrationskommentar für diesen Sonntag.",
        "source_line": "Lesungen · {lang} · Quelle {source}",
    },
    "EN": {
        "edit_banner": "Content editing — selected language",
        "edit_hint": "Readings, illustration and generation below follow this language.",
        "content_banner": "Liturgical content",
        "identity": "Day identity",
        "readings": "Readings",
        "liturgy_details": "Details on the liturgical season",
        "season": "Season",
        "cycle": "Cycle",
        "color": "Colour",
        "feast": "Feast / memorial",
        "illustration_caption": "Sunday illustration — {date}",
        "image_comment": "Image commentary",
        "no_image_comment": "No illustration commentary for this Sunday.",
        "source_line": "Readings · {lang} · source {source}",
    },
    "ES": {
        "edit_banner": "Edición del contenido — idioma seleccionado",
        "edit_hint": "Las lecturas, la ilustración y la generación siguen este idioma.",
        "content_banner": "Contenido litúrgico",
        "identity": "Identidad del día",
        "readings": "Lecturas",
        "liturgy_details": "Detalles del tiempo litúrgico",
        "season": "Tiempo",
        "cycle": "Ciclo",
        "color": "Color",
        "feast": "Fiesta / memoria",
        "illustration_caption": "Ilustración del domingo {date}",
        "image_comment": "Comentario de la imagen",
        "no_image_comment": "Sin comentario de ilustración para este domingo.",
        "source_line": "Lecturas · {lang} · fuente {source}",
    },
    "IT": {
        "edit_banner": "Modifica del contenuto — lingua selezionata",
        "edit_hint": "Letture, illustrazione e generazione seguono questa lingua.",
        "content_banner": "Contenuto liturgico",
        "identity": "Identità del giorno",
        "readings": "Letture",
        "liturgy_details": "Dettagli sul tempo liturgico",
        "season": "Tempo",
        "cycle": "Ciclo",
        "color": "Colore",
        "feast": "Festa / memoria",
        "illustration_caption": "Illustrazione della domenica {date}",
        "image_comment": "Commento dell’immagine",
        "no_image_comment": "Nessun commento all’illustrazione per questa domenica.",
        "source_line": "Letture · {lang} · fonte {source}",
    },
    "PT": {
        "edit_banner": "Edição do conteúdo — idioma selecionado",
        "edit_hint": "As leituras, a ilustração e a geração abaixo seguem este idioma.",
        "content_banner": "Conteúdo litúrgico",
        "identity": "Identidade do dia",
        "readings": "Leituras",
        "liturgy_details": "Detalhes sobre o tempo litúrgico",
        "season": "Tempo",
        "cycle": "Ciclo",
        "color": "Cor",
        "feast": "Festa / memória",
        "illustration_caption": "Ilustração do domingo {date}",
        "image_comment": "Comentário da imagem",
        "no_image_comment": "Sem comentário de ilustração para este domingo.",
        "source_line": "Leituras · {lang} · fonte {source}",
    },
}

_TIME_HINTS: dict[str, dict[str, str]] = {
    "FR": {
        "avent": "Temps de préparation à la venue du Seigneur : conversion douce, veille et espérance.",
        "noel": "Temps qui célèbre l’Incarnation : la Parole faite chair parmi nous.",
        "temps_ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "careme": "Temps de préparation pascale : prière, jeûne (intérieur) et partage.",
        "saint": "Mémoire ou fête d’un saint : exemplarité concrète de la foi.",
        "pascal": "Temps pascal : les cinquante jours qui prolongent la joie de la Résurrection jusqu’à la Pentecôte.",
        "pentecote": "Solennité de l’effusion de l’Esprit Saint sur l’Église.",
        "_default": "Grand mouvement liturgique qui colore la prière et la lecture de la Parole ce jour-là.",
    },
    "DE": {
        "avent": "Zeit der Vorbereitung auf das Kommen des Herrn: sanfte Umkehr, Wachen und Hoffnung.",
        "noel": "Zeit, die die Menschwerdung feiert: das Wort ist unter uns Fleisch geworden.",
        "temps_ordinaire": "Zeit « inmitten » der großen Feste: stilles Wachstum und Alltagsstreue.",
        "ordinaire": "Zeit « inmitten » der großen Feste: stilles Wachstum und Alltagsstreue.",
        "careme": "Zeit der österlichen Vorbereitung: Gebet, (inneres) Fasten und Teilen.",
        "saint": "Gedenken oder Fest eines Heiligen: konkretes Vorbild des Glaubens.",
        "pascal": "Osterzeit: fünfzig Tage Freude der Auferstehung bis Pfingsten.",
        "pentecote": "Hochfest der Ausgießung des Heiligen Geistes auf die Kirche.",
        "_default": "Großer liturgischer Zug, der Gebet und Schriftlesung an diesem Tag prägt.",
    },
    "EN": {
        "avent": "Season of preparation for the Lord’s coming: gentle conversion, watchfulness and hope.",
        "noel": "Season celebrating the Incarnation: the Word made flesh among us.",
        "temps_ordinaire": "Season « in between » the great feasts: quiet growth and daily faithfulness.",
        "ordinaire": "Season « in between » the great feasts: quiet growth and daily faithfulness.",
        "careme": "Season of Easter preparation: prayer, (inner) fasting and sharing.",
        "saint": "Memorial or feast of a saint: a concrete example of faith.",
        "pascal": "Easter season: fifty days carrying the joy of the Resurrection to Pentecost.",
        "pentecote": "Solemnity of the outpouring of the Holy Spirit upon the Church.",
        "_default": "A major liturgical movement that colours prayer and the reading of the Word this day.",
    },
    "ES": {
        "avent": "Tiempo de preparación a la venida del Señor: conversión serena, vigilia y esperanza.",
        "noel": "Tiempo que celebra la Encarnación: la Palabra hecha carne entre nosotros.",
        "temps_ordinaire": "Tiempo « en medio » de las grandes fiestas: crecimiento discreto y fidelidad cotidiana.",
        "ordinaire": "Tiempo « en medio » de las grandes fiestas: crecimiento discreto y fidelidad cotidiana.",
        "careme": "Tiempo de preparación pascual: oración, ayuno (interior) y compartir.",
        "saint": "Memoria o fiesta de un santo: ejemplaridad concreta de la fe.",
        "pascal": "Tiempo pascual: cincuenta días que prolongan la alegría de la Resurrección hasta Pentecostés.",
        "pentecote": "Solemnidad de la efusión del Espíritu Santo sobre la Iglesia.",
        "_default": "Gran movimiento litúrgico que colorea la oración y la lectura de la Palabra este día.",
    },
    "IT": {
        "avent": "Tempo di preparazione alla venuta del Signore: conversione mite, veglia e speranza.",
        "noel": "Tempo che celebra l’Incarnazione: la Parola fatta carne in mezzo a noi.",
        "temps_ordinaire": "Tempo « in mezzo » alle grandi feste: crescita discreta e fedeltà quotidiana.",
        "ordinaire": "Tempo « in mezzo » alle grandi feste: crescita discreta e fedeltà quotidiana.",
        "careme": "Tempo di preparazione pasquale: preghiera, digiuno (interiore) e condivisione.",
        "saint": "Memoria o festa di un santo: esemplarità concreta della fede.",
        "pascal": "Tempo pasquale: cinquanta giorni che prolungano la gioia della Risurrezione fino a Pentecoste.",
        "pentecote": "Solennità dell’effusione dello Spirito Santo sulla Chiesa.",
        "_default": "Grande movimento liturgico che colora la preghiera e la lettura della Parola in questo giorno.",
    },
    "PT": {
        "avent": "Tempo de preparação para a vinda do Senhor: conversão serena, vigilância e esperança.",
        "noel": "Tempo que celebra a Encarnação: a Palavra feita carne entre nós.",
        "temps_ordinaire": "Tempo « no meio » das grandes festas: crescimento discreto e fidelidade quotidiana.",
        "ordinaire": "Tempo « no meio » das grandes festas: crescimento discreto e fidelidade quotidiana.",
        "careme": "Tempo de preparação pascal: oração, jejum (interior) e partilha.",
        "saint": "Memória ou festa de um santo: exemplaridade concreta da fé.",
        "pascal": "Tempo pascal: os cinquenta dias que prolongam a alegria da Ressurreição até Pentecostes.",
        "pentecote": "Solenidade da efusão do Espírito Santo sobre a Igreja.",
        "_default": "Grande movimento litúrgico que colore a oração e a leitura da Palavra neste dia.",
    },
}

_COLOR_HINTS: dict[str, dict[str, str]] = {
    "FR": {
        "blanc": "Couleur de joie et de gloire : grandes fêtes du Seigneur et de Marie (selon le temps).",
        "vert": "Couleur du Temps Ordinaire : vie chrétienne qui grandit dans la fidélité.",
        "rouge": "Couleur du martyre et de l’Esprit : don total et charité jusqu’au bout.",
        "violet": "Couleur de pénitence et d’attente : conversion et préparation (Avent/Carême selon le temps).",
        "rose": "Couleur d’allégement ponctuel au milieu de l’attente (Guadete / Laetare).",
        "noir": "Solennité funéraire ou jour marqué par le deuil liturgique.",
        "_default": "La couleur vestimentaire traduit visuellement le climat liturgique du jour.",
    },
    "DE": {
        "blanc": "Farbe der Freude und Herrlichkeit: große Feste des Herrn und Mariens (je nach Zeit).",
        "vert": "Farbe der Zeit im Jahreskreis: christliches Leben, das in Treue wächst.",
        "rouge": "Farbe des Martyriums und des Geistes: totale Hingabe und Liebe bis zum Ende.",
        "violet": "Farbe der Buße und Erwartung: Umkehr und Vorbereitung (Advent/Fastenzeit).",
        "rose": "Farbe der vorübergehenden Erleichterung in der Mitte der Erwartung (Gaudete / Laetare).",
        "noir": "Trauerfeier oder Tag mit liturgischer Trauer.",
        "_default": "Die liturgische Farbe macht das Klima des Tages sichtbar.",
    },
    "EN": {
        "blanc": "Colour of joy and glory: major feasts of the Lord and of Mary (by season).",
        "vert": "Colour of Ordinary Time: Christian life growing in faithfulness.",
        "rouge": "Colour of martyrdom and of the Spirit: total gift and charity to the end.",
        "violet": "Colour of penance and waiting: conversion and preparation (Advent/Lent).",
        "rose": "Colour of brief relief amid waiting (Gaudete / Laetare).",
        "noir": "Funeral solemnity or a day marked by liturgical mourning.",
        "_default": "Vestment colour visually expresses the liturgical climate of the day.",
    },
    "ES": {
        "blanc": "Color de alegría y gloria: grandes fiestas del Señor y de María (según el tiempo).",
        "vert": "Color del Tiempo Ordinario: vida cristiana que crece en la fidelidad.",
        "rouge": "Color del martirio y del Espíritu: don total y caridad hasta el final.",
        "violet": "Color de penitencia y espera: conversión y preparación (Adviento/Cuaresma).",
        "rose": "Color de alivio puntual en medio de la espera (Gaudete / Laetare).",
        "noir": "Solemnidad fúnebre o día marcado por el duelo litúrgico.",
        "_default": "El color de los ornamentos expresa visualmente el clima litúrgico del día.",
    },
    "IT": {
        "blanc": "Colore di gioia e gloria: grandi feste del Signore e di Maria (secondo il tempo).",
        "vert": "Colore del Tempo Ordinario: vita cristiana che cresce nella fedeltà.",
        "rouge": "Colore del martirio e dello Spirito: dono totale e carità fino in fondo.",
        "violet": "Colore di penitenza e attesa: conversione e preparazione (Avvento/Quaresima).",
        "rose": "Colore di lieve alleggerimento in mezzo all’attesa (Gaudete / Laetare).",
        "noir": "Solennità funebre o giorno segnato dal lutto liturgico.",
        "_default": "Il colore dei paramenti traduce visivamente il clima liturgico del giorno.",
    },
    "PT": {
        "blanc": "Cor de alegria e glória: grandes festas do Senhor e de Maria (segundo o tempo).",
        "vert": "Cor do Tempo Comum: vida cristã que cresce na fidelidade.",
        "rouge": "Cor do martírio e do Espírito: dom total e caridade até ao fim.",
        "violet": "Cor de penitência e espera: conversão e preparação (Advento/Quaresma).",
        "rose": "Cor de alívio pontual no meio da espera (Gaudete / Laetare).",
        "noir": "Solenidade fúnebre ou dia marcado pelo luto litúrgico.",
        "_default": "A cor dos paramentos traduz visualmente o clima litúrgico do dia.",
    },
}

_CYCLE_HINTS: dict[str, dict[str, str]] = {
    "FR": {
        "a": "Année A : le dimanche met souvent en avant l’Évangile selon Matthieu.",
        "b": "Année B : le dimanche met souvent en avant l’Évangile selon Marc.",
        "c": "Année C : le dimanche met souvent en avant l’Évangile selon Luc.",
        "_default": "Le cycle liturgique fait tourner les lectures dominicales pour nourrir la foi sur plusieurs années.",
    },
    "DE": {
        "a": "Jahr A: der Sonntag stellt oft das Matthäusevangelium in den Vordergrund.",
        "b": "Jahr B: der Sonntag stellt oft das Markusevangelium in den Vordergrund.",
        "c": "Jahr C: der Sonntag stellt oft das Lukasevangelium in den Vordergrund.",
        "_default": "Der liturgische Zyklus wechselt die Sonntagslesungen, um den Glauben über Jahre zu nähren.",
    },
    "EN": {
        "a": "Year A: Sundays often feature the Gospel according to Matthew.",
        "b": "Year B: Sundays often feature the Gospel according to Mark.",
        "c": "Year C: Sundays often feature the Gospel according to Luke.",
        "_default": "The liturgical cycle rotates Sunday readings to nourish faith across several years.",
    },
    "ES": {
        "a": "Año A: el domingo suele destacar el Evangelio según Mateo.",
        "b": "Año B: el domingo suele destacar el Evangelio según Marcos.",
        "c": "Año C: el domingo suele destacar el Evangelio según Lucas.",
        "_default": "El ciclo litúrgico hace rotar las lecturas dominicales para alimentar la fe a lo largo de los años.",
    },
    "IT": {
        "a": "Anno A: la domenica mette spesso in rilievo il Vangelo secondo Matteo.",
        "b": "Anno B: la domenica mette spesso in rilievo il Vangelo secondo Marco.",
        "c": "Anno C: la domenica mette spesso in rilievo il Vangelo secondo Luca.",
        "_default": "Il ciclo liturgico fa ruotare le letture domenicali per nutrire la fede nel corso degli anni.",
    },
    "PT": {
        "a": "Ano A: o domingo destaca frequentemente o Evangelho segundo Mateus.",
        "b": "Ano B: o domingo destaca frequentemente o Evangelho segundo Marcos.",
        "c": "Ano C: o domingo destaca frequentemente o Evangelho segundo Lucas.",
        "_default": "O ciclo litúrgico faz rodar as leituras dominicais para alimentar a fé ao longo dos anos.",
    },
}


def sunday_ui(pref_langue: object | None) -> dict[str, str]:
    lg = coerce_aip_langue(pref_langue)
    return dict(_UI.get(lg) or _UI[DEFAULT_PREF_LANGUE])


def lang_flag(pref_langue: object | None) -> str:
    return LANG_FLAGS.get(coerce_aip_langue(pref_langue), "🏳️")


def lang_flag_html(pref_langue: object | None, *, height: int = 14) -> str:
    """Drapeau via image CDN (lisible desktop Windows + mobile)."""
    lg = coerce_aip_langue(pref_langue)
    code = LANG_FLAG_CDN.get(lg, lg.lower()[:2])
    h = max(10, min(int(height), 28))
    return (
        f'<img src="https://flagcdn.com/h20/{html_escape(code)}.png" '
        f'height="{h}" width="{int(h * 4 / 3)}" alt="{html_escape(lg)}" '
        f'title="{html_escape(lg)}" '
        f'style="vertical-align:-2px;margin-right:0.28rem;border-radius:2px;" loading="lazy"/>'
    )


def reading_titles(pref_langue: object | None) -> dict[str, str]:
    lg = coerce_aip_langue(pref_langue)
    return dict(PDF_READING_TITLES.get(lg) or PDF_READING_TITLES[DEFAULT_PREF_LANGUE])


def explain_time_localized(periode: str | None, pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    hints = _TIME_HINTS.get(lg) or _TIME_HINTS[DEFAULT_PREF_LANGUE]
    k = norm_key(periode)
    if k in hints:
        return hints[k]
    if "pentecot" in k:
        return hints.get("pentecote") or hints["_default"]
    return hints["_default"]


def explain_color_localized(couleur: str | None, pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    hints = _COLOR_HINTS.get(lg) or _COLOR_HINTS[DEFAULT_PREF_LANGUE]
    return hints.get(norm_key(couleur), hints["_default"])


def explain_cycle_localized(annee: str | None, pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    hints = _CYCLE_HINTS.get(lg) or _CYCLE_HINTS[DEFAULT_PREF_LANGUE]
    k = norm_key(annee)
    if k in hints:
        return hints[k]
    if k in ("annee_i", "i"):
        return hints.get("a") or hints["_default"]  # fallback soft
    return hints["_default"]


def lang_panel_banner_html(
    *,
    pref_langue: object | None,
    kind: str = "content",
    source_id: str | None = None,
) -> str:
    """Bandeau HTML (drapeau + titre) pour ouvrir une zone contextualisée."""
    lg = coerce_aip_langue(pref_langue)
    ui = sunday_ui(lg)
    style = LANG_PANEL_STYLE.get(lg) or LANG_PANEL_STYLE[DEFAULT_PREF_LANGUE]
    flag = lang_flag_html(lg, height=18)
    native = html_escape(output_language_label(lg))
    title = html_escape(ui["edit_banner"] if kind == "edit" else ui["content_banner"])
    if kind == "edit":
        hint = html_escape(ui["edit_hint"])
    else:
        hint = ui["source_line"].format(
            lang=html_escape(lg), source=html_escape(str(source_id or "—"))
        )
    return f"""
<div class="lv-lang-banner lv-lang-banner-{html_escape(lg)}" style="
  background:{style['head_bg']};
  border:1px solid {style['border']};
  border-left:5px solid {style['accent']};
  border-radius:10px;
  padding:0.65rem 0.85rem;
  margin:0.35rem 0 0.75rem;
">
  <div style="display:flex;align-items:center;gap:0.55rem;flex-wrap:wrap;">
    <span style="line-height:1;display:inline-flex;align-items:center;" aria-hidden="true">{flag}</span>
    <div style="min-width:0;">
      <div style="font-weight:700;color:{style['accent']};font-size:1.02rem;line-height:1.25;">
        {title} · {html_escape(lg)}
      </div>
      <div style="font-size:0.86rem;color:#5f4f3a;opacity:0.95;margin-top:0.15rem;">
        {native} — {hint}
      </div>
    </div>
  </div>
</div>
""".strip()


def lang_panel_css(pref_langue: object | None, *, container_key: str) -> str:
    """CSS ciblant le conteneur Streamlit ``key=…`` pour un fond de section."""
    lg = coerce_aip_langue(pref_langue)
    style = LANG_PANEL_STYLE.get(lg) or LANG_PANEL_STYLE[DEFAULT_PREF_LANGUE]
    # Streamlit expose souvent st-key-<key> sur le wrapper.
    safe_key = str(container_key).replace('"', "")
    return f"""
<style>
div[class*="st-key-{safe_key}"],
div[data-testid="stVerticalBlockBorderWrapper"]:has(.lv-lang-banner-{lg}) {{
  background: {style['bg']} !important;
  border-color: {style['border']} !important;
  border-radius: 12px !important;
  padding-top: 0.35rem !important;
  padding-bottom: 0.55rem !important;
}}
</style>
""".strip()
