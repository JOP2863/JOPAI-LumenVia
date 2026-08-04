"""Ambiance audio pour habiller les TTS (intro / outro / bed).

V1 : bibliothèque curatée (Sheets ``audio_ambiance`` / AAMB + GCS), mix WAV pur
(stdlib ``wave`` + ``array``) — pas de dépendance ffmpeg/pydub.
"""

from __future__ import annotations

import array
import hashlib
import wave
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from core.audio_utils import join_wav_with_silence, pcm16le_to_wav_bytes
from core.locale_codes import normalize_pref_langue
from core.sheets_db import sheet_row_status_is_live

AUDIO_AMBIANCE_TABLE = "audio_ambiance"
AMBIANCE_GCS_PREFIX = "Audio/ambiance"

ROLES: tuple[str, ...] = ("intro", "outro", "bed")
CIBLES: tuple[str, ...] = ("lectures", "synthese", "both")
LANGUES: tuple[str, ...] = ("ALL", "FR", "DE", "EN", "ES", "IT")
LICENCES: tuple[str, ...] = ("CC0", "CC-BY", "domaine_public", "autre")

# Licences acceptées pour un usage producteur sans risque majeur (hors « autre » à valider).
LICENCES_SAFE: tuple[str, ...] = ("CC0", "CC-BY", "domaine_public")

# Bed ~ −28 dB par rapport à la voix (gain linéaire ≈ 0.04).
DEFAULT_BED_GAIN = 0.04
DEFAULT_EDGE_PAUSE_MS = 400


def _truthy_preferred(raw: object | None) -> bool:
    s = str(raw or "").strip().casefold()
    return s in ("1", "oui", "yes", "true", "x", "preferred", "prioritaire")


@dataclass(frozen=True)
class AmbianceClip:
    entity_id: str
    title: str
    role: str
    cible: str
    langue: str
    licence: str
    attribution: str
    gcs_path: str
    duration_s: float | None = None
    preferred: bool = False


def ambiance_gcs_path(*, entity_id: str, ext: str = "wav") -> str:
    e = str(ext or "wav").lstrip(".")
    return f"{AMBIANCE_GCS_PREFIX}/{str(entity_id).strip()}.{e}"


def clip_from_row(row: dict[str, Any]) -> AmbianceClip | None:
    if not sheet_row_status_is_live(row.get("status")):
        return None
    path = str(row.get("gcs_path") or "").strip()
    role = str(row.get("role") or "").strip().lower()
    if not path or role not in ROLES:
        return None
    dur_raw = str(row.get("duration_s") or "").strip().replace(",", ".")
    try:
        dur = float(dur_raw) if dur_raw else None
    except Exception:
        dur = None
    return AmbianceClip(
        entity_id=str(row.get("entity_id") or "").strip(),
        title=str(row.get("title") or "").strip() or path.rsplit("/", 1)[-1],
        role=role,
        cible=str(row.get("cible") or "both").strip().lower() or "both",
        langue=str(row.get("langue") or "ALL").strip().upper() or "ALL",
        licence=str(row.get("licence") or "").strip(),
        attribution=str(row.get("attribution") or "").strip(),
        gcs_path=path,
        duration_s=dur,
        preferred=_truthy_preferred(row.get("preferred")),
    )


def list_active_clips(rows: list[dict[str, Any]]) -> list[AmbianceClip]:
    out: list[AmbianceClip] = []
    for r in rows or []:
        c = clip_from_row(r)
        if c:
            out.append(c)
    return out


def _clip_matches(clip: AmbianceClip, *, cible: str, pref_langue: str) -> bool:
    c = (cible or "synthese").strip().lower()
    if clip.cible not in (c, "both"):
        return False
    lg = normalize_pref_langue(pref_langue)
    cl = (clip.langue or "ALL").upper()
    return cl in ("ALL", "", lg)


