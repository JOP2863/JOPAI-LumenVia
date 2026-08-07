"""Clips d’ambiance TTS générés en WAV (stdlib) — secours sans ffmpeg / Freesound.

Utilisé quand AAMB n’a pas d’intro/outro/bed actifs (ex. Streamlit Cloud sans ffmpeg :
seuls les chants ``ecoute`` OGG ont pu être importés).
"""

from __future__ import annotations

import math
import struct
import wave
from io import BytesIO


def _pcm16_wav(*, samples: list[float], rate: int = 44100) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for x in samples:
            v = max(-1.0, min(1.0, float(x)))
            frames += struct.pack("<h", int(v * 32767.0))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _envelope(i: int, n: int, *, attack: float = 0.04, release: float = 0.25) -> float:
    if n <= 1:
        return 1.0
    t = i / (n - 1)
    a = min(1.0, t / max(1e-6, attack))
    r = min(1.0, (1.0 - t) / max(1e-6, release))
    return min(a, r)


def synth_intro_chime_wav(*, duration_s: float = 2.2, rate: int = 44100) -> bytes:
    """Petite cloche douce (sinusoïdes amorties) — intro TTS."""
    n = max(1, int(duration_s * rate))
    out: list[float] = []
    for i in range(n):
        t = i / rate
        env = math.exp(-2.8 * t) * _envelope(i, n, attack=0.01, release=0.35)
        # Harmoniques type cloche (non dissonantes).
        s = (
            0.55 * math.sin(2 * math.pi * 660 * t)
            + 0.28 * math.sin(2 * math.pi * 990 * t)
            + 0.12 * math.sin(2 * math.pi * 1320 * t)
        )
        out.append(0.35 * env * s)
    return _pcm16_wav(samples=out, rate=rate)


def synth_outro_organ_wav(*, duration_s: float = 2.8, rate: int = 44100) -> bytes:
    """Accord d’orgue très doux qui s’éteint — outro TTS."""
    n = max(1, int(duration_s * rate))
    freqs = (196.0, 246.94, 293.66)  # G3–B3–D4
    out: list[float] = []
    for i in range(n):
        t = i / rate
        env = _envelope(i, n, attack=0.08, release=0.55) * math.exp(-0.55 * t)
        s = 0.0
        for f in freqs:
            s += math.sin(2 * math.pi * f * t)
            s += 0.35 * math.sin(2 * math.pi * (2 * f) * t)
        out.append(0.18 * env * (s / len(freqs)))
    return _pcm16_wav(samples=out, rate=rate)


def synth_bed_drone_wav(*, duration_s: float = 12.0, rate: int = 44100) -> bytes:
    """Nappe très basse (sera atténuée encore par le mix bed) — fond sous la voix."""
    n = max(1, int(duration_s * rate))
    out: list[float] = []
    for i in range(n):
        t = i / rate
        env = _envelope(i, n, attack=0.4, release=0.4)
        s = (
            0.55 * math.sin(2 * math.pi * 110 * t)
            + 0.35 * math.sin(2 * math.pi * 165 * t)
            + 0.15 * math.sin(2 * math.pi * 220 * t)
        )
        # Légère modulation d’amplitude (pas de rythme).
        trem = 0.92 + 0.08 * math.sin(2 * math.pi * 0.15 * t)
        out.append(0.22 * env * trem * s)
    return _pcm16_wav(samples=out, rate=rate)


def synthetic_mix_wavs() -> dict[str, bytes]:
    """Trio intro / outro / bed prêts pour ``dress_speech_wav``."""
    return {
        "intro": synth_intro_chime_wav(),
        "outro": synth_outro_organ_wav(),
        "bed": synth_bed_drone_wav(),
    }
