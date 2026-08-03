"""Localisation des prompts synthèse / titres PDF-TTS (pivot FR).

Les consignes métier restent en Sheets ``Paramètres_IA`` (append-only, Actif/Version).
La colonne ``Langue`` (FR/DE/EN/ES/IT) scope la sélection ; absence de colonne ou
cellule vide = FR (rétrocompat).
"""

from __future__ import annotations

from core.locale_codes import DEFAULT_PREF_LANGUE, normalize_pref_langue

AIP_PROMPT_LANGS: tuple[str, ...] = ("FR", "DE", "EN", "ES", "IT")

# Noms natifs pour consignes Vertex (« rédige en … »)
_OUTPUT_LANG_NATIVE: dict[str, str] = {
    "FR": "français",
    "DE": "allemand (Deutsch)",
    "EN": "English",
    "ES": "español",
    "IT": "italiano",
}

# Titres sections PDF / assemblage prompt
PDF_READING_TITLES: dict[str, dict[str, str]] = {
    "FR": {
        "premiere_lecture": "Première lecture",
        "psaume": "Psaume",
        "deuxieme_lecture": "Deuxième lecture",
        "evangile": "Évangile",
        "synthese": "Synthèse (LumenVia)",
        "synthese_missing": (
            "Synthèse non encore générée pour cette date — utilise « Générer la synthèse et l’audio » "
            "dans l’application."
        ),
    },
    "DE": {
        "premiere_lecture": "Erste Lesung",
        "psaume": "Antwortpsalm",
        "deuxieme_lecture": "Zweite Lesung",
        "evangile": "Evangelium",
        "synthese": "Synthese (LumenVia)",
        "synthese_missing": (
            "Für dieses Datum liegt noch keine Synthese vor — bitte in der Anwendung "
            "« Synthese und Audio erzeugen » verwenden."
        ),
    },
    "EN": {
        "premiere_lecture": "First reading",
        "psaume": "Responsorial psalm",
        "deuxieme_lecture": "Second reading",
        "evangile": "Gospel",
        "synthese": "Synthesis (LumenVia)",
        "synthese_missing": (
            "No synthesis has been generated for this date yet — use “Generate synthesis and audio” "
            "in the app."
        ),
    },
    "ES": {
        "premiere_lecture": "Primera lectura",
        "psaume": "Salmo responsorial",
        "deuxieme_lecture": "Segunda lectura",
        "evangile": "Evangelio",
        "synthese": "Síntesis (LumenVia)",
        "synthese_missing": (
            "Aún no hay síntesis para esta fecha — usa « Generar la síntesis y el audio » "
            "en la aplicación."
        ),
    },
    "IT": {
        "premiere_lecture": "Prima lettura",
        "psaume": "Salmo responsoriale",
        "deuxieme_lecture": "Seconda lettura",
        "evangile": "Vangelo",
        "synthese": "Sintesi (LumenVia)",
        "synthese_missing": (
            "Sintesi non ancora generata per questa data — usa « Genera la sintesi e l’audio » "
            "nell’applicazione."
        ),
    },
}

CATECHESE_TITLE_BY_LANG: dict[str, str] = {
    "FR": "Passerelle catéchèse",
    "DE": "Katechese-Brücke",
    "EN": "Catechesis bridge",
    "ES": "Puente de catequesis",
    "IT": "Ponte di catechesi",
}

# Sous-titres passerelle (exact match attendu dans la synthèse)
CATECHESE_SUBTITLES_BY_LANG: dict[str, tuple[str, str, str, str, str]] = {
    "FR": (
        "L’Essentiel",
        "La Scène Visuelle",
        "Le Mot-Clé",
        "L’Analogie du Quotidien",
        "Le Pas de la Semaine",
    ),
    "DE": (
        "Das Wesentliche",
        "Die visuelle Szene",
        "Das Schlüsselwort",
        "Die Alltagsanalogie",
        "Der Schritt der Woche",
    ),
    "EN": (
        "The Essence",
        "The Visual Scene",
        "The Key Word",
        "The Everyday Analogy",
        "The Step for the Week",
    ),
    "ES": (
        "Lo esencial",
        "La escena visual",
        "La palabra clave",
        "La analogía cotidiana",
        "El paso de la semana",
    ),
    "IT": (
        "L’essenziale",
        "La scena visiva",
        "La parola chiave",
        "L’analogia quotidiana",
        "Il passo della settimana",
    ),
}

PSALM_SECTION_TITLE: dict[str, str] = {
    "FR": "Le Psaume",
    "DE": "Der Psalm",
    "EN": "The Psalm",
    "ES": "El Salmo",
    "IT": "Il Salmo",
}