def pick_ambiance_set(
    clips: list[AmbianceClip],
    *,
    cible: str,
    pref_langue: str = "FR",
    seed: str = "",
) -> dict[str, AmbianceClip | None]:
    """Choisit intro / outro / bed (priorité « preferred », sinon déterministe via ``seed``)."""
    pool = [c for c in clips if _clip_matches(c, cible=cible, pref_langue=pref_langue)]
    out: dict[str, AmbianceClip | None] = {"intro": None, "outro": None, "bed": None}
    for role in ROLES:
        cand = [c for c in pool if c.role == role]
        if not cand:
            continue
        # Préférer langue exacte puis ALL
        lg = normalize_pref_langue(pref_langue)
        exact = [c for c in cand if c.langue == lg]
        use = exact or cand
        pinned = [c for c in use if c.preferred]
        use = pinned or use
        h = hashlib.sha256(f"{seed}|{cible}|{role}|{lg}".encode("utf-8")).hexdigest()
        idx = int(h[:8], 16) % len(use)
        out[role] = use[idx]
    return out


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(BytesIO(wav_bytes), "rb") as wf:
        fr = wf.getframerate() or 1
        return float(wf.getnframes()) / float(fr)


def _read_wav_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    with wave.open(BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sw != 2:
        raise ValueError("Seuls les WAV PCM 16-bit sont supportés pour le mix ambiance.")
    if nch not in (1, 2):
        raise ValueError("WAV ambiance : mono ou stéréo uniquement.")
    return frames, nch, sw, fr


def _to_mono16(pcm: bytes, *, nch: int) -> bytes:
    if nch == 1:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    mono = array.array("h")
    for i in range(0, len(samples), 2):
        if i + 1 < len(samples):
            mono.append(int((int(samples[i]) + int(samples[i + 1])) // 2))
        else:
            mono.append(int(samples[i]))
    return mono.tobytes()


def _resample_linear_mono16(pcm: bytes, *, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm)
    if not src:
        return b""
    n_dst = max(1, int(round(len(src) * float(dst_rate) / float(src_rate))))
    dst = array.array("h")
    for i in range(n_dst):
        x = i * (len(src) - 1) / max(1, n_dst - 1)
        i0 = int(x)
        i1 = min(i0 + 1, len(src) - 1)
        frac = x - i0
        v = src[i0] * (1.0 - frac) + src[i1] * frac
        dst.append(int(max(-32768, min(32767, round(v)))))
    return dst.tobytes()


def normalize_clip_to_voice_format(clip_wav: bytes, *, voice_wav: bytes) -> bytes:
    """Aligne un clip sur le format (rate, mono 16-bit) de la voix TTS."""
    v_pcm, v_nch, _v_sw, v_fr = _read_wav_pcm(voice_wav)
    del v_pcm
    c_pcm, c_nch, _c_sw, c_fr = _read_wav_pcm(clip_wav)
    mono = _to_mono16(c_pcm, nch=c_nch)
    if v_nch != 1:
        # Voix LumenVia = mono ; on force mono.
        pass
    resampled = _resample_linear_mono16(mono, src_rate=c_fr, dst_rate=v_fr)
    return pcm16le_to_wav_bytes(resampled, sample_rate=v_fr, channels=1)


def _scale_pcm16(pcm: bytes, gain: float) -> bytes:
    samples = array.array("h")
    samples.frombytes(pcm)
    g = float(gain)
    for i in range(len(samples)):
        samples[i] = int(max(-32768, min(32767, round(samples[i] * g))))
    return samples.tobytes()


def _mix_bed_under_voice(
    voice_wav: bytes,
    bed_wav: bytes,
    *,
    bed_gain: float = DEFAULT_BED_GAIN,
) -> bytes:
    v_pcm, v_nch, _sw, v_fr = _read_wav_pcm(voice_wav)
    if v_nch != 1:
        v_pcm = _to_mono16(v_pcm, nch=v_nch)
    bed_aligned = normalize_clip_to_voice_format(bed_wav, voice_wav=voice_wav)
    b_pcm, _bn, _bs, _br = _read_wav_pcm(bed_aligned)
    b_pcm = _scale_pcm16(b_pcm, bed_gain)
    v_s = array.array("h")
    v_s.frombytes(v_pcm)
    b_s = array.array("h")
    b_s.frombytes(b_pcm)
    if not b_s:
        return voice_wav
    # Boucle / coupe le bed à la longueur de la voix
    out = array.array("h")
    blen = len(b_s)
    for i in range(len(v_s)):
        mixed = int(v_s[i]) + int(b_s[i % blen])
        out.append(int(max(-32768, min(32767, mixed))))
    return pcm16le_to_wav_bytes(out.tobytes(), sample_rate=v_fr, channels=1)


def dress_speech_wav(
    speech_wav: bytes,
    *,
    intro_wav: bytes | None = None,
    outro_wav: bytes | None = None,
    bed_wav: bytes | None = None,
    bed_gain: float = DEFAULT_BED_GAIN,
    edge_pause_ms: int = DEFAULT_EDGE_PAUSE_MS,
) -> bytes:
    """Assemble intro + voix (±bed) + outro. Tous les clips sont réalignés sur la voix."""
    if not speech_wav:
        return speech_wav
    body = speech_wav
    if bed_wav:
        try:
            body = _mix_bed_under_voice(body, bed_wav, bed_gain=bed_gain)
        except Exception:
            body = speech_wav
    parts: list[bytes] = []
    if intro_wav:
        try:
            parts.append(normalize_clip_to_voice_format(intro_wav, voice_wav=speech_wav))
        except Exception:
            pass
    parts.append(body)
    if outro_wav:
        try:
            parts.append(normalize_clip_to_voice_format(outro_wav, voice_wav=speech_wav))
        except Exception:
            pass
    if len(parts) == 1:
        return parts[0]
    return join_wav_with_silence(parts, pause_ms=edge_pause_ms)


def load_clip_bytes_from_gcs(*, gcs: object, bucket_name: str, path: str) -> bytes | None:
    from core.storage import download_bytes

    p = str(path or "").strip()
    if not p or not bucket_name:
        return None
    try:
        return download_bytes(gcs=gcs, bucket_name=bucket_name, path=p)
    except Exception:
        return None


def dress_tts_with_library(
    speech_wav: bytes,
    *,
    gcs: object | None,
    bucket_name: str,
    clips: list[AmbianceClip],
    cible: str,
    pref_langue: str = "FR",
    seed: str = "",
    bed_gain: float = DEFAULT_BED_GAIN,
) -> tuple[bytes, dict[str, str]]:
    """
    Applique la bibliothèque si des clips matchent.

    Retourne ``(wav, meta)`` où meta contient les entity_id / titres utilisés (attribution).
    """
    meta: dict[str, str] = {}
    if not speech_wav or gcs is None or not str(bucket_name or "").strip():
        return speech_wav, meta
    chosen = pick_ambiance_set(clips, cible=cible, pref_langue=pref_langue, seed=seed)
    intro_b = outro_b = bed_b = None
    for role, clip in chosen.items():
        if not clip:
            continue
        raw = load_clip_bytes_from_gcs(gcs=gcs, bucket_name=bucket_name, path=clip.gcs_path)
        if not raw or raw[:4] != b"RIFF":
            continue
        if role == "intro":
            intro_b = raw
        elif role == "outro":
            outro_b = raw
        elif role == "bed":
            bed_b = raw
        meta[role] = clip.title
        if clip.attribution:
            meta[f"{role}_attribution"] = clip.attribution
        if clip.licence:
            meta[f"{role}_licence"] = clip.licence
    if not (intro_b or outro_b or bed_b):
        return speech_wav, meta
    dressed = dress_speech_wav(
        speech_wav,
        intro_wav=intro_b,
        outro_wav=outro_b,
        bed_wav=bed_b,
        bed_gain=bed_gain,
    )
    return dressed, meta
