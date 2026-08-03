"""E-mail d’accueil compte LumenVia (création / init mot de passe côté admin)."""

from __future__ import annotations

from html import escape as html_escape

from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE
from core.outbound import SmtpConfig, send_smtp_email


def smtp_config_from_streamlit_secrets() -> SmtpConfig:
    """Lit SMTP depuis ``st.secrets`` (racine ou section ``smtp``)."""
    import streamlit as st

    def _get(*keys: str) -> str:
        try:
            s = st.secrets
        except Exception:
            return ""
        for k in keys:
            try:
                v = s.get(k)
            except Exception:
                v = None
            if v is not None and str(v).strip():
                return str(v).strip()
        try:
            block = s.get("smtp")
        except Exception:
            block = None
        if isinstance(block, dict):
            for k in keys:
                v = block.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
        return ""

    return SmtpConfig(
        host=_get("SMTP_HOST"),
        port=int(_get("SMTP_PORT") or 587),
        username=_get("SMTP_USER"),
        password=_get("SMTP_PASSWORD"),
        from_email=_get("SMTP_FROM"),
        use_tls=str(_get("SMTP_USE_TLS") or "true").strip().lower()
        not in ("0", "false", "no", "off"),
    )


def build_account_welcome_email(
    *,
    first_name: str,
    email: str,
    login_url: str,
    temp_password: str | None = None,
    reset_password_url: str | None = None,
) -> tuple[str, str, str]:
    """
    Retourne ``(subject, body_text, body_html)``.

    - Si ``temp_password`` : mot de passe provisoire communiqué (à changer après connexion).
    - Sinon ``reset_password_url`` : lien pour choisir un mot de passe.
    """
    who = (first_name or "").strip() or "ami(e)"
    login = (login_url or "").strip()
    account_url = ""
    if login:
        # Mon compte
        base = login.rstrip("/")
        if "route=" in base:
            account_url = base
        else:
            account_url = base + "/?route=account"

    subject = "LumenVia — Votre compte est prêt"

    lines: list[str] = [
        f"Bonjour {who},",
        "",
        "Un compte LumenVia a été créé pour toi.",
        "",
        "Pour te connecter :",
        f"1. Ouvre LumenVia : {login or account_url or 'l’application LumenVia'}",
        "2. Va dans « Mon compte » (ou « Se connecter »).",
        f"3. Identifiant : {email}",
    ]
    if temp_password:
        lines.extend(
            [
                f"4. Mot de passe provisoire : {temp_password}",
                "",
                "Important : change ce mot de passe dès ta première connexion "
                "(Mon compte → réinitialisation, ou le lien ci-dessous).",
            ]
        )
    elif reset_password_url:
        lines.extend(
            [
                "4. Choisis ton mot de passe via ce lien (valide quelques heures) :",
                reset_password_url,
            ]
        )
    else:
        lines.append(
            "4. Utilise « Réinitialiser le mot de passe » sur la page Mon compte si besoin."
        )

    lines.extend(
        [
            "",
            "Ensuite tu pourras :",
            "• consulter « La lumière du dimanche » et tes mémos,",
            "• gérer ton inscription à la newsletter,",
            "• modifier tes informations (dont la langue de consultation).",
            "",
            "À très bientôt sur LumenVia.",
            "",
            "—",
            LUMENVIA_DEVELOPMENT_NOTICE,
        ]
    )
    body_text = "\n".join(lines)

    # HTML minimal (même contenu)
    def _p(s: str) -> str:
        return f"<p style=\"margin:0 0 10px 0;line-height:1.5;\">{html_escape(s)}</p>"

    html_parts = [
        "<!doctype html><html><body style=\"font-family:Georgia,serif;color:#2F3640;\">",
        f"<p style=\"margin:0 0 12px 0;\"><strong>Bonjour {html_escape(who)},</strong></p>",
        _p("Un compte LumenVia a été créé pour toi."),
        _p("Pour te connecter :"),
        "<ol style=\"margin:0 0 12px 1.2rem;line-height:1.55;\">",
    ]
    if login or account_url:
        href = html_escape(login or account_url)
        html_parts.append(f"<li>Ouvre <a href=\"{href}\">LumenVia</a></li>")
    else:
        html_parts.append("<li>Ouvre l’application LumenVia</li>")
    html_parts.append("<li>Va dans « Mon compte » (ou « Se connecter »).</li>")
    html_parts.append(f"<li>Identifiant : <strong>{html_escape(email)}</strong></li>")
    if temp_password:
        html_parts.append(
            f"<li>Mot de passe provisoire : <strong>{html_escape(temp_password)}</strong></li>"
        )
        html_parts.append("</ol>")
        html_parts.append(
            _p(
                "Important : change ce mot de passe dès ta première connexion "
                "(Mon compte → réinitialisation)."
            )
        )
    elif reset_password_url:
        html_parts.append(
            f"<li>Choisis ton mot de passe via "
            f"<a href=\"{html_escape(reset_password_url)}\">ce lien</a> "
            f"(valide quelques heures).</li>"
        )
        html_parts.append("</ol>")
    else:
        html_parts.append(
            "<li>Utilise « Réinitialiser le mot de passe » sur Mon compte si besoin.</li>"
        )
        html_parts.append("</ol>")

    html_parts.extend(
        [
            _p("Ensuite tu pourras consulter le dimanche, gérer la newsletter et tes préférences."),
            _p("À très bientôt sur LumenVia."),
            "<hr style=\"border:none;border-top:1px solid #e7e5e4;margin:16px 0;\"/>",
            f"<p style=\"font-size:11px;color:#7F8C8D;\"><em>{html_escape(LUMENVIA_DEVELOPMENT_NOTICE)}</em></p>",
            "</body></html>",
        ]
    )
    body_html = "".join(html_parts)
    return subject, body_text, body_html


def send_account_welcome_email(
    *,
    to_email: str,
    first_name: str,
    login_url: str,
    temp_password: str | None = None,
    reset_password_url: str | None = None,
    smtp_cfg: SmtpConfig | None = None,
) -> None:
    cfg = smtp_cfg or smtp_config_from_streamlit_secrets()
    if not cfg.host or not cfg.from_email:
        raise RuntimeError("SMTP non configuré (SMTP_HOST / SMTP_FROM).")
    subject, body_text, body_html = build_account_welcome_email(
        first_name=first_name,
        email=to_email,
        login_url=login_url,
        temp_password=temp_password,
        reset_password_url=reset_password_url,
    )
    send_smtp_email(
        cfg=cfg,
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )
