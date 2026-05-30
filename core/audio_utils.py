from __future__ import annotations

import re
import wave
from io import BytesIO


_RATE_RE = re.compile(r"rate=(\d+)")


def pcm16le_to_wav_bytes(pcm: bytes, *, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """
    Encapsule des bytes PCM 16-bit little-endian dans un conteneur WAV.
    """
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def normalize_audio_bytes(*, audio_bytes: bytes, mime_type: str | None) -> tuple[bytes, str, str]:
    """
    Retourne (bytes, mime_type, extension) compatibles pour stockage + lecture Streamlit.

    - Si l'API renvoie du PCM type audio/L16;rate=24000 -> on convertit en WAV.
    - Si c'est déjà du WAV (RIFF) -> on garde.
    """
    m = (mime_type or "").lower().strip()

    # Déjà un WAV (header RIFF)
    if audio_bytes[:4] == b"RIFF" and b"WAVE" in audio_bytes[:16]:
        return audio_bytes, "audio/wav", "wav"

    if m.startswith("audio/l16"):
        rate = 24000
        mm = _RATE_RE.search(m)
        if mm:
            try:
                rate = int(mm.group(1))
            except Exception:
                rate = 24000
        wav = pcm16le_to_wav_bytes(audio_bytes, sample_rate=rate, channels=1)
        return wav, "audio/wav", "wav"

    if "audio/wav" in m or "audio/x-wav" in m:
        return audio_bytes, "audio/wav", "wav"
    if "audio/mpeg" in m or "mp3" in m:
        return audio_bytes, "audio/mpeg", "mp3"
    if "audio/ogg" in m or "ogg" in m:
        return audio_bytes, "audio/ogg", "ogg"

    # Dernier recours
    return audio_bytes, (mime_type or "application/octet-stream"), "bin"


def join_wav_bytes(parts: list[bytes]) -> bytes:
    """
    Concatène plusieurs WAV mono 16-bit en un seul WAV.
    Hypothèse: même format / sample rate.
    """
    if not parts:
        return b""
    if len(parts) == 1:
        return parts[0]

    # Ouvre le premier pour récupérer le format
    with wave.open(BytesIO(parts[0]), "rb") as wf0:
        nch = wf0.getnchannels()
        sw = wf0.getsampwidth()
        fr = wf0.getframerate()
        frames = [wf0.readframes(wf0.getnframes())]

    for p in parts[1:]:
        with wave.open(BytesIO(p), "rb") as wf:
            if wf.getnchannels() != nch or wf.getsampwidth() != sw or wf.getframerate() != fr:
                raise ValueError("Formats WAV incompatibles pour concaténation.")
            frames.append(wf.readframes(wf.getnframes()))

    return pcm16le_to_wav_bytes(b"".join(frames), sample_rate=fr, channels=nch)


def join_wav_with_silence(parts: list[bytes], *, pause_ms: int = 750) -> bytes:
    """
    Concatène des WAV en insérant un silence entre chaque segment (ex. sections liturgiques).
    """
    if not parts:
        return b""
    if len(parts) == 1:
        return parts[0]

    with wave.open(BytesIO(parts[0]), "rb") as wf0:
        nch = wf0.getnchannels()
        sw = wf0.getsampwidth()
        fr = wf0.getframerate()
        frames: list[bytes] = [wf0.readframes(wf0.getnframes())]

    pause_ms = max(0, int(pause_ms))
    silence = b"\x00" * int(fr * pause_ms / 1000) * nch * sw

    for p in parts[1:]:
        with wave.open(BytesIO(p), "rb") as wf:
            if wf.getnchannels() != nch or wf.getsampwidth() != sw or wf.getframerate() != fr:
                raise ValueError("Formats WAV incompatibles pour concaténation.")
            frames.append(silence)
            frames.append(wf.readframes(wf.getnframes()))

    return pcm16le_to_wav_bytes(b"".join(frames), sample_rate=fr, channels=nch)

