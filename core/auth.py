from __future__ import annotations

import base64
import hashlib
import os


def hash_password(password: str, *, salt_b64: str | None = None, iterations: int = 210_000) -> tuple[str, str]:
    """
    Retourne (salt_b64, hash_b64) via PBKDF2-HMAC-SHA256.
    Pas de dépendance externe, suffisant pour un MVP.
    """
    if not password:
        raise ValueError("Mot de passe vide.")
    salt = base64.b64decode(salt_b64) if salt_b64 else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(dk).decode("ascii")


def verify_password(password: str, *, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt2, h2 = hash_password(password, salt_b64=salt_b64)
        return h2 == hash_b64 and salt2 == salt_b64
    except Exception:
        return False

