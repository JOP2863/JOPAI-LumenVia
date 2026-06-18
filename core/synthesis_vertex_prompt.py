"""Assemblage du prompt Vertex pour la synthèse dominicale."""

from __future__ import annotations

from core.catechese_section_strip import CATECHESE_SECTION_TITLE

# Budget fixe pour la section « Passerelle catéchèse » (indépendant du % synthèse).
CATECHESE_BRIDGE_TARGET_WORDS = 275


def build_sunday_vertex_synthesis_prompt(
    *,
    instructions: str,
    length_words: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    catechese_bridge_words: int | None = None,
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
    bridge_words = 0
    if include_catechese_bridge:
        bridge_words = int(catechese_bridge_words or CATECHESE_BRIDGE_TARGET_WORDS)
        catechese_block = tpls.get("overlay_catechese_bridge") or (
            f"\nAjouter à la fin une section titrée exactement : « {CATECHESE_SECTION_TITLE} ».\n"
            "Cette passerelle catéchèse doit être structurée en 5 sous-parties (titres exacts) :\n"
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

    takeaways_note = ', section « À retenir » incluse' if include_takeaways else ""
    length_synth = (
        f"Contrainte de longueur — synthèse générale (mise en situation, développement{takeaways_note}, "
        f"hors passerelle catéchèse) : vise environ {length_words} mots (+/- 10%)."
    )
    length_parts = [length_synth]
    if include_catechese_bridge:
        length_parts.append(
            f"Contrainte de longueur — passerelle catéchèse seule (« {CATECHESE_SECTION_TITLE} ») : "
            f"vise environ {bridge_words} mots (+/- 10%), indépendamment du pourcentage de synthèse ; "
            f"ne rogne pas cette section pour respecter la synthèse générale."
        )
    length_block = "\n".join(length_parts)

    return f"""
{instructions}

Paramètres:
- length_words_synthesis: {length_words}
- length_words_catechese_bridge: {bridge_words if include_catechese_bridge else 0}
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
{length_block}
""".strip()
