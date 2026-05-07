from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class GeminiTtsResult:
    model: str
    audio_bytes: bytes
    mime_type: str
    raw: dict[str, Any]


class GeminiTtsApiClient:
    """
    TTS via Gemini API (API key), utile en fallback si Vertex AUDIO est refusé (allowlist).
    """

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self._session = requests.Session()

    def generate_audio(
        self,
        *,
        model: str,
        text: str,
        voice_name: str = "Kore",
        max_retries: int = 6,
    ) -> GeminiTtsResult:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_name},
                    }
                },
            },
        }
        last_err: str | None = None
        for attempt in range(max(1, int(max_retries))):
            r = self._session.post(url, json=payload, timeout=90)
            if r.status_code < 400:
                raw: dict[str, Any] = r.json()
                b64, mime = _extract_inline_audio(raw)
                audio = base64.b64decode(b64) if b64 else b""
                return GeminiTtsResult(model=model, audio_bytes=audio, mime_type=mime or "audio/wav", raw=raw)

            # Quotas / surcharge : réessais avec backoff.
            last_err = f"{r.status_code}: {r.text}"
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt < max_retries - 1:
                    ra = (r.headers or {}).get("Retry-After")
                    try:
                        wait_s = float(ra) if ra else 0.0
                    except Exception:
                        wait_s = 0.0
                    if wait_s <= 0:
                        wait_s = min(2.0 * (2**attempt), 60.0)
                    time.sleep(wait_s)
                    continue
            break

        raise RuntimeError(f"Gemini API TTS error: {last_err or 'inconnue'}")


def _extract_inline_audio(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return None, None
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        return None, None
    inline = (parts[0] or {}).get("inlineData") or (parts[0] or {}).get("inline_data") or {}
    data = inline.get("data")
    mime = inline.get("mimeType") or inline.get("mime_type")
    if isinstance(data, str) and data:
        return data, (str(mime) if mime else None)
    return None, (str(mime) if mime else None)

