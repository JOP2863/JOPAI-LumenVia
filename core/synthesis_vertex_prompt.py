"""Assemblage du prompt Vertex pour la synthèse dominicale."""

from __future__ import annotations


def build_sunday_vertex_synthesis_prompt(
    *,
    instructions: str,
    length_words: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    templates: dict[str, str] | None = None,
    identity: dict,
    readings: dict,
    liturgical_context: str | None = None,
) -> str:
    takeaways = "true" if include_takeaways else "false"
    ctx = (liturgical_context or "").strip()
    ctx_block = ""
    if ctx:
        ctx_block = f"\nRepères liturgiques (résumé pédagogique, à intégrer sans invention hors textes AELF):\n{ctx}\n"
    tpls = dict(templates or {})
    default_takeaways = (
        "\nInclure une sous-section titrée exactement « Le Psaume » : uniquement à partir du texte du psaume fourni, "
        "explique comment ce psaume permet de répondre en prière aux lectures (sans sources externes).\n"
        "Structurer aussi la synthèse pour mettre en relief la promesse / préfiguration (Première lecture, AT si applicable) "
        "et son accomplissement ou réponse dans l’Évangile, strictement à partir des textes fournis.\n"
        "Terminer par une section « À retenir » avec 3 à 5 puces commençant par un verbe.\n"
    )
    default_no_takeaways = (
        "\nMettre en relief la promesse / préfiguration (Première lecture) et l’accomplissement (Évangile), strictement à partir des textes fournis.\n"
    )
    psalm_block = (tpls.get("overlay_takeaways") or default_takeaways) if include_takeaways else (
        tpls.get("overlay_no_takeaways") or default_no_takeaways
    )

    catechese_block = ""
    if include_catechese_bridge:
        catechese_block = tpls.get("overlay_catechese_bridge") or (
            "\nAjouter à la fin une section titrée exactement : « Passerelle catéchèse — L’écho des paraboles ».\n"
            "Cette section doit être une “Stone Card” structurée en 5 sous-parties (titres exacts) :\n"
            "Important : ne mets pas de numérotation (pas de « 1) », « 2) », etc.).\n"
            "Important : n'utilise aucun emoji, aucune puce décorative, aucun symbole (ni carrés, ni ronds), et aucun caractère isolé en préfixe.\n"
            "Chaque sous-partie doit commencer par le TITRE SEUL sur une ligne (ex: « La Scène Visuelle »), puis le texte sur les lignes suivantes.\n"
            "« L’Essentiel » : une seule phrase percutante (le cœur du message), fidèle aux textes.\n"
            "« La Scène Visuelle » : décrire la scène comme un tableau vivant (sensoriel) sans inventer de paroles.\n"
            "« Le Mot-Clé » : choisir 1 concept (ex. Grâce, Alliance…) et le définir simplement.\n"
            "« L’Analogie du Quotidien » : une analogie moderne, digne, non trivialisante, qui éclaire le texte sans le remplacer.\n"
            "« Le Pas de la Semaine » : un défi concret à vivre (école, famille, paroisse).\n"
            "Garde-fous :\n"
            "- Prudence interprétative : ne pas inventer de paroles du Christ ni changer le sens de l’Écriture.\n"
            "- Ton d’accompagnement respectueux ; pas de langage culpabilisant.\n"
            "- Si un point théologique est complexe/controversé, inviter à en parler avec un animateur/catéchiste.\n"
        )

    return f"""
{instructions}

Paramètres:
- length_words: {length_words}
- include_takeaways: {takeaways}
- include_catechese_bridge: {"true" if include_catechese_bridge else "false"}
- style: simple
- addressing: vous
{ctx_block}
Identité du jour (AELF):
{identity}

Textes (AELF, source unique):
{readings}

Tâche:
Commence par un court paragraphe de mise en situation : comment la couleur liturgique, le temps liturgique et le cycle annoncés ci-dessus cadrent la lecture du jour (sans ajouter de faits non présents dans les textes).
Ensuite, rédige la synthèse en français en respectant STRICTEMENT les contraintes (zéro invention).
{psalm_block}
{catechese_block}
Contrainte de longueur: vise {length_words} mots (+/- 10%). Ne termine pas avant d'avoir atteint la longueur cible.
""".strip()
