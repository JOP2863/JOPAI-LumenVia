from __future__ import annotations

import smtplib
import quopri
from dataclasses import dataclass
from email.message import EmailMessage
import re
from time import sleep


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool = True  # STARTTLS


_TAG_RE = re.compile(r"(?is)<[^>]+>")


def _strip_html(html: str) -> str:
    # Fallback texte simple si on n'a que du HTML.
    s = re.sub(r"(?i)<br\\s*/?>", "\n", html or "")
    s = re.sub(r"(?i)</p\\s*>", "\n\n", s)
    s = re.sub(_TAG_RE, "", s)
    return "\n".join([ln.rstrip() for ln in s.splitlines()]).strip()


def send_smtp_email(
    *,
    cfg: SmtpConfig,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    html_only: bool = False,
) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.from_email
    msg["To"] = to_email
    # Assure un affichage UTF‑8 correct (accents/œ) sur un maximum de clients.
    msg["Subject"] = str(subject or "")
    text_part = (body_text or "").strip()
    html_part = (body_html or "").strip() if body_html else ""
    if not text_part and html_part:
        text_part = _strip_html(html_part)

    # Force l'encodage + un transfer encoding robuste.
    msg.set_charset("utf-8")

    if html_part and html_only:
        # Forcer explicitement un email HTML (utile pour tests clients trop "texte").
        msg.set_content(html_part, subtype="html", charset="utf-8", cte="quoted-printable")
    else:
        msg.set_content(text_part or "", charset="utf-8", cte="quoted-printable")
        if html_part:
            msg.add_alternative(html_part, subtype="html", charset="utf-8", cte="quoted-printable")

    with smtplib.SMTP(cfg.host, int(cfg.port), timeout=25) as s:
        s.ehlo()
        if cfg.use_tls:
            s.starttls()
            s.ehlo()
        if cfg.username and cfg.password:
            s.login(cfg.username, cfg.password)
        s.send_message(msg)


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_phone_e164: str


def fetch_twilio_message_status(*, cfg: TwilioConfig, sid: str) -> dict[str, str]:
    """
    Retourne un statut lisible depuis Twilio (best effort).
    Keys: status, error_code, error_message.
    """
    sid0 = (sid or "").strip()
    if not sid0:
        return {"status": "", "error_code": "", "error_message": ""}
    try:
        from twilio.rest import Client  # type: ignore
    except Exception:
        return {"status": "", "error_code": "", "error_message": ""}
    try:
        cli = Client(cfg.account_sid, cfg.auth_token)
        m = cli.messages(sid0).fetch()
        return {
            "status": str(getattr(m, "status", "") or ""),
            "error_code": str(getattr(m, "error_code", "") or ""),
            "error_message": str(getattr(m, "error_message", "") or ""),
        }
    except Exception as e:
        # Exemple fréquent: 20404 "The requested resource ... was not found"
        msg = str(e) or ""
        not_found = "20404" in msg or "not found" in msg.lower()
        return {
            "status": "not_found" if not_found else "",
            "error_code": "20404" if not_found else "",
            "error_message": msg[:500],
        }


def send_twilio_sms(*, cfg: TwilioConfig, to_phone_e164: str, body_text: str) -> str:
    try:
        from twilio.rest import Client  # type: ignore
    except ModuleNotFoundError as e:
        import sys

        raise ModuleNotFoundError(
            "Le module `twilio` n'est pas installé dans l'environnement Python qui exécute l'app.\n"
            f"Python utilisé: {sys.executable}\n"
            "Corrige en installant Twilio dans CE Python:\n"
            f"  \"{sys.executable}\" -m pip install twilio\n"
            "Puis redémarre Streamlit."
        ) from e

    cli = Client(cfg.account_sid, cfg.auth_token)
    m = cli.messages.create(
        body=str(body_text or "")[:1500],
        from_=cfg.from_phone_e164,
        to=to_phone_e164,
    )
    sid = str(getattr(m, "sid", "") or "")
    # Best effort: laisse un court délai pour permettre à Twilio d'assigner un statut utile.
    if sid:
        try:
            sleep(0.35)
        except Exception:
            pass
    return sid

