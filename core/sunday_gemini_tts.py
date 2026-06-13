"""TTS Gemini fragmenté (lectures longues, synthèse)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from core.audio_utils import join_wav_bytes, join_wav_with_silence, normalize_audio_bytes
from core.gemini_tts_api import GeminiTtsApiClient
from core.config import resolve_gemini_api_key
from core.sunday_readings_tts import (
    is_liturgy_readings_tts_text,
    parse_liturgy_reading_sections,
    spoken_text_for_tts,
)
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

# Pause entre Première lecture / Psaume / Deuxième lecture / Évangile (millisecondes).
_LITURGY_SECTION_PAUSE_MS = 750

# En dessous de ce seuil, Gemini TTS invente parfois du contenu (ex. « menhirs ») sur un titre seul.
_MIN_LITURGY_TTS_CHARS = 64


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


def _liturgy_section_tts_pieces(title: str, body: str, *, max_chars: int) -> list[str]:
    """
    Morceaux TTS d'une section liturgique.

    Le titre (« Première lecture. », etc.) est **toujours** lu avec le début du corps —
    jamais isolé en morceau de 2 mots (Gemini TTS hallucine alors du contenu inventé).
    """
    from core.sunday_readings_tts import normalize_liturgy_section_title

    body = " ".join((body or "").split())
    if not body:
        return []
    title_norm = normalize_liturgy_section_title(title) if title else ""
    prefix = f"{title_norm}. " if title_norm else ""
    full = f"{prefix}{body}".strip()
    if len(full) <= max_chars:
        return [full]
    pieces: list[str] = []
    first_budget = max(max_chars, _MIN_LITURGY_TTS_CHARS) - len(prefix)
    if first_budget < 32:
        first_budget = max_chars - len(prefix)
    first_body = body[:first_budget]
    if len(body) > first_budget and " " in first_body:
        first_body = first_body.rsplit(" ", 1)[0]
    if not first_body:
        first_body = body[: max_chars - len(prefix)]
    pieces.append(f"{prefix}{first_body}".strip())
    rest = body[len(first_body) :].strip()
    if rest:
        pieces.extend(_split_by_size_at_word(rest, max_chars=max_chars))
    return pieces


def _liturgy_readings_tts_section_chunks(text: str, *, max_chars: int) -> list[list[str]]:
    """
    Par section liturgique : morceaux TTS avec annonce + corps (titre jamais seul).
    """
    grouped: list[list[str]] = []
    started = False
    for title, body in parse_liturgy_reading_sections(text):
        if not title:
            if not started:
                continue
            pieces = _split_by_size_at_word(body, max_chars=max_chars) if body else []
        else:
            started = True
            pieces = _liturgy_section_tts_pieces(title, body, max_chars=max_chars)
        if pieces:
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
    return final


def _tts_chunks_to_wav(
    *,
    tts_api: GeminiTtsApiClient,
    voice_name: str,
    chunks: list[str],
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
    transient = ("429" in msg) or ("quota" in msg) or ("rate" in msg) or ("tempor" in msg) or ("503" in msg)
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


def _tts_vertex_chunks_to_wav(*, vertex_client: object, voice_name: str, chunks: list[str]) -> bytes:
    if not chunks:
        raise ValueError("Texte vide")
    wav_parts: list[bytes] = []
    for ch in chunks:
        audio = vertex_client.generate_audio_auto(
            preferred_models=list(_VERTEX_TTS_MODELS),
            text=ch,
            voice_name=voice_name,
        )
        b, mt, _ = normalize_audio_bytes(
            audio_bytes=getattr(audio, "audio_bytes", b""),
            mime_type=getattr(audio, "mime_type", None),
        )
        if mt != "audio/wav":
            b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
        wav_parts.append(b)
    return join_wav_bytes(wav_parts)


def _tts_chunked_bytes_from_spoken(
    *,
    spoken: str,
    voice_name: str,
    gemini_api_key: str | None,
    vertex_client: object | None,
) -> bytes:
    if is_liturgy_readings_tts_text(spoken):
        section_groups = _liturgy_readings_tts_section_chunks(spoken, max_chars=1400)
        if not section_groups:
            raise ValueError("Texte liturgique vide")
        section_wavs: list[bytes] = []
        for section in section_groups:
            if gemini_api_key:
                tts_api = GeminiTtsApiClient(api_key=str(gemini_api_key))
                section_wavs.append(
                    _tts_chunks_to_wav(tts_api=tts_api, voice_name=voice_name, chunks=section)
                )
            elif vertex_client is not None:
                section_wavs.append(
                    _tts_vertex_chunks_to_wav(
                        vertex_client=vertex_client, voice_name=voice_name, chunks=section
                    )
                )
            else:
                raise RuntimeError(
                    "Audio des lectures impossible : ajoute GEMINI_API_KEY dans les secrets "
                    "ou vérifie que Vertex TTS (AUDIO) est autorisé sur le projet GCP."
                )
        return join_wav_with_silence(section_wavs, pause_ms=_LITURGY_SECTION_PAUSE_MS)

    chunks = chunk_text_for_tts(spoken, max_chars=1400)
    if gemini_api_key:
        tts_api = GeminiTtsApiClient(api_key=str(gemini_api_key))
        return _tts_chunks_to_wav(tts_api=tts_api, voice_name=voice_name, chunks=chunks)
    if vertex_client is not None:
        return _tts_vertex_chunks_to_wav(vertex_client=vertex_client, voice_name=voice_name, chunks=chunks)
    raise RuntimeError(
        "Audio des lectures impossible : ajoute GEMINI_API_KEY dans les secrets "
        "ou vérifie que Vertex TTS (AUDIO) est autorisé sur le projet GCP."
    )


def _resolve_tts_gemini_key(*, cfg: object, gemini_api_key: str | None) -> str | None:
    explicit = str(gemini_api_key or "").strip()
    if explicit:
        return explicit
    from_cfg = str(getattr(cfg, "gemini_api_key", "") or "").strip()
    if from_cfg:
        return from_cfg
    return resolve_gemini_api_key()


def tts_readings_audio_bytes(
    *,
    cfg: object,
    text: str,
    voice_name: str | None = None,
    vertex_client: object | None = None,
    gemini_api_key: str | None = None,
) -> tuple[bytes, str, str]:
    """
    Audio des lectures intégrales : Vertex TTS fragmenté en priorité,
    repli Gemini API fragmenté (même logique que la synthèse dominicale).
    """
    if voice_name is None or not str(voice_name).strip():
        voice_name = DEFAULT_GEMINI_TTS_VOICE
    spoken = spoken_text_for_tts(text)
    if not spoken:
        raise ValueError("Texte des lectures vide")
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


def tts_gemini_chunked_bytes(*, cfg: object, text: str, voice_name: str | None = None) -> tuple[bytes, str, str]:
    """Synthèse vocale longue via Gemini API (fragments), même stratégie que le fallback synthèse."""
    if not getattr(cfg, "gemini_api_key", None):
        raise RuntimeError("GEMINI_API_KEY requise pour le TTS fragmenté Gemini API.")
    return tts_readings_audio_bytes(cfg=cfg, text=text, voice_name=voice_name, vertex_client=None)
