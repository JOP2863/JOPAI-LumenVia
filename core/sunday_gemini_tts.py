"""TTS Gemini fragmenté (lectures longues, synthèse)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from core.audio_utils import join_wav_bytes, join_wav_with_silence, normalize_audio_bytes
from core.gemini_tts_api import GeminiTtsApiClient
from core.config import resolve_gemini_api_key
from core.aelf_reading_meta import split_readings_tts_body_meta
from core.catechese_section_strip import (
    CATECHESE_SECTION_TITLE,
    CATECHESE_TTS_INTRO,
)
from core.sunday_readings_tts import (
    coalesce_liturgy_reading_sections,
    dedupe_tts_section_body,
    default_first_reading_tts_title,
    is_liturgy_readings_tts_text,
    liturgy_section_oral_announcement,
    normalize_liturgy_section_title,
    parse_synthesis_tts_sections,
    pick_tts_french_accent,
    spoken_text_for_tts,
)
from core.prompt_locale import canonical_reading_section_key
from core.voix_audio import DEFAULT_GEMINI_TTS_VOICE

# Modèles TTS — l'API Gemini (clé) et Vertex (GCP) n'exposent pas les mêmes noms.
_GEMINI_API_TTS_MODELS = (
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
)
_VERTEX_TTS_MODELS = (
    "gemini-2.5-flash-tts",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)

_LAST_TTS_ROUTE_SESSION_KEY = "lumenvia_last_tts_route"

# Pause entre sections liturgiques / sous-sections de synthèse.
_LITURGY_SECTION_PAUSE_MS = 750
# Pause entre l'annonce d'une césure et le début de son corps.
_SECTION_INTRO_PAUSE_MS = 750
_CATECHESE_TTS_PAUSE_MS = 900
# Sous ce seuil, un morceau isolé pousse Gemini TTS à inventer du texte.
_MIN_TTS_CHUNK_CHARS = 100


def coalesce_short_tts_chunks(
    chunks: list[str],
    *,
    min_chars: int = _MIN_TTS_CHUNK_CHARS,
    max_chars: int = 1400,
) -> list[str]:
    """Fusionne les micro-morceaux pour éviter un appel TTS trop court (hallucinations)."""
    cleaned = [c.strip() for c in chunks if (c or "").strip()]
    if not cleaned:
        return []
    out: list[str] = [cleaned[0]]
    for ch in cleaned[1:]:
        prev = out[-1]
        if len(prev) < min_chars and len(prev) + 1 + len(ch) <= max_chars:
            out[-1] = f"{prev} {ch}".strip()
        elif len(ch) < min_chars and len(prev) + 1 + len(ch) <= max_chars:
            out[-1] = f"{prev} {ch}".strip()
        else:
            out.append(ch)
    if len(out) >= 2 and len(out[-1]) < min_chars and len(out[-2]) + 1 + len(out[-1]) <= max_chars:
        out[-2] = f"{out[-2]} {out[-1]}".strip()
        out.pop()
    return out


def _split_by_size(text: str, *, max_chars: int) -> list[str]:
    t = " ".join((text or "").split())
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    return [t[i : i + max_chars] for i in range(0, len(t), max_chars)]


def _split_by_size_at_word(text: str, *, max_chars: int) -> list[str]:
    t = " ".join((text or "").split())
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    out: list[str] = []
    rest = t
    while rest:
        if len(rest) <= max_chars:
            out.append(rest)
            break
        cut = rest[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        if not cut:
            cut = rest[:max_chars]
        out.append(cut.strip())
        rest = rest[len(cut) :].strip()
    return out


def _liturgy_section_tts_pieces(
    title: str,
    body: str,
    *,
    max_chars: int,
    intro_lue: str | None = None,
    ref: str | None = None,
) -> list[str]:
    """
    Morceaux TTS d'une section liturgique ou d'une sous-section de synthèse.

    L'annonce (intro AELF) est collée au début du premier morceau du corps : un appel TTS
    trop court sur la seule annonce provoque des hallucinations Gemini (texte inventé).
    """
    body = dedupe_tts_section_body(title, body, intro_lue=intro_lue)
    title_norm = normalize_liturgy_section_title(title) if title else ""
    if title_norm == "Psaume" or canonical_reading_section_key(title) == "psaume":
        body = re.sub(r"[ \t]+\n", "\n", (body or ""))
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
    else:
        body = " ".join((body or "").split())
    if not body:
        return []

    announcement = liturgy_section_oral_announcement(
        title or title_norm,
        intro_lue=intro_lue,
        ref=ref,
    )
    body_chunks = _split_by_size_at_word(body, max_chars=max_chars)
    if not body_chunks:
        return []
    # Annonce + premier segment du corps dans le même appel TTS.
    room = max(32, max_chars - len(announcement) - 2)
    first_body = body_chunks[0]
    if len(first_body) > room and room > 64:
        # Recoupe le premier segment si l'annonce + corps dépasse max_chars.
        split_first = _split_by_size_at_word(first_body, max_chars=room)
        first_body = split_first[0]
        body_chunks = split_first[1:] + body_chunks[1:]
    else:
        body_chunks = body_chunks[1:]
    first = f"{announcement} {first_body}".strip()
    return coalesce_short_tts_chunks([first, *body_chunks], max_chars=max_chars)


def _liturgy_readings_tts_section_chunk_specs(
    text: str, *, max_chars: int
) -> list[tuple[list[str], int | None]]:
    """Par section liturgique : morceaux TTS + pause éventuelle après l'annonce."""
    specs: list[tuple[list[str], int | None]] = []
    started = False
    for title, body in coalesce_liturgy_reading_sections(text):
        intro_lue: str | None = None
        ref: str | None = None
        if body:
            meta_intro, meta_ref, rest = split_readings_tts_body_meta(body)
            if meta_intro is not None or meta_ref is not None:
                intro_lue, ref, body = meta_intro, meta_ref, rest
        if not title:
            if not started:
                if (body or "").strip():
                    started = True
                    pieces = _liturgy_section_tts_pieces(
                        default_first_reading_tts_title(text),
                        body,
                        max_chars=max_chars,
                        intro_lue=intro_lue,
                        ref=ref,
                    )
                    if pieces:
                        # Pause entre sections uniquement (plus entre annonce isolée et corps).
                        specs.append((pieces, None))
                continue
            pieces = (
                coalesce_short_tts_chunks(
                    _split_by_size_at_word(body, max_chars=max_chars),
                    max_chars=max_chars,
                )
                if body
                else []
            )
            intro_pause = None
        else:
            if not (body or "").strip():
                continue
            started = True
            pieces = _liturgy_section_tts_pieces(
                title,
                body,
                max_chars=max_chars,
                intro_lue=intro_lue,
                ref=ref,
            )
            intro_pause = None
        if pieces:
            specs.append((pieces, intro_pause))
    return specs


