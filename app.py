from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta
import re
import time
import unicodedata
from hashlib import sha256
from io import StringIO
from pathlib import Path
from html import escape as html_escape
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

import streamlit as st
import streamlit.components.v1 as components

from core.aelf import AelfClient
from core.local_aelf_cache import load_aelf_snapshot, persist_aelf_snapshot
from core.config import load_config
from core.gemini_tts_api import GeminiTtsApiClient
from core.gcp_clients import build_gcs_client, build_vision_image_annotator_client
from core.audio_utils import normalize_audio_bytes, join_wav_bytes
from core.auth import hash_password, verify_password
from core.vertex_gemini import VertexGeminiClient
from core.sheets_db import append_immutable_row, build_gspread_client, fetch_records, utc_now_iso
from core.storage import blob_exists, upload_text, upload_bytes, download_bytes
from core.local_bundle_cache import load_sunday_bundle, persist_sunday_bundle
from core.pdf_liturgy_sunday import build_liturgy_sunday_pdf_bytes
from core.illustration_text_audit import (
    all_errors_are_vision_service_disabled,
    audit_targets_for_text,
    existing_illustration_blob_path,
    extract_console_url_from_error,
    filter_rows_with_text,
    shorten_audit_error_message,
)
from core.illustration_thumbs import (
    THUMB_GCS_PREFIX,
    extract_gcp_project_id_from_error,
    gcs_thumb_path_from_source_blob,
    generate_thumb_from_source_and_upload,
    thumb_blob_exists,
    vision_console_activation_url,
)
from core.pdf_graine_parole_mensuel import build_graine_parole_monthly_pdf_bytes, strip_light_markdown_to_plain
from ui.liturgy_render import render_liturgy_block


def _public_app_listen_url(*, date_str: str) -> tuple[str | None, str | None]:
    """
    URL optionnelle pour le lien « Écouter » dans le PDF (secrets ``PUBLIC_APP_URL`` ou ``public_app_url``).
    Ajoute ``?sunday=YYYY-MM-DD`` pour ouvrir directement la page du dimanche si l’app gère ce paramètre.
    """
    try:
        s = st.secrets
        base = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip().rstrip("/")
    except Exception:
        base = ""
    if not base:
        return None, None
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}sunday={date_str[:10]}", None


def next_sunday(d: date) -> date:
    # Sunday = 6 (Mon=0)
    days_ahead = (6 - d.weekday()) % 7
    return d + timedelta(days=days_ahead or 7)


def _inject_viewport_meta() -> None:
    """Injecte ``<meta name="viewport" content="width=device-width, initial-scale=1">`` dans le <head> réel (parent document).

    Streamlit ne pose pas cette balise via ``st.set_page_config`` ; un ``components.html`` minimal exécute un script
    dans le document parent pour que le CSS mobile s’applique (évite le « dézoom » sur téléphone).
    """
    components.html(
        """
<script>
(function () {
  var doc = window.parent && window.parent.document ? window.parent.document : document;
  try {
    var m = doc.querySelector('meta[name="viewport"]');
    if (!m) {
      m = doc.createElement('meta');
      m.setAttribute('name', 'viewport');
      doc.head.appendChild(m);
    }
    m.setAttribute('content', 'width=device-width, initial-scale=1');
  } catch (e) {}
})();
</script>
        """,
        height=0,
        width=0,
    )


def loading_overlay(message: str = "LumenVia travaille pour toi…") -> object:
    """Calque plein écran (glassmorphism) pendant une opération serveur longue."""
    slot = st.empty()
    safe = html_escape(message or "")
    slot.markdown(
        f"""
<div id="lumenvia-loader-overlay" style="position:fixed;inset:0;background:rgba(253,251,247,0.88);backdrop-filter:blur(10px);z-index:999999;display:flex;align-items:center;justify-content:center;">
  <div style="font-family:'Cormorant Garamond',Georgia,serif;font-size:1.35rem;color:#342E29;text-align:center;max-width:min(520px,92vw);padding:1rem 1.25rem;border-bottom:2px solid #D4AF37;">
    ✨ {safe}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    return slot


def _french_month_year(d: date) -> str:
    mois = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    return f"{mois[d.month - 1].capitalize()} {d.year}"


def _fmt_created_fr(created_at: str) -> str:
    s = (created_at or "").strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        mois = (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        )
        return f"{dt.day} {mois[dt.month - 1]} {dt.year}"
    except Exception:
        return s[:10] if len(s) >= 10 else s


def _extract_liturgical_week_num(semaine: str | None) -> str | None:
    if not semaine:
        return None
    m = re.match(r"\s*(\d+)", semaine.strip())
    return m.group(1) if m else None


def _jour_liturgique(identity: object) -> str | None:
    v = getattr(identity, "jour_liturgique_nom", None)
    return (str(v).strip() if v else None) or None


def _memo_option_label(m: dict, ident: object | None) -> str:
    title = str(m.get("title") or "(sans titre)")
    if len(title) > 50:
        title = title[:47] + "…"
    created = _fmt_created_fr(str(m.get("created_at") or ""))
    if ident is not None:
        wn = _extract_liturgical_week_num(getattr(ident, "semaine", None))
        temps = (getattr(ident, "periode", None) or "").strip() or "—"
        semaine_txt = (getattr(ident, "semaine", None) or "").strip()
        if wn:
            head = f"Semaine {wn} · {temps}"
        elif semaine_txt:
            head = _liturgy_display_label(semaine_txt)
        else:
            head = temps
        return f"{head} · {title} · noté le {created}"
    ds = str(m.get("date") or "?")
    return f"{ds} · {title} · noté le {created}"


def _latest_subscription_record(subs: list[dict], user_entity_id: str, sub_type: str) -> dict | None:
    rows = [
        s
        for s in subs
        if str(s.get("user_entity_id", "")).strip() == user_entity_id and str(s.get("type", "")).strip() == sub_type
    ]
    if not rows:
        return None
    return sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]


def _subscription_is_active(sub: dict | None) -> bool:
    if not sub:
        return False
    return str(sub.get("active", "")).strip().lower() in ("true", "1", "oui", "yes", "active")


def _next_newsletter_send_caption() -> str:
    """Envoi hebdo annoncé le vendredi soir ; on compte jusqu’au prochain vendredi calendaire."""
    today = date.today()
    wd = today.weekday()
    delta = (4 - wd) % 7
    if delta == 0:
        return "Le prochain envoi est prévu **ce vendredi** en fin de journée (e-mail)."
    return f"Le prochain envoi est prévu **dans {delta} jour(s)** (vendredi en fin de journée, e-mail)."


def set_page_style() -> None:
    _icon = Path("assets/branding/favicon.png")
    page_icon: str | Path = str(_icon) if _icon.is_file() else "✨"
    st.set_page_config(page_title="JOPAI LumenVia", layout="centered", page_icon=page_icon)
    _inject_viewport_meta()
    st.markdown(
        """
<style>
/* 
   CSS Liturgique V4 - JOPAI Verbum 
   Correction spécifique des boutons sombres et widgets Streamlit
*/

@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;600&family=Lora:ital,wght@0,400;0,500;1,400&display=swap');

:root {
  --liturgie-gold: #D4AF37;
  --liturgie-cream: #FDFBF7;
  --liturgie-text: #342E29;
  --liturgie-accent: var(--liturgie-gold);
  /* Renforce le thème Streamlit (config.toml) pour widgets natifs */
  --primary-color: #D4AF37;
}

/* 1. Reset Global & Fond */
html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
  background-color: var(--liturgie-cream) !important;
  color: var(--liturgie-text) !important;
  font-family: 'Lora', serif !important;
}

@media (max-width: 1024px) {
  html, body {
    overflow-x: hidden !important;
  }
  /* Clavier vs saisie : quand un textarea est actif, réserver de la hauteur pour faire défiler la zone au-dessus du clavier */
  section[data-testid="stMain"] .block-container:has(textarea:focus) {
    padding-bottom: max(20vh, 12rem, env(safe-area-inset-bottom, 0px)) !important;
  }
}

/*
  Navigation principale (top_nav) : une rangée à 5 colonnes — popover « Menu » (mobile) + 4 boutons (desktop).
  Desktop (≥1025px) : masquer la colonne popover.
  Mobile (≤1024px) : masquer les 4 boutons, empiler / pleine largeur pour le déclencheur menu.
*/
@media (min-width: 1025px) {
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(5):last-child)
    > div[data-testid="column"]:first-child {
    display: none !important;
  }
}

@media (max-width: 1024px) {
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(5):last-child) {
    flex-direction: column !important;
    align-items: stretch !important;
  }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(5):last-child)
    > div[data-testid="column"]:first-child {
    width: 100% !important;
    max-width: 100% !important;
    flex: 1 1 auto !important;
  }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(5):last-child)
    > div[data-testid="column"]:not(:first-child) {
    display: none !important;
  }
  /* Boutons du menu dans le panneau popover (tactile) */
  div[data-testid="stPopoverBody"] button[kind="secondary"],
  [data-testid="stPopoverContent"] button[kind="secondary"],
  [data-baseweb="popover"] button[kind="secondary"] {
    width: 100% !important;
    min-height: 55px !important;
    font-size: 1rem !important;
  }
}

/* 2. Bouton Primaire (Le bouton "Générer") */
button[kind="primary"] {
  background-color: var(--liturgie-gold) !important;
  color: white !important;
  border: 1px solid var(--liturgie-gold) !important;
  border-radius: 0px !important;
  text-transform: uppercase !important;
  letter-spacing: 2px !important;
  font-weight: 600 !important;
  width: 100% !important;
  padding: 1rem !important;
  box-shadow: 0px 4px 10px rgba(212, 175, 55, 0.2) !important;
}

/* Menu: hauteur homogène + centrage vertical même si une ligne */
button[kind="secondary"] {
  min-height: 64px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
}
button[kind="secondary"] p {
  white-space: pre-line !important;
  text-align: center !important;
  line-height: 1.15 !important;
}

button[kind="primary"]:hover {
  background-color: #B8952D !important; /* Or plus profond */
  border-color: #B8952D !important;
}

/* 3. Boutons Secondaires (Navigation & Segmented Control) */
button[kind="secondary"], 
[data-testid="stBaseButton-segmented_control"],
[data-testid="stBaseButton-segmented_controlActive"] {
  border: 1px solid var(--liturgie-gold) !important;
  background-color: white !important;
  color: var(--liturgie-text) !important;
  border-radius: 0px !important;
}

/* État actif du Segmented Control (ex: "120 mots" sélectionné) */
[data-testid="stBaseButton-segmented_controlActive"] {
  background-color: var(--liturgie-gold) !important;
  color: white !important;
}

/* 4. Champs de saisie (Date, Textarea, Inputs) */
input, textarea, [data-baseweb="input"], [data-baseweb="textarea"] {
  background-color: white !important;
  color: var(--liturgie-text) !important;
  border: 1px solid rgba(212, 175, 55, 0.3) !important;
  font-family: 'Lora', serif !important;
}

/* Focus sur les inputs */
input:focus, textarea:focus {
  border-color: var(--liturgie-gold) !important;
  box-shadow: 0 0 0 1px var(--liturgie-gold) !important;
}

/* 5. Alertes (Success/Warning) - On les adoucit */
[data-testid="stAlert"] {
  background-color: rgba(255, 255, 255, 0.8) !important;
  border: 1px solid var(--liturgie-gold) !important;
  color: var(--liturgie-text) !important;
  border-radius: 0px !important;
}

/* 6. Titres et Structure */
h1, h2, h3 {
  font-family: 'Cormorant Garamond', serif !important;
  text-align: center;
  border-bottom: 1px solid rgba(212, 175, 55, 0.3);
  padding-bottom: 10px;
}

/* Lectures: conserve les retours à la ligne */
.liturgy-block {
  white-space: pre-wrap;
  line-height: 1.35;
  padding: 0.10rem 0.15rem;
}

/* Style spécifique pour les textes de lecture AELF */
.liturgical-reading {
    font-family: 'Lora', serif !important;
    line-height: 1.8 !important; /* Plus d'espace entre les lignes pour la méditation */
    color: var(--liturgie-text);
    text-align: justify;
    white-space: pre-line; /* CRITIQUE : Respecte les retours à la ligne de l'API */
    padding: 20px;
    background-color: rgba(255, 255, 255, 0.3);
    border-left: 3px solid var(--liturgie-gold); /* Rappel élégant sur le côté */
    margin: 1.5rem 0;
}

/* Mise en avant des premiers mots (Incipit) */
.liturgical-reading::first-line {
    font-variant: small-caps;
    font-weight: bold;
    color: var(--liturgie-gold);
}

/* Neutralisation des doubles sauts de ligne */
.liturgical-reading p {
    margin-bottom: 0px !important;
    margin-top: 0px !important;
    padding: 0px !important;
    line-height: 1.6 !important;
}

/* On garde un petit espace uniquement entre les grands blocs de texte
   si Cursor utilise des doubles sauts de ligne dans son nettoyage */
.liturgical-reading {
    white-space: normal !important; /* On laisse le HTML (<p>) gérer les lignes */
    line-height: 1.6 !important;
}

/* Bloc d'URL / Code - Style Parchemin */
div[data-testid="stCodeBlock"] {
  border: 1px solid rgba(212, 175, 55, 0.3) !important;
  border-radius: 4px !important;
}

div[data-testid="stCodeBlock"] pre {
  background: #F4F0E6 !important; /* Couleur vieux papier */
  color: #5D4037 !important;
}

/* Éléments de structure */
hr {
  border-top: 1px double var(--liturgie-gold) !important;
  opacity: 0.4;
  margin: 2rem 0 !important;
}

[data-testid="stSidebar"], [data-testid="collapsedControl"] {
  display: none;
}

/* Suppression des bordures sombres sur l'audio */
audio {
  filter: sepia(20%) contrast(90%);
  width: 100%;
}

/* 7. Correction radicale — Expanders & Selectbox */

[data-testid="stExpander"] summary {
  background-color: white !important;
  color: var(--liturgie-text) !important;
  border: 1px solid rgba(212, 175, 55, 0.3) !important;
  transition: background-color 0.3s ease !important;
}

[data-testid="stExpander"] summary:hover {
  background-color: var(--liturgie-cream) !important;
}

[data-testid="stExpander"] summary svg {
  fill: var(--liturgie-gold) !important;
}

