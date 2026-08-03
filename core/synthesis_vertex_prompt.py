"""Assemblage du prompt Vertex pour la synthèse dominicale (multi-langues)."""

from __future__ import annotations

from core.prompt_locale import (
    catechese_title,
    coerce_aip_langue,
    default_overlay_catechese_bridge,
    default_overlay_no_takeaways,
    default_overlay_takeaways,
    language_override_block,
    output_language_label,
    TAKEAWAYS_SECTION_TITLE,
)
from core.locale_codes import DEFAULT_PREF_LANGUE

# Budget fixe pour la section passerelle catéchèse (indépendant du % synthèse).
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
    pref_langue: object | None = None,
) -> str:
    lg = coerce_aip_langue(pref_langue)
    native = output_language_label(lg)
    takeaways = "true" if include_takeaways else "false"
    ctx = (liturgical_context or "").strip()
    ctx_block = ""
    if ctx:
        ctx_block = (
            "\nRepères liturgiques (résumé pédagogique, à intégrer sans invention hors textes sources):\n"
            f"{ctx}\n"
        )
    tpls = dict(templates or {})
    take_title = TAKEAWAYS_SECTION_TITLE.get(lg) or TAKEAWAYS_SECTION_TITLE[DEFAULT_PREF_LANGUE]

    psalm_block = (
        (tpls.get("overlay_takeaways") or default_overlay_takeaways(lg))
        if include_takeaways
        else (tpls.get("overlay_no_takeaways") or default_overlay_no_takeaways(lg))
    )

    catechese_block = ""
    bridge_words = 0
    if include_catechese_bridge:
        bridge_words = int(catechese_bridge_words or CATECHESE_BRIDGE_TARGET_WORDS)
        catechese_block = tpls.get("overlay_catechese_bridge") or default_overlay_catechese_bridge(
            lg, bridge_words=bridge_words
        )

    takeaways_note = f", section « {take_title} » incluse" if include_takeaways else ""
    length_synth = (
        f"Contrainte de longueur — synthèse générale (mise en situation, développement{takeaways_note}, "
        f"hors passerelle catéchèse) : vise environ {length_words} mots (+/- 10%)."
    )
    length_parts = [length_synth]
    if include_catechese_bridge:
        ctitle = catechese_title(lg)
        length_parts.append(
            f"Contrainte de longueur — passerelle catéchèse seule (« {ctitle} ») : "
            f"vise environ {bridge_words} mots (+/- 10%), indépendamment du pourcentage de synthèse ; "
            f"ne rogne pas cette section pour respecter la synthèse générale."
        )
    length_block = "\n".join(length_parts)
    lang_override = language_override_block(lg)
    source_label = "AELF" if lg == DEFAULT_PREF_LANGUE else "lectionnaire (source locale)"

    return f"""
{instructions}{lang_override}

Paramètres:
- output_language: {lg} ({native})
- length_words_synthesis: {length_words}
- length_words_catechese_bridge: {bridge_words if include_catechese_bridge else 0}
- include_takeaways: {takeaways}
- include_catechese_bridge: {"true" if include_catechese_bridge else "false"}
- style: simple
- addressing: vous / Sie / you / usted / lei (selon la langue)
{ctx_block}
Identité du jour ({source_label}):
{identity}

Textes ({source_label}, source unique — ne pas traduire les lectures):
{readings}

Tâche:
Commence par un court paragraphe de mise en situation : comment la couleur liturgique, le temps liturgique et le cycle annoncés ci-dessus cadrent la lecture du jour (sans ajouter de faits non présents dans les textes).
Ensuite, rédige la synthèse **entièrement en {native}** en respectant STRICTEMENT les contraintes (zéro invention).
{psalm_block}
{catechese_block}
{length_block}
""".strip()
