"""Traduction pivot FR → autres langues pour prompts ``Paramètres_IA``."""

from __future__ import annotations

from core.prompt_locale import (
    AIP_PROMPT_LANGS,
    coerce_aip_langue,
    default_overlays_for_lang,
    output_language_label,
)
from core.locale_codes import DEFAULT_PREF_LANGUE

# Clés dont on a des traductions code (pas besoin de Vertex).
_OVERLAY_KEYS = frozenset(
    {"overlay_takeaways", "overlay_no_takeaways", "overlay_catechese_bridge"}
)


def translate_prompt_markdown_fr_to(
    body_fr: str,
    *,
    target_lang: object,
    key: str = "",
    vertex_client: object | None = None,
) -> str:
    """
    Produit le contenu Markdown pour ``target_lang``.

    - Surcouches connues : défauts localisés code (stables).
    - Autres clés : Vertex si client fourni, sinon préfixe OVERRIDE + corps FR.
    """
    lg = coerce_aip_langue(target_lang)
    if lg == DEFAULT_PREF_LANGUE:
        return (body_fr or "").strip()
    k = str(key or "").strip()
    if k in _OVERLAY_KEYS:
        defaults = default_overlays_for_lang(lg)
        return str(defaults.get(k) or body_fr or "").strip()

    src = (body_fr or "").strip()
    if not src:
        return ""
    native = output_language_label(lg)
    if vertex_client is not None:
        prompt = (
            f"Translate the following liturgical AI prompt instructions from French to {native} ({lg}).\n"
            "Keep Markdown structure, headings, bullet lists, and any technical identifiers/keys unchanged.\n"
            "Do not add commentary. Output only the translated Markdown.\n\n"
            f"---\n{src}\n---"
        )
        try:
            if hasattr(vertex_client, "generate_text_auto"):
                res = vertex_client.generate_text_auto(
                    preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                    prompt=prompt,
                    max_output_tokens=8192,
                )
            else:
                res = vertex_client.generate_text(
                    model="gemini-2.0-flash", prompt=prompt, max_output_tokens=8192
                )
            text = str(getattr(res, "text", None) or res or "").strip()
            if text:
                return text
        except Exception:
            pass
    return (
        f"# [{lg}] Translated from French pivot (review recommended)\n\n"
        f"Output language for generation: {native}.\n\n"
        f"{src}"
    )


def sibling_langs(*, pivot: object = DEFAULT_PREF_LANGUE) -> tuple[str, ...]:
    p = coerce_aip_langue(pivot)
    return tuple(lg for lg in AIP_PROMPT_LANGS if lg != p)