[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
  background-color: white !important;
  color: var(--liturgie-text) !important;
  border-radius: 0px !important;
}

[data-testid="stSelectbox"] div[data-testid="stMarkdownContainer"] p {
  color: var(--liturgie-text) !important;
}

[data-testid="stSelectbox"] svg {
  fill: var(--liturgie-gold) !important;
}

[data-testid="stExpander"] {
  background-color: white !important;
  border: none !important;
}

[data-testid="stExpanderDetails"] {
  background-color: var(--liturgie-cream) !important;
  border: 1px solid rgba(212, 175, 55, 0.2) !important;
  border-top: none !important;
  padding: 1rem !important;
}

/* Légendes : contraste suffisant sur fond crème (évite gris fantôme) */
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p,
[data-testid="stCaptionContainer"] span {
  color: #342E29 !important;
  opacity: 0.92 !important;
  font-size: 0.88rem !important;
}

/* Onglets : libellés toujours lisibles (inactif ≠ gris fantôme sur crème) */
div[data-testid="stTabs"] button[data-baseweb="tab"],
div[data-testid="stTabs"] [role="tab"],
[data-testid="stTabs"] button[role="tab"] {
  color: #342E29 !important;
  opacity: 1 !important;
}
div[data-testid="stTabs"] [aria-selected="false"] {
  color: #342E29 !important;
  opacity: 0.88 !important;
}
div[data-testid="stTabs"] [aria-selected="true"] {
  color: #342E29 !important;
  opacity: 1 !important;
  font-weight: 600 !important;
}

/* Cases à cocher + libellés de widgets : contraste texte */
[data-testid="stCheckbox"] label,
[data-testid="stCheckbox"] label p,
[data-testid="stCheckbox"] span,
[data-testid="stCheckbox"] [data-testid="stMarkdownContainer"] p,
[data-testid="stCheckbox"] div[data-testid="stMarkdownContainer"] {
  color: #342E29 !important;
}
label[data-testid="stWidgetLabel"] p,
label[data-testid="stWidgetLabel"] span {
  color: #342E29 !important;
}

/* Libellé widget : pas de bandeau « bouton » (fond bleu / texte blanc) surtout avec radio horizontal */
label[data-testid="stWidgetLabel"] {
  background-color: transparent !important;
  color: #342E29 !important;
}

/* Radio : options lisibles, fonds neutres (corrige point noir seul = texte blanc sur blanc) */
[data-testid="stRadio"] div[role="radiogroup"],
[data-testid="stRadio"] [data-testid="column"],
[data-testid="stRadio"] section {
  background-color: transparent !important;
}

[data-testid="stRadio"] label,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] label span,
[data-testid="stRadio"] [data-testid="stMarkdownContainer"] p,
[data-testid="stRadio"] [data-testid="column"] p,
[data-testid="stRadio"] [data-testid="column"] span {
  color: #342E29 !important;
  background-color: transparent !important;
}

[data-testid="stRadio"] svg circle,
[data-testid="stRadio"] svg path {
  fill: var(--liturgie-gold) !important;
}

/* Slider : valeur + curseur alignés sur l’or (évite rouge accent système) */
[data-testid="stSlider"] [data-testid="stThumbValue"] {
  color: #342E29 !important;
}

[data-testid="stSlider"] [role="slider"],
[data-testid="stSlider"] div[data-baseweb="slider"] [role="slider"] {
  background-color: var(--liturgie-gold) !important;
  border: 2px solid #B8952D !important;
}

[data-testid="stSlider"] div[data-baseweb="slider"] [data-baseweb="thumb"] {
  background-color: var(--liturgie-gold) !important;
  border: 2px solid #B8952D !important;
}

/* Number input : pas de bloc +/- noir ; charte or */
[data-testid="stNumberInput"] button {
  background-color: var(--liturgie-gold) !important;
  color: #FFFFFF !important;
  border: 1px solid #B8952D !important;
}

[data-testid="stNumberInput"] button:hover {
  background-color: #B8952D !important;
}

[data-testid="stNumberInput"] input {
  color: #342E29 !important;
}

/* Toggle (ex. mode debug) : état actif en or */
[data-testid="stToggle"] label span,
[data-testid="stToggle"] label p {
  color: #342E29 !important;
}

[data-testid="stToggle"] div[data-baseweb="switch"] [aria-checked="true"] {
  background-color: var(--liturgie-gold) !important;
}

/* Étape identité visuelle : navigation compacte sur petit écran */
@media (max-width: 520px) {
  button[kind="secondary"] {
    min-height: 56px !important;
    font-size: 0.78rem !important;
    padding: 0.35rem 0.25rem !important;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )


def top_nav() -> str:
    if "route" not in st.session_state:
        st.session_state.route = "about"

    logo_path = Path("assets/branding/logo_mark.svg")
    if logo_path.is_file():
        _, mid, _ = st.columns([1, 1, 1])
        with mid:
            st.image(str(logo_path), width=56)

    labels = [
        ("about", "JOPAI LumenVia :\nC’est quoi ?"),
        ("sunday", "La Lumière du\nDimanche"),
        ("memo", "Mon Aide-\nMémoire"),
        ("join", "Nous\nrejoindre"),
    ]

    cols = st.columns([1, 1, 1, 1, 1], gap="small")
    with cols[0]:
        with st.popover("Menu", use_container_width=True):
            for route, label in labels:
                short = label.replace("\n", " ")
                if st.button(short, key=f"nav_m_{route}", use_container_width=True, type="secondary"):
                    st.session_state.route = route
    for i, (route, label) in enumerate(labels):
        with cols[i + 1]:
            if st.button(label, key=f"nav_d_{route}", use_container_width=True, type="secondary"):
                st.session_state.route = route

    uid = str(st.session_state.get("auth_user_entity_id") or "").strip()
    email = str(st.session_state.get("auth_email_lc") or "").strip()
    if uid:
        b1, b2 = st.columns([4, 1], gap="small")
        with b1:
            st.caption(f"🟢 Connecté · {email or 'session active'}")
        with b2:
            if st.button("Déconnexion", key="auth_logout_nav"):
                for k in ("auth_user_entity_id", "auth_email_lc"):
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.pop("admin_authenticated", None)
                st.session_state.pop("admin_phone_preview", None)
                st.rerun()

    admin_nav_bar()

    return st.session_state.route


def _admin_login_and_password() -> tuple[str, str]:
    """Identifiant et mot de passe administrateur (`.streamlit/secrets.toml` ou valeurs par défaut)."""
    try:
        s = st.secrets
        login = str(s.get("ADMIN_LOGIN", s.get("admin_login", "jop"))).strip().lower()
        password = str(s.get("ADMIN_PASSWORD", s.get("admin_password", "JOP28")))
    except Exception:
        login, password = "jop", "JOP28"
    return login, password


def admin_nav_bar() -> None:
    """Menu complémentaire réservé à la session administrateur (après connexion)."""
    if not st.session_state.get("admin_authenticated"):
        return
    st.markdown("---")
    st.caption("Administration")
    acols = st.columns([2, 2, 2, 2, 2, 1], gap="small")
    with acols[0]:
        if st.button("Visuels liturgiques", key="adm_nav_step3", use_container_width=True):
            st.session_state.route = "admin_step3"
            st.rerun()
    with acols[1]:
        if st.button("Vignettes GCS", key="adm_nav_thumbs", use_container_width=True):
            st.session_state.route = "admin_thumbs"
            st.rerun()
    with acols[2]:
        if st.button("Cahier des charges", key="adm_nav_cdc", use_container_width=True):
            st.session_state.route = "admin_cdc"
            st.rerun()
    with acols[3]:
        if st.button("Test ressources", key="adm_nav_res", use_container_width=True):
            st.session_state.route = "admin_resources"
            st.rerun()
    with acols[4]:
        if st.button("Plan consolidé", key="adm_nav_plan", use_container_width=True):
            st.session_state.route = "admin_plan"
            st.rerun()
    with acols[5]:
        if st.button("Quitter session admin", key="adm_nav_logout", use_container_width=True):
            st.session_state.pop("admin_authenticated", None)
            st.session_state.pop("admin_phone_preview", None)
            st.session_state.route = "about"
            st.rerun()
    st.toggle(
        "Aperçu mobile 390px",
        key="admin_phone_preview",
        help="Force la largeur type iPhone (~390px) et un cadre arrondi pour tester le rendu liturgique sur bureau.",
    )


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_key(s: str | None) -> str:
    t = _strip_accents((s or "").strip().lower())
    return "".join(ch if ch.isalnum() else "_" for ch in t).strip("_")


def _explain_liturgical_time(periode: str | None) -> str:
    k = _norm_key(periode)
    hints: dict[str, str] = {
        "avent": "Temps de préparation à la venue du Seigneur : conversion douce, veille et espérance.",
        "noel": "Temps qui célèbre l’Incarnation : la Parole faite chair parmi nous.",
        "temps_ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "ordinaire": "Temps « au milieu » des grandes fêtes : croissance discrète et fidélité au quotidien.",
        "careme": "Temps de préparation pascale : prière, jeûne (intérieur) et partage.",
        "saint": "Mémoire ou fête d’un saint : exemplarité concrète de la foi.",
        "pascal": "Temps pascal : les cinquante jours qui prolongent la joie de la Résurrection jusqu’à la Pentecôte.",
        "pentecote": "Solennité de l’effusion de l’Esprit Saint sur l’Église.",
    }
    if k in hints:
        return hints[k]
    if "pentecot" in k:
        return hints["pentecote"]
    return "Grand mouvement liturgique qui colore la prière et la lecture de la Parole ce jour-là."


def _explain_liturgical_color(couleur: str | None) -> str:
    k = _norm_key(couleur)
    hints: dict[str, str] = {
        "blanc": "Couleur de joie et de gloire : grandes fêtes du Seigneur et de Marie (selon le temps).",
        "vert": "Couleur du Temps Ordinaire : vie chrétienne qui grandit dans la fidélité.",
        "rouge": "Couleur du martyre et de l’Esprit : don total et charité jusqu’au bout.",
        "violet": "Couleur de pénitence et d’attente : conversion et préparation (Avent/Carême selon le temps).",
        "rose": "Couleur d’allégement ponctuel au milieu de l’attente (Guadete / Laetare).",
        "noir": "Solennité funéraire ou jour marqué par le deuil liturgique.",
    }
    return hints.get(k, "La couleur vestimentaire traduit visuellement le climat liturgique du jour.")


def _explain_liturgical_cycle(annee: str | None) -> str:
    k = _norm_key(annee)
    hints: dict[str, str] = {
        "a": "Année A : le dimanche met souvent en avant l’Évangile selon Matthieu.",
        "b": "Année B : le dimanche met souvent en avant l’Évangile selon Marc.",
        "c": "Année C : le dimanche met souvent en avant l’Évangile selon Luc.",
        "annee_i": "Année des lectures propres au Temps Ordinaire (Année I).",
        "annee_ii": "Année des lectures propres au Temps Ordinaire (Année II).",
        "i": "Année des lectures propres au Temps Ordinaire (Année I).",
        "ii": "Année des lectures propres au Temps Ordinaire (Année II).",
    }
    return hints.get(k, "Le cycle liturgique fait tourner les lectures dominicales pour nourrir la foi sur plusieurs années.")


def _liturgical_accent_hex(couleur: str | None) -> str:
    k = _norm_key(couleur)
    palette: dict[str, str] = {
        "vert": "#27AE60",
        "violet": "#8E44AD",
        "blanc": "#D4AF37",
        "rouge": "#C0392B",
        "rose": "#C0879C",
        "noir": "#2C3E50",
    }
    return palette.get(k, "#D4AF37")


def _inject_liturgical_accent_style(couleur: str | None) -> None:
    hx = _liturgical_accent_hex(couleur)
    if not re.match(r"^#[0-9A-Fa-f]{6}$", hx):
        hx = "#D4AF37"
    st.markdown(
        f"""
<style>
:root {{
  --liturgie-accent: {hx};
}}
button[kind="primary"] {{
  background-color: var(--liturgie-accent) !important;
  border-color: var(--liturgie-accent) !important;
}}
[data-testid="stBaseButton-segmented_controlActive"] {{
  background-color: var(--liturgie-accent) !important;
}}
.liturgical-reading {{
  border-left-color: var(--liturgie-accent) !important;
}}
button[kind="primary"]:hover {{
  filter: brightness(0.93);
}}
input:focus, textarea:focus {{
  border-color: var(--liturgie-accent) !important;
  box-shadow: 0 0 0 1px var(--liturgie-accent) !important;
}}
[data-testid="stAlert"] {{
  border-color: var(--liturgie-accent) !important;
}}
</style>
""",
        unsafe_allow_html=True,
    )


def _fmt_cached_at_human(iso_s: str) -> str:
    s = (iso_s or "").strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        mois = (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        )
        return f"{dt.day} {mois[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d} UTC"
    except Exception:
        return s[:19]


def _offline_cache_caption(cached_at: str) -> str:
    return f"Consultation hors-ligne (mise en cache le {_fmt_cached_at_human(cached_at)})."


def _random_takeaway_line(synthesis_text: str) -> str | None:
    t = synthesis_text or ""
    low = t.lower()
    idx = low.find("à retenir")
    if idx == -1:
        idx = low.find("a retenir")
    chunk = t[idx:] if idx != -1 else t
    bullets: list[str] = []
    for line in chunk.splitlines():
        s = line.strip()
        if len(s) < 4:
            continue
        if s.startswith(("- ", "• ", "* ", "– ")):
            bullets.append(s[2:].strip())
        else:
            for prefix in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."):
                if s.startswith(prefix):
                    bullets.append(s[len(prefix) :].strip())
                    break
    bullets = [b for b in bullets if len(b) > 8]
    if not bullets:
        return None
    return random.choice(bullets)


def _normalize_roman_liturgy_token(token: str) -> str:
    """
    Met en majuscules les nombres romains (AELF peut renvoyer « Iii », « Ii », etc.).
    Ne modifie que les jetons composés uniquement des lettres I, V, X, L, C, D, M.
    """
    if not token:
        return token
    prefix = ""
    suffix = ""
    core = token
    while core and not core[0].isalpha():
        prefix += core[0]
        core = core[1:]
    while core and not core[-1].isalpha():
        suffix = core[-1] + suffix
        core = core[:-1]
    if not core or any(not c.isalpha() for c in core):
        return token
    if len(core) > 15:
        return token
    if not all(c.upper() in "IVXLCDM" for c in core):
        return token
    return prefix + core.upper() + suffix


def _liturgy_display_label(s: str | None) -> str:
    """Majuscules d'usage (ex. Pascal, Blanc, Temps Ordinaire) ; articles courts en minuscules."""
    if not s or not str(s).strip():
        return "—"
    raw = str(s).strip().replace("_", " ")
    small = {"de", "du", "des", "la", "le", "les", "et", "à", "au", "aux", "en", "un", "une"}
    parts = raw.split()
    out: list[str] = []
    for i, p in enumerate(parts):
        lw = p.lower()
        if i > 0 and lw in small:
            out.append(lw)
        else:
            titled = p[:1].upper() + p[1:].lower() if p else ""
            out.append(_normalize_roman_liturgy_token(titled))
    return " ".join(out) if out else "—"


def _cycle_year_display(s: str | None) -> str:
    if not s or not str(s).strip():
        return "—"
    t = str(s).strip()
    if len(t) <= 2 and t.upper() in ("A", "B", "C"):
        return t.upper()
    return _liturgy_display_label(t)


def _fetch_existing_sunday_bundle(
    *,
    gs: object,
    gcs: object,
    cfg: object,
    date_str: str,
    zone: str,
) -> tuple[tuple[bytes, str] | None, str | None]:
    """Dernière génération du jour : (audio bytes, mime) + texte synthèse GCS (même ligne generations)."""
    try:
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=3000)
        gens_day = [
            g
            for g in gens
            if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
        ]
        if not gens_day:
            return None, None
        latest = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        gen_eid = str(latest.get("entity_id") or "").strip()
        if not gen_eid:
            return None, None

        syn_text: str | None = None
        tp = str(latest.get("text_gcs_path") or "").strip()
        if tp:
            try:
                syn_text = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=tp).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                syn_text = None

        audios = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=5000)
        aud_rows = [a for a in audios if str(a.get("gen_entity_id", "")).strip() == gen_eid]
        if not aud_rows:
            return None, syn_text
        aud = sorted(aud_rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        path = str(aud.get("gcs_path") or "").strip()
        if not path:
            return None, syn_text
        raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        mime_guess = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        b, mime, _ = normalize_audio_bytes(audio_bytes=raw, mime_type=mime_guess)
        return (b, mime), syn_text
    except Exception:
        return None, None


def _fetch_liturgy_illustration_display_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """Vignette ``Images/thumbs`` si présente, sinon image pleine taille (affiches / grille)."""
    year = date_str[:4]
    thumb_path = f"{THUMB_GCS_PREFIX}/{year}/{date_str}.webp"
    try:
        return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=thumb_path)
    except Exception:
        pass
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        path = f"Images/illustrations/{year}/{date_str}{ext}"
        try:
            return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        except Exception:
            continue
    return None


