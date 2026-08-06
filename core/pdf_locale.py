"""Libellés PDF fascicule selon ``pref_langue`` (couverture, pied, À propos, dos).

Le chrome du site reste FR ; ce module ne couvre que le fascicule PDF.
"""

from __future__ import annotations

from datetime import date

from core.liturgy_display_helpers import cycle_year_display, liturgy_display_label
from core.locale_codes import DEFAULT_PREF_LANGUE
from core.prompt_locale import coerce_aip_langue
from core.sunday_view_locale import sunday_ui

# Mois / jours pour la ligne de date couverture
_MONTHS: dict[str, tuple[str, ...]] = {
    "FR": (
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ),
    "DE": (
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
    "EN": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "ES": (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ),
    "IT": (
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    ),
}

_WEEKDAYS: dict[str, tuple[str, ...]] = {
    "FR": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "DE": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "EN": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "ES": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "IT": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
}

# Libellés courts période / couleur (couverture) — les codes AELF restent FR en entrée.
_SEASON_SHORT: dict[str, dict[str, str]] = {
    "FR": {
        "avent": "Avent",
        "noel": "Noël",
        "temps_ordinaire": "Temps Ordinaire",
        "ordinaire": "Temps Ordinaire",
        "careme": "Carême",
        "pascal": "Temps Pascal",
        "pentecote": "Pentecôte",
    },
    "DE": {
        "avent": "Advent",
        "noel": "Weihnachten",
        "temps_ordinaire": "Jahreskreis",
        "ordinaire": "Jahreskreis",
        "careme": "Fastenzeit",
        "pascal": "Osterzeit",
        "pentecote": "Pfingsten",
    },
    "EN": {
        "avent": "Advent",
        "noel": "Christmas",
        "temps_ordinaire": "Ordinary Time",
        "ordinaire": "Ordinary Time",
        "careme": "Lent",
        "pascal": "Easter Time",
        "pentecote": "Pentecost",
    },
    "ES": {
        "avent": "Adviento",
        "noel": "Navidad",
        "temps_ordinaire": "Tiempo Ordinario",
        "ordinaire": "Tiempo Ordinario",
        "careme": "Cuaresma",
        "pascal": "Tiempo Pascual",
        "pentecote": "Pentecostés",
    },
    "IT": {
        "avent": "Avvento",
        "noel": "Natale",
        "temps_ordinaire": "Tempo Ordinario",
        "ordinaire": "Tempo Ordinario",
        "careme": "Quaresima",
        "pascal": "Tempo Pasquale",
        "pentecote": "Pentecoste",
    },
}

_COLOR_SHORT: dict[str, dict[str, str]] = {
    "FR": {
        "blanc": "Blanc",
        "vert": "Vert",
        "rouge": "Rouge",
        "violet": "Violet",
        "rose": "Rose",
        "noir": "Noir",
    },
    "DE": {
        "blanc": "Weiß",
        "vert": "Grün",
        "rouge": "Rot",
        "violet": "Violett",
        "rose": "Rosa",
        "noir": "Schwarz",
    },
    "EN": {
        "blanc": "White",
        "vert": "Green",
        "rouge": "Red",
        "violet": "Violet",
        "rose": "Rose",
        "noir": "Black",
    },
    "ES": {
        "blanc": "Blanco",
        "vert": "Verde",
        "rouge": "Rojo",
        "violet": "Morado",
        "rose": "Rosa",
        "noir": "Negro",
    },
    "IT": {
        "blanc": "Bianco",
        "vert": "Verde",
        "rouge": "Rosso",
        "violet": "Viola",
        "rose": "Rosa",
        "noir": "Nero",
    },
}

_PDF_UI: dict[str, dict[str, str]] = {
    "FR": {
        "listen_readings": "Écouter les lectures",
        "listen_synthesis": "Écouter la synthèse audio",
        "about_title": "À propos de JOPAI LumenVia",
        "footer_rights": " LumenVia - 2026 | TOUS DROITS RESERVES",
        "dev_notice": (
            "LumenVia est encore en développement — Contenu et fonctionnalités non contractuels."
        ),
        "back_cover_note": (
            "Ce canevas déploie les 51 étapes de notre marche liturgique. Chaque vignette est une "
            "fenêtre ouverte sur la Parole, une escale visuelle pour méditer les mystères de la semaine. "
            "Suivez ce fil de lumière, de dimanche en dimanche, pour habiter le temps avec espérance"
        ),
        "about_quote_needle": "Ta Parole est une lampe",
        "about_closing_needle": "Puisse cet outil",
    },
    "DE": {
        "listen_readings": "Lesungen anhören",
        "listen_synthesis": "Audiosynthese anhören",
        "about_title": "Über JOPAI LumenVia",
        "footer_rights": " LumenVia - 2026 | ALLE RECHTE VORBEHALTEN",
        "dev_notice": (
            "LumenVia befindet sich noch in der Entwicklung — Inhalte und Funktionen unverbindlich."
        ),
        "back_cover_note": (
            "Diese Bildtafel entfaltet die 51 Stationen unseres liturgischen Weges. Jede Vignette ist "
            "ein Fenster auf das Wort, ein visueller Halt, um die Geheimnisse der Woche zu bedenken. "
            "Folgen Sie diesem Lichtfaden, Sonntag für Sonntag, um die Zeit mit Hoffnung zu bewohnen"
        ),
        "about_quote_needle": "Dein Wort ist eine Leuchte",
        "about_closing_needle": "Möge dieses Werkzeug",
    },
    "EN": {
        "listen_readings": "Listen to the readings",
        "listen_synthesis": "Listen to the audio synthesis",
        "about_title": "About JOPAI LumenVia",
        "footer_rights": " LumenVia - 2026 | ALL RIGHTS RESERVED",
        "dev_notice": (
            "LumenVia is still under development — Content and features are non-contractual."
        ),
        "back_cover_note": (
            "This canvas unfolds the 51 stages of our liturgical journey. Each vignette is a window "
            "onto the Word, a visual stop to meditate on the mysteries of the week. Follow this thread "
            "of light, Sunday after Sunday, to inhabit time with hope"
        ),
        "about_quote_needle": "Your word is a lamp",
        "about_closing_needle": "May this tool",
    },
    "ES": {
        "listen_readings": "Escuchar las lecturas",
        "listen_synthesis": "Escuchar la síntesis en audio",
        "about_title": "Acerca de JOPAI LumenVia",
        "footer_rights": " LumenVia - 2026 | TODOS LOS DERECHOS RESERVADOS",
        "dev_notice": (
            "LumenVia aún está en desarrollo — Contenido y funciones no contractuales."
        ),
        "back_cover_note": (
            "Este lienzo despliega las 51 etapas de nuestra marcha litúrgica. Cada viñeta es una "
            "ventana abierta a la Palabra, una escala visual para meditar los misterios de la semana. "
            "Sigan este hilo de luz, domingo tras domingo, para habitar el tiempo con esperanza"
        ),
        "about_quote_needle": "Tu Palabra es una lámpara",
        "about_closing_needle": "Que esta herramienta",
    },
    "IT": {
        "listen_readings": "Ascoltare le letture",
        "listen_synthesis": "Ascoltare la sintesi audio",
        "about_title": "A proposito di JOPAI LumenVia",
        "footer_rights": " LumenVia - 2026 | TUTTI I DIRITTI RISERVATI",
        "dev_notice": (
            "LumenVia è ancora in sviluppo — Contenuti e funzionalità non contrattuali."
        ),
        "back_cover_note": (
            "Questo canovaccio dispiega le 51 tappe del nostro cammino liturgico. Ogni vignetta è "
            "una finestra aperta sulla Parola, una sosta visiva per meditare i misteri della settimana. "
            "Seguite questo filo di luce, domenica dopo domenica, per abitare il tempo con speranza"
        ),
        "about_quote_needle": "La tua Parola è lampada",
        "about_closing_needle": "Possa questo strumento",
    },
}

_ABOUT_MD: dict[str, str] = {
    "FR": """
« *Ta Parole est une lampe sur mes pas, une lumière sur mon sentier.* »


JOPAI LumenVia est un compagnon spirituel conçu pour vous aider à franchir le seuil de la célébration avec un cœur ouvert et une intelligence éclairée.
Trop souvent, nous arrivons à la messe sans avoir eu le temps de déposer le bruit du monde. Ce site est une pause, un chemin de lumière (**LumenVia**) pour vous préparer à recevoir la Parole de Dieu.

**Pourquoi utiliser LumenVia ?**

- **Comprendre l’essentiel** : avec l'aide de l'Intelligence Artificielle, nous mettons en perspective les lectures du dimanche pour vous en offrir la synthèse. Il ne s’agit pas d’inventer, mais de souligner le fil rouge qui relie les textes entre eux.
- **Se préparer en chemin** : que vous préfériez lire ou écouter, LumenVia génère pour vous un résumé écrit et un audio. Écoutez la synthèse dans les transports ou en marchant vers l'église pour laisser l’esprit de la fête infuser en vous.
- **Vivre le temps liturgique** : de l’or du Temps Ordinaire au violet du Carême, l’application s’habille aux couleurs de l’Église pour vous aider à habiter pleinement chaque saison de l’année.

**Comment parcourir ce chemin ?**

- **La Lumière du Dimanche** : découvrez les textes du jour et leur synthèse pour nourrir votre méditation.
- **Mon Aide-Mémoire** : créez vos propres mémos pour garder une trace de ce qui a touché votre cœur.
- **Nous rejoindre** : abonnez-vous pour recevoir chaque vendredi soir votre préparation dominicale directement par e-mail, ou par SMS.

Puisse cet outil vous aider à transformer chaque messe en une rencontre plus profonde et plus consciente avec le Christ.
""".strip(),
    "DE": """
« *Dein Wort ist eine Leuchte für meinen Fuß und ein Licht auf meinem Weg.* »


JOPAI LumenVia ist ein geistlicher Begleiter, der Ihnen hilft, die Schwelle der Feier mit offenem Herzen und erleuchtetem Verstand zu überschreiten.
Allzu oft kommen wir zur Messe, ohne den Lärm der Welt abgelegt zu haben. Diese Seite ist eine Pause, ein Lichtweg (**LumenVia**), um Sie auf den Empfang des Wortes Gottes vorzubereiten.

**Warum LumenVia nutzen?**

- **Das Wesentliche verstehen**: Mit Hilfe der Künstlichen Intelligenz stellen wir die Sonntagslesungen in Beziehung und bieten Ihnen eine Synthese. Es geht nicht um Erfindung, sondern darum, den roten Faden zwischen den Texten zu zeigen.
- **Unterwegs vorbereiten**: Ob Sie lieber lesen oder hören — LumenVia erzeugt für Sie eine schriftliche Zusammenfassung und ein Audio. Hören Sie die Synthese unterwegs oder auf dem Weg zur Kirche, damit der Geist des Festes in Ihnen wirkt.
- **Die liturgische Zeit leben**: Vom Gold des Jahreskreises bis zum Violett der Fastenzeit kleidet sich die Anwendung in die Farben der Kirche, damit Sie jede Jahreszeit voll bewohnen.

**Wie gehen Sie diesen Weg?**

- **Das Licht des Sonntags**: Entdecken Sie die Texte des Tages und ihre Synthese für Ihre Meditation.
- **Mein Merkzettel**: Erstellen Sie eigene Notizen zu dem, was Ihr Herz berührt hat.
- **Mitmachen**: Abonnieren Sie, um jeden Freitagabend Ihre Sonntagsvorbereitung per E-Mail oder SMS zu erhalten.

Möge dieses Werkzeug Ihnen helfen, jede Messe in eine tiefere und bewusstere Begegnung mit Christus zu verwandeln.
""".strip(),
    "EN": """
« *Your word is a lamp for my feet, a light on my path.* »


JOPAI LumenVia is a spiritual companion designed to help you cross the threshold of the celebration with an open heart and an enlightened mind.
Too often we arrive at Mass without having set aside the noise of the world. This site is a pause, a path of light (**LumenVia**) to prepare you to receive the Word of God.

**Why use LumenVia?**

- **Grasp the essentials**: with the help of Artificial Intelligence, we put the Sunday readings in perspective and offer you a synthesis. This is not invention, but highlighting the thread that connects the texts.
- **Prepare on the way**: whether you prefer to read or listen, LumenVia generates a written summary and an audio for you. Listen to the synthesis on public transport or while walking to church so the spirit of the feast may seep in.
- **Live the liturgical season**: from the gold of Ordinary Time to the violet of Lent, the app dresses in the Church’s colours to help you inhabit each season fully.

**How to walk this path?**

- **The Light of Sunday**: discover the day’s texts and their synthesis to nourish your meditation.
- **My Memo**: create your own notes to keep what touched your heart.
- **Join us**: subscribe to receive your Sunday preparation by e-mail or SMS every Friday evening.

May this tool help you turn every Mass into a deeper and more conscious encounter with Christ.
""".strip(),
    "ES": """
« *Tu Palabra es una lámpara para mis pasos, una luz en mi sendero.* »


JOPAI LumenVia es un compañero espiritual pensado para ayudarte a cruzar el umbral de la celebración con el corazón abierto y la inteligencia iluminada.
Demasiado a menudo llegamos a la misa sin haber dejado el ruido del mundo. Este sitio es una pausa, un camino de luz (**LumenVia**) para prepararte a recibir la Palabra de Dios.

**¿Por qué usar LumenVia?**

- **Comprender lo esencial**: con la ayuda de la Inteligencia Artificial, ponemos en perspectiva las lecturas del domingo y te ofrecemos una síntesis. No se trata de inventar, sino de subrayar el hilo rojo que une los textos.
- **Prepararse en el camino**: ya prefieras leer o escuchar, LumenVia genera para ti un resumen escrito y un audio. Escucha la síntesis en el transporte o caminando hacia la iglesia para dejar que el espíritu de la fiesta te impregne.
- **Vivir el tiempo litúrgico**: del oro del Tiempo Ordinario al morado de la Cuaresma, la aplicación se viste con los colores de la Iglesia para ayudarte a habitar plenamente cada estación del año.

**¿Cómo recorrer este camino?**

- **La Luz del Domingo**: descubre los textos del día y su síntesis para alimentar tu meditación.
- **Mi Ayuda-Memoria**: crea tus propias notas de lo que ha tocado tu corazón.
- **Únete**: suscríbete para recibir cada viernes por la noche tu preparación dominical por correo electrónico o SMS.

Que esta herramienta te ayude a transformar cada misa en un encuentro más profundo y consciente con Cristo.
""".strip(),
    "IT": """
« *La tua Parola è lampada ai miei passi, luce sul mio cammino.* »


JOPAI LumenVia è un compagno spirituale pensato per aiutarti ad attraversare la soglia della celebrazione con un cuore aperto e un’intelligenza illuminata.
Troppo spesso arriviamo alla messa senza aver deposto il rumore del mondo. Questo sito è una pausa, un cammino di luce (**LumenVia**) per prepararti a ricevere la Parola di Dio.

**Perché usare LumenVia?**

- **Comprendere l’essenziale**: con l’aiuto dell’Intelligenza Artificiale, mettiamo in prospettiva le letture della domenica e ti offriamo una sintesi. Non si tratta di inventare, ma di sottolineare il filo rosso che collega i testi.
- **Prepararsi per strada**: che tu preferisca leggere o ascoltare, LumenVia genera per te un riassunto scritto e un audio. Ascolta la sintesi nei trasporti o camminando verso la chiesa perché lo spirito della festa possa permearti.
- **Vivere il tempo liturgico**: dall’oro del Tempo Ordinario al viola della Quaresima, l’applicazione si veste dei colori della Chiesa per aiutarti ad abitare pienamente ogni stagione dell’anno.

**Come percorrere questo cammino?**

- **La Luce della Domenica**: scopri i testi del giorno e la loro sintesi per nutrire la tua meditazione.
- **Il mio Promemoria**: crea i tuoi appunti su ciò che ha toccato il tuo cuore.
- **Unisciti a noi**: iscriviti per ricevere ogni venerdì sera la tua preparazione domenicale via e-mail o SMS.

Possa questo strumento aiutarti a trasformare ogni messa in un incontro più profondo e consapevole con Cristo.
""".strip(),
}


def pdf_ui(pref_langue: object | None) -> dict[str, str]:
    lg = coerce_aip_langue(pref_langue)
    return dict(_PDF_UI.get(lg) or _PDF_UI[DEFAULT_PREF_LANGUE])


def about_markdown_for_lang(pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    return _ABOUT_MD.get(lg) or _ABOUT_MD[DEFAULT_PREF_LANGUE]


def pdf_cover_date_line(date_str: str, pref_langue: object | None = None) -> str:
    """Ligne date couverture : « Sonntag, 16. August 2026 », « Dimanche 16 août 2026 », …"""
    lg = coerce_aip_langue(pref_langue)
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
    months = _MONTHS.get(lg) or _MONTHS[DEFAULT_PREF_LANGUE]
    days = _WEEKDAYS.get(lg) or _WEEKDAYS[DEFAULT_PREF_LANGUE]
    wd = days[d.weekday()]
    month = months[d.month - 1]
    if lg == "EN":
        return f"{wd}, {month} {d.day}, {d.year}"
    if lg == "DE":
        return f"{wd}, {d.day}. {month} {d.year}"
    if lg in ("ES", "IT"):
        return f"{wd.capitalize()} {d.day} {month} {d.year}"
    return f"{wd.capitalize()} {d.day} {month} {d.year}"


def email_date_dimanche_label(date_str: str, pref_langue: object | None = None) -> str:
    """
    Valeur ``{{date_dimanche}}`` pour l’e-mail hebdo : **sans** jour de semaine.

    Les templates disent déjà « dimanche / Sonntag / Sunday … {{date_dimanche}} » ;
    inclure le weekday (comme la couverture PDF) produit « Sonntag, den Sonntag, … ».
    """
    lg = coerce_aip_langue(pref_langue)
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
    months = _MONTHS.get(lg) or _MONTHS[DEFAULT_PREF_LANGUE]
    month = months[d.month - 1]
    if lg == "EN":
        return f"{month} {d.day}, {d.year}"
    if lg == "DE":
        return f"{d.day}. {month} {d.year}"
    return f"{d.day} {month} {d.year}"


def _short_season(periode: object | None, pref_langue: object | None) -> str:
    from core.liturgy_theme import norm_key

    lg = coerce_aip_langue(pref_langue)
    table = _SEASON_SHORT.get(lg) or _SEASON_SHORT[DEFAULT_PREF_LANGUE]
    k = norm_key(periode)
    if k in table:
        return table[k]
    if "pentecot" in k:
        return table.get("pentecote") or "—"
    # Repli : libellé AELF / Evangelizo tel quel (souvent déjà localisé)
    raw = liturgy_display_label(str(periode) if periode else None)
    return raw if raw and raw != "—" else "—"


def _short_color(couleur: object | None, pref_langue: object | None) -> str:
    from core.liturgy_theme import norm_key

    lg = coerce_aip_langue(pref_langue)
    table = _COLOR_SHORT.get(lg) or _COLOR_SHORT[DEFAULT_PREF_LANGUE]
    k = norm_key(couleur)
    if k in table:
        return table[k]
    raw = liturgy_display_label(str(couleur) if couleur else None)
    return raw if raw and raw != "—" else "—"


def pdf_cover_meta_line(
    *,
    periode: object | None,
    annee: object | None,
    couleur: object | None,
    pref_langue: object | None = None,
) -> str:
    """Ligne méta couverture : saison · cycle · couleur (libellés localisés)."""
    lg = coerce_aip_langue(pref_langue)
    ui = sunday_ui(lg)
    cycle_lbl = ui.get("cycle") or "Cycle"
    return (
        f"{_short_season(periode, lg)} · "
        f"{cycle_lbl} {cycle_year_display(annee)} · "
        f"{_short_color(couleur, lg)}"
    )
