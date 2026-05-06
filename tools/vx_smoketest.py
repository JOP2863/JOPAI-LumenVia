from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config_from_secrets_toml
from core.vertex_gemini import VertexGeminiClient


def main() -> None:
    cfg = load_config_from_secrets_toml(".streamlit/secrets.toml")
    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
    prompt = (
        "Écris en français une synthèse d'environ 250 mots. "
        "Fais au moins 220 mots. Ne réponds pas en une phrase."
    )
    res = vx.generate_text_auto(
        preferred_models=["gemini-2.5-flash", "gemini-2.0-flash", "gemini-pro-latest"],
        prompt=prompt,
    )
    print("MODEL:", res.model)
    print("LEN_CHARS:", len(res.text))
    print("LEN_WORDS:", len(res.text.split()))
    print("---")
    print(res.text)
    print("\nRAW_KEYS:", list(res.raw.keys()))
    c0 = (res.raw.get("candidates") or [{}])[0]
    print("CAND0_KEYS:", list(c0.keys()))
    print("FINISH_REASON:", c0.get("finishReason") or c0.get("finish_reason"))
    print("SAFETY_RATINGS:", c0.get("safetyRatings") or c0.get("safety_ratings"))


if __name__ == "__main__":
    main()