def _fetch_liturgy_illustration_full_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """Image pleine résolution (ex. couverture PDF), sans passer par la vignette."""
    year = date_str[:4]
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        path = f"Images/illustrations/{year}/{date_str}{ext}"
        try:
            return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        except Exception:
            continue
    return None


def _try_show_liturgy_illustration(*, gcs: object, cfg: object, date_str: str) -> None:
    """Étape produit 3 : affiche une image si présente dans GCS (vignette ou originale)."""
    img_b = _fetch_liturgy_illustration_display_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
    if img_b:
        st.image(io.BytesIO(img_b), use_container_width=True)
        st.caption("Illustration du dimanche")


def _french_long_date_label(date_str: str) -> str:
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
    mois = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    return f"{jours[d.weekday()].capitalize()} {d.day} {mois[d.month - 1]} {d.year}"


def _liturgy_cover_pdf_title(identity: object) -> str:
    wn = _extract_liturgical_week_num(getattr(identity, "semaine", None))
    temps = _liturgy_display_label(getattr(identity, "periode", None))
    if wn and temps and temps != "—":
        return f"Semaine {wn} · {temps}"
    if wn:
        return f"Semaine {wn}"
    if temps and temps != "—":
        return temps
    return "La Lumière du Dimanche"


@st.cache_data(show_spinner=False, ttl=3600)
def cached_aelf(date_str: str, zone: str = "france", *, _identity_schema: int = 4):
    """_identity_schema invalide le cache quand le dataclass AelfDayIdentity évolue."""
    c = AelfClient()
    identity = c.informations(date_str, zone=zone)
    texts = c.messes(date_str, zone=zone)
    return identity, texts


def render_about() -> None:
    st.title("JOPAI LumenVia")
    try:
        st.image("Parole.jpg", use_container_width=True)
    except Exception:
        pass

    st.markdown(
        """
« *Ta Parole est une lampe sur mes pas, une lumière sur mon sentier.* »

JOPAI LumenVia est un compagnon spirituel conçu pour vous aider à franchir le seuil de la célébration avec un cœur ouvert et une intelligence éclairée.  
Trop souvent, nous arrivons à la messe sans avoir eu le temps de déposer le bruit du monde. Ce site est une halte, un chemin de lumière (**LumenVia**) pour vous préparer à recevoir la Parole de Dieu.

**Pourquoi utiliser LumenVia ?**

- **Comprendre l’essentiel** : avec l'aide de l'Intelligence Artificielle, nous mettons en perspective les lectures du dimanche pour vous en offrir la synthèse. Il ne s’agit pas d’inventer, mais de souligner le fil rouge qui relie les textes entre eux.
- **Se préparer en chemin** : que vous préfériez lire ou écouter, LumenVia génère pour vous un résumé écrit et un audio. Écoutez la synthèse dans les transports ou en marchant vers l'église pour laisser l’esprit de la fête infuser en vous.
- **Vivre le temps liturgique** : de l’or du Temps Ordinaire au violet du Carême, l’application s’habille aux couleurs de l’Église pour vous aider à habiter pleinement chaque saison de l’année.

**Comment parcourir ce chemin ?**

- **La Lumière du Dimanche** : découvrez les textes du jour et leur synthèse pour nourrir votre méditation.
- **Mon Aide-Mémoire** : créez vos propres mémos pour garder une trace de ce qui a touché votre cœur.
- **Nous rejoindre** : abonnez-vous pour recevoir chaque vendredi soir votre préparation dominicale directement par e-mail, ou par SMS.

Puisse cet outil vous aider à transformer chaque messe en une rencontre plus profonde et plus consciente avec le Christ.
        """.strip()
    )
    st.subheader("Référence")
    st.markdown(
        "Source liturgique : AELF (Association Épiscopale Liturgique pour les pays Francophones). "
        "[AELF API](https://api.aelf.org/)"
    )