def _liturgy_readings_tts_section_chunks(text: str, *, max_chars: int) -> list[list[str]]:
    """Compatibilité : liste plate de morceaux par section."""
    grouped: list[list[str]] = []
    for pieces, _pause in _liturgy_readings_tts_section_chunk_specs(text, max_chars=max_chars):
        grouped.append(pieces)
    return grouped


def _chunk_liturgy_readings_by_section(text: str, *, max_chars: int) -> list[str]:
    """
    Une (ou plusieurs) requêtes TTS par section AELF (``\\n\\n``).

    Évite de fusionner « Première lecture » + « Deuxième lecture » dans un seul appel
    Gemini : cela provoquait parfois un long blanc audio avant l'Évangile (morceau suivant).
    """
    flat: list[str] = []
    for section in _liturgy_readings_tts_section_chunks(text, max_chars=max_chars):
        flat.extend(section)
    return flat


def chunk_text_for_tts(text: str, *, max_chars: int = 1400) -> list[str]:
    """
    Découpe en morceaux pour éviter les limites TTS (et éviter l'audio tronqué).

    Lectures du lectionnaire : une section liturgique par morceau (puis découpe taille si besoin).
    Autres textes : fusion de paragraphes jusqu'à ``max_chars``.
    """
    t = (text or "").strip()
    if not t:
        return []
    if is_liturgy_readings_tts_text(t):
        return _chunk_liturgy_readings_by_section(t, max_chars=max_chars)

    flat = " ".join(t.split())
    if len(flat) <= max_chars:
        return [flat]

    paras = [p.strip() for p in t.split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        p = " ".join(p.split())
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= max_chars:
            cur = cur + " " + p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)

    final: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            final.extend(_split_by_size(c, max_chars=max_chars))
    return coalesce_short_tts_chunks(final, max_chars=max_chars)


