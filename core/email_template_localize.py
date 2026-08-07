"""Localisation des templates e-mail FR → DE/EN/ES/IT/PT.

Moteur : **Vertex** (traduction stricte, pas rédaction). MyMemory en filet uniquement.
Les balises ``{{...}}`` sont protégées puis restaurées.
"""

from __future__ import annotations

import re

from core.liturgy_day import supported_liturgy_langs
from core.locale_codes import DEFAULT_PREF_LANGUE
from core.prompt_locale import coerce_aip_langue, output_language_label

# Tout contenu entre {{ }} (y compris espaces / apostrophes FR).
_EMAIL_TAG_RE = re.compile(r"\{\{[^}]+\}\}")
_TAG_TOKEN = "⟦LVT{0}⟧"

# Indices FR qui ne doivent plus figurer massivement après traduction.
_FR_MARKERS = (
    "Bonjour",
    "Voici de quoi nourrir",
    "Beau chemin vers dimanche",
    "L’équipe LumenVia",
    "L'équipe LumenVia",
    "Vous recevez cet e-mail car vous êtes membre",
    "Préparez la célébration du dimanche",
    "La fin de semaine approche",
    "Donner mon avis sur cette expérience",
)


def email_localize_target_langs() -> tuple[str, ...]:
    """Langues à synchroniser depuis le pivot FR (hors FR)."""
    return tuple(lg for lg in supported_liturgy_langs() if lg != DEFAULT_PREF_LANGUE)


def _protect_tags(text: str) -> tuple[str, list[str]]:
    tags = _EMAIL_TAG_RE.findall(text or "")
    i = 0

    def _repl(_m: re.Match) -> str:
        nonlocal i
        tok = _TAG_TOKEN.format(i)
        i += 1
        return tok

    protected = _EMAIL_TAG_RE.sub(_repl, text or "")
    return protected, tags


def _restore_tags(text: str, tags: list[str]) -> str:
    out = text or ""
    for i, tag in enumerate(tags):
        tok = _TAG_TOKEN.format(i)
        # Variantes éventuelles si le modèle altère légèrement le token.
        for cand in (tok, tok.replace("⟦", "[").replace("⟧", "]"), f"[[LVT{i}]]", f"__LVT{i}__"):
            if cand in out:
                out = out.replace(cand, tag)
                break
    return out


def _looks_still_french(src: str, out: str, *, target_lang: str) -> bool:
    """True si la sortie ressemble encore au FR (échec silencieux de traduction)."""
    a = (src or "").strip()
    b = (out or "").strip()
    if not b:
        return True
    if b.casefold() == a.casefold():
        return True
    lg = coerce_aip_langue(target_lang)
    if lg == DEFAULT_PREF_LANGUE:
        return False
    hits = sum(1 for m in _FR_MARKERS if m in b)
    # Objet court : 1 marqueur suffit ; corps long : 2+.
    need = 1 if len(a) < 180 else 2
    return hits >= need


def _translate_with_vertex(
    text: str,
    *,
    target_lang: str,
    vertex_client: object,
    field_kind: str,
) -> str:
    lg = coerce_aip_langue(target_lang)
    native = output_language_label(lg)
    protected, tags = _protect_tags(text)
    if not protected.strip():
        return text

    max_tok = 1024 if field_kind == "subject" else 8192
    prompt = (
        f"You are a professional translator for pastoral / newsletter emails.\n"
        f"Translate the following French email {field_kind} into {native} ({lg}).\n"
        "Rules (strict):\n"
        "- Translate meaning and warm pastoral tone; do NOT rewrite or invent content.\n"
        "- Keep every placeholder token exactly as-is (forms like ⟦LVT0⟧, ⟦LVT1⟧, …).\n"
        "- Keep emoji, line breaks, and the overall structure.\n"
        "- Do not add titles, markdown fences, or commentary.\n"
        "- Output ONLY the translation.\n\n"
        f"---\n{protected}\n---"
    )
    if hasattr(vertex_client, "generate_text_auto"):
        res = vertex_client.generate_text_auto(
            preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
            prompt=prompt,
            max_output_tokens=max_tok,
            thinking_budget=0,
        )
    else:
        res = vertex_client.generate_text(
            model="gemini-2.0-flash", prompt=prompt, max_output_tokens=max_tok
        )
    raw = str(getattr(res, "text", None) or res or "").strip()
    # Retire éventuels fences ```...```
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    out = _restore_tags(raw, tags)
    if _looks_still_french(text, out, target_lang=lg):
        raise RuntimeError(
            f"Traduction Vertex {lg} ({field_kind}) encore en français — refus d’écrire la ligne."
        )
    return out


def _translate_with_mymemory(text: str, *, target_lang: str) -> str:
    """Filet MyMemory (souvent saturé) — segmenté, balises protégées."""
    from core.synthesis_localize import localize_plain_from_fr

    protected, tags = _protect_tags(text)
    mid = localize_plain_from_fr(protected, target_lang=target_lang, pause_s=0.2)
    out = _restore_tags(mid, tags)
    if _looks_still_french(text, out, target_lang=target_lang):
        raise RuntimeError(
            f"Traduction MyMemory {coerce_aip_langue(target_lang)} encore en français."
        )
    return out


def localize_email_field_from_fr(
    text: str,
    *,
    target_lang: object,
    vertex_client: object | None = None,
    field_kind: str = "body",
    pause_s: float = 0.2,
) -> str:
    """Traduit un champ template en préservant intégralement les ``{{balises}}``."""
    lg = coerce_aip_langue(target_lang)
    src = text or ""
    if not src.strip() or lg == DEFAULT_PREF_LANGUE:
        return src

    errors: list[str] = []
    if vertex_client is not None:
        try:
            return _translate_with_vertex(
                src, target_lang=lg, vertex_client=vertex_client, field_kind=field_kind
            )
        except Exception as ex:
            errors.append(f"vertex:{type(ex).__name__}:{ex}")

    try:
        _ = pause_s  # compat signature
        return _translate_with_mymemory(src, target_lang=lg)
    except Exception as ex:
        errors.append(f"mymemory:{type(ex).__name__}:{ex}")

    raise RuntimeError(
        "Localisation e-mail impossible (" + " | ".join(errors) + ")"
    )


def localize_email_template_from_fr(
    *,
    subject_fr: str,
    body_fr: str,
    status_note_fr: str = "",
    target_lang: object,
    vertex_client: object | None = None,
) -> tuple[str, str, str]:
    """Retourne ``(subject, body, status_note)`` localisés (échoue si toujours FR)."""
    lg = coerce_aip_langue(target_lang)
    if lg == DEFAULT_PREF_LANGUE:
        return subject_fr, body_fr, status_note_fr
    subj = localize_email_field_from_fr(
        subject_fr, target_lang=lg, vertex_client=vertex_client, field_kind="subject"
    )
    body = localize_email_field_from_fr(
        body_fr, target_lang=lg, vertex_client=vertex_client, field_kind="body"
    )
    note = ""
    if (status_note_fr or "").strip():
        note = localize_email_field_from_fr(
            status_note_fr,
            target_lang=lg,
            vertex_client=vertex_client,
            field_kind="status_note",
        )
    return subj, body, note


__all__ = [
    "email_localize_target_langs",
    "localize_email_field_from_fr",
    "localize_email_template_from_fr",
]