def render_sunday() -> None:
    st.title("La Lumière du Dimanche")
    zone = "france"

    default = next_sunday(date.today())
    if "_lumenvia_sunday_qs" in st.session_state:
        try:
            default = date.fromisoformat(str(st.session_state.pop("_lumenvia_sunday_qs"))[:10])
        except Exception:
            pass
    chosen = st.date_input("Choisir le dimanche", value=default)
    date_str = chosen.isoformat()

    offline = False
    cached_at = ""
    with st.spinner("Récupération des lectures (AELF)…"):
        try:
            identity, texts = cached_aelf(date_str, zone=zone, _identity_schema=4)
            persist_aelf_snapshot(date_str, zone, identity, texts)
        except Exception:
            snap = load_aelf_snapshot(date_str, zone)
            if not snap:
                st.error(
                    "Impossible de joindre l’API AELF pour ce jour, et aucune copie locale n’est encore disponible. "
                    "Réessaie avec du réseau, ou choisis une date déjà consultée récemment sur cet appareil."
                )
                return
            identity, texts, cached_at = snap
            offline = True

    _inject_liturgical_accent_style(getattr(identity, "couleur", None))
    if offline:
        st.caption(_offline_cache_caption(cached_at))

    cfg = load_config()
    bundle_audio: tuple[bytes, str] | None = None
    bundle_synth_text: str | None = None
    bundle_from_disk = False
    gcs_top = None
    if cfg.gcp_service_account and cfg.gsheet_id and cfg.gcs_bucket_name:
        try:
            gs_top = build_gspread_client(cfg.gcp_service_account)
            gcs_top = build_gcs_client(cfg.gcp_service_account)
            bundle_audio, bundle_synth_text = _fetch_existing_sunday_bundle(
                gs=gs_top, gcs=gcs_top, cfg=cfg, date_str=date_str, zone=zone
            )
            if bundle_audio or (bundle_synth_text or "").strip():
                persist_sunday_bundle(
                    date_str=date_str,
                    zone=zone,
                    synth_text=bundle_synth_text,
                    audio_bytes=bundle_audio[0] if bundle_audio else None,
                    audio_mime=bundle_audio[1] if bundle_audio else None,
                )
        except Exception:
            bundle_audio, bundle_synth_text = None, None

    if not bundle_audio and not (bundle_synth_text or "").strip():
        disk_bundle = load_sunday_bundle(date_str, zone)
        if disk_bundle:
            bundle_synth_text, aud_b, aud_mime, _disk_at = disk_bundle
            bundle_from_disk = True
            if aud_b and aud_mime:
                bundle_audio = (aud_b, aud_mime)

    st.subheader("Identité du jour")
    col_id, col_aud = st.columns([3, 2], gap="medium")
    with col_id:
        fete_raw = (identity.fete or "").strip() or (_jour_liturgique(identity) or "").strip()
        fete_line = _liturgy_display_label(fete_raw) if fete_raw else "—"
        st.markdown(
            f"<div style='font-size:0.95rem;line-height:1.45;color:var(--liturgie-text);'>"
            f"<strong>{identity.date}</strong> · {_liturgy_display_label(identity.periode)} · "
            f"Cycle {_cycle_year_display(identity.annee)} · {_liturgy_display_label(identity.couleur)}"
            f"<br/><span style='opacity:0.9'>Fête / mémoire : {html_escape(fete_line)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        with st.expander("Détails sur le temps liturgique", expanded=True):
            st.markdown(f"**Temps** : {_explain_liturgical_time(identity.periode)}")
            st.markdown(f"**Cycle** : {_explain_liturgical_cycle(identity.annee)}")
            couleur_nom = _liturgy_display_label(identity.couleur)
            st.markdown(
                f"**Couleur** : **{couleur_nom}** — {_explain_liturgical_color(identity.couleur)}"
            )
    with col_aud:
        if bundle_audio:
            st.caption(
                "Synthèse en cache sur cet appareil"
                if bundle_from_disk
                else "Synthèse audio déjà générée"
            )
            st.audio(bundle_audio[0], format=bundle_audio[1])
        if bundle_synth_text or bundle_audio:
            with st.expander("Lire le texte de cette synthèse", expanded=False):
                if bundle_synth_text:
                    st.markdown(bundle_synth_text)
                else:
                    st.info(
                        "Le texte de la synthèse n’est pas disponible (GCS ou cache local). "
                        "Vérifie `text_gcs_path` dans la table generations si tu utilises le cloud."
                    )

    if gcs_top and cfg.gcs_bucket_name:
        _try_show_liturgy_illustration(gcs=gcs_top, cfg=cfg, date_str=date_str)

    st.subheader("Lectures")
    total_words = _count_words(
        (texts.premiere_lecture or "")
        + "\n"
        + (texts.psaume or "")
        + "\n"
        + (texts.deuxieme_lecture or "")
        + "\n"
        + (texts.evangile or "")
    )
    st.caption(f"Total lectures : **{total_words} mots** (AELF)")
    render_liturgy_block("Première lecture", texts.premiere_lecture)
    render_liturgy_block("Psaume", texts.psaume)
    render_liturgy_block("Deuxième lecture", texts.deuxieme_lecture)
    render_liturgy_block("Évangile", texts.evangile)

    if gcs_top and cfg.gcs_bucket_name:
        prep_key = f"prep_liturgy_pdf_{date_str}"
        pdf_key = f"liturgy_sunday_pdf_{date_str}"
        pc1, pc2 = st.columns(2)
        with pc1:
            if st.button("Préparer le PDF du dimanche (complet)", key=prep_key):
                ov_pdf = loading_overlay("Préparation du PDF (couverture + lectures + synthèse)…")
                try:
                    img_b = _fetch_liturgy_illustration_full_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
                    aud_url, aud_note = _public_app_listen_url(date_str=date_str)
                    pdf_b = build_liturgy_sunday_pdf_bytes(
                        image_bytes=img_b,
                        week_title=_liturgy_cover_pdf_title(identity),
                        date_line=_french_long_date_label(date_str),
                        premiere_lecture=texts.premiere_lecture,
                        psaume=texts.psaume,
                        deuxieme_lecture=texts.deuxieme_lecture,
                        evangile=texts.evangile,
                        synthesis_text=bundle_synth_text,
                        audio_listen_url=aud_url,
                        audio_listen_note=aud_note,
                    )
                    st.session_state[pdf_key] = pdf_b
                finally:
                    ov_pdf.empty()
        with pc2:
            if st.session_state.get(pdf_key):
                st.download_button(
                    label="Télécharger le PDF",
                    data=st.session_state[pdf_key],
                    file_name=f"lumenvia_dimanche_{date_str}.pdf",
                    mime="application/pdf",
                    key=f"dl_{pdf_key}",
                )
        st.caption(
            "Le PDF inclut la couverture, les lectures AELF, la synthèse si elle existe, "
            "et un lien vers l’audio lorsque `PUBLIC_APP_URL` est défini dans les secrets."
        )

    st.divider()
    st.subheader("Synthèse (texte + audio)")

    pct = st.segmented_control(
        "Longueur (en % du total des lectures)",
        options=[10, 15, 20, 25, 30, 35, 40, 45, 50],
        default=20,
        format_func=lambda x: f"{x}%",
    )
    include_takeaways = st.checkbox("Inclure “À retenir” (3–5 points)", value=True)
    debug = st.toggle("Mode debug", value=False)

    if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
        st.warning("Configuration incomplète (service account / gsheet_id / bucket).")
        return

    if st.button("Générer la synthèse et l’audio", type="primary"):
        overlay = loading_overlay("LumenVia prépare la synthèse et l’audio…")
        try:
            _run_generate_sunday_flow(
                _overlay=overlay,
                identity=identity,
                texts=texts,
                zone=zone,
                total_words=total_words,
                pct=int(pct or 20),
                include_takeaways=bool(include_takeaways),
                debug=bool(debug),
                cfg=cfg,
            )
        finally:
            overlay.empty()


def _run_generate_sunday_flow(
    *,
    _overlay: object,
    identity: object,
    texts: object,
    zone: str,
    total_words: int,
    pct: int,
    include_takeaways: bool,
    debug: bool,
    cfg: object,
) -> None:
    target_words = max(80, int(total_words * (pct / 100.0)))
    instructions = Path("data/instructions_ia.md").read_text(encoding="utf-8")
    liturgical_context = "\n".join(
        [
            f"- Temps liturgique ({identity.periode or '—'}): {_explain_liturgical_time(identity.periode)}",
            f"- Couleur ({identity.couleur or '—'}): {_explain_liturgical_color(identity.couleur)}",
            f"- Année / cycle ({identity.annee or '—'}): {_explain_liturgical_cycle(identity.annee)}",
        ]
    )
    prompt = _build_prompt(
        instructions=instructions,
        length_words=int(target_words),
        include_takeaways=bool(include_takeaways),
        identity={
            "date": identity.date,
            "zone": identity.zone,
            "periode": identity.periode,
            "annee": identity.annee,
            "couleur": identity.couleur,
            "fete": identity.fete,
            "jour_liturgique_nom": _jour_liturgique(identity),
        },
        readings={
            "premiere_lecture": texts.premiere_lecture,
            "psaume": texts.psaume,
            "deuxieme_lecture": texts.deuxieme_lecture,
            "evangile": texts.evangile,
        },
        liturgical_context=liturgical_context,
    )

    source_hash = sha256(
        (identity.date + "|" + (texts.premiere_lecture or "") + "|" + (texts.psaume or "") + "|" + (texts.evangile or "")).encode(
            "utf-8"
        )
    ).hexdigest()

    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
    perf: dict[str, float | int | str] = {}
    with st.spinner("Génération IA (Gemini)…"):
        t0 = time.perf_counter()
        try:
            gen = vx.generate_text_auto(
                preferred_models=[
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-pro-latest",
                    "gemini-flash-latest",
                ],
                prompt=prompt,
            )
        except Exception as e:
            if debug:
                st.exception(e)
            else:
                st.error("Erreur lors de la génération de la synthèse. Active le mode debug pour détails.")
            return
        t1 = time.perf_counter()
        perf["vertex_text_s"] = round(t1 - t0, 3)

    if debug:
        usage = (gen.raw or {}).get("usageMetadata") or {}
        cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
        st.markdown("**Debug génération**")
        st.write(
            {
                "model": gen.model,
                "elapsed_s": perf.get("vertex_text_s"),
                "finishReason": cand0.get("finishReason"),
                "promptTokenCount": usage.get("promptTokenCount"),
                "candidatesTokenCount": usage.get("candidatesTokenCount"),
                "totalTokenCount": usage.get("totalTokenCount"),
                "text_chars": len(gen.text or ""),
                "text_words": len((gen.text or "").split()),
            }
        )

    if not gen.text.strip():
        st.error("Réponse IA vide.")
        return

    gcs = build_gcs_client(cfg.gcp_service_account)
    gs = build_gspread_client(cfg.gcp_service_account)

    gen_entity_id = sha256(f"{identity.date}|{zone}|{source_hash}".encode("utf-8")).hexdigest()[:24]

    text_path = f"Syntheses/{identity.date}/{gen_entity_id}.txt"
    ut0 = time.perf_counter()
    upload_text(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path, text=gen.text)
    perf["upload_text_s"] = round(time.perf_counter() - ut0, 3)

    row_gen = append_immutable_row(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table="generations",
        values_by_col={
            "entity_id": gen_entity_id,
            "date": identity.date,
            "zone": zone,
            "cycle": identity.annee or "",
            "season": identity.periode or "",
            "length": int(target_words),
            "prompt_version": "v1",
            "model": gen.model,
            "source_hash": source_hash,
            "text_gcs_path": text_path,
        },
    )

    audio_route = "vertex"
    with st.spinner("Synthèse audio (Vertex AI)…"):
        try:
            at0 = time.perf_counter()
            audio = vx.generate_audio_auto(
                preferred_models=[
                    "gemini-2.5-flash-preview-tts",
                    "gemini-2.5-pro-preview-tts",
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                ],
                text=gen.text,
                voice_name="Kore",
            )
            perf["audio_vertex_s"] = round(time.perf_counter() - at0, 3)
        except RuntimeError as e:
            # Cas courant: pas allowlist AUDIO sur Vertex.
            if "not allowlisted" in str(e).lower() or "allowlisted" in str(e).lower():
                if not cfg.gemini_api_key:
                    st.error(
                        "Audio indisponible via Vertex AI (compte non allowlist AUDIO). "
                        "Ajoute/valide GEMINI_API_KEY pour activer le fallback Gemini API TTS."
                    )
                    st.stop()
                # Fallback silencieux: Gemini API TTS (souvent limité par requête -> on chunk).
                audio_route = "gemini_api_chunked"
                ft0 = time.perf_counter()
                tts_api = GeminiTtsApiClient(api_key=cfg.gemini_api_key)
                chunks = _chunk_text_for_tts(gen.text, max_chars=900)
                perf["tts_chunks"] = len(chunks)
                wav_parts: list[bytes] = []
                tts_chunk_total_s = 0.0
                for ch in chunks:
                    ct0 = time.perf_counter()
                    tts_audio = tts_api.generate_audio(
                        model="gemini-2.5-flash-preview-tts",
                        text=ch,
                        voice_name="Kore",
                    )
                    tts_chunk_total_s += time.perf_counter() - ct0
                    b, mt, _ = normalize_audio_bytes(audio_bytes=tts_audio.audio_bytes, mime_type=tts_audio.mime_type)
                    # Normalise en wav (join_wav_bytes attend du wav)
                    if mt != "audio/wav":
                        b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
                    wav_parts.append(b)
                joined = join_wav_bytes(wav_parts)
                perf["audio_fallback_s"] = round(time.perf_counter() - ft0, 3)
                perf["tts_chunk_total_s"] = round(tts_chunk_total_s, 3)
                audio = type("AudioWrap", (), {})()
                audio.audio_bytes = joined
                audio.mime_type = "audio/wav"
                audio.model = "gemini-api-tts:chunked"
            else:
                raise

        if not getattr(audio, "audio_bytes", b""):
            st.error("Réponse audio vide.")
            st.stop()

    audio_bytes_norm, audio_mime_norm, audio_ext = normalize_audio_bytes(
        audio_bytes=getattr(audio, "audio_bytes", b""),
        mime_type=getattr(audio, "mime_type", None),
    )
    audio_path = f"Audio/{identity.date}/{gen_entity_id}.{audio_ext}"
    uat0 = time.perf_counter()
    upload_bytes(
        gcs=gcs,
        bucket_name=cfg.gcs_bucket_name,
        path=audio_path,
        data=audio_bytes_norm,
        content_type=audio_mime_norm,
    )
    perf["upload_audio_s"] = round(time.perf_counter() - uat0, 3)
    perf["audio_route"] = audio_route

    append_immutable_row(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table="audio",
        values_by_col={
            "entity_id": sha256(f"audio|{gen_entity_id}|{audio_path}".encode("utf-8")).hexdigest()[:24],
            "gen_entity_id": row_gen["entity_id"],
            "voice": "Kore",
            "format": audio_ext,
            "gcs_path": audio_path,
        },
    )

    persist_sunday_bundle(
        date_str=str(identity.date),
        zone=zone,
        synth_text=gen.text,
        audio_bytes=audio_bytes_norm,
        audio_mime=audio_mime_norm,
    )

    st.subheader("Résumé du temps liturgique")
    try:
        dt0 = time.perf_counter()
        txt_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path)
        txt = txt_bytes.decode("utf-8", errors="replace")
        perf["download_text_verify_s"] = round(time.perf_counter() - dt0, 3)
    except Exception as e:
        txt = f"[Erreur lecture GCS texte] {e}"
    st.text_area("Synthèse", value=txt, height=320)

    try:
        da0 = time.perf_counter()
        aud_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=audio_path)
        aud_play, aud_mime_play, _ = normalize_audio_bytes(audio_bytes=aud_bytes, mime_type=audio_mime_norm)
        perf["download_audio_verify_s"] = round(time.perf_counter() - da0, 3)
        st.subheader("Écouter le résumé")
        st.audio(aud_play, format=aud_mime_play)
    except Exception as e:
        st.error(f"Erreur lecture/lecture audio GCS: {e}")
    if debug:
        total_keys = (
            "vertex_text_s",
            "upload_text_s",
            "audio_vertex_s",
            "audio_fallback_s",
            "tts_chunk_total_s",
            "upload_audio_s",
            "download_text_verify_s",
            "download_audio_verify_s",
        )
        perf["perf_total_tracked_s"] = round(
            sum(float(perf.get(k) or 0) for k in total_keys if isinstance(perf.get(k), (int, float))),
            3,
        )
        st.markdown("**Chronométrage (debug)**")
        st.write(perf)


