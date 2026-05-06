from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class GeminiResult:
    model: str
    text: str
    raw: dict[str, Any]


class GeminiClient:
    """
    Client minimal via Generative Language API (API key).
    On reste volontairement simple pour le MVP.
    """

    def __init__(self, *, api_key: str, model: str = "gemini-flash-latest") -> None:
        self.api_key = api_key
        self.model = model
        self._session = requests.Session()

    def generate_text(self, *, prompt: str) -> GeminiResult:
        # Modèles “modernes” vus via list_models() + quelques alias stables.
        preferred = [
            self.model,
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-001",
        ]
        last_err: Exception | None = None

        for m in _dedupe(preferred):
            try:
                raw = self._generate_with_model(model=m, prompt=prompt)
                text = _extract_text(raw)
                return GeminiResult(model=m, text=text, raw=raw)
            except requests.HTTPError as e:
                # 404 = modèle inconnu / non disponible sur ce projet
                if e.response is not None and e.response.status_code == 404:
                    last_err = e
                    continue
                # Pour les autres HTTP errors, on enrichit le message.
                body = ""
                try:
                    body = e.response.text if e.response is not None else ""
                except Exception:
                    body = ""
                raise RuntimeError(f"Erreur Gemini HTTP {getattr(e.response, 'status_code', '?')}: {body}") from e
            except Exception as e:  # pragma: no cover
                last_err = e
                continue

        # Dernier recours: tenter de lister les modèles pour aider au debug.
        available = []
        try:
            available = self.list_models()
        except Exception:
            available = []

        # Si on a une liste de modèles, on tente automatiquement les premiers "gemini" disponibles.
        for m in [x for x in available if x.startswith("gemini")][:10]:
            try:
                raw = self._generate_with_model(model=m, prompt=prompt)
                text = _extract_text(raw)
                return GeminiResult(model=m, text=text, raw=raw)
            except Exception as e:
                last_err = e

        msg = (
            "Aucun modèle Gemini n’a répondu.\n"
            f"- Modèles testés: {', '.join(_dedupe(preferred))}\n"
            + (f"- Modèles disponibles (API): {', '.join(available[:20])}\n" if available else "")
            + "Vérifie que la clé API est valide, que l’API Generative Language est activée, et que le modèle choisi est autorisé."
        )
        raise RuntimeError(msg) from last_err

    def list_models(self) -> list[str]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
        r = self._session.get(url, timeout=30)
        r.raise_for_status()
        raw: dict[str, Any] = r.json()
        models = raw.get("models") or []
        out: list[str] = []
        for m in models:
            name = (m or {}).get("name")
            if isinstance(name, str) and name.startswith("models/"):
                out.append(name.replace("models/", ""))
        return out

    def _generate_with_model(self, *, model: str, prompt: str) -> dict[str, Any]:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "topP": 0.9,
                "maxOutputTokens": 900,
            },
        }
        r = self._session.post(url, json=payload, timeout=45)
        r.raise_for_status()
        return r.json()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


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