TAKEAWAYS_SECTION_TITLE: dict[str, str] = {
    "FR": "À retenir",
    "DE": "Zum Mitnehmen",
    "EN": "Takeaways",
    "ES": "Para recordar",
    "IT": "Da ricordare",
}


def coerce_aip_langue(raw: object | None) -> str:
    """Normalise une langue AIP (FR par défaut ; cellule vide = FR)."""
    s = str(raw or "").strip().upper()
    if not s or s in ("F", "FRA", "FRANCAIS", "FRANÇAIS"):
        return DEFAULT_PREF_LANGUE
    return normalize_pref_langue(s)


def output_language_label(pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    return _OUTPUT_LANG_NATIVE.get(lg, _OUTPUT_LANG_NATIVE[DEFAULT_PREF_LANGUE])


def pdf_titles(pref_langue: object | None) -> dict[str, str]:
    lg = coerce_aip_langue(pref_langue)
    return dict(PDF_READING_TITLES.get(lg) or PDF_READING_TITLES[DEFAULT_PREF_LANGUE])


def catechese_title(pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    return CATECHESE_TITLE_BY_LANG.get(lg) or CATECHESE_TITLE_BY_LANG[DEFAULT_PREF_LANGUE]


def all_catechese_titles() -> tuple[str, ...]:
    """Tous les titres connus (détection multi-langues dans PDF / strip)."""
    out: list[str] = [
        "Passerelle catéchèse",
        "Passerelle catéchèse — L\u2019écho des paraboles",
        "Passerelle catéchèse — L'écho des paraboles",
    ]
    for t in CATECHESE_TITLE_BY_LANG.values():
        if t not in out:
            out.append(t)
    return tuple(out)


def default_overlay_takeaways(pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    psalm = PSALM_SECTION_TITLE[lg]
    take = TAKEAWAYS_SECTION_TITLE[lg]
    if lg == "FR":
        return (
            f"\nInclure une sous-section titrée exactement « {psalm} » : uniquement à partir du texte du psaume fourni, "
            "explique comment ce psaume permet de répondre en prière aux lectures (sans sources externes).\n"
            "Structurer aussi la synthèse pour mettre en relief la promesse / préfiguration (Première lecture, AT si applicable) "
            "et son accomplissement ou réponse dans l’Évangile, strictement à partir des textes fournis.\n"
            f"Terminer par une section « {take} » avec 3 à 5 puces commençant par un verbe.\n"
        )
    if lg == "DE":
        return (
            f"\nFüge einen Unterabschnitt mit dem genauen Titel « {psalm} » ein: allein aus dem gelieferten Psalmtext "
            "erkläre, wie dieser Psalm betend auf die Lesungen antwortet (keine externen Quellen).\n"
            "Strukturiere die Synthese so, dass Verheißung / Vorausbild (Erste Lesung, AT falls zutreffend) "
            "und Erfüllung / Antwort im Evangelium klar hervortreten — streng aus den gelieferten Texten.\n"
            f"Schließe mit einem Abschnitt « {take} » mit 3 bis 5 Aufzählungspunkten, die mit einem Verb beginnen.\n"
        )
    if lg == "EN":
        return (
            f"\nInclude a subsection titled exactly “{psalm}”: using only the psalm text provided, "
            "explain how this psalm answers the readings in prayer (no external sources).\n"
            "Also structure the synthesis to highlight promise / foreshadowing (First reading, OT if applicable) "
            "and its fulfilment or response in the Gospel, strictly from the texts provided.\n"
            f"End with a “{take}” section of 3 to 5 bullet points, each starting with a verb.\n"
        )
    if lg == "ES":
        return (
            f"\nIncluye una subsección titulada exactamente « {psalm} »: solo a partir del texto del salmo facilitado, "
            "explica cómo este salmo responde en oración a las lecturas (sin fuentes externas).\n"
            "Estructura también la síntesis para resaltar la promesa / prefiguración (Primera lectura, AT si aplica) "
            "y su cumplimiento o respuesta en el Evangelio, estrictamente a partir de los textos dados.\n"
            f"Termina con una sección « {take} » con 3 a 5 viñetas que empiecen por un verbo.\n"
        )
    # IT
    return (
        f"\nIncludi una sottosezione intitolata esattamente « {psalm} »: solo dal testo del salmo fornito, "
        "spiega come questo salmo risponde in preghiera alle letture (niente fonti esterne).\n"
        "Struttura anche la sintesi per evidenziare promessa / prefigurazione (Prima lettura, AT se applicabile) "
        "e il suo compimento o risposta nel Vangelo, rigorosamente dai testi forniti.\n"
        f"Concludi con una sezione « {take} » con 3–5 elenchi puntati che iniziano con un verbo.\n"
    )


def default_overlay_no_takeaways(pref_langue: object | None) -> str:
    lg = coerce_aip_langue(pref_langue)
    if lg == "FR":
        return (
            "\nMettre en relief la promesse / préfiguration (Première lecture) et l’accomplissement (Évangile), "
            "strictement à partir des textes fournis.\n"
        )
    if lg == "DE":
        return (
            "\nHebe Verheißung / Vorausbild (Erste Lesung) und Erfüllung (Evangelium) hervor, "
            "streng aus den gelieferten Texten.\n"
        )
    if lg == "EN":
        return (
            "\nHighlight promise / foreshadowing (First reading) and fulfilment (Gospel), "
            "strictly from the texts provided.\n"
        )
    if lg == "ES":
        return (
            "\nResalta la promesa / prefiguración (Primera lectura) y el cumplimiento (Evangelio), "
            "estrictamente a partir de los textos facilitados.\n"
        )
    return (
        "\nMetti in rilievo promessa / prefigurazione (Prima lettura) e compimento (Vangelo), "
        "rigorosamente dai testi forniti.\n"
    )


def default_overlay_catechese_bridge(pref_langue: object | None, *, bridge_words: int) -> str:
    lg = coerce_aip_langue(pref_langue)
    title = catechese_title(lg)
    s1, s2, s3, s4, s5 = CATECHESE_SUBTITLES_BY_LANG[lg]
    if lg == "FR":
        return (
            f"\nAjouter à la fin une section titrée exactement : « {title} ».\n"
            "Cette passerelle catéchèse doit être structurée en 5 sous-parties (titres exacts) :\n"
            "Important : ne mets pas de numérotation (pas de « 1) », « 2) », etc.).\n"
            "Important : n'utilise aucun emoji, aucune puce décorative, aucun symbole (ni carrés, ni ronds), et aucun caractère isolé en préfixe.\n"
            f"Chaque sous-partie doit commencer par le TITRE SEUL sur une ligne (ex: « {s2} »), puis le texte sur les lignes suivantes.\n"
            f"« {s1} » : une seule phrase percutante (le cœur du message), fidèle aux textes.\n"
            f"« {s2} » : décrire la scène comme un tableau vivant (sensoriel) sans inventer de paroles.\n"
            f"« {s3} » : choisir 1 concept (ex. Grâce, Alliance…) et le définir simplement.\n"
            f"« {s4} » : une analogie moderne, digne, non trivialisante, qui éclaire le texte sans le remplacer.\n"
            f"« {s5} » : un défi concret à vivre (école, famille, paroisse).\n"
            "Garde-fous :\n"
            "- Prudence interprétative : ne pas inventer de paroles du Christ ni changer le sens de l’Écriture.\n"
            "- Ton d’accompagnement respectueux ; pas de langage culpabilisant.\n"
            "- Si un point théologique est complexe/controversé, inviter à en parler avec un animateur/catéchiste.\n"
        )
    if lg == "DE":
        return (
            f"\nFüge am Ende einen Abschnitt mit dem genauen Titel « {title} » hinzu.\n"
            "Diese Katechese-Brücke muss in 5 Unterteilen (genaue Titel) gegliedert sein:\n"
            "Wichtig: keine Nummerierung (kein « 1) », « 2) » usw.).\n"
            "Wichtig: keine Emojis, keine dekorativen Aufzählungszeichen, keine isolierten Präfix-Zeichen.\n"
            f"Jeder Unterteil beginnt mit dem TITEL ALLEIN in einer Zeile (z. B. « {s2} »), danach der Text.\n"
            f"« {s1} » : ein einziger treffender Satz (Kern der Botschaft), texttreu.\n"
            f"« {s2} » : die Szene wie ein lebendiges Bild beschreiben (sinnlich), ohne erfundene Worte.\n"
            f"« {s3} » : ein Konzept wählen (z. B. Gnade, Bund…) und einfach erklären.\n"
            f"« {s4} » : eine moderne, würdevolle Analogie, die den Text erhellt, ohne ihn zu ersetzen.\n"
            f"« {s5} » : eine konkrete Herausforderung (Schule, Familie, Gemeinde).\n"
            "Leitplanken:\n"
            "- Keine erfundenen Christusworte; den Schriftsinn nicht verdrehen.\n"
            "- Respektvoller Begleitton; keine Schuld-Rhetorik.\n"
            "- Bei komplexen theologischen Punkten: zum Gespräch mit Katechet/in einladen.\n"
        )
    if lg == "EN":
        return (
            f"\nAdd at the end a section titled exactly: “{title}”.\n"
            "This catechesis bridge must be structured in 5 subsections (exact titles):\n"
            "Important: no numbering (no “1)”, “2)”, etc.).\n"
            "Important: no emojis, decorative bullets, or isolated prefix characters.\n"
            f"Each subsection must start with the TITLE ALONE on one line (e.g. “{s2}”), then the text.\n"
            f"“{s1}”: one striking sentence (heart of the message), faithful to the texts.\n"
            f"“{s2}”: describe the scene as a living tableau (sensory) without inventing speech.\n"
            f"“{s3}”: choose 1 concept (e.g. Grace, Covenant…) and define it simply.\n"
            f"“{s4}”: a modern, dignified analogy that illuminates the text without replacing it.\n"
            f"“{s5}”: a concrete challenge to live (school, family, parish).\n"
            "Guardrails:\n"
            "- Do not invent words of Christ or distort the sense of Scripture.\n"
            "- Respectful accompanying tone; no guilt-tripping.\n"
            "- If a theological point is complex/controversial, invite talking with a catechist.\n"
        )
    if lg == "ES":
        return (
            f"\nAñade al final una sección titulada exactamente: « {title} ».\n"
            "Este puente de catequesis debe estructurarse en 5 subpartes (títulos exactos):\n"
            "Importante: sin numeración (nada de « 1) », « 2) », etc.).\n"
            "Importante: sin emojis, viñetas decorativas ni caracteres prefijo aislados.\n"
            f"Cada subparte empieza por el TÍTULO SOLO en una línea (ej. « {s2} »), luego el texto.\n"
            f"« {s1} »: una sola frase contundente (el corazón del mensaje), fiel a los textos.\n"
            f"« {s2} »: describir la escena como un cuadro vivo (sensorial) sin inventar palabras.\n"
            f"« {s3} »: elegir 1 concepto (p. ej. Gracia, Alianza…) y definirlo con sencillez.\n"
            f"« {s4} »: una analogía moderna, digna, que ilumine el texto sin sustituirlo.\n"
            f"« {s5} »: un reto concreto (escuela, familia, parroquia).\n"
            "Límites:\n"
            "- No inventar palabras de Cristo ni alterar el sentido de la Escritura.\n"
            "- Tono de acompañamiento respetuoso; sin culpabilizar.\n"
            "- Si un punto teológico es complejo/controvertido, invitar a hablar con un catequista.\n"
        )
    return (
        f"\nAggiungi alla fine una sezione intitolata esattamente: « {title} ».\n"
        "Questo ponte di catechesi deve essere strutturato in 5 sottosezioni (titoli esatti):\n"
        "Importante: niente numerazione (niente « 1) », « 2) », ecc.).\n"
        "Importante: niente emoji, elenchi decorativi o caratteri prefisso isolati.\n"
        f"Ogni sottosezione inizia con il TITOLO SOLO su una riga (es. « {s2} »), poi il testo.\n"
        f"« {s1} »: una sola frase incisiva (cuore del messaggio), fedele ai testi.\n"
        f"« {s2} »: descrivere la scena come un quadro vivo (sensoriale) senza inventare parole.\n"
        f"« {s3} »: scegliere 1 concetto (es. Grazia, Alleanza…) e definirlo in modo semplice.\n"
        f"« {s4} »: un’analogia moderna, dignitosa, che illumina il testo senza sostituirlo.\n"
        f"« {s5} »: una sfida concreta (scuola, famiglia, parrocchia).\n"
        "Limiti:\n"
        "- Non inventare parole di Cristo né snaturare il senso della Scrittura.\n"
        "- Tono di accompagnamento rispettoso; niente colpevolizzazione.\n"
        "- Se un punto teologico è complesso/controverso, invitare a parlarne con un catechista.\n"
    )


def default_overlays_for_lang(pref_langue: object | None, *, bridge_words: int = 275) -> dict[str, str]:
    """Surcouches texte localisées (fallback si Sheets n’a pas encore la Langue)."""
    lg = coerce_aip_langue(pref_langue)
    return {
        "overlay_takeaways": default_overlay_takeaways(lg),
        "overlay_no_takeaways": default_overlay_no_takeaways(lg),
        "overlay_catechese_bridge": default_overlay_catechese_bridge(lg, bridge_words=bridge_words),
    }


def language_override_block(pref_langue: object | None) -> str:
    """Bloc prioritaire si le socle Sheets est encore en FR mais la génération est hors FR."""
    lg = coerce_aip_langue(pref_langue)
    if lg == DEFAULT_PREF_LANGUE:
        return ""
    native = output_language_label(lg)
    return (
        f"\n\n## OVERRIDE LANGUE DE SORTIE (prioritaire)\n"
        f"Rédige **toute** la synthèse (titres de sections inclus) en **{native}** (`{lg}`).\n"
        "Ignore toute consigne antérieure qui demanderait le français comme langue de rédaction.\n"
        "Les lectures sources sont déjà dans cette langue : ne les traduis pas ; commente-les dans la même langue.\n"
        "Zéro invention de faits hors textes fournis.\n"
    )