def _synthesis_tts_section_chunk_specs(
    spoken: str, *, max_chars: int
) -> list[tuple[list[str], int | None]] | None:
    """
    Découpe synthèse en sections orales (Le Psaume, À retenir, passerelle catéchèse…).
    """
    sections = parse_synthesis_tts_sections(spoken)
    if not sections:
        return None

    specs: list[tuple[list[str], int | None]] = []
    for title, body in sections:
        if not title:
            chunks = chunk_text_for_tts(body, max_chars=max_chars)
            if chunks:
                specs.append((chunks, None))
            continue
        if title == CATECHESE_SECTION_TITLE:
            if body:
                body_chunks = chunk_text_for_tts(body, max_chars=max_chars)
                if body_chunks:
                    # Ne jamais envoyer l'annonce seule (hallucinations Gemini TTS).
                    first = f"{CATECHESE_TTS_INTRO} {body_chunks[0]}".strip()
                    pieces = coalesce_short_tts_chunks(
                        [first, *body_chunks[1:]],
                        max_chars=max_chars,
                    )
                    if pieces:
                        specs.append((pieces, None))
            continue
        pieces = _liturgy_section_tts_pieces(title, body, max_chars=max_chars)
        if pieces:
            # Synthèse : annonce collée au corps (même logique anti-hallucination).
            specs.append((pieces, None))
    return specs or None


def _synthesis_catechese_tts_section_chunks(spoken: str, *, max_chars: int) -> list[list[str]] | None:
    """Compatibilité : morceaux plats par groupe de section."""
    specs = _synthesis_tts_section_chunk_specs(spoken, max_chars=max_chars)
    if not specs:
        return None
    return [pieces for pieces, _pause in specs if pieces]


