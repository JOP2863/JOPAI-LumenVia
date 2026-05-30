"""Clés attendues dans l'onglet GSheet ``Paramètres_IA``."""

from __future__ import annotations

PROMPT_TEMPLATE_KEYS = frozenset(
    {
        "instructions_base_md",
        "overlay_takeaways",
        "overlay_no_takeaways",
        "overlay_catechese_bridge",
        "retry_hardened_prefix",
        "audio_style_default",
        "audio_style_paques",
        "audio_style_careme",
        "audio_style_lectures",
        "tts_pronunciation",
    }
)
