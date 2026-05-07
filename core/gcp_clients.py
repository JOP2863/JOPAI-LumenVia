from __future__ import annotations

from typing import Any, Mapping

from google.cloud import storage, vision
from google.oauth2 import service_account


def build_credentials(service_account_info: Mapping[str, Any]):
    info = dict(service_account_info or {})
    creds = service_account.Credentials.from_service_account_info(info)
    # Important: plusieurs APIs GCP (dont Vision) appliquent quota/facturation au "quota project".
    # Sans cela, on peut activer l’API sur un projet mais les requêtes sont comptées sur un autre.
    quota_project_id = str(info.get("quota_project_id") or info.get("project_id") or "").strip()
    if quota_project_id:
        try:
            creds = creds.with_quota_project(quota_project_id)
        except Exception:
            # Compat / tolérance : on garde les creds "bruts" si la méthode n’est pas dispo.
            pass
    return creds


def build_gcs_client(service_account_info: Mapping[str, Any]) -> storage.Client:
    creds = build_credentials(service_account_info)
    return storage.Client(credentials=creds, project=service_account_info.get("project_id"))


def build_tts_client(service_account_info: Mapping[str, Any]):
    try:
        from google.cloud import texttospeech  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Dépendance manquante: installe `google-cloud-texttospeech` pour activer le TTS."
        ) from e
    creds = build_credentials(service_account_info)
    return texttospeech.TextToSpeechClient(credentials=creds)


def build_vision_image_annotator_client(service_account_info: Mapping[str, Any]) -> vision.ImageAnnotatorClient:
    creds = build_credentials(service_account_info)
    return vision.ImageAnnotatorClient(credentials=creds)