def _tts_chunks_to_wav(
    *,
    tts_api: GeminiTtsApiClient,
    voice_name: str,
    chunks: list[str],
    french_accent: str | None = None,
) -> bytes:
    wav_parts_by_i: dict[int, bytes] = {}
    tts_errors: list[str] = []

    def _tts_job(i: int, ch: str) -> tuple[int, bytes]:
        last_err: Exception | None = None
        for model in _GEMINI_API_TTS_MODELS:
            try:
                tts_audio = tts_api.generate_audio(
                    model=model,
                    text=ch,
                    voice_name=voice_name,
                    french_accent=french_accent,
                )
                b, mt, _ = normalize_audio_bytes(
                    audio_bytes=tts_audio.audio_bytes, mime_type=tts_audio.mime_type
                )
                if mt != "audio/wav":
                    b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
                return i, b
            except Exception as ex:
                last_err = ex
        raise last_err or RuntimeError("TTS Gemini échoué")

    workers = 1 if len(chunks) <= 2 else max(1, min(2, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as ex2:
        futs = [ex2.submit(_tts_job, i, ch) for i, ch in enumerate(chunks)]
        for fut in as_completed(futs):
            try:
                i, b = fut.result()
                wav_parts_by_i[i] = b
            except Exception as ex:
                tts_errors.append(str(ex))

    if tts_errors or len(wav_parts_by_i) != len(chunks):
        raise RuntimeError(
            "TTS incomplet : "
            + (tts_errors[0][:200] if tts_errors else "morceaux manquants")
        )

    wav_parts = [wav_parts_by_i[i] for i in range(len(chunks))]
    return join_wav_bytes(wav_parts)


def _synthesize_chunks_to_wav(
    *,
    chunks: list[str],
    voice_name: str,
    gemini_api_key: str | None,
    vertex_client: object | None,
    french_accent: str | None = None,
) -> bytes:
    if gemini_api_key:
        tts_api = GeminiTtsApiClient(api_key=str(gemini_api_key))
        return _tts_chunks_to_wav(
            tts_api=tts_api,
            voice_name=voice_name,
            chunks=chunks,
            french_accent=french_accent,
        )
    if vertex_client is not None:
        return _tts_vertex_chunks_to_wav(
            vertex_client=vertex_client,
            voice_name=voice_name,
            chunks=chunks,
            french_accent=french_accent,
        )
    raise RuntimeError(
        "Audio impossible : ajoute GEMINI_API_KEY dans les secrets "
        "ou vérifie que Vertex TTS (AUDIO) est autorisé sur le projet GCP."
    )


def _tts_pieces_to_wav(
    pieces: list[str],
    *,
    intro_pause_ms: int | None,
    voice_name: str,
    gemini_api_key: str | None,
    vertex_client: object | None,
    french_accent: str | None = None,
) -> bytes:
    if not pieces:
        raise ValueError("Morceaux TTS vides")
    if intro_pause_ms and len(pieces) > 1:
        head_wav = _synthesize_chunks_to_wav(
            chunks=[pieces[0]],
            voice_name=voice_name,
            gemini_api_key=gemini_api_key,
            vertex_client=vertex_client,
            french_accent=french_accent,
        )
        tail_wav = _synthesize_chunks_to_wav(
            chunks=pieces[1:],
            voice_name=voice_name,
            gemini_api_key=gemini_api_key,
            vertex_client=vertex_client,
            french_accent=french_accent,
        )
        return join_wav_with_silence([head_wav, tail_wav], pause_ms=intro_pause_ms)
    return _synthesize_chunks_to_wav(
        chunks=pieces,
        voice_name=voice_name,
        gemini_api_key=gemini_api_key,
        vertex_client=vertex_client,
        french_accent=french_accent,
    )


def _tts_section_specs_to_wav(
    specs: list[tuple[list[str], int | None]],
    *,
    inter_section_pause_ms: int,
    voice_name: str,
    gemini_api_key: str | None,
    vertex_client: object | None,
    french_accent: str | None = None,
) -> bytes:
    section_wavs: list[bytes] = []
    for pieces, intro_pause_ms in specs:
        if not pieces:
            continue
        section_wavs.append(
            _tts_pieces_to_wav(
                pieces,
                intro_pause_ms=intro_pause_ms,
                voice_name=voice_name,
                gemini_api_key=gemini_api_key,
                vertex_client=vertex_client,
                french_accent=french_accent,
            )
        )
    if not section_wavs:
        raise ValueError("Texte vide")
    return join_wav_with_silence(section_wavs, pause_ms=inter_section_pause_ms)


_VERTEX_TTS_ALLOWLIST_SESSION_KEY = "lumenvia_vertex_tts_allowlist_blocked"


def last_tts_route() -> str:
    """Canal TTS du dernier appel ``tts_readings_audio_bytes`` (session Streamlit)."""
    try:
        import streamlit as st  # type: ignore

        return str(st.session_state.get(_LAST_TTS_ROUTE_SESSION_KEY) or "").strip()
    except Exception:
        return ""


def _set_last_tts_route(route: str) -> None:
    try:
        import streamlit as st  # type: ignore

        st.session_state[_LAST_TTS_ROUTE_SESSION_KEY] = route
    except Exception:
        pass


def clear_vertex_tts_allowlist_blocked() -> None:
    """Réinitialise le mémorandum allowlist (ex. après un Vertex TTS réussi)."""
    try:
        import streamlit as st  # type: ignore

        st.session_state.pop(_VERTEX_TTS_ALLOWLIST_SESSION_KEY, None)
    except Exception:
        pass


def vertex_tts_fallback_eligible(exc: BaseException) -> bool:
    """True si l'échec Vertex TTS justifie un repli Gemini API (allowlist ou quota transitoire)."""
    msg = str(exc).lower()
    allowlist = ("not allowlisted" in msg) or ("allowlisted" in msg) or ("audio output" in msg and "400" in msg)
    transient = ("429" in msg) or ("quota" in msg) or ("rate" in msg) or ("tempor" in msg) or ("503" in msg) or ("timeout" in msg)
    return allowlist or transient


def vertex_tts_allowlist_blocked() -> bool:
    """True si Vertex TTS a déjà refusé l'audio (allowlist) durant cette session Streamlit."""
    try:
        import streamlit as st  # type: ignore

        return bool(st.session_state.get(_VERTEX_TTS_ALLOWLIST_SESSION_KEY))
    except Exception:
        return False


def mark_vertex_tts_allowlist_blocked(exc: BaseException) -> None:
    msg = str(exc).lower()
    if ("allowlisted" in msg) or ("audio output" in msg and "400" in msg):
        try:
            import streamlit as st  # type: ignore

            st.session_state[_VERTEX_TTS_ALLOWLIST_SESSION_KEY] = True
        except Exception:
            pass


def format_tts_unavailable_error(
    *,
    vtx_err: BaseException | None,
    gemini_key: str | None,
    gem_err: BaseException | None = None,
) -> RuntimeError:
    if gem_err is not None:
        return RuntimeError(
            "Repli Gemini API échoué après refus Vertex TTS : "
            f"{str(gem_err)[:400]}"
        )
    if vtx_err is not None and vertex_tts_fallback_eligible(vtx_err):
        if not gemini_key:
            return RuntimeError(
                "Audio indisponible via Vertex AI (projet non allowlisté pour l'audio). "
                "Ajoute `GEMINI_API_KEY` dans `.streamlit/secrets.toml` ou les Secrets Streamlit Cloud, "
                "puis redémarre l'app. Admin → Réglages & diagnostic : section « Clé GEMINI_API_KEY »."
            )
    if vtx_err is not None:
        return RuntimeError(str(vtx_err))
    return RuntimeError(
        "Audio impossible : vérifie Vertex TTS (allowlist AUDIO) ou configure `GEMINI_API_KEY`."
    )


def _tts_vertex_chunks_to_wav(
    *,
    vertex_client: object,
    voice_name: str,
    chunks: list[str],
    french_accent: str | None = None,
) -> bytes:
    if not chunks:
        raise ValueError("Texte vide")
    if len(chunks) == 1:
        audio = vertex_client.generate_audio_auto(
            preferred_models=list(_VERTEX_TTS_MODELS),
            text=chunks[0],
            voice_name=voice_name,
            french_accent=french_accent,
        )
        b, mt, _ = normalize_audio_bytes(
            audio_bytes=getattr(audio, "audio_bytes", b""),
            mime_type=getattr(audio, "mime_type", None),
        )
        if mt != "audio/wav":
            b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
        return b

    wav_parts_by_i: dict[int, bytes] = {}
    tts_errors: list[str] = []

    def _vertex_job(i: int, ch: str) -> tuple[int, bytes]:
        audio = vertex_client.generate_audio_auto(
            preferred_models=list(_VERTEX_TTS_MODELS),
            text=ch,
            voice_name=voice_name,
            french_accent=french_accent,
        )
        b, mt, _ = normalize_audio_bytes(
            audio_bytes=getattr(audio, "audio_bytes", b""),
            mime_type=getattr(audio, "mime_type", None),
        )
        if mt != "audio/wav":
            b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
        return i, b

    workers = 1 if len(chunks) <= 2 else max(1, min(2, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as ex2:
        futs = [ex2.submit(_vertex_job, i, ch) for i, ch in enumerate(chunks)]
        for fut in as_completed(futs):
            try:
                i, b = fut.result()
                wav_parts_by_i[i] = b
            except Exception as ex:
                tts_errors.append(str(ex))

    if tts_errors or len(wav_parts_by_i) != len(chunks):
        raise RuntimeError(
            "TTS Vertex incomplet : "
            + (tts_errors[0][:200] if tts_errors else "morceaux manquants")
        )

    wav_parts = [wav_parts_by_i[i] for i in range(len(chunks))]
    return join_wav_bytes(wav_parts)


def _tts_chunked_bytes_from_spoken(
    *,
    spoken: str,
    voice_name: str,
    gemini_api_key: str | None,
    vertex_client: object | None,
    french_accent: str | None = None,
) -> bytes:
    if is_liturgy_readings_tts_text(spoken):
        section_specs = _liturgy_readings_tts_section_chunk_specs(spoken, max_chars=1400)
        if not section_specs:
            raise ValueError("Texte liturgique vide")
        return _tts_section_specs_to_wav(
            section_specs,
            inter_section_pause_ms=_LITURGY_SECTION_PAUSE_MS,
            voice_name=voice_name,
            gemini_api_key=gemini_api_key,
            vertex_client=vertex_client,
            french_accent=french_accent,
        )

    synth_specs = _synthesis_tts_section_chunk_specs(spoken, max_chars=1400)
    if synth_specs:
        has_catechese = any(
            pieces and str(pieces[0]).startswith(CATECHESE_TTS_INTRO)
            for pieces, _pause in synth_specs
        )
        inter_pause = _CATECHESE_TTS_PAUSE_MS if has_catechese else _LITURGY_SECTION_PAUSE_MS
        return _tts_section_specs_to_wav(
            synth_specs,
            inter_section_pause_ms=inter_pause,
            voice_name=voice_name,
            gemini_api_key=gemini_api_key,
            vertex_client=vertex_client,
            french_accent=french_accent,
        )

    chunks = chunk_text_for_tts(spoken, max_chars=1400)
    return _synthesize_chunks_to_wav(
        chunks=chunks,
        voice_name=voice_name,
        gemini_api_key=gemini_api_key,
        vertex_client=vertex_client,
        french_accent=french_accent,
    )


def _resolve_tts_gemini_key(*, cfg: object, gemini_api_key: str | None) -> str | None:
    explicit = str(gemini_api_key or "").strip()
    if explicit:
        return explicit
    from_cfg = str(getattr(cfg, "gemini_api_key", "") or "").strip()
    if from_cfg:
        return from_cfg
    return resolve_gemini_api_key()


def tts_spoken_audio_bytes(
    *,
    cfg: object,
    text: str,
    voice_name: str | None = None,
    vertex_client: object | None = None,
    gemini_api_key: str | None = None,
    sunday_date: date | None = None,
    cible: str = "synthese",
    french_accent: str | None = None,
) -> tuple[bytes, str, str]:
    """
    TTS morcelé (Vertex puis repli Gemini API) pour synthèse ou lectures.

    Les textes longs sont découpés (~1400 car.) : un seul appel Vertex sur toute
    la synthèse provoquait des timeouts silencieux (plusieurs minutes sans fichier Audio/).
    ``french_accent`` : consigne d'accent francophone (sinon rotation déterministe
    dimanche + cible + voix).
    """
    if voice_name is None or not str(voice_name).strip():
        voice_name = DEFAULT_GEMINI_TTS_VOICE
    accent = (french_accent or "").strip() or pick_tts_french_accent(
        sunday_date=sunday_date,
        cible=cible,
        voice_name=str(voice_name),
    )
    spoken = spoken_text_for_tts(text)
    if not spoken:
        raise ValueError("Texte TTS vide")
    gemini_key = _resolve_tts_gemini_key(cfg=cfg, gemini_api_key=gemini_api_key)
    joined: bytes | None = None
    vtx_err: Exception | None = None
    gem_err: Exception | None = None
    route = ""
    allowlist_blocked = vertex_tts_allowlist_blocked()
    try_vertex = vertex_client is not None and not allowlist_blocked
    if try_vertex:
        try:
            joined = _tts_chunked_bytes_from_spoken(
                spoken=spoken,
                voice_name=str(voice_name),
                gemini_api_key=None,
                vertex_client=vertex_client,
                french_accent=accent,
            )
            clear_vertex_tts_allowlist_blocked()
            route = "vertex_tts"
        except Exception as ex:
            vtx_err = ex
            mark_vertex_tts_allowlist_blocked(ex)
            if not (gemini_key and vertex_tts_fallback_eligible(ex)):
                raise format_tts_unavailable_error(vtx_err=vtx_err, gemini_key=gemini_key) from ex
    elif allowlist_blocked and gemini_key:
        pass
    if joined is None and gemini_key:
        try:
            joined = _tts_chunked_bytes_from_spoken(
                spoken=spoken,
                voice_name=str(voice_name),
                gemini_api_key=gemini_key,
                vertex_client=None,
                french_accent=accent,
            )
            route = "gemini_api (repli)" if vtx_err else "gemini_api"
        except Exception as ex:
            gem_err = ex
    if joined is None:
        raise format_tts_unavailable_error(vtx_err=vtx_err, gemini_key=gemini_key, gem_err=gem_err)
    if vtx_err and route.startswith("gemini"):
        route = "vertex_tts → gemini_api"
    elif not route:
        route = "gemini_api"
    _set_last_tts_route(route)
    b_out, mime_out, ext_out = normalize_audio_bytes(audio_bytes=joined, mime_type="audio/wav")
    return b_out, mime_out, ext_out


def tts_readings_audio_bytes(
    *,
    cfg: object,
    text: str,
    voice_name: str | None = None,
    vertex_client: object | None = None,
    gemini_api_key: str | None = None,
    sunday_date: date | None = None,
    french_accent: str | None = None,
) -> tuple[bytes, str, str]:
    """Audio des lectures intégrales (découpage liturgique + pauses entre sections)."""
    return tts_spoken_audio_bytes(
        cfg=cfg,
        text=text,
        voice_name=voice_name,
        vertex_client=vertex_client,
        gemini_api_key=gemini_api_key,
        sunday_date=sunday_date,
        cible="lectures",
        french_accent=french_accent,
    )


def tts_gemini_chunked_bytes(*, cfg: object, text: str, voice_name: str | None = None) -> tuple[bytes, str, str]:
    """Synthèse vocale longue via Gemini API (fragments), même stratégie que le fallback synthèse."""
    if not getattr(cfg, "gemini_api_key", None):
        raise RuntimeError("GEMINI_API_KEY requise pour le TTS fragmenté Gemini API.")
    return tts_readings_audio_bytes(cfg=cfg, text=text, voice_name=voice_name, vertex_client=None)
