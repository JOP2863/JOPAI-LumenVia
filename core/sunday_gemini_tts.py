"""TTS Gemini fragmenté (lectures longues, synthèse)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.audio_utils import join_wav_bytes, normalize_audio_bytes
from core.gemini_tts_api import GeminiTtsApiClient
from core.sunday_readings_tts import spoken_text_for_tts
from core.voix_audio import DEFAULT_GEMINI_TTS_VOICE


def _split_by_size(text: str, *, max_chars: int) -> list[str]:
    t = " ".join((text or "").split())
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    return [t[i : i + max_chars] for i in range(0, len(t), max_chars)]


def _chunk_liturgy_readings_by_section(text: str, *, max_chars: int) -> list[str]:
    """
    Une (ou plusieurs) requêtes TTS par section AELF (``\\n\\n``).

    Évite de fusionner « Première lecture » + « Deuxième lecture » dans un seul appel
    Gemini : cela provoquait parfois un long blanc audio avant l'Évangile (morceau suivant).
    """
    chunks: list[str] = []
    for para in (text or "").split("\n\n"):
        p = " ".join(para.split())
        if not p:
            continue
        if len(p) <= max_chars:
            chunks.append(p)
        else:
            chunks.extend(_split_by_size(p, max_chars=max_chars))
    return chunks


def chunk_text_for_tts(text: str, *, max_chars: int = 1400) -> list[str]:
    """
    Découpe en morceaux pour éviter les limites TTS (et éviter l'audio tronqué).

    Lectures du lectionnaire : une section liturgique par morceau (puis découpe taille si besoin).
    Autres textes : fusion de paragraphes jusqu'à ``max_chars``.
    """
    t = (text or "").strip()
    if not t:
        return []
    if re.match(r"(?i)^(?:Première|Premiere) lecture\b", t):
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


def tts_gemini_chunked_bytes(*, cfg: object, text: str, voice_name: str | None = None) -> tuple[bytes, str, str]:
    """Synthèse vocale longue via Gemini API (fragments), même stratégie que le fallback synthèse."""
    if voice_name is None or not str(voice_name).strip():
        voice_name = DEFAULT_GEMINI_TTS_VOICE
    text = spoken_text_for_tts(text)
    if not text:
        raise ValueError("Texte vide")
    if not getattr(cfg, "gemini_api_key", None):
        raise RuntimeError("GEMINI_API_KEY requise pour l’audio des lectures (TTS fragmenté).")

    tts_api = GeminiTtsApiClient(api_key=str(cfg.gemini_api_key))
    chunks = chunk_text_for_tts(text, max_chars=1400)
    wav_parts_by_i: dict[int, bytes] = {}
    tts_errors: list[str] = []

    def _tts_job(i: int, ch: str) -> tuple[int, bytes]:
        tts_audio = tts_api.generate_audio(
            model="gemini-2.5-flash-preview-tts",
            text=ch,
            voice_name=voice_name,
        )
        b, mt, _ = normalize_audio_bytes(audio_bytes=tts_audio.audio_bytes, mime_type=tts_audio.mime_type)
        if mt != "audio/wav":
            b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
        return i, b

    workers = max(1, min(2, len(chunks)))
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
    joined = join_wav_bytes(wav_parts)
    b_out, mime_out, ext_out = normalize_audio_bytes(audio_bytes=joined, mime_type="audio/wav")
    return b_out, mime_out, ext_out
