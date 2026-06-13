from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Mapping

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from core.sunday_readings_tts import spoken_text_for_tts


@dataclass(frozen=True)
class VertexTextResult:
    model: str
    text: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class VertexAudioResult:
    model: str
    audio_bytes: bytes
    mime_type: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class VertexImageResult:
    model: str
    image_bytes: bytes
    mime_type: str
    raw: dict[str, Any]


def _vertex_api_host(location: str) -> str:
    if (location or "").strip().lower() == "global":
        return "aiplatform.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


class VertexGeminiClient:
    """
    Client REST Vertex AI Gemini.
    - Auth: compte de service (secrets.toml)
    - Sorties: texte (TEXT), audio (AUDIO) et image (IMAGE) via responseModalities
    """

    def __init__(
        self,
        *,
        service_account_info: Mapping[str, Any],
        locations: list[str] | None = None,
        publisher: str = "google",
    ) -> None:
        self.service_account_info = dict(service_account_info)
        self.project_id = str(self.service_account_info.get("project_id", "")).strip()
        if not self.project_id:
            raise ValueError("project_id manquant dans le compte de service.")
        # Vertex AI modèles publisher ne sont pas forcément dispo dans toutes les régions.
        # On teste en priorité EU puis US (classique pour Gemini).
        self.locations = locations or ["europe-west1", "us-central1"]
        self.publisher = publisher
        self._session = requests.Session()
        self._model_list_cache: dict[str, tuple[float, list[str]]] = {}

        self._creds = service_account.Credentials.from_service_account_info(
            self.service_account_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

    def _auth_header(self) -> dict[str, str]:
        if not self._creds.valid or self._creds.token is None:
            self._creds.refresh(GoogleAuthRequest())
        return {"Authorization": f"Bearer {self._creds.token}"}

    def _endpoint(self, *, model: str) -> str:
        raise NotImplementedError("Utiliser _endpoint_at(location=...)")

    def _endpoint_at(self, *, location: str, model: str) -> str:
        host = _vertex_api_host(location)
        return (
            f"https://{host}/v1/"
            f"projects/{self.project_id}/locations/{location}/publishers/{self.publisher}/models/{model}:generateContent"
        )

    def list_models(self, *, location: str) -> list[str]:
        loc = (location or "").strip()
        if not loc:
            return []
        now = time.monotonic()
        cached = self._model_list_cache.get(loc)
        if cached and (now - cached[0]) < 600.0:
            return list(cached[1])
        host = _vertex_api_host(location)
        url = (
            f"https://{host}/v1/"
            f"projects/{self.project_id}/locations/{location}/publishers/{self.publisher}/models"
        )
        r = self._session.get(url, headers={**self._auth_header()}, timeout=45)
        if r.status_code >= 400:
            return []
        raw: dict[str, Any] = r.json()
        models = raw.get("models") or []
        out: list[str] = []
        for m in models:
            name = (m or {}).get("name")
            if isinstance(name, str) and "/models/" in name:
                out.append(name.split("/models/")[-1])
        self._model_list_cache[loc] = (now, out)
        return out

    def pick_first_available(self, *, preferred: list[str], location: str) -> str | None:
        available = set(self.list_models(location=location))
        for m in preferred:
            if m in available:
                return m
        return None

    def generate_text(self, *, model: str, prompt: str, max_output_tokens: int = 2048) -> VertexTextResult:
        raw, used_location, used_model = self._generate_auto(
            preferred_models=[model],
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            generation_config={
                "temperature": 0.3,
                "topP": 0.9,
                "maxOutputTokens": int(max_output_tokens),
            },
        )
        text = _extract_text(raw)
        return VertexTextResult(model=f"{used_location}:{used_model}", text=text, raw=raw)

    def generate_text_auto(
        self,
        *,
        preferred_models: list[str],
        prompt: str,
        max_output_tokens: int = 2048,
    ) -> VertexTextResult:
        raw, used_location, used_model = self._generate_auto(
            preferred_models=preferred_models,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            generation_config={
                "temperature": 0.3,
                "topP": 0.9,
                "maxOutputTokens": int(max_output_tokens),
            },
        )
        text = _extract_text(raw)
        return VertexTextResult(model=f"{used_location}:{used_model}", text=text, raw=raw)

    def generate_text_multimodal_auto(
        self,
        *,
        preferred_models: list[str],
        image_bytes: bytes,
        image_mime_type: str,
        prompt: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> VertexTextResult:
        """Texte à partir d’une image (inline) + consigne — modèles Gemini multimodaux Vertex."""
        mime = (image_mime_type or "image/png").strip()
        if not mime.lower().startswith("image/"):
            mime = "image/png"
        parts: list[dict[str, Any]] = [
            {
                "inlineData": {
                    "mimeType": mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            },
            {"text": prompt},
        ]
        raw, used_location, used_model = self._generate_auto(
            preferred_models=preferred_models,
            contents=[{"role": "user", "parts": parts}],
            generation_config={
                "temperature": float(temperature),
                "topP": 0.9,
                "maxOutputTokens": int(max_output_tokens),
            },
            timeout_s=120,
        )
        text = _extract_text(raw)
        return VertexTextResult(model=f"{used_location}:{used_model}", text=text, raw=raw)

    def generate_audio(
        self,
        *,
        model: str,
        text: str,
        voice_name: str = "Kore",
    ) -> VertexAudioResult:
        text = spoken_text_for_tts(text)
        raw, used_location, used_model = self._generate_auto(
            preferred_models=[model],
            contents=[{"role": "user", "parts": [{"text": text}]}],
            generation_config={
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice_name,
                        }
                    }
                },
            },
            timeout_s=300,
        )

        b64, mime = _extract_inline_audio(raw)
        audio_bytes = base64.b64decode(b64) if b64 else b""
        return VertexAudioResult(
            model=f"{used_location}:{used_model}",
            audio_bytes=audio_bytes,
            mime_type=mime or "audio/wav",
            raw=raw,
        )

    def generate_audio_auto(
        self,
        *,
        preferred_models: list[str],
        text: str,
        voice_name: str = "Kore",
    ) -> VertexAudioResult:
        text = spoken_text_for_tts(text)
        raw, used_location, used_model = self._generate_auto(
            preferred_models=preferred_models,
            contents=[{"role": "user", "parts": [{"text": text}]}],
            generation_config={
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice_name,
                        }
                    }
                },
            },
            timeout_s=300,
        )
        b64, mime = _extract_inline_audio(raw)
        audio_bytes = base64.b64decode(b64) if b64 else b""
        return VertexAudioResult(
            model=f"{used_location}:{used_model}",
            audio_bytes=audio_bytes,
            mime_type=mime or "audio/wav",
            raw=raw,
        )

    def generate_image_auto(
        self,
        *,
        preferred_models: list[str],
        prompt: str,
        aspect_ratio: str = "4:3",
        reference_image_bytes: bytes | None = None,
        reference_image_mime_type: str | None = None,
    ) -> VertexImageResult:
        """Génération d’image (Gemini image / nano-banana) via responseModalities IMAGE."""
        parts: list[dict[str, Any]] = []
        if reference_image_bytes:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": (reference_image_mime_type or "image/png"),
                        "data": base64.b64encode(reference_image_bytes).decode("ascii"),
                    }
                }
            )
        parts.append({"text": prompt})
        raw, used_location, used_model = self._generate_auto(
            preferred_models=preferred_models,
            contents=[{"role": "user", "parts": parts}],
            generation_config={
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio},
            },
            timeout_s=180,
        )
        img_bytes, mime = _extract_inline_image(raw)
        if not img_bytes:
            raise RuntimeError(
                "Réponse Vertex sans image inline (vérifie quotas / safety / disponibilité du modèle image)."
            )
        return VertexImageResult(
            model=f"{used_location}:{used_model}",
            image_bytes=img_bytes,
            mime_type=mime or "image/png",
            raw=raw,
        )

    def _generate(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        generation_config: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError("Utiliser _generate_auto(...)")

    def _generate_auto(
        self,
        *,
        preferred_models: list[str],
        contents: list[dict[str, Any]],
        generation_config: dict[str, Any],
        timeout_s: int = 120,
    ) -> tuple[dict[str, Any], str, str]:
        """
        Essaie plusieurs régions + choisit un modèle existant dans la région.
        Retourne (raw_json, location, model).
        """
        last_err: str | None = None
        payload = {"contents": contents, "generationConfig": generation_config}

        for loc in self.locations:
            # Si on peut lister les modèles, on sélectionne un modèle réellement dispo.
            chosen = self.pick_first_available(preferred=preferred_models, location=loc)
            candidates = [chosen] if chosen else preferred_models

            for model in candidates:
                if not model:
                    continue
                url = self._endpoint_at(location=loc, model=model)
                # 429 (quota / débit) : quelques réessais avec backoff court, puis autre modèle / région.
                max_attempts = 5
                for attempt in range(max_attempts):
                    r = self._session.post(
                        url, headers={**self._auth_header()}, json=payload, timeout=timeout_s
                    )
                    if r.status_code < 400:
                        return r.json(), loc, model
                    last_err = f"{loc}/{model} -> {r.status_code}: {r.text}"
                    if r.status_code == 429:
                        if attempt < max_attempts - 1:
                            wait_s = min(3.0 * (2**attempt), 90.0)
                            time.sleep(wait_s)
                            continue
                        break
                    if r.status_code == 404:
                        break
                    raise RuntimeError(f"Vertex AI error {r.status_code}: {r.text}")

        raise RuntimeError(
            "Aucun modèle Vertex Gemini n’a répondu. Dernière erreur: " + (last_err or "inconnue")
        )


def _extract_text(raw: dict[str, Any]) -> str:
    candidates = raw.get("candidates") or []
    if not candidates:
        return ""
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    for p in parts:
        t = p.get("text")
        if isinstance(t, str) and t.strip():
            texts.append(t.strip())
    return "\n".join(texts).strip()


def _extract_inline_image(raw: dict[str, Any]) -> tuple[bytes | None, str | None]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return None, None
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    for p in parts:
        if not isinstance(p, dict):
            continue
        inline = p.get("inlineData") or p.get("inline_data") or {}
        data = inline.get("data")
        mime = inline.get("mimeType") or inline.get("mime_type")
        if isinstance(data, str) and data and isinstance(mime, str) and mime.lower().startswith("image/"):
            try:
                return base64.b64decode(data), mime
            except Exception:
                continue
    return None, None


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

