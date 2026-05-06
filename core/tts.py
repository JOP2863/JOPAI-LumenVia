from __future__ import annotations

from dataclasses import dataclass

from google.cloud import texttospeech


@dataclass(frozen=True)
class TtsResult:
    audio_bytes: bytes
    voice: str
    audio_format: str


def synthesize_fr_mp3(
    *,
    tts: texttospeech.TextToSpeechClient,
    text: str,
    voice_name: str = "fr-FR-Standard-A",
    speaking_rate: float = 1.0,
) -> TtsResult:
    if not text.strip():
        raise ValueError("Texte vide pour TTS.")

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(language_code="fr-FR", name=voice_name)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speaking_rate,
    )
    response = tts.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    return TtsResult(audio_bytes=response.audio_content, voice=voice_name, audio_format="mp3")