def render_memo() -> None:
    st.markdown(
        """
<style>
/*
  Mémo : marge basse par défaut (bouton « Enregistrer le mémo » / expander).
  Quand le textarea « Ton mémo » est actif, le padding renforcé est dans set_page_style (:has(textarea:focus), 20vh).
*/
@media (max-width: 1024px) {
  section[data-testid="stMain"] .block-container {
    padding-bottom: max(14rem, calc(env(safe-area-inset-bottom, 0px) + 11rem)) !important;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )
    st.title("Mon Aide-Mémoire")
    st.write("Crée et conserve tes mémos pour retrouver ce qui a touché ton cœur.")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
        st.warning("Configuration incomplète (service account / gsheet_id / bucket).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    gcs = build_gcs_client(cfg.gcp_service_account)

    if "auth_user_entity_id" not in st.session_state:
        st.session_state.auth_user_entity_id = ""
    if "auth_email_lc" not in st.session_state:
        st.session_state.auth_email_lc = ""

    user_entity_id = str(st.session_state.auth_user_entity_id or "").strip()

    st.subheader("Connexion")
    if user_entity_id:
        email_disp = str(st.session_state.get("auth_email_lc") or "").strip()
        st.caption(f"Session active pour **{email_disp or 'ton compte'}**.")
        if st.button("Se déconnecter", type="secondary"):
            for k in ("auth_user_entity_id", "auth_email_lc"):
                if k in st.session_state:
                    del st.session_state[k]
            st.session_state.pop("admin_authenticated", None)
            st.rerun()
    else:
        email = st.text_input("Email", key="auth_email").strip().lower()
        password = st.text_input("Mot de passe", type="password", key="auth_password")

    def _latest_user_record(users: list[dict], email_lc: str) -> dict | None:
        rows = [u for u in users if str(u.get("email", "")).strip().lower() == email_lc]
        if not rows:
            return None
        # append-only: on prend la dernière ligne créée (created_at)
        rows_sorted = sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)
        return rows_sorted[0]

    users = []
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=2000)
    except Exception:
        users = []

    if not user_entity_id:
        col_a, col_b = st.columns(2, gap="small")
        with col_a:
            if st.button("Se connecter", type="primary", disabled=not (email and password)):
                ov = loading_overlay("LumenVia vérifie tes identifiants…")
                try:
                    adm_login, adm_pwd = _admin_login_and_password()
                    if email.strip().lower() == adm_login and password == adm_pwd:
                        admin_canon = f"{adm_login}@admin.lumenvia"
                        st.session_state.auth_user_entity_id = sha256(
                            admin_canon.encode("utf-8")
                        ).hexdigest()[:24]
                        st.session_state.auth_email_lc = adm_login
                        st.session_state.admin_authenticated = True
                        st.success("Connecté (administrateur).")
                        st.rerun()
                    rec = _latest_user_record(users, email)
                    if not rec or not rec.get("password_salt_b64") or not rec.get("password_hash_b64"):
                        st.error("Compte introuvable ou mot de passe non défini. Utilise “Créer un compte”.")
                        return
                    ok = verify_password(
                        password,
                        salt_b64=str(rec.get("password_salt_b64")),
                        hash_b64=str(rec.get("password_hash_b64")),
                    )
                    if not ok:
                        st.error("Mot de passe incorrect.")
                        return
                    st.session_state.auth_user_entity_id = sha256(email.encode("utf-8")).hexdigest()[:24]
                    st.session_state.auth_email_lc = email
                    st.session_state.pop("admin_authenticated", None)
                    st.success("Connecté.")
                    st.rerun()
                finally:
                    ov.empty()
        with col_b:
            if st.button("Créer un compte", type="secondary", disabled=not (email and password)):
                ov = loading_overlay("LumenVia crée ton compte…")
                try:
                    salt_b64, hash_b64 = hash_password(password)
                    new_uid = sha256(email.encode("utf-8")).hexdigest()[:24]
                    append_immutable_row(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="users",
                        values_by_col={
                            "entity_id": new_uid,
                            "email": email,
                            "source": "streamlit",
                            "password_salt_b64": salt_b64,
                            "password_hash_b64": hash_b64,
                        },
                    )
                    st.session_state.auth_user_entity_id = new_uid
                    st.session_state.auth_email_lc = email
                    st.success("Compte créé et connecté.")
                    st.rerun()
                finally:
                    ov.empty()

        with st.expander("Mot de passe oublié — définir un nouveau mot de passe"):
            st.caption(
                "Réservé à la récupération de **ton** compte. Une nouvelle ligne « utilisateur » "
                "est ajoutée dans Sheets (append-only) avec le même identifiant ; la connexion utilise toujours la **dernière** ligne."
            )
            remail = st.text_input("E-mail du compte", key="pwd_reset_email").strip().lower()
            rp1 = st.text_input("Nouveau mot de passe", type="password", key="pwd_reset_p1")
            rp2 = st.text_input("Confirmer le mot de passe", type="password", key="pwd_reset_p2")
            if st.button("Enregistrer le nouveau mot de passe", key="pwd_reset_submit"):
                ov = loading_overlay("Mise à jour du mot de passe…")
                try:
                    if len(rp1) < 8:
                        st.error("Minimum 8 caractères.")
                    elif rp1 != rp2:
                        st.error("Les deux saisies ne correspondent pas.")
                    elif not remail:
                        st.error("Indique ton e-mail.")
                    else:
                        rec = _latest_user_record(users, remail)
                        if not rec:
                            st.error("Aucun compte trouvé pour cet e-mail.")
                        else:
                            uid = str(rec.get("entity_id") or "").strip()
                            if not uid:
                                st.error("Identifiant utilisateur invalide.")
                            else:
                                salt_b64, hash_b64 = hash_password(rp1)
                                append_immutable_row(
                                    gspread_client=gs,
                                    spreadsheet_id=cfg.gsheet_id,
                                    table="users",
                                    values_by_col={
                                        "entity_id": uid,
                                        "email": remail,
                                        "source": "password_reset",
                                        "password_salt_b64": salt_b64,
                                        "password_hash_b64": hash_b64,
                                    },
                                )
                                st.success("Nouveau mot de passe enregistré. Tu peux te connecter.")
                finally:
                    ov.empty()

    user_entity_id = str(st.session_state.auth_user_entity_id or "").strip()
    if not user_entity_id:
        st.info("Connecte-toi pour accéder à tes mémos.")
        return

    # Liste des mémos existants
    try:
        memos = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="memos", limit=500)
    except Exception:
        memos = []
    my_memos = [m for m in memos if str(m.get("user_entity_id", "")).strip() == user_entity_id]
    my_memos_sorted = sorted(my_memos, key=lambda r: str(r.get("created_at", "")), reverse=True)

    with st.expander("Mes mémos existants", expanded=bool(my_memos_sorted)):
        if not my_memos_sorted:
            st.write("Aucun mémo pour le moment.")
        else:
            slice_memos = my_memos_sorted[:30]
            dates_u = sorted({str(m.get("date") or "").strip() for m in slice_memos if str(m.get("date") or "").strip()})
            id_by_date: dict[str, object | None] = {}
            for ds in dates_u:
                try:
                    ident_i, _ = cached_aelf(ds, zone, _identity_schema=4)
                    id_by_date[ds] = ident_i
                except Exception:
                    id_by_date[ds] = None
            options = [_memo_option_label(m, id_by_date.get(str(m.get("date") or "").strip())) for m in slice_memos]
            idx = st.selectbox("Ouvrir un mémo", options=list(range(len(options))), format_func=lambda i: options[i])
            chosen = my_memos_sorted[idx]
            path = str(chosen.get("memo_gcs_path") or "").strip()
            if path:
                try:
                    content = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path).decode(
                        "utf-8", errors="replace"
                    )
                except Exception as e:
                    content = f"[Erreur lecture GCS] {e}"
                st.text_area("Contenu", value=content, height=260)
                st.caption(f"GCS: `{path}`")

    st.divider()
    st.subheader("Créer un nouveau mémo")

    zone = "france"
    chosen_date = st.date_input("Date (dimanche)", value=next_sunday(date.today()), key="memo_date")
    date_str = chosen_date.isoformat()

    default_title = f"Mémo — {date_str}"
    title = st.text_input("Titre", value=default_title, key="memo_title").strip()

    # Préremplissage: on déclenche un rerun et on charge AVANT d'instancier le widget memo_body.
    if "memo_prefill_requested" not in st.session_state:
        st.session_state.memo_prefill_requested = False
    if "memo_inspire_requested" not in st.session_state:
        st.session_state.memo_inspire_requested = False

    b_prefill, b_inspire = st.columns(2, gap="small")
    with b_prefill:
        if st.button("Pré-remplir avec la dernière synthèse du jour", type="secondary"):
            st.session_state.memo_prefill_requested = True
            st.session_state.memo_prefill_date = date_str
            st.rerun()
    with b_inspire:
        if st.button("S'inspirer de la synthèse (un point « À retenir »)", type="secondary"):
            st.session_state.memo_inspire_requested = True
            st.session_state.memo_inspire_date = date_str
            st.rerun()

    if st.session_state.get("memo_prefill_requested") and st.session_state.get("memo_prefill_date") == date_str:
        ov = loading_overlay("LumenVia charge la dernière synthèse…")
        try:
            gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=500)
            gens_day = [
                g
                for g in gens
                if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
            ]
            gens_day_sorted = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)
            if gens_day_sorted:
                p = str(gens_day_sorted[0].get("text_gcs_path") or "").strip()
                if p:
                    body_txt = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=p).decode(
                        "utf-8", errors="replace"
                    )
                    st.session_state["memo_body"] = body_txt
                    st.success("OK — synthèse chargée dans le mémo.")
            else:
                st.info("Aucune synthèse trouvée pour cette date.")
        except Exception as e:
            st.error(f"Impossible de pré-remplir: {e}")
        finally:
            ov.empty()
            st.session_state.memo_prefill_requested = False

    if st.session_state.get("memo_inspire_requested") and st.session_state.get("memo_inspire_date") == date_str:
        ov = loading_overlay("LumenVia extrait un point à retenir…")
        try:
            gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=500)
            gens_day = [
                g
                for g in gens
                if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
            ]
            gens_day_sorted = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)
            if gens_day_sorted:
                p = str(gens_day_sorted[0].get("text_gcs_path") or "").strip()
                if p:
                    syn = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=p).decode(
                        "utf-8", errors="replace"
                    )
                    pick = _random_takeaway_line(syn)
                    if pick:
                        st.session_state["memo_body"] = pick
                        st.success("Un point « À retenir » a été inséré dans ton mémo.")
                    else:
                        st.info(
                            "Aucune liste « À retenir » détectée dans cette synthèse. "
                            "Génère une synthèse avec l’option « À retenir », ou utilise le pré-remplissage complet."
                        )
            else:
                st.info("Aucune synthèse trouvée pour cette date.")
        except Exception as e:
            st.error(f"Impossible de charger la synthèse : {e}")
        finally:
            ov.empty()
            st.session_state.memo_inspire_requested = False

    body = st.text_area("Ton mémo", height=220, key="memo_body").strip()
    resolution = st.text_input(
        "Ma résolution (cette semaine)",
        max_chars=140,
        key="memo_resolution",
        placeholder="Une action concrète pour les jours qui viennent…",
    ).strip()

    if st.button("Enregistrer le mémo", type="primary", disabled=not (title and body)):
        ov = loading_overlay("LumenVia enregistre ton mémo…")
        try:
            memo_id = sha256(
                f"memo|{user_entity_id}|{date_str}|{title}|{body}|{resolution}".encode("utf-8")
            ).hexdigest()[:24]
            memo_path = f"Memos/{user_entity_id}/{date_str}/{memo_id}.md"
            md_body = body.rstrip()
            if resolution:
                md_body += "\n\n---\n\n**Ma résolution :** " + resolution
            upload_text(
                gcs=gcs,
                bucket_name=cfg.gcs_bucket_name,
                path=memo_path,
                text=md_body,
                content_type="text/markdown; charset=utf-8",
            )
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="memos",
                values_by_col={
                    "entity_id": memo_id,
                    "user_entity_id": user_entity_id,
                    "date": date_str,
                    "zone": zone,
                    "title": title,
                    "resolution": resolution,
                    "memo_gcs_path": memo_path,
                    "gen_entity_id": "",
                },
            )
            st.success("OK — mémo enregistré.")
        finally:
            ov.empty()

    st.divider()
    st.subheader("Export PDF — Graine de Parole")
    st.caption(
        "Source des mémos : lignes **memos** (Sheets) + fichier Markdown sur GCS ; les **résolutions** viennent du champ "
        "« Ma résolution » pour chaque ligne du mois."
    )
    today = date.today()
    default_month = today.replace(day=1)
    ref_pdf = st.date_input(
        "Mois à exporter (n’importe quel jour dans ce mois)",
        value=default_month,
        key="memo_pdf_month_pick",
    )
    ym_key = ref_pdf.strftime("%Y-%m")
    month_memos_pdf = sorted(
        [m for m in my_memos_sorted if str(m.get("date") or "").strip().startswith(ym_key)],
        key=lambda r: str(r.get("date") or ""),
    )
    st.caption(f"**{len(month_memos_pdf)}** mémo(s) trouvé(s) pour **{_french_month_year(ref_pdf)}**.")

    if st.button("Préparer le PDF du mois", type="secondary", key="memo_pdf_build_btn"):
        ov = loading_overlay("LumenVia compose le PDF mensuel…")
        try:
            items: list[dict] = []
            resolutions_pdf: list[tuple[str, str]] = []
            for m in month_memos_pdf:
                ds = str(m.get("date") or "").strip()[:10]
                title = str(m.get("title") or "Mémo").strip()
                res = str(m.get("resolution") or "").strip()
                if res:
                    resolutions_pdf.append((ds, res))
                body_raw = ""
                mp = str(m.get("memo_gcs_path") or "").strip()
                if mp:
                    try:
                        body_raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=mp).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception as ex:
                        body_raw = f"[Erreur lecture GCS] {ex}"
                items.append(
                    {
                        "title": title,
                        "date_str": ds,
                        "body_plain": strip_light_markdown_to_plain(body_raw),
                    }
                )
            pdf_bytes = build_graine_parole_monthly_pdf_bytes(
                month_label_fr=_french_month_year(ref_pdf),
                items=items,
                resolutions=resolutions_pdf,
            )
            st.session_state[f"memo_pdf_blob_{ym_key}"] = pdf_bytes
        except Exception as ex:
            st.exception(ex)
        finally:
            ov.empty()

    pdf_blob = st.session_state.get(f"memo_pdf_blob_{ym_key}")
    if pdf_blob:
        st.download_button(
            label=f"Télécharger le PDF ({ym_key})",
            data=pdf_blob,
            file_name=f"lumenvia_graine_parole_{ym_key}.pdf",
            mime="application/pdf",
            key=f"memo_pdf_dl_{ym_key}",
        )


def render_join() -> None:
    st.title("Nous rejoindre")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante — inscription indisponible.")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    users: list[dict] = []
    subs: list[dict] = []
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=4000)
        subs = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=4000)
    except Exception:
        pass

    if "join_email" not in st.session_state:
        st.session_state.join_email = ""
    auth_em = str(st.session_state.get("auth_email_lc") or "").strip()
    if auth_em and not str(st.session_state.join_email).strip():
        st.session_state.join_email = auth_em

    def _latest_user_by_email(email_lc: str) -> dict | None:
        rows = [u for u in users if str(u.get("email", "")).strip().lower() == email_lc]
        if not rows:
            return None
        return sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]

    email_in = st.text_input("Email", key="join_email")
    email_lc = email_in.strip().lower()
    uid = sha256(email_lc.encode("utf-8")).hexdigest()[:24] if email_lc else ""
    latest_sub = _latest_subscription_record(subs, uid, "weekly_friday") if uid else None
    already_in = bool(uid) and _subscription_is_active(latest_sub)

    if already_in:
        st.success(f"Tu es déjà inscrit à la lettre du vendredi pour **{email_lc}**.")
        st.markdown(_next_newsletter_send_caption())
        return

    st.write("Laisse ton e-mail pour recevoir le vendredi en fin de journée la synthèse du dimanche à venir.")
    consent = st.checkbox("J’accepte de recevoir ces e-mails (désinscription possible à tout moment).")
    if st.button("S’abonner", type="primary", disabled=not (email_in.strip() and consent)):
        ov = loading_overlay("LumenVia enregistre ton inscription…")
        should_refresh = False
        try:
            email_lc = email_in.strip().lower()
            user_entity_id = sha256(email_lc.encode("utf-8")).hexdigest()[:24]

            rec_u = _latest_user_by_email(email_lc)
            if not rec_u:
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="users",
                    values_by_col={
                        "entity_id": user_entity_id,
                        "email": email_lc,
                        "source": "newsletter",
                    },
                )
            latest_before = _latest_subscription_record(subs, user_entity_id, "weekly_friday")
            if _subscription_is_active(latest_before):
                st.info("Tu étais déjà inscrit — aucune nouvelle ligne nécessaire.")
            else:
                sub_entity = sha256(f"sub|{user_entity_id}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="subscriptions",
                    values_by_col={
                        "entity_id": sub_entity,
                        "user_entity_id": user_entity_id,
                        "type": "weekly_friday",
                        "zone": "france",
                        "length_pref": "250",
                        "active": "true",
                    },
                )
                should_refresh = True
        finally:
            ov.empty()
        if should_refresh:
            st.rerun()

    with st.expander("Pourquoi plusieurs lignes dans Google Sheets ?"):
        st.markdown(
            """
Les tables sont **append-only** : chaque événement peut créer une **nouvelle ligne** avec un nouvel historique,  
sans effacer les anciennes versions.

- **`users`** : identité stable par e-mail (`entity_id` = empreinte de l’e-mail).  
  On **n’ajoute** une ligne « utilisateur » **que si** aucune ligne n’existe encore pour cet e-mail (ex. première inscription newsletter sans compte mémo).

- **`subscriptions`** : préférences d’envoi (newsletter). C’est **ici** que l’abonnement hebdomadaire est stocké, relié à `user_entity_id`.

Si tu testes plusieurs fois « S’abonner », tu ne devrais plus voir de doublons inutiles dans **`users`** ; seule la table **`subscriptions`** reçoit une nouvelle ligne si tu réactives un abonnement après désactivation (futur).
            """.strip()
        )


def _admin_target_has_illustration(*, gcs: object, bucket_name: str, target: dict) -> bool:
    return _admin_first_existing_blob_path(gcs=gcs, bucket_name=bucket_name, target=target) is not None


def _admin_first_existing_blob_path(*, gcs: object, bucket_name: str, target: dict) -> str | None:
    cand: list[str] = []
    p0 = str(target.get("gcs_path_primary") or "").strip()
    if p0:
        cand.append(p0)
    for a in target.get("alternates") or []:
        s = str(a or "").strip()
        if s:
            cand.append(s)
    for path in cand:
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                return path
        except Exception:
            continue
    return None


def _admin_best_display_blob_path(*, gcs: object, bucket_name: str, target: dict) -> str | None:
    """Préfère la vignette ``Images/thumbs`` si elle existe, sinon le fichier illustration."""
    full = _admin_first_existing_blob_path(gcs=gcs, bucket_name=bucket_name, target=target)
    if not full:
        return None
    tp = gcs_thumb_path_from_source_blob(full)
    try:
        if blob_exists(gcs=gcs, bucket_name=bucket_name, path=tp):
            return tp
    except Exception:
        pass
    return full


def _admin_iso_week_label(date_str: str) -> str:
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
        return str(d.isocalendar()[1])
    except Exception:
        return "—"


def _admin_sort_targets_by_date(targets: list[dict]) -> list[dict]:
    return sorted(targets, key=lambda t: str(t.get("date") or ""))


def _admin_execute_image_generations(
    *,
    cfg: object,
    gcs: object,
    vx: VertexGeminiClient,
    to_run: list[dict],
    aspect: str,
    pause_s: float,
    dry_run: bool,
    preferred_models: list[str],
    skip_existing: bool,
) -> list[str]:
    lines: list[str] = []
    n = len(to_run)
    prog = st.progress(0.0)
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    for i, t in enumerate(to_run):
        ds = str(t.get("date") or "")
        if skip_existing and _admin_target_has_illustration(gcs=gcs, bucket_name=bucket, target=t):
            lines.append(f"Skip {ds} — fichier déjà présent.")
            prog.progress(min(1.0, (i + 1) / max(n, 1)))
            continue

        prompt = str(t.get("prompt_midjourney_style") or "").strip()
        if not prompt:
            tempo = str(t.get("temps_liturgique") or "").strip()
            col = str(t.get("couleur") or "").strip()
            prompt = (
                "Minimalist Catholic liturgical illustration, woodcut-inspired line art, "
                f"gold accent #D4AF37 on cream, serene, wordless symbolic scene; "
                f"season mood (no labels): {tempo or 'Sunday'}; palette mood: {col or 'gold'}."
            )
        prompt_final = _augment_vertex_illustration_prompt(prompt)

        overlay = loading_overlay(f"Illustration du dimanche {ds}…")
        try:
            try:
                img_res = vx.generate_image_auto(
                    preferred_models=preferred_models,
                    prompt=prompt_final,
                    aspect_ratio=aspect,
                )
            except Exception as ex:
                lines.append(f"KO {ds} — {ex}")
                prog.progress(min(1.0, (i + 1) / max(n, 1)))
                continue
        finally:
            overlay.empty()

        dest = _admin_pick_gcs_path_for_upload(t, img_res.mime_type)
        ct = img_res.mime_type if (img_res.mime_type or "").startswith("image/") else "image/png"

        if dry_run:
            st.image(io.BytesIO(img_res.image_bytes), caption=f"{ds} — {img_res.model}")
            lines.append(f"Dry-run OK {ds} — modèle {img_res.model}")
        else:
            try:
                upload_bytes(
                    gcs=gcs,
                    bucket_name=bucket,
                    path=dest,
                    data=img_res.image_bytes,
                    content_type=ct,
                )
                lines.append(f"OK {ds} → `gs://{bucket}/{dest}` ({img_res.model})")
            except Exception as ex:
                lines.append(f"Upload KO {ds} — {ex}")

        prog.progress(min(1.0, (i + 1) / max(n, 1)))
        if pause_s > 0 and i < n - 1:
            time.sleep(float(pause_s))

    prog.progress(1.0)
    return lines


def _admin_finish_generation_log(lines: list[str], *, dry_run: bool) -> None:
    if not lines:
        return
    log_txt = "\n".join(lines)
    st.text_area("Journal du lot", value=log_txt, height=min(260, 80 + 18 * max(len(lines), 1)))
    if any(ln.startswith("OK ") for ln in lines):
        st.success(
            "Au moins une image est enregistrée sur le bucket. Cherche les lignes **OK … → `gs://`** ci-dessus."
        )
    elif dry_run and lines:
        st.warning("Mode **aperçu seulement** : aucun fichier n’a été envoyé sur GCS.")


def _augment_vertex_illustration_prompt(base: str) -> str:
    """Consigne stricte anti-texte (les modèles orthographient très mal les mots dans l’image)."""
    prefix = (
        "CRITICAL ZERO-TEXT RULE — The image must contain NO glyphs at all: "
        "no letters, Latin or French words, evangelist names, liturgical titles, numbers, captions, "
        "subtitles, banners, speech bubbles, scrolls with writing, open books with visible lines, "
        "mock typography, watermarks, or logos. "
        "If any word appears it will be misspelled — therefore paint NO words and NO readable characters in any language. "
        "Show mood and theme only through wordless symbolism: figures without labels, landscape, abstract shapes, "
        "crosses, bread/grapes as icons without text. "
        "Any comma-separated theme hints below are for mood only — do not spell them as labels or titles in the picture.\n\n"
    )
    suffix = (
        "\n\nFINAL CHECK: output must be purely visual with zero readable text anywhere in the frame."
    )
    return f"{prefix}{(base or '').strip()}{suffix}"


def _admin_pick_gcs_path_for_upload(target: dict, mime_type: str) -> str:
    """Choisit un chemin manifeste cohérent (PNG/JPG préféré selon le MIME renvoyé par Vertex)."""
    m = (mime_type or "").lower()
    alts = list(target.get("alternates") or [])
    if "png" in m:
        for a in alts:
            if str(a).lower().endswith(".png"):
                return str(a).strip()
    if "jpeg" in m or "jpg" in m:
        for a in alts:
            if str(a).lower().endswith((".jpg", ".jpeg")):
                return str(a).strip()
    ds = str(target.get("date") or "").strip()
    y = ds[:4] if len(ds) >= 4 else "2026"
    return f"Images/illustrations/{y}/{ds}.png"


def render_admin_illustration_gen_panel(*, data: dict, manifest_path: Path) -> None:
    st.subheader("Génération Vertex AI → bucket GCS")
    st.info(
        "**Stockage GCS** : pour que l’image soit **envoyée sur le bucket**, laisse la case "
        "« Aperçu seulement… » **décochée**. Si elle est cochée, tu vois l’image à l’écran mais "
        "**rien n’est enregistré** dans Google Cloud Storage."
    )

    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name` dans les secrets.")
        return

    targets_all = list(data.get("targets") or [])
    if not targets_all:
        st.warning("Aucune cible dans le manifeste.")
        return

    gcs = build_gcs_client(cfg.gcp_service_account)
    bucket_name = str(cfg.gcs_bucket_name).strip()
    sorted_targets = _admin_sort_targets_by_date(targets_all)
    has_map = [
        _admin_target_has_illustration(gcs=gcs, bucket_name=bucket_name, target=t) for t in sorted_targets
    ]
    n_missing = sum(1 for h in has_map if not h)

    COLS, ROWS = 10, 6
    per_page = COLS * ROWS
    n_targets = len(sorted_targets)
    n_pages = max(1, (n_targets + per_page - 1) // per_page)

    # Cocher / décocher en masse : doit s'exécuter AVANT les st.checkbox (adm_sel_*), sinon Streamlit bloque.
    _pg_bulk = int(st.session_state.get("adm_grid_page", 0))
    _pg_bulk = max(0, min(_pg_bulk, n_pages - 1))
    _slice_bulk = _pg_bulk * per_page
    if st.session_state.pop("_adm_bulk_check_page", False):
        for gi in range(_slice_bulk, min(_slice_bulk + per_page, n_targets)):
            if not has_map[gi]:
                st.session_state[f"adm_sel_{gi}"] = True
    if st.session_state.pop("_adm_bulk_uncheck_page", False):
        for gi in range(_slice_bulk, min(_slice_bulk + per_page, n_targets)):
            k = f"adm_sel_{gi}"
            if k in st.session_state:
                st.session_state[k] = False

    c1, c2 = st.columns(2)
    with c1:
        aspect = st.selectbox("Ratio d’image", ["4:3", "3:4", "1:1", "16:9"], index=0, key="adm_img_aspect")
    with c2:
        pause_s = st.number_input(
            "Tempo après chaque image avant la suivante",
            min_value=0,
            max_value=180,
            value=2,
            step=1,
            key="adm_img_pause",
        )

    models_line = st.text_input(
        "Modèles Vertex à essayer (ordre, séparés par des virgules)",
        value="gemini-2.5-flash-image,gemini-3-pro-image-preview",
        key="adm_img_models",
    )
    preferred_models = [x.strip() for x in models_line.split(",") if x.strip()]

    dry_run = st.checkbox(
        "Aperçu seulement — ne pas envoyer sur GCS (aucun fichier dans le bucket)",
        value=False,
        key="adm_img_dry",
    )

    # --- Grille 10 × 6 : semaine ISO, vignette ou sélection si manquant ---
    st.divider()
    st.subheader("Calendrier des illustrations")
    st.caption(
        f"**{n_missing}** dimanche(s) sans fichier sur GCS sur **{len(sorted_targets)}** — "
        f"manifeste `{manifest_path.as_posix()}`. Semaine = **numéro ISO** (semaine civile du dimanche)."
    )

    page_ix = st.number_input(
        "Page grille (60 cases)",
        min_value=0,
        max_value=max(0, n_pages - 1),
        value=0,
        step=1,
        key="adm_grid_page",
    )
    slice_start = int(page_ix) * per_page

    thumb_bytes: dict[int, bytes] = {}
    to_fetch: list[tuple[int, str]] = []
    for gi in range(slice_start, min(slice_start + per_page, n_targets)):
        if not has_map[gi]:
            continue
        bp = _admin_best_display_blob_path(gcs=gcs, bucket_name=bucket_name, target=sorted_targets[gi])
        if bp:
            to_fetch.append((gi, bp))

    if to_fetch:
        with ThreadPoolExecutor(max_workers=12) as ex:
            fut_to_gi: dict = {}
            for gi, bp in to_fetch:
                fut = ex.submit(
                    partial(download_bytes, gcs=gcs, bucket_name=bucket_name, path=bp)
                )
                fut_to_gi[fut] = gi
            for fut in as_completed(fut_to_gi):
                gi = fut_to_gi[fut]
                try:
                    b = fut.result()
                    if b:
                        thumb_bytes[gi] = b
                except Exception:
                    pass

    for row in range(ROWS):
        cols = st.columns(COLS)
        for col_i in range(COLS):
            gi = slice_start + row * COLS + col_i
            with cols[col_i]:
                if gi >= n_targets:
                    continue
                t = sorted_targets[gi]
                ds = str(t.get("date") or "")[:10]
                sw = _admin_iso_week_label(ds)
                st.markdown(
                    f"<div style='font-size:0.72rem;color:#342E29;text-align:center;"
                    f"font-weight:600;margin-bottom:2px;'>S{sw}<br/><span style='font-weight:400'>{ds}</span></div>",
                    unsafe_allow_html=True,
                )
                if has_map[gi]:
                    tb = thumb_bytes.get(gi)
                    if tb:
                        st.image(io.BytesIO(tb), use_container_width=True)
                    else:
                        st.caption("✓ GCS")
                else:
                    st.checkbox(
                        "Manquant",
                        key=f"adm_sel_{gi}",
                        value=False,
                        label_visibility="visible",
                    )

    ga1, ga2, ga3, ga4 = st.columns(4)
    with ga1:
        if st.button("Cocher manquantes (page)", key="adm_grid_chk_page"):
            st.session_state["_adm_bulk_check_page"] = True
            st.rerun()
    with ga2:
        if st.button("Décocher (page)", key="adm_grid_unchk_page"):
            st.session_state["_adm_bulk_uncheck_page"] = True
            st.rerun()
    with ga3:
        run_missing_page = st.button(
            "Générer toutes les manquantes de la page",
            key="adm_grid_run_page_missing",
        )
    with ga4:
        run_selected = st.button(
            "Générer les cases cochées",
            type="primary",
            key="adm_grid_run_selected",
        )

    vx = VertexGeminiClient(
        service_account_info=cfg.gcp_service_account,
        locations=["global", "europe-west1", "us-central1"],
    )

    if run_missing_page:
        to_gen = [
            sorted_targets[gi]
            for gi in range(slice_start, min(slice_start + per_page, n_targets))
            if not has_map[gi]
        ]
        if not to_gen:
            st.info("Aucun dimanche sans fichier sur cette page.")
        else:
            lines = _admin_execute_image_generations(
                cfg=cfg,
                gcs=gcs,
                vx=vx,
                to_run=to_gen,
                aspect=aspect,
                pause_s=float(pause_s),
                dry_run=dry_run,
                preferred_models=preferred_models,
                skip_existing=False,
            )
            _admin_finish_generation_log(lines, dry_run=dry_run)

    if run_selected:
        to_gen = [
            sorted_targets[gi]
            for gi in range(n_targets)
            if st.session_state.get(f"adm_sel_{gi}", False) and not has_map[gi]
        ]
        if not to_gen:
            st.warning("Coche au moins un dimanche encore sans fichier (ou utilise « manquantes de la page »).")
        else:
            lines = _admin_execute_image_generations(
                cfg=cfg,
                gcs=gcs,
                vx=vx,
                to_run=to_gen,
                aspect=aspect,
                pause_s=float(pause_s),
                dry_run=dry_run,
                preferred_models=preferred_models,
                skip_existing=False,
            )
            _admin_finish_generation_log(lines, dry_run=dry_run)

    st.divider()
    st.subheader("Détection de texte dans les images")
    st.caption(
        "Google **Cloud Vision** (`TEXT_DETECTION`) sur les fichiers déjà sur le bucket — pour repérer les visuels "
        "avec glyphes à retoucher. Active l’API **Cloud Vision** sur le projet GCP et vérifie la facturation."
    )
    ta_scope = st.radio(
        "Portée du scan",
        options=("manifeste_complet", "page_grille"),
        format_func=lambda x: (
            "Toutes les dates du manifeste (si fichier sur GCS)"
            if x == "manifeste_complet"
            else "Uniquement la page grille courante (60 cases)"
        ),
        horizontal=True,
        key="adm_text_audit_scope",
    )
    ta_min = st.number_input(
        "Longueur minimale du texte détecté (caractères non blancs)",
        min_value=1,
        max_value=80,
        value=2,
        step=1,
        key="adm_text_audit_min_chars",
        help="Augmente si Vision remonte trop de faux positifs (bruit).",
    )
    ta_workers = st.slider(
        "Parallélisme (téléchargement + Vision)",
        min_value=1,
        max_value=16,
        value=8,
        key="adm_text_audit_workers",
    )
    if st.button("Analyser les images (détection de texte)", key="adm_text_audit_run"):
        overlay = loading_overlay("Analyse Vision des illustrations sur GCS…")
        try:
            scan_targets = (
                sorted_targets
                if ta_scope == "manifeste_complet"
                else sorted_targets[slice_start : slice_start + per_page]
            )
            vc = build_vision_image_annotator_client(cfg.gcp_service_account)
            rows = audit_targets_for_text(
                gcs=gcs,
                bucket_name=bucket_name,
                targets=scan_targets,
                vision_client=vc,
                max_workers=int(ta_workers),
                min_chars=int(ta_min),
            )
            flagged = filter_rows_with_text(rows)
            errs = [r for r in rows if r.get("error")]
            scanned_n = len(rows)
            st.metric("Fichiers analysés", scanned_n)
            if errs:
                if all_errors_are_vision_service_disabled(rows):
                    ex0 = str(errs[0].get("error") or "")
                    sa_project_id = str(cfg.gcp_service_account.get("project_id") or "").strip()
                    sa_quota_project_id = str(
                        cfg.gcp_service_account.get("quota_project_id") or cfg.gcp_service_account.get("project_id") or ""
                    ).strip()
                    pid_from_err = extract_gcp_project_id_from_error(ex0)
                    act_url = extract_console_url_from_error(ex0) or vision_console_activation_url(
                        pid_from_err or sa_quota_project_id or sa_project_id
                    )
                    st.error(
                        "L’API **Google Cloud Vision** n’est pas activée pour ce projet GCP "
                        "(ou la propagation des droits est encore en cours — attends quelques minutes après activation)."
                    )
                    if sa_project_id or sa_quota_project_id or pid_from_err:
                        st.info(
                            "Projet ciblé par la config / credentials : "
                            f"`project_id={sa_project_id or '—'}` · "
                            f"`quota_project_id={sa_quota_project_id or '—'}` · "
                            f"`projet détecté dans l’erreur={pid_from_err or '—'}`"
                        )
                    st.markdown(f"[Ouvrir la console Google Cloud — activer Cloud Vision API]({act_url})")
                else:
                    st.warning(f"{len(errs)} erreur(s) Vision ou téléchargement — voir le détail ci-dessous.")
            if flagged:
                st.error(
                    f"{len(flagged)} image(s) avec texte détecté (≥ {int(ta_min)} caractères) — candidats au post-traitement."
                )
                show_tbl = [
                    {
                        "date": r["date"],
                        "chemin GCS": r["gcs_path"],
                        "URI gs://": f"gs://{bucket_name}/{r['gcs_path']}",
                        "extrait": (r.get("detected_text") or "")[:800],
                    }
                    for r in flagged
                ]
                st.dataframe(show_tbl, use_container_width=True, hide_index=True)
                buf = StringIO()
                w = csv.DictWriter(
                    buf,
                    fieldnames=["date", "gcs_path", "gs_uri", "detected_text"],
                    extrasaction="ignore",
                )
                w.writeheader()
                for r in flagged:
                    w.writerow(
                        {
                            "date": r["date"],
                            "gcs_path": r["gcs_path"],
                            "gs_uri": f"gs://{bucket_name}/{r['gcs_path']}",
                            "detected_text": r.get("detected_text") or "",
                        }
                    )
                st.download_button(
                    "Télécharger la liste (CSV)",
                    data=buf.getvalue().encode("utf-8-sig"),
                    file_name="lumenvia_images_avec_texte.csv",
                    mime="text/csv; charset=utf-8",
                    key="adm_text_audit_csv",
                )
            else:
                if scanned_n == 0:
                    st.info("Aucun fichier sur GCS dans la portée choisie.")
                elif errs and len(errs) >= scanned_n and scanned_n > 0:
                    st.warning(
                        "Aucune analyse réussie : tous les appels Vision ont échoué. "
                        "Corrige la configuration (API activée, facturation, droits du compte de service) puis réessaie."
                    )
                else:
                    st.success("Aucune image avec texte détecté selon ces réglages.")

            if errs:
                err_tbl = [
                    {
                        "date": r.get("date"),
                        "chemin": r.get("gcs_path"),
                        "erreur": shorten_audit_error_message(str(r.get("error") or "")),
                    }
                    for r in errs
                ]
                with st.expander("Détail des erreurs"):
                    st.dataframe(err_tbl, use_container_width=True, hide_index=True)
        except Exception as ex:
            st.exception(ex)
        finally:
            overlay.empty()


def render_admin_thumbs() -> None:
    st.title("Génération des vignettes")
    st.caption(
        "Cette page permet d'identifier les images qui nécessitent d'avoir leur équivalent en vignette "
        "pour optimiser les performances du site. Ces vignettes sont ensuite utilisées pour les illustrations "
        "qui ne nécessitent pas les images en taille pleine."
    )
    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}`.")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return
    render_admin_thumbs_panel(data=data)


def render_admin_thumbs_panel(*, data: dict) -> None:
    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name`.")
        return

    gcs = build_gcs_client(cfg.gcp_service_account)
    bucket_name = str(cfg.gcs_bucket_name).strip()
    sorted_targets = _admin_sort_targets_by_date(list(data.get("targets") or []))
    if not sorted_targets:
        st.warning("Aucune cible dans le manifeste.")
        return

    n_src = 0
    n_thumb = 0
    missing_sources: list[str] = []
    for t in sorted_targets:
        src = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
        if not src:
            continue
        n_src += 1
        if thumb_blob_exists(gcs=gcs, bucket_name=bucket_name, source_blob_path=src):
            n_thumb += 1
        else:
            missing_sources.append(src)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Images pleines sur GCS", n_src)
    with c2:
        st.metric("Vignettes présentes", n_thumb)
    with c3:
        st.metric("Vignettes manquantes", len(missing_sources))

    mx = st.slider("Taille max. du côté (pixels)", min_value=280, max_value=720, value=420, step=20, key="adm_thumb_max")

    if not missing_sources:
        st.success("Toutes les vignettes sont déjà générées pour les illustrations présentes sur le bucket.")
    else:
        n_missing = len(missing_sources)
        st.info(
            f"**{n_missing}** vignette(s) manquante(s) sur **{n_src}** image(s) présentes sur GCS — "
            "tu peux les générer avec le bouton ci-dessous."
        )
        if st.button(
            "Générer les vignettes manquantes",
            type="primary",
            key="adm_thumb_gen_missing",
        ):
            overlay = loading_overlay("Génération des vignettes sur GCS…")
            prog = st.progress(0.0)
            ok = 0
            err_n = 0
            ntot = len(missing_sources)
            try:

                def _job(src: str) -> None:
                    generate_thumb_from_source_and_upload(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        source_blob_path=src,
                        download_bytes_fn=download_bytes,
                        upload_bytes_fn=upload_bytes,
                        max_side=int(mx),
                    )

                with ThreadPoolExecutor(max_workers=12) as ex:
                    fut_map = {ex.submit(_job, src): src for src in missing_sources}
                    for i, fut in enumerate(as_completed(fut_map)):
                        try:
                            fut.result()
                            ok += 1
                        except Exception:
                            err_n += 1
                        prog.progress(min(1.0, (i + 1) / max(ntot, 1)))
                prog.progress(1.0)
                if ok:
                    st.success(f"{ok} vignette(s) enregistrée(s) sous `{THUMB_GCS_PREFIX}/`.")
                if err_n:
                    st.warning(f"{err_n} erreur(s) — vérifie les logs ou relance.")
            except Exception as ex:
                st.exception(ex)
            finally:
                overlay.empty()
            st.rerun()


def render_admin_plan_consolide() -> None:
    """Vue synthèse : protocole LumenVia + reste à faire (alignement retours Gemini)."""
    st.title("Plan consolidé")
    st.caption(
        "Synthèse du protocole (`.cursor/rules/lumenvia.mdc`), de l’état du code et des chantiers à traiter — "
        "dont les arbitrages issus des échanges avec **Gemini** (ligne dédiée ci-dessous, à compléter demain)."
    )

    plan_html = """
<style>
.lv-plan-wrap { font-family: Lora, Georgia, serif; color: #342E29; font-size: 0.92rem; }
.lv-plan-table { width: 100%; border-collapse: collapse; margin: 0.75rem 0 1.25rem 0; }
.lv-plan-table th {
  text-align: left; padding: 10px 12px; background: rgba(212, 175, 55, 0.18);
  border: 1px solid rgba(212, 175, 55, 0.45); font-weight: 600;
}
.lv-plan-table td {
  vertical-align: top; padding: 10px 12px; border: 1px solid rgba(52, 46, 41, 0.15);
  background: rgba(255, 255, 255, 0.65);
}
.lv-plan-table tr:nth-child(even) td { background: rgba(253, 251, 247, 0.95); }
.lv-st-ok { color: #1b5e20; font-weight: 600; }
.lv-st-partiel { color: #bf360c; font-weight: 600; }
.lv-st-todo { color: #6a1b9a; font-weight: 600; }
.lv-keylist { margin-top: 1rem; padding: 12px 14px; border-left: 3px solid #D4AF37; background: rgba(255,255,255,0.75); }
.lv-keylist dt { font-weight: 600; margin-top: 8px; color: #342E29; }
.lv-keylist dd { margin: 4px 0 0 0; padding-left: 0.5rem; border-left: 2px solid rgba(212, 175, 55, 0.35); }
</style>
<div class="lv-plan-wrap">
<table class="lv-plan-table">
  <thead>
    <tr><th>Thème</th><th>Statut</th><th>Reste à faire / notes</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>Manifestes étape 2–3 + illustrations GCS + grille Vertex admin</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>Bascule annuelle ; retouches unitaires si besoin (charte, date).</td>
    </tr>
    <tr>
      <td>Vignettes <code>Images/thumbs/</code> + perf site / grille admin</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>Régénérer les thumbs si changement de fichier source ou de taille max.</td>
    </tr>
    <tr>
      <td>Détection de texte dans les images (Cloud Vision)</td>
      <td><span class="lv-st-partiel">Partiel</span></td>
      <td>Confirmer stabilité GCP (projet, facturation, IAM) ; post-trait des visuels signalés si besoin.</td>
    </tr>
    <tr>
      <td>Cache local lectures AELF + synthèse / audio</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>Extensions possibles (autres médias) si le produit le demande.</td>
    </tr>
    <tr>
      <td>PDF page de garde (dimanche) + PDF mensuel « Graine de Parole » (encart résolutions)</td>
      <td><span class="lv-st-partiel">Livré v1</span></td>
      <td>Finitions : mise en page longue, sommaire, branding newsletter / fascicule papier.</td>
    </tr>
    <tr>
      <td>PWA / installation « Ajouter à l’écran d’accueil »</td>
      <td><span class="lv-st-todo">À finaliser</span></td>
      <td>Couches hébergeur / reverse-proxy : HTTPS, en-têtes, injection manifest dans <code>&lt;head&gt;</code>.</td>
    </tr>
    <tr>
      <td>Typologie biblique / Psaume « Ma réponse » (<code>data/instructions_ia.md</code>)</td>
      <td><span class="lv-st-ok">En données</span></td>
      <td>Pilotage éditorial continu ; pas de sources hors AELF.</td>
    </tr>
    <tr>
      <td><strong>Suivi Gemini + toi</strong> (retours consolidation)</td>
      <td><span class="lv-st-todo">À traiter demain</span></td>
      <td>Consolider ici les décisions suite à la génération massive d’illustrations et aux échanges Gemini : arbitrages qualité, Vision, PDF, PWA, newsletter.</td>
    </tr>
    <tr>
      <td>Cahier des charges — <strong>version générée automatiquement</strong>, consultation admin, export PDF</td>
      <td><span class="lv-st-todo">À faire</span></td>
      <td>
        Pipeline à définir : snapshot à partir du Markdown versionné (<code>data/cahier_des_charges.md</code> + journal Sheets),
        rendu lisible dans l’administration (aperçu « document »), export PDF au standard graphique JOPAI (bandeau, typo).
      </td>
    </tr>
    <tr>
      <td>CSS responsive <strong>mobile &amp; tablette</strong> (&lt; 1024&nbsp;px)</td>
      <td><span class="lv-st-partiel">Partiel</span></td>
      <td>
        Voir <strong>points chirurgicaux</strong> ci-dessous (référence). Déjà dans <code>app.py</code> : popover <code>Menu</code>, viewport,
        padding mémo + <code>:has(textarea:focus)</code>. Reste : extractions CSS dédiées, largeur max type « app » (~480–600&nbsp;px), simulateur admin, audit expander « Mes mémos ».
      </td>
    </tr>
    <tr>
      <td>Administration — <strong>simulateur vision mobile</strong></td>
      <td><span class="lv-st-todo">À faire</span></td>
      <td>
        Nouvelle page ou panneau admin pour prévisualiser l’app comme sur téléphone (viewport réduit / iframe ou gabarit dédié),
        afin de valider navigation, lectures et expander « Mes mémos » sans que le clavier virtuel ne masque tout l’écran.
      </td>
    </tr>
  </tbody>
</table>

<dl class="lv-keylist">
  <dt>Trois points chirurgicaux UX mobile (référence verrouillée)</dt>
  <dd>
    <strong>1 — Navigation.</strong> Sur mobile uniquement, remplacer la rangée des 4 boutons horizontaux par un
    <code>st.popover</code> étiqueté <strong>« Menu »</strong> (composant le plus proche d’un menu smartphone moderne) ;
    conserver les 4 boutons sur grand écran.
  </dd>
  <dd>
    <strong>2 — Clavier vs saisie / expander.</strong> Ajouter un <code>padding-bottom</code> substantiel au conteneur principal lorsqu’un champ
    <code>st.text_area</code> est actif (ex.&nbsp;<strong>20vh</strong>), pour permettre le défilement et garder la zone de frappe visible au-dessus du clavier virtuel.
  </dd>
  <dd>
    <strong>3 — Viewport.</strong> Le document doit inclure impérativement
    <code>&lt;meta name=&quot;viewport&quot; content=&quot;width=device-width, initial-scale=1&quot;&gt;</code>
    dans le <code>&lt;head&gt;</code> (Streamlit : pas via <code>st.set_page_config</code> seul — injection par composant / script ciblant le document parent).
    Sans cela, certains téléphones « dézooment » au lieu d’appliquer le CSS mobile.
  </dd>
</dl>

<dl class="lv-keylist">
  <dt>Note de cadrage — adaptation responsive (référence)</dt>
  <dd>Rendu « application mobile » dès largeur &lt; 1024&nbsp;px ; pas de scroll horizontal ; marges respiration ; audit « Mes mémos » + clavier.</dd>
  <dd>Intégrer les media queries dans le CSS global LumenVia ; ajuster <code>.block-container</code>, blocs horizontaux Streamlit, boutons primaires/secondaires, titres <code>h1</code>/<code>h3</code>, classe <code>.liturgical-reading</code>.</dd>
</dl>

<dl class="lv-keylist">
  <dt>Priorités rapides (key list)</dt>
  <dd>Cahier des charges : génération automatique d’une version « livrable », visualisation admin, export PDF.</dd>
  <dd>Responsive : media queries &lt; 1024&nbsp;px, navigation empilée ou menu alternatif, tests réels tablette / téléphone.</dd>
  <dd>Admin : simulateur mobile pour recette avant déploiement.</dd>
  <dd>Stabiliser Vision sur le bon projet GCP et valider une analyse complète sans 403.</dd>
  <dd>Repasser sur le PDF mensuel et la couverture si tu veux un gabarit « fascicule » multi-pages.</dd>
  <dd>PWA : choix d’hébergement et socle technique pour exposer le manifest au navigateur.</dd>
</dl>
</div>
"""
    st.markdown(plan_html, unsafe_allow_html=True)


_CDC_MARKDOWN_PATH = Path("data/cahier_des_charges.md")


def render_admin_cahier_charges() -> None:
    """Document Markdown versionné + journal append-only Sheets."""
    st.title("Cahier des charges")
    st.markdown(
        """
**Document principal** : fichier Markdown dans le dépôt (`data/cahier_des_charges.md`), éditable ci-dessous puis sauvegardé sur le serveur qui exécute Streamlit.

**Journal des évolutions** : entrées **append-only** dans la table Google Sheets `admin_changelog` (traçabilité des décisions sans effacer l’historique).
        """.strip()
    )

    _CDC_MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _CDC_MARKDOWN_PATH.is_file():
        _CDC_MARKDOWN_PATH.write_text(
            "# Cahier des charges — JOPAI LumenVia\n\n"
            "*Édite ce texte depuis l’administration, puis clique sur Enregistrer.*\n",
            encoding="utf-8",
        )
    cdc_body = _CDC_MARKDOWN_PATH.read_text(encoding="utf-8")
    edited = st.text_area(
        "Contenu (Markdown)",
        value=cdc_body,
        height=420,
        key="adm_cdc_editor",
    )
    if st.button("Enregistrer sur le disque", type="primary", key="adm_cdc_save"):
        _CDC_MARKDOWN_PATH.write_text(edited, encoding="utf-8")
        st.success(f"Sauvegardé : `{_CDC_MARKDOWN_PATH.as_posix()}` — pense à **commit** Git si tu veux versionner.")
        st.rerun()

    st.divider()
    st.subheader("Journal des évolutions (Sheets)")
    st.caption(
        "Ancien bloc « cahier des charges incrémental » déplacé ici : chaque ajout crée une nouvelle ligne dans `admin_changelog`."
    )

    cfg = load_config()
    title = st.text_input("Titre de l’entrée", key="adm_cdc_cl_title")
    detail = st.text_area("Détail", key="adm_cdc_cl_detail", height=160)
    if st.button("Ajouter une entrée au journal", type="primary", disabled=not (title and detail), key="adm_cdc_cl_add"):
        if not cfg.gcp_service_account or not cfg.gsheet_id:
            st.error("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        else:
            gs = build_gspread_client(cfg.gcp_service_account)
            entry_id = sha256(f"adm|{title}|{detail}".encode("utf-8")).hexdigest()[:24]
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="admin_changelog",
                values_by_col={
                    "entity_id": entry_id,
                    "title": title.strip(),
                    "detail": detail.strip(),
                },
            )
            st.success("Entrée ajoutée au journal.")
            st.rerun()

    if cfg.gsheet_id and cfg.gcp_service_account:
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            cl = fetch_records(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="admin_changelog",
                limit=300,
            )
            cl_sorted = sorted(cl, key=lambda r: str(r.get("created_at", "")), reverse=True)
            st.markdown(f"**{len(cl_sorted)}** entrée(s) ; les 40 dernières :")
            for row in cl_sorted[:40]:
                t = str(row.get("title") or "—").strip()
                with st.expander(t[:100] + ("…" if len(t) > 100 else "")):
                    st.markdown(str(row.get("detail") or ""))
                    st.caption(f"`created_at` : {row.get('created_at', '—')}")
        except Exception as e:
            st.warning(f"Lecture du journal impossible : {e}")
    else:
        st.info("Configure `gsheet_id` pour afficher le journal Sheets ici.")


def render_admin_login() -> None:
    st.title("Connexion administration")
    login_ok, pwd_ok = _admin_login_and_password()
    with st.form("admin_login_form"):
        login_id = st.text_input("Identifiant", key="adm_login_id", autocomplete="username")
        pwd = st.text_input("Mot de passe", type="password", key="adm_login_pwd", autocomplete="current-password")
        submitted = st.form_submit_button("Connexion", type="primary")
    if submitted:
        if login_id.strip().lower() == login_ok and pwd == pwd_ok:
            st.session_state.admin_authenticated = True
            st.session_state.route = "admin_step3"
            st.rerun()
        else:
            st.error("Identifiant ou mot de passe incorrect.")


def render_admin_step3() -> None:
    st.title("Admin — Génération des visuels liturgiques")
    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}` (relatif à la racine du projet).")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return

    targets = data.get("targets") or []
    year_hint = ""
    if targets:
        ds0 = str(targets[0].get("date") or "")
        if len(ds0) >= 4:
            year_hint = ds0[:4]

    st.markdown(
        f"""
### À quoi servent ces illustrations

- **Une image par dimanche** listée dans le manifeste : elle correspond à **la semaine liturgique** centrée sur ce dimanche.
- **Dans l’app** : sur « La Lumière du Dimanche », l’image affichée est celle du **dimanche choisi** par l’utilisateur (fichier présent dans GCS au chemin du manifeste).
- **Communication** : la même illustration peut illustrer le **SMS**, l’**e-mail** ou la **newsletter** de la semaine pour laquelle tu fixes ce dimanche comme référence.

**Autres usages possibles** : visuel pour **réseaux sociaux** ou **Open Graph** du lien du jour ; **PDF** ou fascicule mensuel ; **diaporama** ou fond d’écran en paroisse ; **carte de partage** (PWA / lien) ; **miniature** dans un récap hebdomadaire ; **kit presse** ou **affiche** locale pour une grande solennité.

### Fréquence de production

Le manifeste est construit **pour une année civile** (script étape 2 avec `--year`). Une fois **toutes** les images générées et déposées sur GCS pour cette année, **tu n’as pas besoin d’y revenir** tant que tu restes sur cette même année — sauf **retouche ponctuelle**, **changement de charte**, ou passage à **l’année suivante** (nouveau manifeste + nouvelles images).

{f"**Année couverte par ce fichier** : **{year_hint}** ({len(targets)} dimanches)." if year_hint else f"**Dimanches dans ce manifeste** : {len(targets)}."}
        """.strip()
    )

    render_admin_illustration_gen_panel(data=data, manifest_path=manifest_path)


def render_admin_test_resources() -> None:
    st.title("Admin — test ressources")
    cfg = load_config()
    st.write("Cette page sert à valider l’accès aux ressources configurées dans `secrets.toml`.")

    if not cfg.gcp_service_account:
        st.error("gcp_service_account manquant dans secrets.")
        return

    if cfg.gcs_bucket_name:
        try:
            gcs = build_gcs_client(cfg.gcp_service_account)
            bucket = gcs.bucket(cfg.gcs_bucket_name)
            blobs = list(gcs.list_blobs(bucket, max_results=20, prefix="Images/"))
            st.success(f"GCS OK — bucket `{cfg.gcs_bucket_name}` (exemples: {len(blobs)} objets sous Images/)")
            for b in blobs[:10]:
                st.write(f"- {b.name}")
        except Exception as e:
            st.error(f"GCS KO — {e}")
    else:
        st.warning("gcs_bucket_name manquant.")

    if cfg.gsheet_id:
        st.success("Google Sheets: gsheet_id présent.")
    else:
        st.warning("gsheet_id manquant.")

    st.caption(
        "Journal produit / décisions d’architecture : menu **Administration → Cahier des charges**."
    )


def _build_prompt(
    *,
    instructions: str,
    length_words: int,
    include_takeaways: bool,
    identity: dict,
    readings: dict,
    liturgical_context: str | None = None,
) -> str:
    # Prompt “grounded”: on fournit toutes les sources AELF textuelles, et on rappelle les contraintes.
    takeaways = "true" if include_takeaways else "false"
    ctx = (liturgical_context or "").strip()
    ctx_block = ""
    if ctx:
        ctx_block = f"\nRepères liturgiques (résumé pédagogique, à intégrer sans invention hors textes AELF):\n{ctx}\n"
    psalm_block = ""
    if include_takeaways:
        psalm_block = (
            "\nInclure une sous-section titrée exactement « Le Psaume : Ma réponse » : uniquement à partir du texte du psaume fourni, "
            "explique comment ce psaume permet de répondre en prière aux lectures (sans sources externes).\n"
            "Structurer aussi la synthèse pour mettre en relief la promesse / préfiguration (Première lecture, AT si applicable) "
            "et son accomplissement ou réponse dans l’Évangile, strictement à partir des textes fournis.\n"
            "Terminer par une section « À retenir » avec 3 à 5 puces commençant par un verbe.\n"
        )
    else:
        psalm_block = (
            "\nMettre en relief la promesse / préfiguration (Première lecture) et l’accomplissement (Évangile), strictement à partir des textes fournis.\n"
        )

    return f"""
{instructions}

Paramètres:
- length_words: {length_words}
- include_takeaways: {takeaways}
- style: simple
- addressing: vous
{ctx_block}
Identité du jour (AELF):
{identity}

Textes (AELF, source unique):
{readings}

Tâche:
Commence par un court paragraphe de mise en situation : comment la couleur liturgique, le temps liturgique et le cycle annoncés ci-dessus cadrent la lecture du jour (sans ajouter de faits non présents dans les textes).
Ensuite, rédige la synthèse en français en respectant STRICTEMENT les contraintes (zéro invention).
{psalm_block}
Contrainte de longueur: vise {length_words} mots (+/- 10%). Ne termine pas avant d'avoir atteint la longueur cible.
""".strip()


def _count_words(text: str) -> int:
    # Compteur simple, suffisant pour calibrer un pourcentage.
    return len([w for w in (text or "").replace("\n", " ").split(" ") if w.strip()])


def _ext_from_mime(mime: str | None) -> str:
    m = (mime or "").lower()
    if "audio/wav" in m or "audio/x-wav" in m or "wav" in m:
        return "wav"
    if "audio/mpeg" in m or "mp3" in m:
        return "mp3"
    if "audio/ogg" in m or "ogg" in m:
        return "ogg"
    # Certains modèles renvoient du PCM du type: audio/L16;rate=24000
    if m.startswith("audio/"):
        return "wav"
    return "bin"


def _chunk_text_for_tts(text: str, *, max_chars: int = 900) -> list[str]:
    """
    Découpe en morceaux pour éviter les limites TTS (et éviter l'audio tronqué).
    Stratégie simple: découpe sur paragraphes puis sur phrases si besoin.
    """
    t = " ".join((text or "").split())
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]

    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        p = " ".join(p.split())
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= max_chars:
            cur = cur + " " + p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)

    # Si un chunk est encore trop long, découpe brute
    final: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            for i in range(0, len(c), max_chars):
                final.append(c[i : i + max_chars])
    return final


def _inject_admin_phone_preview_css() -> None:
    """Admin uniquement : largeur 390px + cadre arrondi type smartphone pour recette bureau."""
    if not st.session_state.get("admin_authenticated"):
        return
    if not st.session_state.get("admin_phone_preview"):
        return
    st.markdown(
        """
<style>
/* Aperçu « iPhone » 390px — activé par le toggle Administration */
[data-testid="stAppViewContainer"] {
  background: linear-gradient(165deg, #4a4a52 0%, #1e1e22 55%, #121214 100%) !important;
  min-height: 100vh !important;
}
[data-testid="stHeader"] {
  background: transparent !important;
}
section[data-testid="stMain"] {
  max-width: 390px !important;
  width: 100% !important;
  margin-left: auto !important;
  margin-right: auto !important;
  margin-top: 0.75rem !important;
  margin-bottom: 1.5rem !important;
  box-sizing: border-box !important;
  border: 12px solid #0d0d0f !important;
  border-radius: 40px !important;
  box-shadow:
    0 0 0 1px rgba(255, 255, 255, 0.07) inset,
    0 22px 56px rgba(0, 0, 0, 0.48) !important;
  min-height: min(88vh, 844px) !important;
  overflow-x: hidden !important;
  background: var(--liturgie-cream, #fdfbf7) !important;
}
section[data-testid="stMain"] .block-container {
  padding-left: max(0.65rem, env(safe-area-inset-left, 0px)) !important;
  padding-right: max(0.65rem, env(safe-area-inset-right, 0px)) !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    set_page_style()
    _inject_admin_phone_preview_css()

    if "route" not in st.session_state:
        st.session_state.route = "about"

    # Liens admin optionnels : ?admin=1 (test ressources), ?admin=login, ?admin=step3
    params = st.query_params
    adm = (params.get("admin") or "").strip().lower()
    if adm == "1":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_resources"
        else:
            st.session_state.route = "admin_login"
    elif adm == "login":
        st.session_state.route = "admin_login"
    elif adm == "step3":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_step3"
        else:
            st.session_state.route = "admin_login"
    elif adm == "cdc":
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_cdc"
        else:
            st.session_state.route = "admin_login"

    sun_qp = (params.get("sunday") or "").strip()
    if sun_qp and len(sun_qp) >= 10:
        try:
            date.fromisoformat(sun_qp[:10])
            st.session_state.route = "sunday"
            st.session_state["_lumenvia_sunday_qs"] = sun_qp[:10]
        except Exception:
            pass
        try:
            del st.query_params["sunday"]
        except Exception:
            pass

    if adm in ("1", "login", "step3", "cdc"):
        try:
            del st.query_params["admin"]
        except Exception:
            pass

    route = top_nav()
    st.divider()

    if route == "about":
        render_about()
    elif route == "sunday":
        render_sunday()
    elif route == "memo":
        render_memo()
    elif route == "join":
        render_join()
    elif route == "admin_login":
        render_admin_login()
    elif route == "admin_step3":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_step3"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_step3()
    elif route == "admin_thumbs":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_thumbs"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_thumbs()
    elif route == "admin_resources":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_res"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_test_resources()
    elif route == "admin_plan":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_plan"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_plan_consolide()
    elif route == "admin_cdc":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_cdc"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_cahier_charges()
    else:
        render_about()


if __name__ == "__main__":
    main()

