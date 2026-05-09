from __future__ import annotations

import csv
import io
from io import BytesIO
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
from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE
from core.gemini_tts_api import GeminiTtsApiClient
from core.gcp_clients import build_gcs_client, build_vision_image_annotator_client
from core.audio_utils import normalize_audio_bytes, join_wav_bytes
from core.auth import hash_password, verify_password
from core.vertex_gemini import VertexGeminiClient
from core.sheets_db import (
    BASE_COLUMNS,
    SHEETS_ROW_STATUS_ACTIVE,
    SHEETS_ROW_STATUS_INACTIVE,
    append_immutable_row,
    append_immutable_rows_bulk,
    build_gspread_client,
    compute_concat,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.prompt_templates import compute_sha256_text
from core.parametres_ia import pick_effective_templates
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


_PROMPT_TEMPLATE_KEYS = {
    # Clés_Prompt attendues dans l'onglet GSheet `Paramètres_IA`
    "instructions_base_md",
    "overlay_takeaways",
    "overlay_no_takeaways",
    "overlay_catechese_bridge",
    "retry_hardened_prefix",
}

_PROMPT_TEMPLATE_LABELS: dict[str, str] = {
    "instructions_base_md": "Socle — consignes générales (structure du prompt)",
    "overlay_takeaways": "Surcouche — inclure « Le Psaume : Ma réponse » + « À retenir »",
    "overlay_no_takeaways": "Surcouche — sans section « À retenir »",
    "overlay_catechese_bridge": "Surcouche — passerelle catéchèse (Stone Card)",
    "retry_hardened_prefix": "Surcouche — préfixe de relance (anti-hallucination renforcée)",
}


@st.cache_data(ttl=300, show_spinner=False)
def _load_prompt_templates_cached(*, gsheet_id: str, service_account_fingerprint: str) -> dict[str, str]:
    """
    Charge les prompts IA depuis Google Sheets (onglet `Paramètres_IA`, standard MARPA).
    Cache court pour éviter de relire Sheets à chaque run Streamlit.
    """
    if not gsheet_id:
        return {}
    # Le fingerprint force une séparation de cache par environnement/compte.
    _ = service_account_fingerprint

    cfg = load_config()
    if not cfg.gcp_service_account:
        return {}

    gs = build_gspread_client(cfg.gcp_service_account)
    rows = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="Paramètres_IA", limit=5000)
    latest = pick_effective_templates(rows, allowed_keys=set(_PROMPT_TEMPLATE_KEYS))
    return {k: v.content_md for k, v in latest.items() if k in _PROMPT_TEMPLATE_KEYS and v.content_md}


def _service_account_fingerprint(sa: object) -> str:
    try:
        d = dict(sa or {})
        stable = "|".join(
            [
                str(d.get("project_id") or ""),
                str(d.get("client_email") or ""),
                str(d.get("private_key_id") or ""),
            ]
        )
        return compute_sha256_text(stable)[:16]
    except Exception:
        return "na"


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
    m.setAttribute('content', 'width=device-width, initial-scale=1, viewport-fit=cover');
  } catch (e) {}
})();
</script>
        """,
        height=0,
        width=0,
    )


def _inject_expander_footer_scroll() -> None:
    """Ouverture d’un ``st.expander`` : petite correction de scroll pour ne pas perdre le contenu sous le footer fixe."""
    components.html(
        """
<script>
(function () {
  var rootWin = window.parent || window;
  var doc = rootWin.document || document;
  if (doc.__lumenviaExpFooterScroll) return;
  doc.__lumenviaExpFooterScroll = true;

  function footerReservePx() {
    try {
      var f = doc.querySelector(".lv-footer-stack") || doc.querySelector(".lv-footer-fixed");
      if (f && f.getBoundingClientRect)
        return Math.ceil(f.getBoundingClientRect().height) + 20;
    } catch (e) {}
    return 92;
  }

  function bumpScroll(detailsEl) {
    if (!detailsEl || !detailsEl.open) return;
    var reserve = footerReservePx();
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        try {
          detailsEl.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
          var rect = detailsEl.getBoundingClientRect();
          var vh = rootWin.innerHeight || doc.documentElement.clientHeight || 720;
          var bottomLimit = vh - reserve;
          var overflow = rect.bottom - bottomLimit;
          if (overflow <= 6) return;
          var dy = overflow + 8;
          var se = doc.scrollingElement || doc.documentElement || doc.body;
          try {
            se.scrollBy({ top: dy, behavior: "smooth" });
          } catch (e2) {
            rootWin.scrollBy(0, dy);
          }
        } catch (e) {}
      });
    });
  }

  try {
    var appRoot = doc.querySelector('[data-testid="stAppViewContainer"]') || doc.body;
    if (!appRoot || !window.MutationObserver) return;

    function onDetails(details) {
      if (!details.open) return;
      if (!(details.closest && details.closest('[data-testid="stExpander"]'))) return;
      bumpScroll(details);
    }

    appRoot.querySelectorAll('[data-testid="stExpander"] details').forEach(function (d) {
      if (d.open) onDetails(d);
    });

    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        var t = m.target;
        if (!t || t.tagName !== "DETAILS") continue;
        if (!(t.closest && t.closest('[data-testid="stExpander"]'))) continue;
        if (m.attributeName !== "open") continue;
        onDetails(t);
      }
    });
    mo.observe(appRoot, { attributes: true, attributeFilter: ["open"], subtree: true });

    doc.addEventListener(
      "click",
      function (ev) {
        try {
          var s = ev.target && ev.target.closest && ev.target.closest('[data-testid="stExpander"] summary');
          if (!s) return;
          var exp0 = s.closest('[data-testid="stExpander"]');
          var det = exp0
            ? exp0.querySelector("details") ||
              exp0.querySelector('[data-testid="stExpanderDetails"]')
            : null;
          if (!det) return;
          window.setTimeout(function () {
            onDetails(det);
          }, 90);
          window.setTimeout(function () {
            onDetails(det);
          }, 320);
        } catch (e) {}
      },
      true
    );
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
    _inject_expander_footer_scroll()
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
  --jopai-turquoise: #0d9488;
  --jopai-petrole: #0b2745;
}

/* 1. Reset Global & Fond */
html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
  background-color: var(--liturgie-cream) !important;
  color: var(--liturgie-text) !important;
  font-family: 'Lora', serif !important;
}

/* Chrome Streamlit (Deploy / menu ⋮) : assez d’air en haut pour ne pas tronquer le logo ; safe-area sur mobile. */
header[data-testid="stHeader"] {
  padding-top: max(0.28rem, env(safe-area-inset-top, 0px)) !important;
  padding-bottom: 0.28rem !important;
}
[data-testid="stToolbar"] {
  padding-top: 0 !important;
  padding-bottom: 0.35rem !important;
  margin-bottom: 0 !important;
}
[data-testid="stDecoration"] hr {
  margin: 0.2rem auto !important;
}
section[data-testid="stMain"] .block-container {
  padding-top: max(0.45rem, calc(0.2rem + env(safe-area-inset-top, 0px))) !important;
  /* Footer fixe (immuable + bandeau dev) */
  padding-bottom: max(6.6rem, calc(5.35rem + env(safe-area-inset-bottom, 0px))) !important;
}
/* Bureau / large fenêtre : marge haute un peu plus généreuse (logo + menu ne doivent pas toucher / être coupés par la chrome). */
@media (min-width: 1025px) {
  header[data-testid="stHeader"] {
    padding-top: max(1.05rem, env(safe-area-inset-top, 0px)) !important;
    padding-bottom: 0.52rem !important;
  }
  section[data-testid="stMain"] .block-container {
    padding-top: max(1.75rem, calc(1.2rem + env(safe-area-inset-top, 0px))) !important;
  }
}
@media (max-width: 1024px) {
  header[data-testid="stHeader"] {
    padding-top: max(0.42rem, env(safe-area-inset-top, 0px)) !important;
    padding-bottom: 0.32rem !important;
  }
  section[data-testid="stMain"] .block-container {
    padding-top: max(0.85rem, calc(0.45rem + env(safe-area-inset-top, 0px))) !important;
  }
  html, body {
    overflow-x: hidden !important;
  }
  /* Clavier vs saisie : quand un textarea est actif, réserver de la hauteur pour faire défiler la zone au-dessus du clavier */
  section[data-testid="stMain"] .block-container:has(textarea:focus) {
    padding-bottom: max(20vh, 12rem, env(safe-area-inset-bottom, 0px)) !important;
  }
}

/*
  Navigation (top_nav) : colonne Menu + 4 tuiles Rubriques.
  ≥1025px : boutons Rubriques visibles, colonne Menu masquée.
  ≤1024px : uniquement « Menu ⌵ » — secours `lv_nav_five_cols` (clé Stable Streamlit) si :has ne matche pas.
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
  div[data-testid="stPopoverBody"] button[kind="secondary"],
  [data-testid="stPopoverContent"] button[kind="secondary"],
  [data-baseweb="popover"] button[kind="secondary"] {
    width: 100% !important;
    min-height: 55px !important;
    font-size: 1rem !important;
  }
  /* Barre grille admin (bureau) : hors-champ réel téléphone/tablette ; aperçu mobile via tuile Simulateur */
  div[class*="st-key-lv_admin_desktop_shell"],
  div[data-testid="stVerticalBlock"][class*="st-key-lv_admin_desktop_shell"] {
    display: none !important;
  }
}

/* Fallback ciblé — ancêtre avec clé projet (couvre mobiles où le bloc à 5 colonnes n’est pas le « dernier enfant ») */
@media (max-width: 1024px) {
  div[class*="st-key-lv_nav_five_cols"] div[data-testid="stHorizontalBlock"] {
    flex-direction: column !important;
    align-items: stretch !important;
  }
  div[class*="st-key-lv_nav_five_cols"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:first-child {
    width: 100% !important;
    max-width: 100% !important;
    flex: 1 1 auto !important;
  }
  div[class*="st-key-lv_nav_five_cols"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:not(:first-child) {
    display: none !important;
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
  /* Empêche les retours à la ligne au milieu des mots */
  word-break: keep-all !important;
  overflow-wrap: normal !important;
  hyphens: none !important;
}
/* Parfois Streamlit rend le label dans un <span> */
button[kind="secondary"] span {
  white-space: pre-line !important;
  text-align: center !important;
  line-height: 1.15 !important;
  word-break: keep-all !important;
  overflow-wrap: normal !important;
  hyphens: none !important;
  color: var(--liturgie-text) !important;
}

/* Navigation web : jamais de débordement de texte hors tuile (max 2 lignes) */
div[class*="st-key-lv_nav_web_one_row"] button[kind="secondary"] p {
  display: -webkit-box !important;
  -webkit-line-clamp: 2 !important;
  -webkit-box-orient: vertical !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
div[class*="st-key-lv_nav_web_one_row"] button[kind="secondary"] span {
  display: -webkit-box !important;
  -webkit-line-clamp: 2 !important;
  -webkit-box-orient: vertical !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
div[class*="st-key-lv_nav_web_one_row"] button[kind="secondary"] {
  min-height: 70px !important;
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
  /* Défilement : réserve sous le bloc quand footer fixe (complément au script `scrollIntoView`) */
  scroll-margin-bottom: max(7rem, calc(5.65rem + env(safe-area-inset-bottom, 0px))) !important;
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
    font-size: 0.74rem !important;
    padding: 0.35rem 0.25rem !important;
  }
  button[kind="secondary"] p {
    word-break: keep-all !important;
    overflow-wrap: normal !important;
  }
}

</style>
        """,
        unsafe_allow_html=True,
    )

    # Pied de page : bandeau « développement » + marque JOPAI (fixes, tous les écrans).
    dn = html_escape(LUMENVIA_DEVELOPMENT_NOTICE)
    st.markdown(
        f"""
<div class="lv-footer-stack">
  <div class="lv-dev-notice-banner" role="note" aria-label="Mention développement">{dn}</div>
  <div class="lv-footer-fixed" role="contentinfo" aria-label="Pied JOPAI">
    <div class="lv-footer-inner">
      <span class="lv-jopai-mark">
        <span class="lv-jop">JOP</span><span class="lv-ai">AI</span><sup>©</sup>
      </span>
      <span class="lv-footer-sep">·</span>
      <span class="lv-footer-txt">LumenVia - 2026 | TOUS DROITS RESERVES</span>
    </div>
  </div>
</div>
<style>
.lv-footer-stack{{
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 2147483000;
  display: flex;
  flex-direction: column;
}}
/* Première carte = hors bas d’écran ; dernière = bandeau JOPAI collé au bord inférieur. */
.lv-dev-notice-banner{{
  background: #F2F2F2;
  color: #7F8C8D;
  font-size: 10px;
  line-height: 1.38;
  text-align: center;
  padding: 5px 10px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  border-top: 1px solid rgba(0,0,0,0.06);
  border-bottom: 1px solid rgba(0,0,0,0.04);
}}
.lv-footer-fixed{{
  position: relative;
  background: var(--jopai-petrole);
  color: #ffffff;
  border-top: 1px solid rgba(255,255,255,0.10);
}}
.lv-footer-inner{{
  max-width: 920px;
  margin: 0 auto;
  padding: 0.65rem 0.9rem;
  display: flex;
  gap: 0.55rem;
  align-items: baseline;
  justify-content: center;
  font-family: 'Lora', serif;
  letter-spacing: 0.2px;
}}
.lv-jopai-mark{{
  color: var(--jopai-turquoise);
  font-size: 0.95rem;
}}
.lv-jopai-mark .lv-jop{{ font-weight: 700; }}
.lv-jopai-mark .lv-ai{{ font-style: italic; font-weight: 500; }}
.lv-jopai-mark sup{{ font-size: 0.65em; vertical-align: super; margin-left: 1px; }}
.lv-footer-sep{{ opacity: 0.55; }}
.lv-footer-txt{{ opacity: 0.92; font-size: 0.92rem; }}
@media (max-width: 520px){{
  .lv-footer-inner{{ padding: 0.62rem 0.7rem; }}
  .lv-footer-txt{{ font-size: 0.88rem; }}
  .lv-dev-notice-banner{{ font-size: 9.5px; padding: 4px 8px; }}
}}
</style>
        """.strip(),
        unsafe_allow_html=True,
    )


def _jopai_mark_html() -> str:
    """Marque immuable : JOP (gras) + AI (italique) + © (exposant)."""
    return (
        '<span class="lv-jopai-mark">'
        '<span class="lv-jop">JOP</span><span class="lv-ai">AI</span><sup>©</sup>'
        "</span>"
    )


# Pages Administration (sans « Quitter administration » ni toggle) — même ordre que la grille bureau.
_ADMIN_PAGES: tuple[tuple[str, str, str], ...] = (
    ("step3", "Visuels\nliturgiques", "admin_step3"),
    ("thumbs", "Vignettes\nCloud", "admin_thumbs"),
    ("vision", "Texte\nimages", "admin_vision"),
    ("readings_cache", "Cache\nlectures", "admin_readings_cache"),
    ("accounts", "Comptes\ninscrits", "admin_accounts"),
    ("emailing", "Emailing", "admin_emailing"),
    ("scheduler", "Planificateur", "admin_scheduler"),
    ("res", "Test\nressources", "admin_resources"),
    ("cdc", "Cahier\ndes\ncharges", "admin_cdc"),
    ("plan", "Plan\nconsolidé", "admin_plan"),
    ("mobile_sim", "Simulateur\nmobile", "admin_mobile_sim"),
)


def _admin_pages_for_device() -> list[tuple[str, str, str]]:
    """Sur téléphone réel, le simulateur mobile n’a pas de sens → masqué."""
    pages = list(_ADMIN_PAGES)
    try:
        if _lumenvia_phone_like_user_agent():
            pages = [p for p in pages if p[0] != "mobile_sim"]
    except Exception:
        pass
    return pages


def _admin_do_logout_navigation() -> None:
    """Sortie administration : même effet depuis la grille bureau ou depuis le Menu mobile."""
    st.session_state.pop("admin_authenticated", None)
    st.session_state.pop("admin_phone_preview", None)
    st.session_state.route = "about"


def _lumenvia_phone_like_user_agent() -> bool:
    """Détection téléphone via User-Agent (``st.context.headers``), sans dépendre du viewport/CSS.

    Sur certains téléphones Streamlit/hosting, les media-queries voient encore une largeur « bureau » alors que les
    contrôles tactiles nécessitent le layout « Menu seul ».
    """
    try:
        hdrs = getattr(st.context, "headers", None)
        if hdrs is None:
            return False
        ua = str(hdrs.get("user-agent") or hdrs.get("User-Agent") or "").lower()
    except Exception:
        return False
    if not ua.strip():
        return False
    # iPad en mode bureau : UA type Mac sans « mobi » — on garde la grille large.
    if "ipad" in ua and "mobi" not in ua:
        return False
    if "iphone" in ua or "ipod" in ua:
        return True
    if "android" in ua and "tablet" not in ua:
        return True
    if "mobi" in ua:
        return True
    return False


def _use_compact_top_nav() -> bool:
    """Menu dépliant seul : iframe (`lumenvia_narrow_nav`) ou client téléphone détecté par UA."""
    if st.session_state.get("lumenvia_narrow_nav"):
        return True
    return _lumenvia_phone_like_user_agent()


def render_admin_navigation_in_popover() -> None:
    """Tuiles Administration dans le Menu dépliant (viewport étroit / iframe / téléphone UA)."""
    if not st.session_state.get("admin_authenticated"):
        return
    st.divider()
    st.caption("Administration")
    for slug, label, rte in _admin_pages_for_device():
        short = label.replace("\n", " ")
        if st.button(short, key=f"adm_p_{slug}", use_container_width=True, type="secondary"):
            st.session_state.route = rte
            st.rerun()
    if st.button("Quitter administration", key="adm_p_logout", use_container_width=True, type="secondary"):
        _admin_do_logout_navigation()
        st.rerun()


def top_nav() -> str:
    if "route" not in st.session_state:
        st.session_state.route = "about"

    logo_path = Path("assets/branding/logo_mark.svg")
    uid = str(st.session_state.get("auth_user_entity_id") or "").strip()
    email = str(st.session_state.get("auth_email_lc") or "").strip()
    is_admin = bool(st.session_state.get("admin_authenticated"))
    compact_nav = _use_compact_top_nav()

    if logo_path.is_file():
        _, mid, _ = st.columns([1, 1, 1])
        with mid:
            st.image(str(logo_path), width=56)

    nbsp = "\u00A0"
    labels = [
        # Évite le markdown dans les labels (peut décaler le rendu). "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮" = LumenVia en gras unicode.
        ("about", f"𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮{nbsp}:\nc'est quoi?"),
        ("sunday", "La lumière\ndu dimanche"),
        ("memo", f"Mon\nAide‑Mémoire"),
        ("join", "S'inscrire à la Newsletter"),
        ("account", "Mon Compte"),
    ]

    def _nav_popover_body() -> None:
        for route, label in labels:
            short = label.replace("\n", " ")
            if st.button(short, key=f"nav_m_{route}", use_container_width=True, type="secondary"):
                st.session_state.route = route
                st.rerun()
        if st.button("Donner\nVotre avis", key="nav_m_feedback", use_container_width=True, type="secondary"):
            st.session_state.route = "feedback"
            st.rerun()
        if is_admin:
            render_admin_navigation_in_popover()

    # Nouvelle instance du popover par route pour refermer après navigation (BaseWeb peut garder le volet ouvert).
    _menu_pop_key = f"lv_menu_pop_{str(st.session_state.route)}"

    if compact_nav:
        # Téléphone / iframe étroit : un seul “Menu”
        with st.popover("Menu", use_container_width=True, key=_menu_pop_key):
            _nav_popover_body()
    else:
        # Tuile active : on colore l'entrée correspondant à la route courante.
        try:
            cur = str(st.session_state.get("route") or "").strip().lower()
        except Exception:
            cur = ""
        # Styles d'état actif (tuile courante) + bouton “Donner votre avis”
        active_tile_css = ""
        if cur in {r for r, _ in labels}:
            active_tile_css = f"""
div[class*="st-key-nav_w_{cur}"] button[kind="secondary"] {{
  background: rgba(212, 175, 55, 0.16) !important;
  border-color: rgba(212, 175, 55, 0.65) !important;
}}
            """.strip()
        # “Donner votre avis” (hors tuiles) : actif si route feedback
        feedback_bg = "rgba(212, 175, 55, 0.16)" if cur == "feedback" else "white"
        feedback_border = "rgba(212, 175, 55, 0.65)" if cur == "feedback" else "var(--liturgie-gold)"
        st.markdown(
            f"""
<style>
{active_tile_css}
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] {{
  background: {feedback_bg} !important;
  border-color: {feedback_border} !important;
}}
/* Force le retour à la ligne “Donner / Votre avis” */
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] p,
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] span {{
  white-space: pre-line !important;
  color: var(--liturgie-text) !important;
}}
</style>
            """.strip(),
            unsafe_allow_html=True,
        )
        # Version web : pas de tuile “Menu”, uniquement les entrées directes sur une seule ligne.
        # Les libellés peuvent avoir au plus 2 lignes via '\n' (CSS: white-space: pre-line).
        with st.container(key="lv_nav_web_one_row"):
            cols = st.columns([1, 1, 1, 1, 1], gap="small")
            for i, (route, label) in enumerate(labels):
                with cols[i]:
                    if st.button(label, key=f"nav_w_{route}", use_container_width=True, type="secondary"):
                        st.session_state.route = route
                        st.rerun()

    if uid:
        b1, b2, b3 = st.columns([3.35, 1.45, 1.95], gap="small")
        with b1:
            st.caption(f"🟢 Connecté · {email or 'session active'}")
        with b2:
            if st.button(
                "Donner\nVotre avis",
                key="nav_feedback_beside_logout",
                type="secondary",
                use_container_width=True,
            ):
                st.session_state.route = "feedback"
                st.rerun()
        with b3:
            if st.button("Déconnexion", key="auth_logout_nav", use_container_width=True):
                for k in ("auth_user_entity_id", "auth_email_lc"):
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.pop("admin_authenticated", None)
                st.session_state.pop("admin_phone_preview", None)
                st.rerun()

    admin_nav_bar()

    # Styles des boutons “Déconnexion” / “Quitter administration” (couleurs distinctes si le DOM expose la clé).
    if uid or st.session_state.get("admin_authenticated"):
        _inject_admin_action_buttons_css()

    return st.session_state.route


def _inject_admin_action_buttons_css() -> None:
    """
    Accentue deux actions sensibles (Déconnexion / Quitter administration) sans changer la grille.
    Cible plusieurs versions Streamlit : `id`/data contenant la clé du widget lorsqu’elle est exposée.
    """
    st.markdown(
        """
<style>
/* Déconnexion — ton pétrole (charte footer) */
div[class*="st-key-nav_feedback_beside_logout"] button,
div[class*="nav_feedback_beside_logout"] button,
div[id*="nav_feedback_beside_logout"] button,
div[data-anchor-streamlit*="nav_feedback_beside_logout"] button {
  background-color: #ffffff !important;
  color: #0b2745 !important;
  border: 1px solid #D4AF37 !important;
  justify-content: center !important;
  min-height: 64px !important;
}
div[class*="st-key-nav_feedback_beside_logout"] button p,
div[class*="nav_feedback_beside_logout"] button p,
div[id*="nav_feedback_beside_logout"] button p,
div[data-anchor-streamlit*="nav_feedback_beside_logout"] button p {
  text-align: center !important;
  white-space: normal !important;
  overflow: visible !important;
  word-break: keep-all !important;
  line-height: 1.18 !important;
  width: 100% !important;
}
div[class*="st-key-auth_logout_nav"] button,
div[class*="auth_logout_nav"] button,
div[id*="auth_logout_nav"] button,
div[data-anchor-streamlit*="auth_logout_nav"] button {
  background-color: #145a72 !important;
  color: #ffffff !important;
  border-color: #0f4456 !important;
  justify-content: center !important;
  text-align: center !important;
  min-height: 64px !important;
}
div[class*="st-key-auth_logout_nav"] button p,
div[class*="auth_logout_nav"] button p,
div[id*="auth_logout_nav"] button p,
div[data-anchor-streamlit*="auth_logout_nav"] button p {
  color: #ffffff !important;
  text-align: center !important;
  white-space: normal !important;
  overflow: visible !important;
  word-break: keep-all !important;
  overflow-wrap: normal !important;
  hyphens: none !important;
  width: 100% !important;
  line-height: 1.2 !important;
}
div[class*="st-key-auth_logout_nav"] button span,
div[class*="auth_logout_nav"] button span,
div[id*="auth_logout_nav"] button span,
div[data-anchor-streamlit*="auth_logout_nav"] button span {
  color: #ffffff !important;
}
div[class*="st-key-auth_logout_nav"] button:hover,
div[class*="auth_logout_nav"] button:hover,
div[id*="auth_logout_nav"] button:hover,
div[data-anchor-streamlit*="auth_logout_nav"] button:hover {
  filter: brightness(1.06);
}

/* Quitter administration — doré/ocre */
div[class*="st-key-adm_nav_logout"] button,
div[class*="adm_nav_logout"] button,
div[id*="adm_nav_logout"] button,
div[data-anchor-streamlit*="adm_nav_logout"] button {
  background-color: #8b6914 !important;
  color: #ffffff !important;
  border-color: #654d0f !important;
}
div[class*="st-key-adm_nav_logout"] button p,
div[class*="adm_nav_logout"] button p,
div[id*="adm_nav_logout"] button p,
div[data-anchor-streamlit*="adm_nav_logout"] button p {
  color: #ffffff !important;
}
div[class*="st-key-adm_nav_logout"] button:hover,
div[class*="adm_nav_logout"] button:hover,
div[id*="adm_nav_logout"] button:hover,
div[data-anchor-streamlit*="adm_nav_logout"] button:hover {
  filter: brightness(1.06);
}

div[class*="st-key-adm_p_logout"] button,
div[class*="adm_p_logout"] button,
div[id*="adm_p_logout"] button,
div[data-anchor-streamlit*="adm_p_logout"] button {
  background-color: #8b6914 !important;
  color: #ffffff !important;
  border-color: #654d0f !important;
}
div[class*="st-key-adm_p_logout"] button p,
div[class*="adm_p_logout"] button p,
div[id*="adm_p_logout"] button p,
div[data-anchor-streamlit*="adm_p_logout"] button p {
  color: #ffffff !important;
}
div[class*="st-key-adm_p_logout"] button:hover,
div[class*="adm_p_logout"] button:hover,
div[id*="adm_p_logout"] button:hover,
div[data-anchor-streamlit*="adm_p_logout"] button:hover {
  filter: brightness(1.06);
}
</style>

        """,
        unsafe_allow_html=True,
    )


def _admin_login_and_password() -> tuple[str, str]:
    """Identifiant et mot de passe administrateur (exclusivement via `st.secrets`).

    Important sécurité : pas de valeurs par défaut dans le code.
    En environnement public, l’admin doit rester désactivée tant que non configurée.
    """
    try:
        s = st.secrets
        login = str(s.get("ADMIN_LOGIN", s.get("admin_login", ""))).strip().lower()
        password = str(s.get("ADMIN_PASSWORD", s.get("admin_password", ""))).strip()
    except Exception:
        login, password = "", ""
    return login, password


def admin_nav_bar() -> None:
    """Menu complémentaire réservé à la session administrateur (après connexion).

    Masqué en session **iframe simulateur** (`lumenvia_narrow_nav`) : l’admin y est uniquement sous Menu.
    Sur grand écran : `lv_admin_desktop_shell`. Sinon entrées sous Menu (CSS compact ou UA / iframe).
    """
    if not st.session_state.get("admin_authenticated"):
        return
    if _use_compact_top_nav():
        return
    pages = _admin_pages_for_device()
    with st.container(key="lv_admin_desktop_shell"):
        st.markdown("---")
        st.caption("Administration")
        # Rend toutes les tuiles admin sans dépendre d’index fixes
        tiles = list(pages)
        cols_per_row = 4
        for start in range(0, len(tiles), cols_per_row):
            row = tiles[start : start + cols_per_row]
            rcols = st.columns(cols_per_row, gap="small")
            for i, (slug, label, rte) in enumerate(row):
                with rcols[i]:
                    if st.button(label, key=f"adm_nav_{slug}", use_container_width=True, type="secondary"):
                        st.session_state.route = rte
                        st.rerun()
            # Complète la ligne avec “Quitter administration” si c’est la dernière ligne et qu’il reste de la place
            if start + cols_per_row >= len(tiles) and len(row) < cols_per_row:
                with rcols[len(row)]:
                    if st.button(
                        "Quitter\nadministration",
                        key="adm_nav_logout",
                        use_container_width=True,
                        type="secondary",
                    ):
                        _admin_do_logout_navigation()
                        st.rerun()
        # Si la dernière ligne était pleine, ajoute un bouton de sortie sur une ligne dédiée
        if len(tiles) % cols_per_row == 0:
            r = st.columns(cols_per_row, gap="small")
            with r[cols_per_row - 1]:
                if st.button(
                    "Quitter\nadministration",
                    key="adm_nav_logout",
                    use_container_width=True,
                    type="secondary",
                ):
                    _admin_do_logout_navigation()
                    st.rerun()


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
) -> tuple[tuple[bytes, str] | None, str | None, str | None]:
    """Dernière génération du jour : (audio bytes, mime) + texte synthèse GCS + URL publique audio (si possible)."""
    try:
        gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=3000)
        gens_day = [
            g
            for g in gens
            if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
        ]
        if not gens_day:
            return None, None, None
        latest = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        gen_eid = str(latest.get("entity_id") or "").strip()
        if not gen_eid:
            return None, None, None

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
            return None, syn_text, None
        aud = sorted(aud_rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]
        path = str(aud.get("gcs_path") or "").strip()
        if not path:
            return None, syn_text, None
        raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
        mime_guess = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        b, mime, _ = normalize_audio_bytes(audio_bytes=raw, mime_type=mime_guess)
        # On renvoie le path GCS, le lien public éventuel sera construit côté UI si besoin.
        return (b, mime), syn_text, path
    except Exception:
        return None, None, None


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


def _fetch_existing_fascicule_pdf_bytes(*, gcs: object, cfg: object, date_str: str) -> bytes | None:
    """PDF déjà généré et stocké sous Fascicules/ (si présent)."""
    path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
    try:
        return download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path)
    except Exception:
        return None


def _try_show_liturgy_illustration(*, gcs: object, cfg: object, date_str: str) -> None:
    """Étape produit 3 : affiche une image si présente dans GCS (vignette ou originale)."""
    img_b = _fetch_liturgy_illustration_display_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
    if img_b:
        st.image(io.BytesIO(img_b), use_container_width=True)
        st.caption(f"Illustration du dimanche {_french_day_month_year(date_str)}")


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


def _french_day_month_year(date_str: str) -> str:
    """Date courte : jour + mois + année (sans jour de semaine)."""
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
    return f"{d.day} {mois[d.month - 1]} {d.year}"


def _french_weekday_day_month_year(date_str: str) -> str:
    """Pour une phrase comme « … la célébration du dimanche 10 mai 2026 » — jour en minuscules dans le cours de phrase."""
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
    except Exception:
        return str(date_str).strip()[:10]
    jours_sem = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    return f"{jours_sem[d.weekday()]} {_french_day_month_year(date_str)}"


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


_ABOUT_MARKDOWN = """
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


def render_about() -> None:
    st.title("JOPAI LumenVia")
    try:
        st.image("Parole.jpg", use_container_width=True)
    except Exception:
        pass

    # Citation : centrée + couleur thème (autre que noir)
    try:
        quote, rest = _ABOUT_MARKDOWN.split("\n\n", 1)
    except Exception:
        quote, rest = _ABOUT_MARKDOWN, ""
    qtxt = quote.strip().strip("«").strip("»").strip()
    if qtxt:
        st.markdown(
            f"<div style='text-align:center;color:var(--liturgie-accent);font-style:italic;"
            f"font-size:1.02rem;line-height:1.55;margin:0.25rem auto 0.95rem;max-width:min(44rem,95vw);'>"
            f"« {html_escape(qtxt.strip('* ').strip())} »</div>",
            unsafe_allow_html=True,
        )
    if rest.strip():
        st.markdown(rest.strip())
    st.subheader("Référence")
    st.markdown(
        'Source liturgique : [AELF](https://api.aelf.org/) (Association Épiscopale Liturgique pour les pays Francophones).'
    )


def render_sunday() -> None:
    st.title("La Lumière du Dimanche")
    zone = "france"
    cfg = load_config()

    def _normalize_aelf_text_for_cache(s: str | None) -> str:
        """
        Normalise les textes AELF pour le stockage en Sheets.

        Mode “extrême” : on supprime TOUS les retours chariot et on stocke un seul bloc.
        Le rendu (PDF / UI) se chargera ensuite du wrap et de la mise en forme.
        """
        raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        # Remplace tout whitespace (incluant \n) par des espaces, puis compacte.
        return re.sub(r"\s+", " ", raw).strip()

    def _sunday_of_week(d: date) -> date:
        """Retourne le dimanche de la semaine ISO contenant d (dimanche inclus)."""
        return d + timedelta(days=(6 - d.weekday()) % 7)

    # UX: l’utilisateur peut choisir n’importe quel jour ; on affiche le DIMANCHE de la semaine.
    default = date.today()
    if "_lumenvia_sunday_qs" in st.session_state:
        try:
            default = date.fromisoformat(str(st.session_state.pop("_lumenvia_sunday_qs"))[:10])
        except Exception:
            pass
    chosen_any = st.date_input(
        "Sélectionnez une date au calendrier pour préparer ou revivre la synthèse illustrée du dimanche correspondant.",
        value=default,
    )

    @st.cache_data(ttl=180, show_spinner=False, max_entries=24)
    def _month_content_status(
        *,
        gsheet_id: str,
        service_account_fp: str,
        year: int,
        month: int,
        zone: str,
        bucket_name: str | None,
    ) -> dict[str, dict[str, bool]]:
        """
        Retourne un mapping date_iso -> {text,audio,pdf} pour les dimanches du mois.
        Objectif : affichage indicatif (encerclage) sans empêcher la régénération.
        """
        out: dict[str, dict[str, bool]] = {}
        try:
            gs = build_gspread_client(load_config().gcp_service_account)
            gens = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="generations", limit=6000)
            aud = fetch_records(gspread_client=gs, spreadsheet_id=gsheet_id, table="audio", limit=6000)
        except Exception:
            gens, aud = [], []

        # Dernière génération par date
        gen_by_date: dict[str, dict] = {}
        for r in gens:
            if str(r.get("zone") or "").strip() != zone:
                continue
            ds = str(r.get("date") or "").strip()[:10]
            if len(ds) != 10:
                continue
            try:
                d = date.fromisoformat(ds)
            except Exception:
                continue
            if d.year != int(year) or d.month != int(month):
                continue
            prev = gen_by_date.get(ds)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                gen_by_date[ds] = r

        # Audio par gen_entity_id (au moins une ligne)
        audio_gen_ids = {str(r.get("gen_entity_id") or "").strip() for r in aud if str(r.get("gen_entity_id") or "").strip()}

        # PDF : existence objet GCS (best-effort)
        pdf_exists: set[str] = set()
        if bucket_name:
            try:
                cfg2 = load_config()
                gcs = build_gcs_client(cfg2.gcp_service_account)
                for ds in gen_by_date.keys():
                    path = f"Fascicules/{ds}/lumenvia_dimanche_{ds}.pdf"
                    try:
                        if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                            pdf_exists.add(ds)
                    except Exception:
                        continue
            except Exception:
                pass

        # Dimanches du mois (tous) : on marque ceux qui ont contenu
        import calendar as _cal

        cal = _cal.Calendar(firstweekday=0)  # lundi
        for d in cal.itermonthdates(int(year), int(month)):
            if d.month != int(month):
                continue
            if d.weekday() != 6:  # dimanche
                continue
            ds = d.isoformat()
            g = gen_by_date.get(ds)
            has_text = bool(str((g or {}).get("text_gcs_path") or "").strip())
            gen_id = str((g or {}).get("entity_id") or "").strip()
            has_audio = bool(gen_id and gen_id in audio_gen_ids)
            out[ds] = {"text": has_text, "audio": has_audio, "pdf": (ds in pdf_exists)}
        return out

    # Mini-calendrier HTML : dimanches encerclés si contenu déjà présent
    if cfg.gcp_service_account and cfg.gsheet_id:
        try:
            try:
                qp_open_cal = str(st.query_params.get("open_cal") or "").strip().lower() in ("1", "true", "oui", "yes", "on")
            except Exception:
                qp_open_cal = False
            fp = _service_account_fingerprint(getattr(cfg, "gcp_service_account", {}) or {})
            bucket = str(cfg.gcs_bucket_name or "").strip() or None
            st_map = _month_content_status(
                gsheet_id=str(cfg.gsheet_id).strip(),
                service_account_fp=fp,
                year=int(chosen_any.year),
                month=int(chosen_any.month),
                zone=zone,
                bucket_name=bucket,
            )
            # Rendu HTML
            import calendar as _cal2

            cal2 = _cal2.Calendar(firstweekday=0)
            weeks = cal2.monthdatescalendar(int(chosen_any.year), int(chosen_any.month))
            mois_fr = (
                "janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"
            )[int(chosen_any.month) - 1]
            rows_html: list[str] = []
            for w in weeks:
                tds: list[str] = []
                for d in w:
                    in_month = (d.month == int(chosen_any.month))
                    ds = d.isoformat()
                    st0 = st_map.get(ds) or {}
                    is_sun = d.weekday() == 6
                    has_any = bool(st0.get("text") or st0.get("audio") or st0.get("pdf"))
                    ring = "lv-ring" if (in_month and is_sun and has_any) else ("lv-sun" if (in_month and is_sun) else "")
                    muted = "lv-muted" if not in_month else ""
                    # Clique sur un dimanche avec contenu → charge ce dimanche (comme si sélectionné au date_input).
                    href = f"?sunday={ds}&open_cal=1" if (in_month and is_sun and has_any) else ""
                    inner = (
                        f"<a class='lv-daylink' href='{href}' target='_self'>{d.day}</a>"
                        if href
                        else str(d.day)
                    )
                    tds.append(
                        f"<td class='{muted}'><div class='lv-day {ring}'>{inner}</div></td>"
                    )
                rows_html.append("<tr>" + "".join(tds) + "</tr>")

            html = f"""
<div style="margin:0.35rem auto 0.15rem;max-width:420px;">
  <div style="text-align:center;color:#6b5918;font-weight:700;margin-bottom:0.25rem;font-size:0.95rem;">
    Dimanches déjà générés — {mois_fr} {chosen_any.year}
  </div>
  <div style="border:1px solid rgba(212,175,55,0.30);background:rgba(255,255,255,0.62);padding:0.25rem 0.25rem 0.35rem;">
    <table style="width:100%;border-collapse:collapse;text-align:center;font-size:0.85rem;">
      <thead>
        <tr style="opacity:0.85;">
          <th style="padding:3px 0;">L</th><th>M</th><th>M</th><th>J</th><th>V</th><th>S</th><th>D</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
    <div style="display:flex;gap:0.55rem;justify-content:center;margin-top:0.25rem;font-size:0.78rem;opacity:0.9;">
      <span><span class="lv-legend-ring"></span> Dimanche avec contenu</span>
    </div>
  </div>
</div>
<style>
.lv-day{{position:relative;display:inline-flex;align-items:center;justify-content:center;width:28px;height:24px;border-radius:9px;margin:1px auto;color:var(--liturgie-text);font-size:0.86rem;}}
.lv-sun{{color:#6b5918;font-weight:600;}}
.lv-ring{{outline:1px solid var(--liturgie-accent);outline-offset:1px;border-radius:9px;}}
.lv-daylink{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:100%;color:inherit;text-decoration:none;}}
.lv-daylink:hover{{text-decoration:underline;}}
.lv-muted .lv-day{{opacity:0.35;}}
.lv-legend-ring{{display:inline-block;width:9px;height:9px;border-radius:3px;outline:1px solid var(--liturgie-accent);outline-offset:1px;margin-right:0.25rem;vertical-align:middle;}}
</style>
            """.strip()
            with st.expander(
                f"Voir les contenus déjà disponibles — {mois_fr} {chosen_any.year}",
                expanded=bool(qp_open_cal),
            ):
                st.markdown(html, unsafe_allow_html=True)
        except Exception:
            pass
    chosen = _sunday_of_week(chosen_any)
    if chosen_any != chosen:
        d_fr = html_escape(_french_day_month_year(chosen.isoformat()))
        st.caption(f"Le dimanche **{d_fr}**")
    date_str = chosen.isoformat()

    gcs_top: object | None = None
    if cfg.gcp_service_account and cfg.gcs_bucket_name:
        try:
            gcs_top = build_gcs_client(cfg.gcp_service_account)
        except Exception:
            gcs_top = None

    pdf_key = f"liturgy_sunday_pdf_{date_str}"
    pdf_bytes_for_user: bytes | None = st.session_state.get(pdf_key)
    if pdf_bytes_for_user is None and gcs_top and cfg.gcs_bucket_name:
        try:
            pdf_bytes_for_user = _fetch_existing_fascicule_pdf_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
        except Exception:
            pdf_bytes_for_user = None

    # Lectures : on utilise d'abord un cache Sheets (si configuré), sinon AELF, sinon cache local disque.
    offline = False
    cached_at = ""
    with st.spinner("Récupération des lectures…"):
        identity = None
        texts = None
        # 1) Cache Sheets (si disponible)
        if cfg.gcp_service_account and cfg.gsheet_id:
            try:
                from core.sheets_db import TableSpec, ensure_table

                gs = build_gspread_client(cfg.gcp_service_account)
                ensure_table(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table=TableSpec(
                        name="readings_cache",
                        columns=with_concat(
                            [
                                *BASE_COLUMNS,
                                "date",
                                "zone",
                                "periode",
                                "semaine",
                                "annee",
                                "couleur",
                                "fete",
                                "jour_liturgique_nom",
                                "premiere_lecture",
                                "psaume",
                                "deuxieme_lecture",
                                "evangile",
                                "source",
                                "error",
                            ]
                        ),
                    ),
                )
                rc = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=800)
                hits = [
                    r
                    for r in rc
                    if str(r.get("date") or "").strip() == date_str[:10]
                    and str(r.get("zone") or "").strip() == zone
                    and sheet_row_status_is_live(r.get("status"))
                    and not str(r.get("error") or "").strip()
                ]
                if hits:
                    best = sorted(hits, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
                    from core.aelf import AelfDayIdentity, AelfTexts

                    identity = AelfDayIdentity(
                        date=str(best.get("date") or date_str[:10]),
                        zone=str(best.get("zone") or zone),
                        periode=str(best.get("periode") or "") or None,
                        semaine=str(best.get("semaine") or "") or None,
                        annee=str(best.get("annee") or "") or None,
                        couleur=str(best.get("couleur") or "") or None,
                        fete=str(best.get("fete") or "") or None,
                        jour_liturgique_nom=str(best.get("jour_liturgique_nom") or "") or None,
                    )
                    texts = AelfTexts(
                        premiere_lecture=_normalize_aelf_text_for_cache(str(best.get("premiere_lecture") or "")) or None,
                        psaume=_normalize_aelf_text_for_cache(str(best.get("psaume") or "")) or None,
                        deuxieme_lecture=_normalize_aelf_text_for_cache(str(best.get("deuxieme_lecture") or "")) or None,
                        evangile=_normalize_aelf_text_for_cache(str(best.get("evangile") or "")) or None,
                    )
            except Exception:
                pass

        # 2) AELF API (cache streamlit) + snapshot disque
        if identity is None or texts is None:
            try:
                identity, texts = cached_aelf(date_str, zone=zone, _identity_schema=4)
                persist_aelf_snapshot(date_str, zone, identity, texts)
                # Écrit dans Sheets (sans champs chiffrés) pour éviter les appels futurs.
                if cfg.gcp_service_account and cfg.gsheet_id:
                    try:
                        gs2 = build_gspread_client(cfg.gcp_service_account)
                        append_immutable_row(
                            gspread_client=gs2,
                            spreadsheet_id=cfg.gsheet_id,
                            table="readings_cache",
                            values_by_col={
                                "entity_id": sha256(f"read|{date_str[:10]}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "date": date_str[:10],
                                "zone": zone,
                                "periode": getattr(identity, "periode", None) or "",
                                "semaine": getattr(identity, "semaine", None) or "",
                                "annee": getattr(identity, "annee", None) or "",
                                "couleur": getattr(identity, "couleur", None) or "",
                                "fete": getattr(identity, "fete", None) or "",
                                "jour_liturgique_nom": getattr(identity, "jour_liturgique_nom", None) or "",
                                "premiere_lecture": _normalize_aelf_text_for_cache(texts.premiere_lecture),
                                "psaume": _normalize_aelf_text_for_cache(texts.psaume),
                                "deuxieme_lecture": _normalize_aelf_text_for_cache(texts.deuxieme_lecture),
                                "evangile": _normalize_aelf_text_for_cache(texts.evangile),
                                "source": "aelf_api",
                                "error": "",
                            },
                        )
                    except Exception:
                        pass
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

    bundle_audio: tuple[bytes, str] | None = None
    bundle_synth_text: str | None = None
    bundle_audio_gcs_path: str | None = None
    bundle_from_disk = False
    if cfg.gcp_service_account and cfg.gsheet_id and cfg.gcs_bucket_name:
        try:
            gs_top = build_gspread_client(cfg.gcp_service_account)
            if gcs_top is None:
                gcs_top = build_gcs_client(cfg.gcp_service_account)
            bundle_audio, bundle_synth_text, bundle_audio_gcs_path = _fetch_existing_sunday_bundle(
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
            bundle_audio, bundle_synth_text, bundle_audio_gcs_path = None, None, None

    if not bundle_audio and not (bundle_synth_text or "").strip():
        disk_bundle = load_sunday_bundle(date_str, zone)
        if disk_bundle:
            bundle_synth_text, aud_b, aud_mime, _disk_at = disk_bundle
            bundle_from_disk = True
            if aud_b and aud_mime:
                bundle_audio = (aud_b, aud_mime)

    is_admin_sunday = bool(st.session_state.get("admin_authenticated"))

    total_words = _count_words(
        (texts.premiere_lecture or "")
        + "\n"
        + (texts.psaume or "")
        + "\n"
        + (texts.deuxieme_lecture or "")
        + "\n"
        + (texts.evangile or "")
    )

    st.subheader("Identité du jour")
    with st.container():
        # Trois formats : intro datée puis Pdf / Audio / Texte côte à côte (colonnes).
        has_pdf_fmt = bool(pdf_bytes_for_user)
        has_audio_fmt = bundle_audio is not None
        has_text_fmt = bool((bundle_synth_text or "").strip())
        date_prep = html_escape(_french_weekday_day_month_year(date_str))
        # Teintes tirées du couple or / sépia (charte liturgique) : lisibles sur fond crème, distinctes du corps #342E29.
        st.markdown(
            f"<p style=\"font-size:clamp(0.95rem, 0.35vw + 0.94rem, 1.06rem);line-height:1.52;"
            f"text-align:center;text-wrap:balance;max-width:min(42rem,calc(100% - 0.75rem));"
            f"margin:0 auto 0.85rem;color:#5f4f3a;\">"
            f"<strong style=\"color:#6b5918;font-weight:600;\">Trois formats</strong>"
            f"<span style=\"color:#5f4f3a;\"> disponibles proposé par "
            f"<strong style=\"color:#6b5918;font-weight:600;\">{_jopai_mark_html()} LumenVia</strong>"
            f" pour vous préparer</span>"
            f"<span style=\"color:#5f4f3a;\"><br/>à la célébration du "
            f"<strong style=\"color:#584610;\">{date_prep}</strong>.</span></p>",
            unsafe_allow_html=True,
        )
        col_pdf, col_audio, col_texte = st.columns([1, 1.25, 1], gap="medium")
        with col_pdf:
            if has_pdf_fmt:
                st.download_button(
                    label="Télécharger le PDF du dimanche",
                    data=pdf_bytes_for_user,
                    file_name=f"lumenvia_dimanche_{date_str}.pdf",
                    mime="application/pdf",
                    key=f"dl_sunday_top_{date_str}",
                    type="secondary",
                    use_container_width=True,
                )
            else:
                st.caption("Indisponible pour cette date.")
        with col_audio:
            if has_audio_fmt:
                st.markdown(
                    "<p style=\"text-align:center;margin:0 0 0.3rem;line-height:1.35;color:#5f4f3a;"
                    "font-size:0.95rem;\"><strong>Audio</strong></p>",
                    unsafe_allow_html=True,
                )
                if bundle_from_disk:
                    st.markdown(
                        "<p style=\"text-align:center;margin:0 0 0.35rem;line-height:1.35;"
                        "color:#5f4f3a;font-size:0.78rem;opacity:0.88;\">En cache sur cet appareil</p>",
                        unsafe_allow_html=True,
                    )
                st.audio(bundle_audio[0], format=bundle_audio[1])
            else:
                st.markdown(
                    "<p style=\"text-align:center;margin:0 0 0.25rem;line-height:1.4;color:#5f4f3a;"
                    "font-size:0.85rem;\">Pas encore publié. Les lectures sont affichées plus bas.</p>",
                    unsafe_allow_html=True,
                )
        with col_texte:
            with st.expander("Lire le texte de cette synthèse", expanded=False):
                if has_text_fmt:
                    st.markdown(bundle_synth_text)
                elif has_audio_fmt:
                    st.info(
                        "Le texte de la synthèse n’est pas disponible (Cloud ou cache local). "
                        "Vérifie `text_gcs_path` dans la table generations si tu utilises le cloud."
                    )
                else:
                    st.caption("Le texte de la synthèse n’est pas encore disponible pour cette date.")

        if not has_pdf_fmt and not has_audio_fmt and not has_text_fmt:
            _synth_na_msg = (
                "Pour le moment, **seules les lectures** du dimanche sont disponibles sur cette page : "
                "la synthèse (texte et audio) réalisée avec l’aide de l’IA n’a pas encore été publiée.\n\n"
                "Si vous vous êtes **inscrit au service** depuis la rubrique **« Nous rejoindre »**, "
                "vous recevrez une **notification automatique** lorsqu’elle sera prête — en général "
                "**quelques jours avant** la célébration."
            )
            if is_admin_sunday:
                _synth_na_msg += (
                    "\n\n**Administrateur —** C’est le message vu par tous les visiteurs tant qu’il n’y a ni synthèse "
                    "ni PDF. Tu peux **générer la synthèse et l’audio**, puis **préparer le fascicule PDF**, "
                    "dans les blocs **Administration** affichés juste ci‑dessous."
                )
            st.info(_synth_na_msg, icon="📖")
        if is_admin_sunday:
            st.divider()
            if gcs_top and cfg.gcs_bucket_name:
                prep_key = f"prep_liturgy_pdf_{date_str}"
                st.caption("Administration — fascicule PDF")
                has_any_synthesis = bool((bundle_synth_text or "").strip()) or (bundle_audio is not None)
                if not has_any_synthesis:
                    st.info(
                        "Le fascicule PDF « complet » (avec synthèse) n’a pas encore de contenu : "
                        "génère d’abord **la synthèse et l’audio** ci‑dessous. "
                        "Sinon, le PDF contiendrait essentiellement les lectures.",
                        icon="ℹ️",
                    )
                include_catechese_pdf = st.checkbox(
                    "Inclure la « Passerelle catéchèse — L’écho des paraboles » dans le PDF",
                    value=True,
                    key=f"pdf_catechese_{date_str}",
                    help="Si la synthèse contient cette section, elle sera incluse dans le PDF (coché par défaut).",
                )
                force_regen_pdf = st.checkbox(
                    "Régénérer le PDF (ignorer le PDF déjà stocké sur Cloud)",
                    value=False,
                    key=f"pdf_force_regen_{date_str}",
                )
                allow_readings_only_pdf = st.checkbox(
                    "Autoriser un PDF lectures seules (sans synthèse)",
                    value=False,
                    key=f"pdf_allow_readings_only_{date_str}",
                    help="À utiliser uniquement si tu veux un PDF minimal (lectures), en attendant la synthèse.",
                    disabled=has_any_synthesis,
                )
                can_build_pdf = bool(has_any_synthesis or allow_readings_only_pdf)
                if st.button("Préparer le PDF du dimanche (complet)", key=prep_key, disabled=not can_build_pdf):
                    ov_pdf = loading_overlay("Préparation du PDF (couverture + lectures + synthèse)…")
                    try:
                        if not force_regen_pdf:
                            cached_pdf = _fetch_existing_fascicule_pdf_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
                            if cached_pdf:
                                st.session_state[pdf_key] = cached_pdf
                                st.info("PDF déjà généré — réutilisation depuis Cloud.")
                                cached_pdf = None
                        img_b = _fetch_liturgy_illustration_full_bytes(gcs=gcs_top, cfg=cfg, date_str=date_str)
                        aud_url, aud_note = _public_app_listen_url(date_str=date_str)
                        if bundle_audio_gcs_path:
                            signed = _gcs_signed_url(
                                gcs=gcs_top,
                                bucket_name=str(cfg.gcs_bucket_name).strip(),
                                path=bundle_audio_gcs_path,
                            )
                            if signed:
                                aud_url = signed
                        synth_for_pdf = bundle_synth_text if has_any_synthesis else ""
                        if not include_catechese_pdf:
                            synth_for_pdf = _strip_catechese_bridge(synth_for_pdf)
                        back_cover_b = None
                        try:
                            y = str(date_str)[:4]
                            back_cover_b = download_bytes(
                                gcs=gcs_top,
                                bucket_name=str(cfg.gcs_bucket_name).strip(),
                                path=f"Images/thumbs/montage_{y}.png",
                            )
                        except Exception:
                            back_cover_b = None

                        # Titre PDF sur 2 lignes : fête puis (semaine du Psautier uniquement)
                        semaine_psautier = (getattr(identity, "semaine", None) or "").strip()
                        line1 = _liturgy_display_label(
                            (getattr(identity, "fete", None) or "").strip()
                            or (_jour_liturgique(identity) or "").strip()
                            or _liturgy_cover_pdf_title(identity)
                        )
                        line2 = ""
                        if semaine_psautier and ("psautier" in semaine_psautier.lower()):
                            lbl = _liturgy_display_label(semaine_psautier).strip()
                            line2 = f"({lbl})" if lbl else ""
                        week_title_pdf = (line1 + ("\n" + line2 if line2 else "")).strip()

                        # Index de la vignette du dimanche dans le montage annuel (pour encadrer la semaine correspondante)
                        highlight_idx = None
                        try:
                            manifest = json.loads(
                                Path("data/manifests/illustration_pipeline.json").read_text(encoding="utf-8")
                            )
                            targets = manifest.get("targets") or []
                            year = str(date_str)[:4]
                            year_targets = [t for t in targets if str(t.get("date") or "").startswith(year)]
                            year_dates = [str(t.get("date") or "")[:10] for t in year_targets]
                            if str(date_str)[:10] in year_dates:
                                highlight_idx = int(year_dates.index(str(date_str)[:10]))
                        except Exception:
                            highlight_idx = None

                        pdf_b = build_liturgy_sunday_pdf_bytes(
                            image_bytes=img_b,
                            week_title=week_title_pdf,
                            date_line=_french_long_date_label(date_str),
                            meta_line=(
                                f"{_liturgy_display_label(getattr(identity, 'periode', None))} · "
                                f"Cycle {_cycle_year_display(getattr(identity, 'annee', None))} · "
                                f"{_liturgy_display_label(getattr(identity, 'couleur', None))}"
                            ),
                            premiere_lecture=texts.premiere_lecture,
                            psaume=texts.psaume,
                            deuxieme_lecture=texts.deuxieme_lecture,
                            evangile=texts.evangile,
                            synthesis_text=synth_for_pdf,
                            audio_listen_url=aud_url,
                            audio_listen_note=aud_note,
                            about_markdown=_ABOUT_MARKDOWN,
                            back_cover_image_bytes=back_cover_b,
                            accent_hex=_liturgical_accent_hex(getattr(identity, "couleur", None)),
                            back_cover_highlight_cell_index=highlight_idx,
                        )
                        st.session_state[pdf_key] = pdf_b
                        try:
                            fasc_path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
                            upload_bytes(
                                gcs=gcs_top,
                                bucket_name=str(cfg.gcs_bucket_name).strip(),
                                path=fasc_path,
                                data=pdf_b,
                                content_type="application/pdf",
                            )
                            st.success("PDF enregistré.")
                        except Exception as ex:
                            st.warning(f"Impossible d’enregistrer le PDF sur Cloud (Fascicules/) : {ex}")
                    finally:
                        ov_pdf.empty()
                st.divider()
            st.caption("Administration — synthèse (texte + audio)")
            already_has_bundle = bool((bundle_synth_text or "").strip()) or (bundle_audio is not None)
            if already_has_bundle:
                st.info(
                    "Une synthèse existe déjà pour ce dimanche (texte et/ou audio). "
                    "Tout est **téléchargeable en haut de la page**. "
                    "Tu peux régénérer ci-dessous si besoin.",
                    icon="ℹ️",
                )
            pct = st.segmented_control(
                "Longueur (en % du total des lectures)",
                options=[10, 15, 20, 25, 30, 35, 40, 45, 50],
                default=20,
                format_func=lambda x: f"{x}%",
                key=f"adm_sunday_pct_{date_str}",
            )
            include_takeaways = st.checkbox(
                "Inclure “À retenir” (3–5 points)", value=True, key=f"adm_sunday_takeaways_{date_str}"
            )
            include_catechese_bridge_gen = st.checkbox(
                "Inclure « Passerelle catéchèse — L’écho des paraboles » (Stone Card)",
                value=True,
                help="Ajoute un encart pédagogique structuré pour la transmission (jeunes / catéchèse).",
                key=f"adm_sunday_catech_{date_str}",
            )
            auto_pdf = st.checkbox(
                "Générer aussi le PDF dans la foulée (peut prendre plus de temps)",
                value=False,
                key=f"adm_sunday_auto_pdf_{date_str}",
            )
            debug = st.toggle("Mode debug", value=False, key=f"adm_sunday_debug_{date_str}")
            if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
                st.warning("Configuration incomplète (service account / gsheet_id / bucket). Synthèse indisponible.")
            elif st.button(
                "Régénérer la synthèse et l’audio" if already_has_bundle else "Générer la synthèse et l’audio",
                type="primary",
                key=f"adm_sunday_gen_{date_str}",
            ):
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
                        include_catechese_bridge=bool(include_catechese_bridge_gen),
                        generate_pdf=bool(auto_pdf),
                        debug=bool(debug),
                        cfg=cfg,
                    )
                    st.rerun()
                finally:
                    overlay.empty()

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

    if gcs_top and cfg.gcs_bucket_name:
        _try_show_liturgy_illustration(gcs=gcs_top, cfg=cfg, date_str=date_str)

    st.subheader("Lectures")
    # (supprimé) Total lectures : non affiché
    render_liturgy_block("Première lecture", texts.premiere_lecture)
    render_liturgy_block("Psaume", texts.psaume)
    render_liturgy_block("Deuxième lecture", texts.deuxieme_lecture)
    render_liturgy_block("Évangile", texts.evangile)


def _run_generate_sunday_flow(
    *,
    _overlay: object,
    identity: object,
    texts: object,
    zone: str,
    total_words: int,
    pct: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    generate_pdf: bool,
    debug: bool,
    cfg: object,
) -> None:
    target_words = max(80, int(total_words * (pct / 100.0)))
    # La Passerelle catéchèse ajoute un module structuré : on augmente le budget pour éviter qu’elle disparaisse.
    if include_catechese_bridge:
        target_words += 180
    templates: dict[str, str] = {}
    try:
        templates = _load_prompt_templates_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=_service_account_fingerprint(getattr(cfg, "gcp_service_account", {}) or {}),
        )
    except Exception:
        templates = {}

    instructions_struct = templates.get("instructions_base_md") or Path("data/instructions_ia.md").read_text(
        encoding="utf-8"
    )
    # Double blind : la "secret sauce" n'est pas dans Sheets (A), mais dans st.secrets (B).
    try:
        s = st.secrets
        secret_sauce = str(s.get("IA_SECRET_SAUCE_MD") or s.get("ia_secret_sauce_md") or "").strip()
    except Exception:
        secret_sauce = ""
    instructions = (instructions_struct + "\n\n" + secret_sauce).strip() if secret_sauce else instructions_struct
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
        include_catechese_bridge=bool(include_catechese_bridge),
        templates=templates,
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
            # Évite les synthèses tronquées : 2048 tokens est souvent trop court pour une synthèse “longue”.
            # Heuristique simple (français) : ~2.2 tokens / mot avec marge.
            max_out = min(8192, max(2048, int(target_words * 2.2)))
            gen = vx.generate_text_auto(
                preferred_models=[
                    "gemini-2.5-flash",
                    "gemini-2.0-flash",
                    "gemini-pro-latest",
                    "gemini-flash-latest",
                ],
                prompt=prompt,
                max_output_tokens=max_out,
            )
        except Exception as e:
            if debug:
                st.exception(e)
            else:
                st.error("Erreur lors de la génération de la synthèse. Active le mode debug pour détails.")
            return
        t1 = time.perf_counter()
        perf["vertex_text_s"] = round(t1 - t0, 3)

    # Fiabilisation : si la sortie est tronquée, on retente automatiquement une fois
    # avec un modèle moins “pensant” et un budget de sortie maximal.
    cand0 = ((gen.raw or {}).get("candidates") or [{}])[0]
    fr = str(cand0.get("finishReason") or "").strip().upper()
    words_out = len((gen.text or "").split())
    has_citations = bool((cand0.get("citationMetadata") or {}).get("citations")) if isinstance(cand0, dict) else False
    looks_truncated = (fr in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH")) or (words_out < int(target_words * 0.85))
    if looks_truncated or has_citations:
        # Prompt “durci” : aucune URL / aucune citation / uniquement textes fournis.
        hardened_prefix = templates.get("retry_hardened_prefix") or (
            "IMPORTANT — SOURCES: ne cite aucune source externe, aucune URL, aucun site web. "
            "Utilise exclusivement les textes AELF fournis ci-dessous. "
            "IMPORTANT — FORMAT: réponds uniquement avec la synthèse, sans préambule technique."
        )
        hardened = hardened_prefix.strip() + "\n\n" + prompt
        try:
            t0b = time.perf_counter()
            gen2 = vx.generate_text_auto(
                preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                prompt=hardened,
                max_output_tokens=8192,
            )
            perf["vertex_text_retry_s"] = round(time.perf_counter() - t0b, 3)
            cand0b = ((gen2.raw or {}).get("candidates") or [{}])[0]
            fr2 = str(cand0b.get("finishReason") or "").strip().upper()
            words2 = len((gen2.text or "").split())
            cites2 = bool((cand0b.get("citationMetadata") or {}).get("citations")) if isinstance(cand0b, dict) else False
            if (fr2 in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH")) or (words2 < int(target_words * 0.85)) or cites2:
                st.error(
                    "Synthèse incomplète malgré une relance automatique (MAX_TOKENS ou contenu trop court / citations). "
                    "Réessaie plus tard, ou réduis le % demandé."
                )
                if debug:
                    st.write(
                        {
                            "finishReason_1": fr,
                            "words_1": words_out,
                            "finishReason_2": fr2,
                            "words_2": words2,
                            "has_citations_1": has_citations,
                            "has_citations_2": cites2,
                        }
                    )
                return
            gen = gen2
        except Exception as e:
            if debug:
                st.exception(e)
            else:
                st.error("Relance automatique impossible (quota/erreur). Réessaie dans quelques minutes.")
            return

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
                "target_words": int(target_words),
                "maxOutputTokens": int(max_out),
            }
        )
        with st.expander("Prompt envoyé à Gemini (debug)", expanded=False):
            st.text_area("Prompt complet", value=prompt, height=320)
        with st.expander("Réponse brute Vertex (debug)", expanded=False):
            st.write(gen.raw)
        if str(cand0.get("finishReason") or "").strip().upper() in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH"):
            st.warning(
                "La synthèse semble tronquée (finishReason = MAX_TOKENS). "
                "Augmenter encore `maxOutputTokens` ou réduire le % demandé."
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
        except Exception as e:
            # Fallback si Vertex refuse AUDIO (allowlist) OU si erreur transitoire/quota.
            msg = str(e).lower()
            allowlist = ("not allowlisted" in msg) or ("allowlisted" in msg)
            transient = ("429" in msg) or ("quota" in msg) or ("rate" in msg) or ("tempor" in msg) or ("503" in msg)
            if (allowlist or transient) and cfg.gemini_api_key:
                audio_route = "gemini_api_chunked"
                ft0 = time.perf_counter()
                tts_api = GeminiTtsApiClient(api_key=cfg.gemini_api_key)
                chunks = _chunk_text_for_tts(gen.text, max_chars=1400)
                perf["tts_chunks"] = len(chunks)
                wav_parts_by_i: dict[int, bytes] = {}
                tts_chunk_total_s = 0.0
                tts_errors: list[str] = []

                def _tts_job(i: int, ch: str) -> tuple[int, bytes, float]:
                    ct0 = time.perf_counter()
                    tts_audio = tts_api.generate_audio(
                        model="gemini-2.5-flash-preview-tts",
                        text=ch,
                        voice_name="Kore",
                    )
                    elapsed = time.perf_counter() - ct0
                    b, mt, _ = normalize_audio_bytes(audio_bytes=tts_audio.audio_bytes, mime_type=tts_audio.mime_type)
                    if mt != "audio/wav":
                        b, mt, _ = normalize_audio_bytes(audio_bytes=b, mime_type=mt)
                    return i, b, elapsed

                # Quotas Gemini API : réduire le parallélisme limite les 429.
                workers = max(1, min(2, len(chunks)))
                with ThreadPoolExecutor(max_workers=workers) as ex2:
                    futs = [ex2.submit(_tts_job, i, ch) for i, ch in enumerate(chunks)]
                    for fut in as_completed(futs):
                        try:
                            i, b, elapsed = fut.result()
                            wav_parts_by_i[i] = b
                            tts_chunk_total_s += float(elapsed)
                        except Exception as ex:
                            tts_errors.append(str(ex))

                if tts_errors or len(wav_parts_by_i) != len(chunks):
                    st.error(
                        "Audio incomplet : certains morceaux TTS ont échoué (quota/erreur). "
                        "Réessaie dans quelques minutes."
                    )
                    if debug and tts_errors:
                        st.write({"tts_errors": tts_errors[:6], "chunks_ok": len(wav_parts_by_i), "chunks_total": len(chunks)})
                    st.stop()

                wav_parts = [wav_parts_by_i[i] for i in range(len(chunks)) if i in wav_parts_by_i]
                joined = join_wav_bytes(wav_parts)
                perf["audio_fallback_s"] = round(time.perf_counter() - ft0, 3)
                perf["tts_chunk_total_s"] = round(tts_chunk_total_s, 3)
                audio = type("AudioWrap", (), {})()
                audio.audio_bytes = joined
                audio.mime_type = "audio/wav"
                audio.model = "gemini-api-tts:chunked"
            else:
                # Pas de clé Gemini : on remonte l'erreur.
                if allowlist and not cfg.gemini_api_key:
                    st.error(
                        "Audio indisponible via Vertex AI (compte non allowlist AUDIO). "
                        "Ajoute/valide GEMINI_API_KEY pour activer le fallback TTS."
                    )
                    st.stop()
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

    # Optimisation : les downloads de vérification (Cloud → UI) sont coûteux.
    # On ne les fait que si debug est activé.
    if debug:
        st.subheader("Résumé du temps liturgique")
        try:
            dt0 = time.perf_counter()
            txt_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=text_path)
            txt = txt_bytes.decode("utf-8", errors="replace")
            perf["download_text_verify_s"] = round(time.perf_counter() - dt0, 3)
        except Exception as e:
            txt = f"[Erreur lecture Cloud texte] {e}"
        st.text_area("Synthèse", value=txt, height=320)

        try:
            da0 = time.perf_counter()
            aud_bytes = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=audio_path)
            aud_play, aud_mime_play, _ = normalize_audio_bytes(audio_bytes=aud_bytes, mime_type=audio_mime_norm)
            perf["download_audio_verify_s"] = round(time.perf_counter() - da0, 3)
            st.subheader("Écouter le résumé")
            st.audio(aud_play, format=aud_mime_play)
        except Exception as e:
            st.error(f"Erreur lecture/lecture audio Cloud: {e}")

    # Option : générer aussi le PDF juste après (mêmes sources, même overlay)
    if generate_pdf and cfg.gcs_bucket_name:
        try:
            tpdf0 = time.perf_counter()
            date_str = str(identity.date)
            img_b = _fetch_liturgy_illustration_full_bytes(gcs=gcs, cfg=cfg, date_str=date_str)
            back_cover_b = None
            try:
                y = str(date_str)[:4]
                back_cover_b = download_bytes(
                    gcs=gcs,
                    bucket_name=str(cfg.gcs_bucket_name).strip(),
                    path=f"Images/thumbs/montage_{y}.png",
                )
            except Exception:
                back_cover_b = None

            # Titre 2 lignes comme dans “Préparer le PDF…” (Psautier uniquement)
            semaine_psautier = (getattr(identity, "semaine", None) or "").strip()
            line1 = _liturgy_display_label(
                (getattr(identity, "fete", None) or "").strip()
                or (_jour_liturgique(identity) or "").strip()
                or _liturgy_cover_pdf_title(identity)
            )
            line2 = ""
            if semaine_psautier and ("psautier" in semaine_psautier.lower()):
                lbl = _liturgy_display_label(semaine_psautier).strip()
                line2 = f"({lbl})" if lbl else ""
            week_title_pdf = (line1 + ("\n" + line2 if line2 else "")).strip()

            # highlight index (best-effort)
            highlight_idx = None
            try:
                manifest = json.loads(
                    Path("data/manifests/illustration_pipeline.json").read_text(encoding="utf-8")
                )
                targets = manifest.get("targets") or []
                year = str(date_str)[:4]
                year_targets = [t for t in targets if str(t.get("date") or "").startswith(year)]
                year_dates = [str(t.get("date") or "")[:10] for t in year_targets]
                if str(date_str)[:10] in year_dates:
                    highlight_idx = int(year_dates.index(str(date_str)[:10]))
            except Exception:
                highlight_idx = None

            aud_url, aud_note = _public_app_listen_url(date_str=date_str)
            pdf_b = build_liturgy_sunday_pdf_bytes(
                image_bytes=img_b,
                week_title=week_title_pdf,
                date_line=_french_long_date_label(date_str),
                meta_line=(
                    f"{_liturgy_display_label(getattr(identity, 'periode', None))} · "
                    f"Cycle {_cycle_year_display(getattr(identity, 'annee', None))} · "
                    f"{_liturgy_display_label(getattr(identity, 'couleur', None))}"
                ),
                premiere_lecture=getattr(texts, "premiere_lecture", None),
                psaume=getattr(texts, "psaume", None),
                deuxieme_lecture=getattr(texts, "deuxieme_lecture", None),
                evangile=getattr(texts, "evangile", None),
                synthesis_text=gen.text,
                audio_listen_url=aud_url,
                audio_listen_note=aud_note,
                about_markdown=_ABOUT_MARKDOWN,
                back_cover_image_bytes=back_cover_b,
                accent_hex=_liturgical_accent_hex(getattr(identity, "couleur", None)),
                back_cover_highlight_cell_index=highlight_idx,
            )
            fasc_path = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
            upload_bytes(
                gcs=gcs,
                bucket_name=str(cfg.gcs_bucket_name).strip(),
                path=fasc_path,
                data=pdf_b,
                content_type="application/pdf",
            )
            st.session_state[f"liturgy_sunday_pdf_{date_str}"] = pdf_b
            perf["pdf_auto_s"] = round(time.perf_counter() - tpdf0, 3)
        except Exception as e:
            st.warning(f"PDF non généré automatiquement : {e}")
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
    # Accès réservé : l'aide-mémoire nécessite une session utilisateur (compte).
    st.write("Espace réservé aux utilisateurs connectés.")

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
    if not user_entity_id:
        st.warning("Pour accéder à **Mon Aide‑Mémoire**, il faut être connecté.", icon="🔒")
        if st.button("Aller à Mon compte", type="primary", key="memo_go_account"):
            st.session_state.route = "account"
            st.rerun()
        return

    user_entity_id = str(st.session_state.auth_user_entity_id or "").strip()
    if not user_entity_id:
        # Message supprimé (peu utile / bruit)
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
                    content = f"[Erreur lecture Cloud] {e}"
                st.text_area("Contenu", value=content, height=260)
                st.caption(f"Cloud: `{path}`")

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
        "Source des mémos : lignes **memos** (Sheets) + fichier Markdown sur Cloud ; les **résolutions** viennent du champ "
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
                        body_raw = f"[Erreur lecture Cloud] {ex}"
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


def render_feedback() -> None:
    """Questionnaire flash post-envoi (Sheets RSTN `experience_feedback`)."""
    st.title("Donner votre avis")
    st.markdown(
        """
**« LumenVia est un chemin que nous construisons ensemble.**  
En tant que premier passager de cette aventure, votre retour nous est précieux pour ajuster nos pas. Pourriez-vous nous accorder environ **une minute** ? »
        """.strip()
    )

    try:
        q_campaign = str(st.query_params.get("campaign") or "").strip()
    except Exception:
        q_campaign = ""
    try:
        q_dim = str(st.query_params.get("date_dimanche") or "").strip()
    except Exception:
        q_dim = ""
    try:
        qp_email_fb = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        qp_email_fb = ""

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Enregistrement indisponible : configuration Google Sheets manquante.")
        return

    from core.sheets_db import TableSpec, ensure_table

    gs = build_gspread_client(cfg.gcp_service_account)
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="experience_feedback",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "submitter_email",
                    "emotion_global",
                    "rating_illustration",
                    "rating_synthesis",
                    "rating_audio",
                    "utility_liturgy",
                    "touch_memorable",
                    "wish_improve_one",
                    "campaign_hint",
                    "date_dimanche_hint",
                    "source_route",
                ]
            ),
        ),
    )

    auth_em = str(st.session_state.get("auth_email_lc") or "").strip().lower()

    def _em_ok_feedback(e: str) -> bool:
        em = (e or "").strip().lower()
        return bool(em and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em))

    # Préremplissage : lien e-mail (?email=) prioritaire ; sinon session connectée.
    if _em_ok_feedback(qp_email_fb):
        st.session_state.fb_email_in = qp_email_fb
    else:
        cur = str(st.session_state.get("fb_email_in") or "").strip()
        if not _em_ok_feedback(cur) and _em_ok_feedback(auth_em):
            st.session_state.fb_email_in = auth_em
        elif not st.session_state.get("fb_email_in"):
            st.session_state.fb_email_in = ""

    st.subheader("Vos premiers pas avec LumenVia")
    st.caption(
        "Les trois évaluations suivantes utilisent une **échelle de 1 à 5** "
        "(**1** = note la plus basse · **5** = note la plus haute)."
    )

    with st.form("experience_feedback_form", clear_on_submit=True):
        em_in = st.text_input(
            "E-mail (optionnel)",
            placeholder="toi@domaine.fr",
            help="Souvent prérempli depuis le lien reçu par e-mail ou depuis ta connexion au site ; tu peux modifier ou effacer.",
            key="fb_email_in",
        )
        emotion = st.radio(
            "Comment décririez-vous votre état d'esprit après avoir consulté cette synthèse ?",
            options=("Apaisé", "Éclairé", "Curieux", "Indifférent"),
            horizontal=True,
            key="fb_emotion",
        )
        st.caption("Échelle 1–5 pour les trois critères ci-dessous (1 = plus faible · 5 = meilleure note).")
        c1, c2, c3 = st.columns(3)
        with c1:
            r_illus = int(
                st.slider(
                    "L'illustration",
                    1,
                    5,
                    4,
                    key="fb_r_illus",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )
        with c2:
            r_synth = int(
                st.slider(
                    "Le pdf de synthèse",
                    1,
                    5,
                    4,
                    key="fb_r_synth",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )
        with c3:
            r_audio = int(
                st.slider(
                    "L'audio",
                    1,
                    5,
                    4,
                    key="fb_r_audio",
                    help="1 = très insatisfaisant · 5 = très satisfaisant.",
                )
            )

        utility = st.select_slider(
            "Ce contenu vous aide-t-il réellement à vous préparer pour la célébration de dimanche ?",
            options=("Pas vraiment", "Un peu", "Oui, beaucoup"),
            value="Un peu",
            key="fb_utility",
        )
        standout = st.text_area(
            "Qu'est-ce qui vous a le plus touché ou semblé le plus utile dans cet envoi ?",
            max_chars=480,
            key="fb_standout",
        )
        wish = st.text_area(
            "Une seule chose à améliorer ou à ajouter (musique d'ambiance, texte plus court, …) ?",
            max_chars=480,
            key="fb_wish",
        )
        submitted = st.form_submit_button("Envoyer mon avis")

    if submitted:
        em_clean = str(em_in or "").strip().lower()
        overlay = loading_overlay("Enregistrement de votre retour…")
        try:
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="experience_feedback",
                values_by_col={
                    "entity_id": sha256(f"fb|{utc_now_iso()}|{random.random()}".encode("utf-8")).hexdigest()[:28],
                    "submitter_email": em_clean,
                    "emotion_global": str(emotion),
                    "rating_illustration": str(r_illus),
                    "rating_synthesis": str(r_synth),
                    "rating_audio": str(r_audio),
                    "utility_liturgy": str(utility),
                    "touch_memorable": (standout or "").strip(),
                    "wish_improve_one": (wish or "").strip(),
                    "campaign_hint": q_campaign or "",
                    "date_dimanche_hint": q_dim[:10] if len(q_dim) >= 10 else q_dim,
                    "source_route": "feedback",
                },
            )
            st.success("Merci infiniment : ton avis nous aide à faire grandir LumenVia.")
        except Exception as ex:
            st.exception(ex)
        finally:
            overlay.empty()


def render_join() -> None:
    # Cette page sert à la fois à l'inscription newsletter et au "Mon compte" via lien e-mail.
    # /?route=account&email=... pré-remplit l'email.
    try:
        qp_email = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        qp_email = ""
    auth_em0 = str(st.session_state.get("auth_email_lc") or "").strip().lower()
    # "Mon compte" doit être accessible via route dédiée,
    # et ne doit pas remplacer l'écran newsletter quand l'utilisateur est connecté.
    cur_route = str(st.session_state.get("route") or "").strip().lower()
    is_account_view = cur_route == "account"
    st.title("Mon compte" if is_account_view else "S'inscrire à la newsletter")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante — inscription indisponible.")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    # Supersession immuable : avant d'ajouter une nouvelle ligne `users` pour un email,
    # on marque les lignes précédentes encore Actives comme Inactif (append-only + “une seule version live”).
    def _supersede_users_by_email(email_lc: str) -> None:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return
        try:
            from core.sheets_db import _resolve_table_name, compute_concat, SHEETS_ROW_STATUS_INACTIVE
        except Exception:
            return
        try:
            sh0 = gs.open_by_key(cfg.gsheet_id)
            ws0 = sh0.worksheet(_resolve_table_name(sh=sh0, table="users"))
            header0 = ws0.row_values(1)
            if not header0 or "status" not in header0:
                return
            col_status = header0.index("status") + 1
            col_concat = header0.index("concat") + 1 if "concat" in header0 else 0
            recs = ws0.get_all_records(numericise_ignore=["all"])
        except Exception:
            return
        for ix, r in enumerate(recs):
            if str(r.get("email") or "").strip().lower() != em0:
                continue
            if not sheet_row_status_is_live(r.get("status")):
                continue
            merged = dict(r)
            merged["status"] = SHEETS_ROW_STATUS_INACTIVE
            row_num = ix + 2
            try:
                ws0.update_cell(row_num, col_status, SHEETS_ROW_STATUS_INACTIVE)
                if col_concat:
                    ws0.update_cell(row_num, col_concat, compute_concat(merged, header=header0))
            except Exception:
                continue
    users: list[dict] = []
    subs: list[dict] = []
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=4000)
        subs = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=4000)
    except Exception:
        pass

    # --- Mon compte : connexion / création / activation (newsletter → compte) ---
    if is_account_view:
        if "auth_user_entity_id" not in st.session_state:
            st.session_state.auth_user_entity_id = ""
        if "auth_email_lc" not in st.session_state:
            st.session_state.auth_email_lc = ""

        user_entity_id = str(st.session_state.get("auth_user_entity_id") or "").strip()
        st.subheader("Connexion")
        if user_entity_id:
            email_disp = str(st.session_state.get("auth_email_lc") or "").strip()
            st.caption(f"Session active pour **{email_disp or 'ton compte'}**.")
            if st.button("Se déconnecter", type="secondary", key="acct_logout"):
                for k in ("auth_user_entity_id", "auth_email_lc"):
                    st.session_state.pop(k, None)
                st.session_state.pop("admin_authenticated", None)
                st.rerun()
        else:
            # Contrôle “standard” (pilotable) plutôt que `st.tabs` (qui ne permet pas de basculer via un bouton).
            mode = st.segmented_control(
                " ",
                options=["login", "signup"],
                default=str(st.session_state.get("acct_mode") or "login"),
                key="acct_mode",
                format_func=lambda x: "Se connecter" if x == "login" else "Créer / activer un compte",
            )

            def _latest_user_record(users0: list[dict], email_lc: str) -> dict | None:
                rows0 = [u for u in users0 if str(u.get("email", "")).strip().lower() == email_lc]
                if not rows0:
                    return None
                rows_sorted0 = sorted(rows0, key=lambda r: str(r.get("created_at", "")), reverse=True)
                return rows_sorted0[0]

            if mode == "login":
                email_login = st.text_input("Email", key="acct_email_login").strip().lower()
                password_login = st.text_input("Mot de passe", type="password", key="acct_password_login")
            else:
                # Pré-remplissage (avant instanciation des widgets) via on_change sur l'e-mail.
                def _prefill_acct_profile_from_existing() -> None:
                    em0 = str(st.session_state.get("acct_email_signup") or "").strip().lower()
                    if not em0:
                        return
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em0):
                        return
                    ex0 = _latest_user_record(users, em0)
                    if not ex0:
                        return
                    # Ne pré-remplit que si l'utilisateur n'a rien saisi
                    st.session_state.setdefault("acct_first_name", str(ex0.get("first_name") or "").strip())
                    st.session_state.setdefault("acct_last_name", str(ex0.get("last_name") or "").strip())
                    st.session_state.setdefault("acct_phone_e164", str(ex0.get("phone_e164") or "").strip())

                email_signup = st.text_input(
                    "Email",
                    key="acct_email_signup",
                    on_change=_prefill_acct_profile_from_existing,
                ).strip().lower()
                password_signup = st.text_input("Mot de passe", type="password", key="acct_password_signup")
                c1, c2 = st.columns([1, 1], gap="small")
                with c1:
                    first_name_su = st.text_input("Prénom", key="acct_first_name").strip()
                with c2:
                    last_name_su = st.text_input("Nom", key="acct_last_name").strip()
                phone_e164_su = st.text_input(
                    "Téléphone (optionnel, format international)",
                    key="acct_phone_e164",
                    placeholder="+33612345678",
                ).strip()
                want_opt_in_su = st.checkbox(
                    "Je souhaite recevoir les e-mails du vendredi (opt-in)",
                    value=True,
                    key="acct_optin",
                )

                is_email_ok = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_signup)) if email_signup else False
                if email_signup.strip() and not is_email_ok:
                    st.error("Merci d’indiquer une adresse e-mail valide (ex. nom@domaine.fr).")
                is_phone_ok = True
                if phone_e164_su:
                    is_phone_ok = bool(re.match(r"^\+\d{8,15}$", phone_e164_su))
                    if not is_phone_ok:
                        st.error("Téléphone invalide. Utilise le format E.164, ex. +33612345678.")

            if mode == "login":
                if st.button("Se connecter", type="primary", disabled=not (email_login and password_login), use_container_width=True, key="acct_login_btn"):
                    ov = loading_overlay("LumenVia vérifie tes identifiants…")
                    try:
                        adm_login, adm_pwd = _admin_login_and_password()
                        if email_login.strip().lower() == adm_login and password_login == adm_pwd:
                            admin_canon = f"{adm_login}@admin.lumenvia"
                            st.session_state.auth_user_entity_id = sha256(admin_canon.encode("utf-8")).hexdigest()[:24]
                            st.session_state.auth_email_lc = adm_login
                            st.session_state.admin_authenticated = True
                            st.success("Connecté (administrateur).")
                            st.rerun()
                        rec = _latest_user_record(users, email_login)
                        if not rec or not rec.get("password_salt_b64") or not rec.get("password_hash_b64"):
                            st.error("Compte introuvable ou mot de passe non défini. Clique sur « Créer / activer un compte ».")
                            if email_login.strip():
                                if st.button("Créer / activer avec cet email", key="acct_go_signup_from_login"):
                                    st.session_state["acct_mode"] = "signup"
                                    st.session_state["acct_email_signup"] = email_login.strip().lower()
                                    st.rerun()
                            return
                        ok = verify_password(
                            password_login,
                            salt_b64=str(rec.get("password_salt_b64")),
                            hash_b64=str(rec.get("password_hash_b64")),
                        )
                        if not ok:
                            st.error("Mot de passe incorrect.")
                            return
                        st.session_state.auth_user_entity_id = sha256(email_login.encode("utf-8")).hexdigest()[:24]
                        st.session_state.auth_email_lc = email_login
                        st.session_state.pop("admin_authenticated", None)
                        st.success("Connecté.")
                        st.rerun()
                    finally:
                        ov.empty()

                # Réinitialisation mot de passe (envoi e-mail)
                st.caption("Réinitialisation : tu recevras un lien valable **2 heures**.")
                if st.button("Réinitialiser le mot de passe", type="secondary", disabled=not bool(email_login.strip()), key="acct_pwd_reset_btn"):
                    em0 = email_login.strip().lower()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em0):
                        st.error("Merci d’indiquer un e-mail valide.")
                    else:
                        ov = loading_overlay("Envoi de l’e-mail de réinitialisation…")
                        try:
                            from core.sheets_db import TableSpec, ensure_table, with_concat, BASE_COLUMNS
                            from secrets import token_urlsafe
                            from datetime import datetime, timedelta, timezone
                            from core.outbound import SmtpConfig, send_smtp_email

                            ensure_table(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table=TableSpec(
                                    name="password_resets",
                                    columns=with_concat(
                                        [
                                            *BASE_COLUMNS,
                                            "email",
                                            "token_hash",
                                            "expires_at",
                                            "used",
                                        ]
                                    ),
                                ),
                            )
                            tok = token_urlsafe(32)
                            tok_h = sha256(tok.encode("utf-8")).hexdigest()
                            exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
                            append_immutable_row(
                                gspread_client=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                table="password_resets",
                                values_by_col={
                                    "entity_id": sha256(f"pwdreset|{em0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                    "email": em0,
                                    "token_hash": tok_h,
                                    "expires_at": exp,
                                    "used": "false",
                                },
                            )

                            origin = _lumenvia_app_origin_url() or ""
                            link = (origin.rstrip("/") + "/?route=reset_password&email=" + em0 + "&token=" + tok) if origin else ""

                            # SMTP
                            def _secret_get(*keys: str) -> str:
                                try:
                                    s = st.secrets
                                except Exception:
                                    return ""
                                for k in keys:
                                    v = s.get(k)
                                    if v is not None and str(v).strip():
                                        return str(v).strip()
                                return ""

                            smtp_cfg = SmtpConfig(
                                host=_secret_get("SMTP_HOST"),
                                port=int(_secret_get("SMTP_PORT") or 587),
                                username=_secret_get("SMTP_USER"),
                                password=_secret_get("SMTP_PASSWORD"),
                                from_email=_secret_get("SMTP_FROM"),
                                use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower() not in ("0", "false", "no", "off"),
                            )
                            if not smtp_cfg.host or not smtp_cfg.from_email:
                                raise RuntimeError("SMTP non configuré (SMTP_HOST/SMTP_FROM).")
                            if not link:
                                raise RuntimeError("URL publique introuvable (PUBLIC_APP_URL requis) pour générer le lien.")

                            subj = "LumenVia — Réinitialisation du mot de passe"
                            body_txt = (
                                "Voici le lien pour réinitialiser ton mot de passe (valide 2 heures) :\n"
                                f"{link}\n\n"
                                "Si tu n'es pas à l'origine de cette demande, ignore cet e-mail."
                            )
                            send_smtp_email(cfg=smtp_cfg, to_email=em0, subject=subj, body_text=body_txt, body_html=None)
                            st.success(f"E-mail envoyé à **{em0}**. Consulte ta boîte de réception.")
                        except Exception as ex:
                            st.error(str(ex))
                        finally:
                            ov.empty()

            else:
                existing = _latest_user_record(users, email_signup) if (email_signup and is_email_ok) else None
                has_pwd = bool(
                    existing
                    and str(existing.get("password_hash_b64") or "").strip()
                    and str(existing.get("password_salt_b64") or "").strip()
                )
                can_activate = bool(existing and not has_pwd)
                if can_activate:
                    st.info(
                        "Cet email est déjà inscrit à la newsletter mais tu peux l'activer en tant que compte "
                        "pour avoir accès aux services réservés aux utilisateurs connectés (aide mémoire etc.). "
                        "Avec les informations renseignées clique juste sur **Activer mon compte**."
                    )

                # Activation (newsletter → compte) : ne force pas prénom/nom si la fiche n'en a pas encore.
                # Création “nouveau compte” : prénom + nom requis.
                can_create = bool(email_signup and password_signup and is_email_ok and is_phone_ok) and (
                    can_activate or (first_name_su.strip() and last_name_su.strip())
                )
                label_create = "Activer mon compte" if can_activate else "Créer un compte"
                if st.button(
                    label_create,
                    type="secondary",
                    disabled=not can_create,
                    use_container_width=True,
                    key="acct_signup_btn",
                ):
                    ov = loading_overlay("LumenVia enregistre ton compte…")
                    try:
                        salt_b64, hash_b64 = hash_password(password_signup)
                        new_uid = sha256(email_signup.encode("utf-8")).hexdigest()[:24]
                        rec0 = _latest_user_record(users, email_signup)
                        if rec0:
                            has_pwd0 = bool(
                                str(rec0.get("password_hash_b64") or "").strip()
                                and str(rec0.get("password_salt_b64") or "").strip()
                            )
                            if has_pwd0:
                                st.error("Un compte existe déjà pour cet e-mail. Utilise l’onglet « Se connecter ».")
                                return
                        _supersede_users_by_email(email_signup)
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="users",
                            values_by_col={
                                "entity_id": new_uid,
                                "email": email_signup,
                                "source": "compte",
                                "first_name": first_name_su.strip(),
                                "last_name": last_name_su.strip(),
                                "phone_e164": phone_e164_su.strip(),
                                "country": "FR",
                                "password_salt_b64": salt_b64,
                                "password_hash_b64": hash_b64,
                            },
                        )
                        if want_opt_in_su:
                            latest_before = _latest_subscription_record(subs, new_uid, "weekly_friday")
                            if not _subscription_is_active(latest_before):
                                sub_entity = sha256(f"sub|{new_uid}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                                append_immutable_row(
                                    gspread_client=gs,
                                    spreadsheet_id=cfg.gsheet_id,
                                    table="subscriptions",
                                    values_by_col={
                                        "entity_id": sub_entity,
                                        "user_entity_id": new_uid,
                                        "type": "weekly_friday",
                                        "zone": "france",
                                        "length_pref": "250",
                                        "opt_in": "true",
                                        "active": "true",
                                    },
                                )
                        st.session_state.auth_user_entity_id = new_uid
                        st.session_state.auth_email_lc = email_signup
                        st.success("Compte créé et connecté.")
                        st.rerun()
                    finally:
                        ov.empty()

        st.divider()
        # Important : en vue "Mon compte", ne pas afficher le formulaire newsletter en dessous.
        return

    # --- Newsletter : inscription / opt-out ---
    auth_email_lc = str(st.session_state.get("auth_email_lc") or "").strip().lower()
    auth_uid = sha256(auth_email_lc.encode("utf-8")).hexdigest()[:24] if auth_email_lc else ""
    auth_latest_sub = _latest_subscription_record(subs, auth_uid, "weekly_friday") if auth_uid else None
    auth_is_in = bool(auth_uid) and _subscription_is_active(auth_latest_sub)

    if auth_email_lc:
        st.caption(
            "Tu peux gérer ici ton opt-in. ET tu peux aussi inscrire quelqu’un d’autre (un ami, un proche) "
            "en renseignant ses informations plus bas."
        )
        with st.expander("Mes préférences newsletter", expanded=False):
            cur_opt_in = str((auth_latest_sub or {}).get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes")
            want_opt_in = st.checkbox(
                "Je souhaite recevoir les e-mails du vendredi (opt-in)",
                value=bool(cur_opt_in),
                key="join_me_optin",
            )
            if st.button("Enregistrer", type="primary", key="join_me_optin_save"):
                ov = loading_overlay("Enregistrement…")
                try:
                    # Append-only : nouvelle ligne subscriptions si changement
                    cur_active = str((auth_latest_sub or {}).get("active") or "").strip().lower() in ("true", "1", "oui", "yes", "active")
                    target_opt_in = bool(want_opt_in)
                    target_active = bool(want_opt_in)
                    if (target_opt_in != cur_opt_in) or (target_active != cur_active):
                        sub_entity = sha256(f"sub|{auth_uid}|prefs|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="subscriptions",
                            values_by_col={
                                "entity_id": sub_entity,
                                "user_entity_id": auth_uid,
                                "type": "weekly_friday",
                                "zone": "france",
                                "length_pref": str((auth_latest_sub or {}).get("length_pref") or "250"),
                                "opt_in": "true" if target_opt_in else "false",
                                "active": "true" if target_active else "false",
                            },
                        )
                    st.success("Préférences enregistrées.")
                    st.rerun()
                finally:
                    ov.empty()

    if "join_email" not in st.session_state:
        st.session_state.join_email = ""
    # Pré-remplissage via lien e-mailing : /?route=join&email=...
    if qp_email and not str(st.session_state.join_email).strip():
        st.session_state.join_email = qp_email

    def _latest_user_by_email(email_lc: str) -> dict | None:
        rows = [u for u in users if str(u.get("email", "")).strip().lower() == email_lc]
        if not rows:
            return None
        return sorted(rows, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]

    col_n1, col_n2 = st.columns([1, 1], gap="small")
    with col_n1:
        first_name = st.text_input("Prénom", key="join_first_name").strip()
    with col_n2:
        last_name = st.text_input("Nom", key="join_last_name").strip()

    country = st.selectbox("Pays", options=["FR"], index=0, key="join_country")
    phone_e164 = st.text_input(
        "Téléphone (optionnel, format international)",
        key="join_phone_e164",
        placeholder="+33612345678",
    ).strip()

    email_in = st.text_input("Email", key="join_email")
    email_lc = email_in.strip().lower()
    is_email_ok = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_lc)) if email_lc else False
    if email_in.strip() and not is_email_ok:
        st.error("Merci d’indiquer une adresse e-mail valide (ex. nom@domaine.fr).")
    is_phone_ok = True
    if phone_e164:
        is_phone_ok = bool(re.match(r"^\+\d{8,15}$", phone_e164))
        if not is_phone_ok:
            st.error("Téléphone invalide. Utilise le format E.164, ex. +33612345678.")

    uid = sha256(email_lc.encode("utf-8")).hexdigest()[:24] if email_lc else ""
    latest_sub = _latest_subscription_record(subs, uid, "weekly_friday") if uid else None
    already_in = bool(uid) and _subscription_is_active(latest_sub)

    if auth_email_lc:
        st.caption("Astuce : si tu es déjà connecté, ce formulaire sert surtout à inscrire quelqu’un d’autre.")

    if already_in:
        st.success(f"Tu es déjà inscrit à la newsletter pour **{email_lc}**.")
        st.caption("Tu peux te désinscrire à tout moment.")
        if st.button("Se désinscrire", type="secondary", key="join_opt_out_btn"):
            ov = loading_overlay("Désinscription…")
            try:
                sub_entity = sha256(f"sub|{uid}|optout|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="subscriptions",
                    values_by_col={
                        "entity_id": sub_entity,
                        "user_entity_id": uid,
                        "type": "weekly_friday",
                        "zone": "france",
                        "length_pref": str((latest_sub or {}).get("length_pref") or "250"),
                        "opt_in": "false",
                        "active": "false",
                    },
                )
                st.success("Désinscription enregistrée.")
                st.rerun()
            finally:
                ov.empty()
        return

    consent = st.checkbox("J’accepte de recevoir ces e-mails (désinscription possible à tout moment).", key="join_consent")
    if st.button("S’abonner", type="primary", disabled=not (is_email_ok and first_name and last_name and is_phone_ok and consent), key="join_subscribe_btn"):
        ov = loading_overlay("LumenVia enregistre ton inscription…")
        should_refresh = False
        try:
            user_entity_id = sha256(email_lc.encode("utf-8")).hexdigest()[:24]
            rec_u = _latest_user_by_email(email_lc)
            if not rec_u:
                _supersede_users_by_email(email_lc)
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="users",
                    values_by_col={
                        "entity_id": user_entity_id,
                        "email": email_lc,
                        "first_name": first_name.strip(),
                        "last_name": last_name.strip(),
                        "phone_e164": phone_e164.strip(),
                        "country": str(country or "").strip(),
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
                        "opt_in": "true",
                        "active": "true",
                    },
                )
                should_refresh = True
        finally:
            ov.empty()
        if should_refresh:
            st.rerun()


def render_reset_password() -> None:
    st.title("Réinitialiser le mot de passe")
    st.caption("Saisis un nouveau mot de passe. Le lien est valide pendant une durée limitée.")

    try:
        em = str(st.query_params.get("email") or "").strip().lower()
    except Exception:
        em = ""
    try:
        tok = str(st.query_params.get("token") or "").strip()
    except Exception:
        tok = ""

    if not em or not tok:
        st.error("Lien invalide (paramètres manquants).")
        return

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configuration Google Sheets manquante.")
        return
    gs = build_gspread_client(cfg.gcp_service_account)
    # Même logique que l'inscription : on supersède l'ancienne ligne `users` (status Inactif) avant d'écrire la nouvelle.
    def _supersede_users_by_email(email_lc: str) -> None:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return
        try:
            from core.sheets_db import _resolve_table_name, compute_concat, SHEETS_ROW_STATUS_INACTIVE
        except Exception:
            return
        try:
            sh0 = gs.open_by_key(cfg.gsheet_id)
            ws0 = sh0.worksheet(_resolve_table_name(sh=sh0, table="users"))
            header0 = ws0.row_values(1)
            if not header0 or "status" not in header0:
                return
            col_status = header0.index("status") + 1
            col_concat = header0.index("concat") + 1 if "concat" in header0 else 0
            recs = ws0.get_all_records(numericise_ignore=["all"])
        except Exception:
            return
        for ix, r in enumerate(recs):
            if str(r.get("email") or "").strip().lower() != em0:
                continue
            if not sheet_row_status_is_live(r.get("status")):
                continue
            merged = dict(r)
            merged["status"] = SHEETS_ROW_STATUS_INACTIVE
            row_num = ix + 2
            try:
                ws0.update_cell(row_num, col_status, SHEETS_ROW_STATUS_INACTIVE)
                if col_concat:
                    ws0.update_cell(row_num, col_concat, compute_concat(merged, header=header0))
            except Exception:
                continue

    new_pwd = st.text_input("Nouveau mot de passe", type="password", key="pwd_reset_new")
    new_pwd2 = st.text_input("Confirmer le mot de passe", type="password", key="pwd_reset_new2")
    if st.button("Mettre à jour mon mot de passe", type="primary", disabled=not (new_pwd and new_pwd2)):
        if new_pwd != new_pwd2:
            st.error("Les deux mots de passe ne correspondent pas.")
            return
        ov = loading_overlay("Mise à jour du mot de passe…")
        try:
            from core.sheets_db import TableSpec, ensure_table, with_concat, BASE_COLUMNS, fetch_records, append_immutable_row
            from datetime import datetime, timezone

            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=TableSpec(
                    name="password_resets",
                    columns=with_concat([*BASE_COLUMNS, "email", "token_hash", "expires_at", "used"]),
                ),
            )
            resets = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="password_resets", limit=8000)
            tok_h = sha256(tok.encode("utf-8")).hexdigest()
            # Dernière demande pour ce token
            cand = [r for r in resets if str(r.get("token_hash") or "").strip() == tok_h and str(r.get("email") or "").strip().lower() == em]
            cand.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            rec = cand[0] if cand else {}
            if not rec:
                st.error("Lien invalide ou expiré.")
                return
            if str(rec.get("used") or "").strip().lower() in ("true", "1", "oui", "yes"):
                st.error("Ce lien a déjà été utilisé.")
                return
            exp = str(rec.get("expires_at") or "").strip()
            try:
                dt_exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if dt_exp.tzinfo is None:
                    dt_exp = dt_exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > dt_exp:
                    st.error("Ce lien a expiré.")
                    return
            except Exception:
                st.error("Ce lien a expiré.")
                return

            # Met à jour le mot de passe via append-only dans `users`
            users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            rows_u = [u for u in users if str(u.get("email") or "").strip().lower() == em]
            rows_u.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            u0 = rows_u[0] if rows_u else {}
            if not u0:
                st.error("Utilisateur introuvable.")
                return
            salt_b64, hash_b64 = hash_password(new_pwd)
            uid = sha256(em.encode("utf-8")).hexdigest()[:24]
            _supersede_users_by_email(em)
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="users",
                values_by_col={
                    "entity_id": uid,
                    "email": em,
                    # Dès qu'un mot de passe est défini, on considère l'utilisateur comme "compte"
                    # (même s'il a commencé par une inscription newsletter).
                    "source": "compte",
                    "first_name": str(u0.get("first_name") or "").strip(),
                    "last_name": str(u0.get("last_name") or "").strip(),
                    "phone_e164": str(u0.get("phone_e164") or "").strip(),
                    "country": str(u0.get("country") or "FR").strip() or "FR",
                    "password_salt_b64": salt_b64,
                    "password_hash_b64": hash_b64,
                },
            )
            # Marque le token comme utilisé (append-only)
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="password_resets",
                values_by_col={
                    "entity_id": sha256(f"pwdreset|used|{em}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "email": em,
                    "token_hash": tok_h,
                    "expires_at": exp,
                    "used": "true",
                },
            )
            st.success("Mot de passe mis à jour. Tu peux maintenant te connecter.")
        finally:
            ov.empty()


def _admin_target_has_illustration(*, gcs: object, bucket_name: str, target: dict) -> bool:
    return _admin_first_existing_blob_path(gcs=gcs, bucket_name=bucket_name, target=target) is not None


def _admin_first_existing_blob_path(
    *,
    gcs: object,
    bucket_name: str,
    target: dict,
    errors: list[str] | None = None,
) -> str | None:
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
        except Exception as ex:
            if errors is not None and len(errors) < 6:
                errors.append(f"{path} — {ex}")
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


def _admin_targets_presence_compact(
    targets_sorted: list[dict],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    sig: list[tuple[str, str, tuple[str, ...]]] = []
    for t in targets_sorted:
        ds = str(t.get("date") or "").strip()[:10]
        p0 = str(t.get("gcs_path_primary") or "").strip()
        alts = tuple(
            sorted(
                {
                    str(a or "").strip()
                    for a in (t.get("alternates") or [])
                    if str(a or "").strip()
                }
            )
        )
        sig.append((ds, p0, alts))
    return tuple(sig)


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def _admin_cached_manifest_cloud_presence(
    bucket_name: str,
    account_fp: str,
    manifest_mtime_ns: int,
    manifest_size: int,
    compact: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[tuple[bool, ...], tuple[str | None, ...], tuple[str, ...]]:
    """
    Probe GCS blob existence for every manifest target (heavy). Cached ~5 min keyed by manifest size/mtime + bucket + SA fingerprint.
    Exécution séquentielle pour éviter les problèmes de concurrence sur le client Storage.
    """
    if not compact:
        return (), (), ()

    errs: list[str] = []
    cfg_inner = load_config()
    gcs_inner = build_gcs_client(cfg_inner.gcp_service_account)
    has_list: list[bool] = []
    path_list: list[str | None] = []
    for row in compact:
        ds, p0, alts = row
        target_dict = {"date": ds, "gcs_path_primary": p0, "alternates": list(alts)}
        pth = _admin_first_existing_blob_path(
            gcs=gcs_inner,
            bucket_name=bucket_name,
            target=target_dict,
            errors=errs if len(errs) < 8 else None,
        )
        has_list.append(pth is not None)
        path_list.append(pth)
    return tuple(has_list), tuple(path_list), tuple(errs[:8])


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
        st.warning("Mode **aperçu seulement** : aucun fichier n’a été envoyé sur Cloud.")


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
    st.subheader("Génération Vertex AI → bucket Cloud")
    st.info(
        "**Stockage Cloud** : pour que l’image soit **envoyée sur le bucket**, laisse la case "
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

    bucket_name = str(cfg.gcs_bucket_name).strip()
    sorted_targets = _admin_sort_targets_by_date(targets_all)
    try:
        mstat = manifest_path.stat()
        m_mtime_ns = int(getattr(mstat, "st_mtime_ns", int(mstat.st_mtime * 1e9)))
        m_sz = int(mstat.st_size)
    except Exception:
        m_mtime_ns, m_sz = 0, 0
    compact_presence = _admin_targets_presence_compact(sorted_targets)
    sa_fp = _service_account_fingerprint(cfg.gcp_service_account)

    c_cache, _ = st.columns([1, 3])
    with c_cache:
        if st.button("Invalider le cache Cloud (rafraîchir la grille)", key="adm_img_cache_clear"):
            _admin_cached_manifest_cloud_presence.clear()
            st.rerun()

    # Présence sur le bucket : résultat mis en cache (TTL) pour accélérer les navigations suivantes.
    ov_load = loading_overlay("Vérification de la présence des fichiers sur Cloud…")
    try:
        gcs = build_gcs_client(cfg.gcp_service_account)
        has_tpl, paths_tpl, err_tpl = _admin_cached_manifest_cloud_presence(
            bucket_name,
            sa_fp,
            m_mtime_ns,
            m_sz,
            compact_presence,
        )
        err_samples = list(err_tpl)
        first_paths = list(paths_tpl)
        has_map = list(has_tpl)
    finally:
        ov_load.empty()
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
        "Aperçu seulement — ne pas envoyer sur Cloud (aucun fichier dans le bucket)",
        value=False,
        key="adm_img_dry",
    )

    # --- Grille 10 × 6 : semaine ISO, vignette ou sélection si manquant ---
    st.divider()
    st.subheader("Calendrier des illustrations")
    st.caption(
        f"**{n_missing}** dimanche(s) sans fichier sur Cloud sur **{len(sorted_targets)}** — "
        f"manifeste `{manifest_path.as_posix()}`. Semaine = **numéro ISO** (semaine civile du dimanche)."
    )
    if err_samples:
        st.error(
            "Accès Cloud en erreur : l’app n’arrive pas à vérifier l’existence des objets "
            "(souvent bucket incorrect, projet/credentials incorrects, ou droits IAM insuffisants)."
        )
        with st.expander("Exemples d’erreurs (vérification d’existence sur Cloud)"):
            st.code("\n".join(err_samples[:6]))

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
        full = first_paths[gi]
        if not full:
            continue
        # Préférer la vignette si présente.
        bp = full
        tp = gcs_thumb_path_from_source_blob(full)
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=tp):
                bp = tp
        except Exception as ex:
            if len(err_samples) < 6:
                err_samples.append(f"{tp} — {ex}")
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
                        st.caption("✓ Cloud")
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
            if not dry_run and any(ln.startswith("OK ") for ln in lines):
                _admin_cached_manifest_cloud_presence.clear()

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
            if not dry_run and any(ln.startswith("OK ") for ln in lines):
                _admin_cached_manifest_cloud_presence.clear()


def render_admin_vision_text() -> None:
    st.title("Admin — Détection de texte (Vision)")

    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}`.")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return

    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name`.")
        return

    targets_all = list(data.get("targets") or [])
    if not targets_all:
        st.warning("Aucune cible dans le manifeste.")
        return

    ov_load = loading_overlay("Chargement des cibles Vision…")
    try:
        gcs = build_gcs_client(cfg.gcp_service_account)
        bucket_name = str(cfg.gcs_bucket_name).strip()
        sorted_targets = _admin_sort_targets_by_date(targets_all)
    finally:
        ov_load.empty()

    # Filtre année (par défaut : année courante).
    years = sorted(
        {str(t.get("date") or "")[:4] for t in sorted_targets if str(t.get("date") or "")[:4].isdigit()}
    )
    y_now = str(date.today().year)
    y_default = y_now if y_now in years else (years[-1] if years else y_now)
    year = st.selectbox("Année", options=years or [y_default], index=(years.index(y_default) if y_default in years else 0))
    targets_year = [t for t in sorted_targets if str(t.get("date") or "").startswith(str(year))]
    if not targets_year:
        st.warning("Aucune cible pour cette année dans le manifeste.")
        return

    # Mode “échantillon” : 60 premières entrées de l’année (utile pour tester vite sans UI de pagination).
    per_page = 60
    slice_start = 0

    st.divider()
    st.subheader("Détection de texte dans les images")
    st.write(
        "Cette page va lancer une détection des textes dans les images générées par l'intelligence artificielle. "
        "Elle va détecter s'il y a des anomalies dans les orthographes et identifier un fichier d'exception à régénérer."
    )

    # Valeurs par défaut validées (UX perf) : pas de sélecteurs.
    ta_min = 2
    ta_workers = 8

    # Calcule le nombre d’images concernées (cibles de l’année qui existent sur le Cloud).
    cache_key = f"_adm_vision_existing_set_{year}"
    set_existing: set[str] | None = st.session_state.get(cache_key)
    if set_existing is None:
        try:
            # Listing par préfixe : beaucoup plus rapide qu'un blob_exists() par cible.
            pref = f"Images/illustrations/{year}/"
            bucket = gcs.bucket(bucket_name)
            set_existing = {b.name for b in gcs.list_blobs(bucket, prefix=pref)}
        except Exception:
            set_existing = set()
        st.session_state[cache_key] = set_existing

    def _targets_with_existing_blob(targets: list[dict]) -> list[dict]:
        out: list[dict] = []
        for t in targets:
            cand: list[str] = []
            p0 = str(t.get("gcs_path_primary") or "").strip()
            if p0:
                cand.append(p0)
            for a in t.get("alternates") or []:
                s = str(a or "").strip()
                if s:
                    cand.append(s)
            if any((c in (set_existing or set())) for c in cand):
                out.append(t)
        return out

    eligible = _targets_with_existing_blob(targets_year)
    st.metric("Images concernées (sur Cloud)", len(eligible))
    st.caption(
        "Méthode : Vision détecte des fragments qui *ressemblent* à du texte, puis on compare les mots à un dictionnaire FR. "
        "Les exceptions sont des mots inconnus / sans signification (ex. suites de lettres) ou manifestement mal orthographiés."
    )

    # Traitement par lots si le volume est important.
    batch_size = 120
    st.caption(f"Traitement par lots : {batch_size} images maximum par lancement.")
    _audit_key = "adm_text_audit_last_rows"
    # Queue persistante par année : permet d'enchaîner les lots sans UI complexe.
    q_key = f"_adm_text_audit_queue_{year}"
    done_key = f"_adm_text_audit_done_{year}"
    init_key = f"_adm_text_audit_inited_{year}"
    if q_key not in st.session_state:
        st.session_state[q_key] = list(eligible)
        st.session_state[done_key] = 0
        st.session_state[init_key] = True
        # Nouvelle analyse (année) : on repart de zéro pour éviter l'accumulation et les incohérences de compteurs.
        st.session_state[_audit_key] = []

    remaining = len(st.session_state.get(q_key) or [])
    done_n = int(st.session_state.get(done_key) or 0)
    if remaining > 0:
        st.info(f"Lot prêt : {min(batch_size, remaining)} image(s) à analyser (restant : {remaining} / total : {done_n + remaining}).")
    else:
        if done_n:
            st.success(f"Analyse terminée pour {year} ({done_n} image(s)). Relance une analyse pour recalculer si besoin.")
            if st.button("Relancer l’analyse (recalculer depuis zéro)", key="adm_text_audit_reset"):
                # Réinitialise la file et les résultats pour cette année.
                st.session_state[q_key] = list(eligible)
                st.session_state[done_key] = 0
                st.session_state[_audit_key] = []
                # Force un rerun immédiat pour réactiver le bouton "lot suivant".
                st.rerun()

    if st.button(
        "Lancer l’analyse (lot suivant)",
        key="adm_text_audit_run",
        type="primary",
        disabled=(len(eligible) == 0 or remaining == 0),
    ):
        overlay = loading_overlay("Analyse Vision des illustrations sur Cloud…")
        try:
            queue: list[dict] = list(st.session_state.get(q_key) or [])
            scan_targets = queue[:batch_size]
            vc = build_vision_image_annotator_client(cfg.gcp_service_account)
            rows_new = audit_targets_for_text(
                gcs=gcs,
                bucket_name=bucket_name,
                targets=scan_targets,
                vision_client=vc,
                max_workers=int(ta_workers),
                min_chars=int(ta_min),
            )
            prev = list(st.session_state.get(_audit_key) or [])
            st.session_state[_audit_key] = [*prev, *rows_new]
            # Avance la queue
            st.session_state[q_key] = queue[len(scan_targets) :]
            st.session_state[done_key] = int(st.session_state.get(done_key) or 0) + len(scan_targets)
        except Exception as ex:
            st.exception(ex)
        finally:
            overlay.empty()

    rows = list(st.session_state.get(_audit_key) or [])
    if rows:
        # Whitelist : permet de confirmer qu'une image est "bonne" même si Vision détecte du bruit.
        whitelist: set[str] = set()
        if cfg.gsheet_id and cfg.gcp_service_account:
            wl_key = f"_adm_vision_whitelist_{year}"
            if wl_key not in st.session_state:
                try:
                    gs_wl = build_gspread_client(cfg.gcp_service_account)
                    wl_rows = fetch_records(
                        gspread_client=gs_wl,
                        spreadsheet_id=cfg.gsheet_id,
                        table="vision_text_whitelist",
                        limit=2000,
                    )
                    whitelist = {
                        str(r.get("gcs_path") or "").strip()
                        for r in wl_rows
                        if str(r.get("gcs_path") or "").strip().startswith(f"Images/illustrations/{year}/")
                        and sheet_row_status_is_live(r.get("status"))
                    }
                except Exception:
                    whitelist = set()
                st.session_state[wl_key] = whitelist
            else:
                whitelist = set(st.session_state.get(wl_key) or set())

        flagged = [r for r in filter_rows_with_text(rows) if str(r.get("gcs_path") or "").strip() not in whitelist]
        errs = [r for r in rows if r.get("error")]
        scanned_unique = len({str(r.get("gcs_path") or "").strip() for r in rows if str(r.get("gcs_path") or "").strip()})
        st.metric("Images analysées (Vision)", scanned_unique)

        if errs:
            if all_errors_are_vision_service_disabled(rows) and len(errs) >= max(1, scanned_unique):
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
                pid_for_links = (pid_from_err or sa_quota_project_id or sa_project_id or "").strip()
                if pid_for_links:
                    billing_url = f"https://console.cloud.google.com/billing?project={pid_for_links}"
                    st.markdown(
                        f"[Vérifier la facturation du projet (souvent la cause si l’API semble « activée »)]({billing_url})"
                    )
            elif all_errors_are_vision_service_disabled(rows):
                st.warning(
                    "Certaines images n’ont pas pu être analysées par Vision (403 service disabled) "
                    "mais d’autres ont réussi. Si l’API vient d’être activée, attends la propagation puis relance."
                )
            else:
                st.warning(f"{len(errs)} erreur(s) Vision ou téléchargement — voir le détail ci-dessous.")

        if flagged:
            st.error(
                f"{len(flagged)} image(s) avec texte détecté (≥ {int(ta_min)} caractères) — candidats au post-traitement."
            )
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
            if not bool(st.session_state.get("_adm_text_audit_hide_csv")):
                st.download_button(
                    "Télécharger la liste (CSV)",
                    data=buf.getvalue().encode("utf-8-sig"),
                    file_name="lumenvia_images_avec_texte.csv",
                    mime="text/csv; charset=utf-8",
                    key="adm_text_audit_csv",
                )
            try:
                from openpyxl import Workbook

                wb = Workbook()
                ws = wb.active
                ws.title = "images_avec_texte"
                ws.append(["date", "gcs_path", "gs_uri", "detected_text"])
                for r in flagged:
                    ws.append(
                        [
                            r.get("date"),
                            r.get("gcs_path"),
                            f"gs://{bucket_name}/{r.get('gcs_path')}",
                            (r.get("detected_text") or ""),
                        ]
                    )
                xbuf = BytesIO()
                wb.save(xbuf)
                st.download_button(
                    "Télécharger la liste (Excel)",
                    data=xbuf.getvalue(),
                    file_name="lumenvia_images_avec_texte.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="adm_text_audit_xlsx",
                )
                st.session_state["_adm_text_audit_hide_csv"] = True
            except Exception as ex:
                st.warning(
                    "Export Excel indisponible (dépendance manquante). Installe `openpyxl` puis relance l’app. "
                    f"Détail: {ex}"
                )

            st.divider()
            st.subheader("Corrections (remplacer → régénérer → écraser sur Cloud)")
            st.caption(
                "Flux économe : on journalise l’audit et les corrections dans Google Sheets (historique), "
                "puis on régénère l’image via Vertex en prenant l’image actuelle comme référence."
            )

            can_sheets = bool(cfg.gsheet_id and cfg.gcp_service_account)
            if not can_sheets:
                st.info("Configure `gsheet_id` (Sheets) pour activer le journal audit/corrections.")

            run_id = sha256(f"vision_audit|{utc_now_iso()}|{bucket_name}".encode("utf-8")).hexdigest()[:12]

            if can_sheets and st.button("Enregistrer cet audit dans Google Sheets", key="adm_vision_audit_save_sheets"):
                ov = loading_overlay("Enregistrement audit Vision dans Sheets…")
                try:
                    from core.sheets_db import TableSpec, ensure_table

                    gs = build_gspread_client(cfg.gcp_service_account)
                    ensure_table(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table=TableSpec(
                            name="vision_text_audit",
                            columns=with_concat(
                                [
                                    *BASE_COLUMNS,
                                    "run_id",
                                    "date",
                                    "gcs_path",
                                    "min_chars",
                                    "detected_text",
                                    "detected_text_chars",
                                    "detected_text_alpha_chars",
                                    "has_meaningful_text",
                                    "error",
                                ]
                            ),
                        ),
                    )

                    # Économise le quota : on journalise par défaut uniquement les exceptions (texte détecté ou erreur).
                    to_save = [r for r in rows if str(r.get("detected_text") or "").strip() or str(r.get("error") or "").strip()]
                    payload: list[dict] = []
                    for r in to_save:
                        dt = str(r.get("detected_text") or "")
                        dt_norm = " ".join(dt.split()).strip()
                        alpha_n = sum(1 for ch in dt_norm if ch.isalpha())
                        ent = sha256(
                            f"audit|{run_id}|{r.get('date')}|{r.get('gcs_path')}|{sha256(dt_norm.encode('utf-8')).hexdigest()}".encode(
                                "utf-8"
                            )
                        ).hexdigest()[:24]
                        payload.append(
                            {
                                "entity_id": ent,
                                "run_id": run_id,
                                "date": r.get("date"),
                                "gcs_path": r.get("gcs_path"),
                                "min_chars": int(ta_min),
                                "detected_text": dt_norm,
                                "detected_text_chars": len(dt_norm),
                                "detected_text_alpha_chars": int(alpha_n),
                                "has_meaningful_text": "true" if bool(r.get("has_text")) else "false",
                                "error": str(r.get("error") or ""),
                            }
                        )
                    saved = append_immutable_rows_bulk(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="vision_text_audit",
                        values_by_col_list=payload,
                        chunk_size=120,
                    )
                    st.success(f"Audit enregistré ({saved} ligne(s) — exceptions uniquement). run_id={run_id}")
                finally:
                    ov.empty()

            flagged_sorted = sorted(flagged, key=lambda r: str(r.get("date") or ""))
            options = [
                f"{r.get('date')} — {str(r.get('gcs_path') or '').split('/')[-1]}".strip()
                for r in flagged_sorted
            ]

            def _sync_vision_pick() -> None:
                sel = str(st.session_state.get("adm_vision_pick_flagged") or "")
                ii = options.index(sel) if sel in options else 0
                pp = flagged_sorted[ii] if flagged_sorted else {}
                txt = str(pp.get("detected_text") or "").strip()
                st.session_state["adm_vision_detected_preview"] = txt[:1200]
                st.session_state["adm_vision_replace_from"] = (txt[:120] if txt else "")
                st.session_state["adm_vision_replace_to"] = ""

            # Post-correction (st.rerun) : la sélection peut changer car la liste "flagged" change,
            # sans déclencher on_change. On force donc la resync si la sélection effective diffère.
            cur = str(st.session_state.get("adm_vision_pick_flagged") or "")
            if options and cur not in options:
                st.session_state["adm_vision_pick_flagged"] = options[0]
                cur = options[0]
            last = str(st.session_state.get("_adm_vision_pick_last") or "")
            if options and cur and cur != last:
                _sync_vision_pick()
                st.session_state["_adm_vision_pick_last"] = cur

            pick = st.selectbox(
                "Image à corriger",
                options=options,
                index=0,
                key="adm_vision_pick_flagged",
                on_change=_sync_vision_pick,
            )
            idx = options.index(pick) if pick in options else 0
            picked = flagged_sorted[idx] if flagged_sorted else {}
            picked_text = str(picked.get("detected_text") or "").strip()
            picked_date = str(picked.get("date") or "").strip()
            picked_path = str(picked.get("gcs_path") or "").strip()

            st.write(f"Chemin : `gs://{bucket_name}/{picked_path}`")
            # Aperçu image (utile pour confirmer qu'il n'y a pas de texte humain).
            try:
                if picked_path:
                    img_prev = download_bytes(gcs=gcs, bucket_name=bucket_name, path=picked_path)
                    if img_prev:
                        st.image(img_prev, caption="Aperçu de l’image (Cloud)", use_container_width=True)
            except Exception:
                pass

            # Bouton "confirmer OK" : ajoute à la whitelist (persistante) pour ne plus remonter.
            if can_sheets and picked_path:
                if st.button("Confirmer : image OK (whitelist)", key="adm_vision_whitelist_add"):
                    ovw = loading_overlay("Ajout à la whitelist (Sheets)…")
                    try:
                        from core.sheets_db import TableSpec, ensure_table

                        gs_w = build_gspread_client(cfg.gcp_service_account)
                        ensure_table(
                            gspread_client=gs_w,
                            spreadsheet_id=cfg.gsheet_id,
                            table=TableSpec(
                                name="vision_text_whitelist",
                                columns=with_concat([*BASE_COLUMNS, "date", "gcs_path", "reason"]),
                            ),
                        )
                        ent = sha256(f"wl|{picked_date}|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                        append_immutable_row(
                            gspread_client=gs_w,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_whitelist",
                            values_by_col={
                                "entity_id": ent,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "reason": "confirmé OK (pas de texte humain)",
                            },
                        )
                        # Met à jour cache whitelist et retire de la liste courante.
                        wl_key2 = f"_adm_vision_whitelist_{year}"
                        cur_wl = set(st.session_state.get(wl_key2) or set())
                        cur_wl.add(picked_path)
                        st.session_state[wl_key2] = cur_wl
                        try:
                            prev_rows = list(st.session_state.get(_audit_key) or [])
                            for rr in prev_rows:
                                if str(rr.get("gcs_path") or "").strip() == picked_path:
                                    rr["has_text"] = False
                                    rr["detected_text"] = ""
                            st.session_state[_audit_key] = prev_rows
                        except Exception:
                            pass
                        st.success("Ajouté à la whitelist : l’image ne remontera plus aux prochaines analyses.")
                        st.rerun()
                    finally:
                        ovw.empty()
            if "adm_vision_detected_preview" not in st.session_state:
                st.session_state["adm_vision_detected_preview"] = picked_text[:1200]
            if "adm_vision_replace_from" not in st.session_state:
                st.session_state["adm_vision_replace_from"] = (picked_text[:120] if picked_text else "")
            if "adm_vision_replace_to" not in st.session_state:
                st.session_state["adm_vision_replace_to"] = ""

            if st.session_state.get("adm_vision_detected_preview"):
                st.text_area(
                    "Texte détecté (extrait)",
                    value=str(st.session_state.get("adm_vision_detected_preview") or ""),
                    height=140,
                    key="adm_vision_detected_preview",
                )

            cfa, cfb = st.columns(2)
            with cfa:
                replace_from = st.text_input("Remplacer (from)", key="adm_vision_replace_from")
            with cfb:
                replace_to = st.text_input("Par (to) — vide = suppression", key="adm_vision_replace_to")

            if st.button(
                "Soumettre la correction + régénérer + écraser (illustration + vignette)",
                type="primary",
                disabled=not bool(picked_path),
                key="adm_vision_do_correction",
            ):
                overlay = loading_overlay("Correction en cours (Vertex → Cloud)…")
                try:
                    corr_entity = sha256(f"corr|{picked_date}|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                    gs = build_gspread_client(cfg.gcp_service_account) if can_sheets else None
                    if gs and cfg.gsheet_id:
                        from core.sheets_db import TableSpec, ensure_table

                        ensure_table(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table=TableSpec(
                                name="vision_text_corrections",
                                columns=with_concat(
                                    [
                                        *BASE_COLUMNS,
                                        "audit_entity_id",
                                        "run_id",
                                        "date",
                                        "gcs_path",
                                        "replace_from",
                                        "replace_to",
                                        "status_detail",
                                        "vertex_model",
                                        "result_mime",
                                        "result_gcs_path",
                                        "thumb_gcs_path",
                                        "error",
                                    ]
                                ),
                            ),
                        )
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_corrections",
                            values_by_col={
                                "entity_id": corr_entity,
                                "audit_entity_id": "",
                                "run_id": run_id,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "replace_from": replace_from.strip(),
                                "replace_to": replace_to.strip(),
                                "status_detail": "requested",
                            },
                        )

                    src_bytes = download_bytes(gcs=gcs, bucket_name=bucket_name, path=picked_path)
                    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
                    rep_from = (replace_from or "").strip()
                    rep_to = (replace_to or "").strip()
                    rep_to_disp = rep_to if rep_to else "(remove)"
                    prompt_edit = (
                        "You are editing the provided reference image.\n"
                        "Task: replace the exact visible text substring delimited by:\n"
                        f"FROM: {rep_from!r}\n"
                        f"TO: {rep_to_disp!r}\n\n"
                        "Constraints:\n"
                        "- Keep the same illustration style, framing, composition, colors.\n"
                        "- Do NOT add any new text anywhere.\n"
                        "- If TO is (remove), remove the text completely.\n"
                        "- Do not introduce any other glyphs, letters, numbers, or watermarks.\n"
                        "- Return only the edited image.\n"
                    )
                    img_res = vx.generate_image_auto(
                        preferred_models=["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
                        prompt=prompt_edit,
                        aspect_ratio="4:3",
                        reference_image_bytes=src_bytes,
                        reference_image_mime_type="image/png",
                    )

                    ct = img_res.mime_type if (img_res.mime_type or "").startswith("image/") else "image/png"
                    upload_bytes(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        path=picked_path,
                        data=img_res.image_bytes,
                        content_type=ct,
                    )
                    thumb_path = generate_thumb_from_source_and_upload(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        source_blob_path=picked_path,
                        download_bytes_fn=download_bytes,
                        upload_bytes_fn=upload_bytes,
                        max_side=420,
                    )

                    if gs and cfg.gsheet_id:
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_corrections",
                            values_by_col={
                                "entity_id": corr_entity,
                                "audit_entity_id": "",
                                "run_id": run_id,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "replace_from": rep_from,
                                "replace_to": rep_to,
                                "status_detail": "done",
                                "vertex_model": img_res.model,
                                "result_mime": ct,
                                "result_gcs_path": picked_path,
                                "thumb_gcs_path": thumb_path,
                                "error": "",
                            },
                        )
                    try:
                        prev_rows = list(st.session_state.get(_audit_key) or [])
                        for rr in prev_rows:
                            if str(rr.get("gcs_path") or "").strip() == picked_path:
                                rr["has_text"] = False
                                rr["detected_text"] = ""
                        st.session_state[_audit_key] = prev_rows
                    except Exception:
                        pass
                    st.success("Correction appliquée (illustration + vignette écrasées).")
                    # Force la resync au prochain rerun (la liste et la sélection vont changer).
                    for k in (
                        "_adm_vision_pick_last",
                        "adm_vision_detected_preview",
                        "adm_vision_replace_from",
                        "adm_vision_replace_to",
                    ):
                        if k in st.session_state:
                            del st.session_state[k]
                except Exception as ex:
                    try:
                        if can_sheets and cfg.gsheet_id and cfg.gcp_service_account:
                            gs2 = build_gspread_client(cfg.gcp_service_account)
                            append_immutable_row(
                                gspread_client=gs2,
                                spreadsheet_id=cfg.gsheet_id,
                                table="vision_text_corrections",
                                values_by_col={
                                    "entity_id": sha256(f"corr_err|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                    "run_id": run_id,
                                    "date": picked_date,
                                    "gcs_path": picked_path,
                                    "replace_from": (replace_from or "").strip(),
                                    "replace_to": (replace_to or "").strip(),
                                    "status_detail": "error",
                                    "error": str(ex),
                                },
                            )
                    except Exception:
                        pass
                    st.exception(ex)
                finally:
                    overlay.empty()
                st.rerun()
        else:
            if scanned_unique == 0:
                st.info("Aucun fichier sur Cloud dans la portée choisie.")
            elif errs and len(errs) >= scanned_unique and scanned_unique > 0:
                st.warning(
                    "Aucune analyse réussie : tous les appels Vision ont échoué. "
                    "Corrige la configuration (API activée, facturation, droits du compte de service) puis réessaie."
                )
            else:
                st.success("Aucune image avec texte détecté selon ces réglages.")

        if errs:
            show_raw = st.checkbox("Afficher les erreurs brutes (debug)", value=False, key="adm_text_audit_show_raw")
            with st.expander("Détail des erreurs", expanded=True):
                if show_raw:
                    err_tbl = [
                        {
                            "date": r.get("date"),
                            "chemin": r.get("gcs_path"),
                            "erreur": str(r.get("error") or ""),
                        }
                        for r in errs
                    ]
                else:
                    err_tbl = [
                        {
                            "date": r.get("date"),
                            "chemin": r.get("gcs_path"),
                            "erreur": shorten_audit_error_message(str(r.get("error") or "")),
                        }
                        for r in errs
                    ]
                st.write(f"{len(err_tbl)} erreur(s).")

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
        st.metric("Images pleines sur Cloud", n_src)
    with c2:
        st.metric("Vignettes présentes", n_thumb)
    with c3:
        st.metric("Vignettes manquantes", len(missing_sources))

    mx = st.slider("Taille max. du côté (pixels)", min_value=280, max_value=720, value=420, step=20, key="adm_thumb_max")

    st.divider()
    st.subheader("Montage annuel (52 vignettes)")
    years = sorted({str(t.get("date") or "")[:4] for t in sorted_targets if str(t.get("date") or "")[:4].isdigit()})
    year = st.selectbox("Année", options=years or ["2026"], index=0, key="adm_thumb_montage_year")
    montage_path = f"{THUMB_GCS_PREFIX}/montage_{year}.png"
    montage_pastel_path = f"{THUMB_GCS_PREFIX}/montage_{year}_pastel.png"
    montage_preview_path = f"{THUMB_GCS_PREFIX}/montage_{year}_preview.webp"
    st.caption(f"Sortie : `gs://{bucket_name}/{montage_path}` et version pastel pour le dos du PDF.")
    # Perf : ne pas retélécharger le montage à chaque rerun (ex: checkbox).
    cache_key = f"_adm_montage_cache_{year}"
    cache = dict(st.session_state.get(cache_key) or {})
    montage_exists = bool(cache.get("exists")) if "exists" in cache else False

    # Rafraîchir l'état (existence) à la demande seulement.
    if st.button("Rafraîchir l’état du montage", key=f"adm_montage_refresh_{year}"):
        overlay = loading_overlay("Vérification du montage sur Cloud…")
        try:
            montage_exists = blob_exists(gcs=gcs, bucket_name=bucket_name, path=montage_path)
            cache = {"exists": montage_exists}
            st.session_state[cache_key] = cache
        finally:
            overlay.empty()

    # Si jamais pas encore vérifié, on fait une vérif légère (sans download).
    if "exists" not in cache:
        try:
            montage_exists = blob_exists(gcs=gcs, bucket_name=bucket_name, path=montage_path)
            st.session_state[cache_key] = {"exists": montage_exists}
        except Exception:
            montage_exists = False
            st.session_state[cache_key] = {"exists": False}

    if montage_exists:
        st.info("Un montage existe déjà sur Cloud pour cette année.")
        with st.expander("Afficher le montage existant", expanded=False):
            if st.button("Charger l’aperçu (Cloud)", key=f"adm_montage_load_{year}"):
                overlay = loading_overlay("Téléchargement de l’aperçu…")
                try:
                    # On charge une vignette (WebP) beaucoup plus légère que le PNG complet.
                    montage_b = b""
                    try:
                        montage_b = download_bytes(gcs=gcs, bucket_name=bucket_name, path=montage_preview_path)
                    except Exception:
                        montage_b = b""
                    if not montage_b:
                        # Fallback si la vignette n'existe pas encore.
                        montage_b = download_bytes(gcs=gcs, bucket_name=bucket_name, path=montage_path)
                    cache2 = dict(st.session_state.get(cache_key) or {})
                    cache2["bytes"] = montage_b
                    st.session_state[cache_key] = cache2
                finally:
                    overlay.empty()
            montage_b = (st.session_state.get(cache_key) or {}).get("bytes")
            if montage_b:
                st.image(montage_b, caption=f"Montage {year} (depuis Cloud)")

    force_regen_montage = st.checkbox(
        "Régénérer le montage même s’il existe déjà",
        value=False,
        key="adm_thumb_montage_force",
    )

    if st.button(
        "Générer le montage (PNG) et l’enregistrer sur Cloud",
        type="primary",
        disabled=bool(montage_exists and not force_regen_montage),
        key="adm_thumb_montage_btn",
    ):
        overlay = loading_overlay(f"Montage des vignettes {year}…")
        try:
            # Liste des thumbs dans l’ordre des dimanches
            year_targets = [t for t in sorted_targets if str(t.get("date") or "").startswith(str(year))]
            thumb_paths: list[str] = []
            for t in year_targets:
                src = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
                if not src:
                    continue
                thumb_paths.append(gcs_thumb_path_from_source_blob(src))

            # Download en parallèle
            from core.illustration_thumbs import build_thumbnail_webp, build_thumbs_montage_png, pastelize_png

            thumbs_bytes: list[tuple[str, bytes]] = []
            with ThreadPoolExecutor(max_workers=16) as ex:
                futs = {ex.submit(download_bytes, gcs=gcs, bucket_name=bucket_name, path=p): p for p in thumb_paths}
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        b = fut.result()
                        if b:
                            thumbs_bytes.append((p, b))
                    except Exception:
                        continue

            # Re-trier selon l’ordre initial (car as_completed)
            idx = {p: i for i, p in enumerate(thumb_paths)}
            thumbs_bytes.sort(key=lambda x: idx.get(x[0], 10**9))

            # Montage portrait (A4) : 52 vignettes → 4 colonnes × 13 lignes.
            montage_png = build_thumbs_montage_png(
                thumbs_bytes,
                cols=4,
                rows=13,
                cell=200,
                pad=10,
                title_cell_text=f"Le Chemin de l'Année\n{year}",
            )
            montage_pastel_png = pastelize_png(montage_png, alpha=0.55)
            montage_preview_webp = build_thumbnail_webp(montage_png, max_side=1200, quality=80)
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_path,
                data=montage_png,
                content_type="image/png",
            )
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_pastel_path,
                data=montage_pastel_png,
                content_type="image/png",
            )
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_preview_path,
                data=montage_preview_webp,
                content_type="image/webp",
            )
            st.success("Montage enregistré.")
            st.image(montage_png, caption=f"Montage {year} (aperçu)")
            # Met à jour le cache : existe désormais.
            st.session_state[cache_key] = {"exists": True, "bytes": montage_preview_webp}
        finally:
            overlay.empty()

    if not missing_sources:
        st.success("Toutes les vignettes sont déjà générées pour les illustrations présentes sur le bucket.")
    else:
        n_missing = len(missing_sources)
        st.info(
            f"**{n_missing}** vignette(s) manquante(s) sur **{n_src}** image(s) présentes sur Cloud — "
            "tu peux les générer avec le bouton ci-dessous."
        )
        if st.button(
            "Générer les vignettes manquantes",
            type="primary",
            key="adm_thumb_gen_missing",
        ):
            overlay = loading_overlay("Génération des vignettes sur Cloud…")
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
        "Synthèse du protocole (`.cursor/rules/lumenvia.mdc`), de l’état du code et des chantiers — "
        "y compris les écarts repérés par rapport à ce qui est déjà documenté (cahier, règles, écran admin)."
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
      <td><strong>Déploiement public (Git + Streamlit Cloud) — sécurité</strong></td>
      <td><span class="lv-st-partiel">À valider</span></td>
      <td>
        Objectif : dépôt public sans fuite de secrets. Déjà en place :
        <ul>
          <li><code>.gitignore</code> ignore <code>.streamlit/secrets.toml</code>, <code>.env*</code>, clés (<code>*.pem</code>, <code>*.key</code>, <code>*service*account*.json</code>…), <code>.venv/</code>, caches.</li>
          <li>Admin login via <code>st.secrets</code> (pas d’identifiants par défaut en dur).</li>
          <li>Prompts IA (structure) externalisés dans Sheets (<code>Paramètres_IA</code>) + “secret sauce” dans <code>st.secrets</code> (<code>IA_SECRET_SAUCE_MD</code>).</li>
          <li>Fallback local <code>data/instructions_ia.md</code> réduit au minimum (repo public).</li>
        </ul>
        Reste : scan final (repo + historique) avant publication, puis paramétrage Streamlit Cloud (Secrets).
      </td>
    </tr>
    <tr>
      <td>Manifestes étape 2–3 + illustrations Cloud + grille Vertex admin</td>
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
      <td><span class="lv-st-ok">Livré</span></td>
      <td>Page dédiée Vision + correction + whitelist + filtres anti-faux-positifs (dictionnaire FR + micro-bounding-boxes).</td>
    </tr>
    <tr>
      <td>Cache local lectures AELF + synthèse / audio</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>Extensions possibles (autres médias) si le produit le demande.</td>
    </tr>
    <tr>
      <td><strong>Automatisation envoi hebdomadaire (vendredi soir) — e-mail / SMS</strong></td>
      <td><span class="lv-st-todo">À faire</span></td>
      <td>
        Objectif produit : « chaque vendredi soir votre préparation dominicale directement par e-mail, ou par SMS ».
        Chantiers : templates éditables (admin), sélection opt-in (Sheets), génération/validation des contenus (PDF/audio),
        module d’envoi (SMTP, Twilio), journal d’envoi (historique) + anti-doublons.
        <strong>Mise en route du scheduler « temps réel » :</strong>
        (1) hébergement qui reste actif (pas seulement lorsqu’un navigateur ouvre l’app) — ex. Streamlit Cloud avec
        quota suffisant ou conteneur GCP/Cloud Run&nbsp;; (2) déclencheur planifié (ex. <strong>Google Cloud Scheduler</strong>
        ou GitHub Actions cron) qui appelle un <strong>endpoint HTTP sécurisé</strong> ou un petit script utilisant le compte de service
        pour lire CMPG/RUNS et lancer l’envoi pour les campagnes dont l’heure est due (fuseau <code>timezone</code> dans CMPG)&nbsp;;
        (3) variables d’environnement / secrets alignés avec SMTP et Twilio comme en test manuel&nbsp;;
        (4) idempotence anti-doublons (clé run + date dans RUNS).
        L’UI «&nbsp;Scheduler&nbsp;» définit déjà les campagnes et le mode manuel&nbsp;; il manque le worker planifié hors session Streamlit.
      </td>
    </tr>
    <tr>
      <td><strong>Captation des retours après mailing (mini-questionnaire)</strong></td>
      <td><span class="lv-st-partiel">Livré base</span></td>
      <td>
        Page publique «&nbsp;Donner votre avis&nbsp;» + route <code>?route=feedback</code>&nbsp;; table Sheets logique
        <code>experience_feedback</code> (acronyme <strong>RSTN</strong> après <code>tools/init_sheets_db.py</code>) en append-only&nbsp;;
        lien automatique dans les e-mails lorsque le template contient la phrase
        <em>👉 Donner mon avis sur cette expérience</em> (éventuellement entre crochets). Reste&nbsp;: relier l’URL depuis le template
        avec paramètres optionnels (campagne, dimanche ciblé), synthèse admin des réponses, ou export vers Google Forms si besoin terrain.
      </td>
    </tr>
    <tr>
      <td>Authentification — récupération « mot de passe oublié »</td>
      <td><span class="lv-st-todo">À refaire</span></td>
      <td>Section retirée (insatisfaisante). Reconcevoir plus tard (UX + sécurité + parcours).</td>
    </tr>
    <tr>
      <td>PDF page de garde (dimanche) + PDF mensuel « Graine de Parole » (encart résolutions)</td>
      <td><span class="lv-st-partiel">Livré v2</span></td>
      <td>
        Déjà en place : fusion couverture + corps, numérotation, chapitre synthèse, <strong>Passerelle catéchèse</strong> en chapitre séparé si présente, page « À propos » (citation centrée, phrase de clôture centrée, dos avec montage si disponible).
        Reste : harmoniser encore la hiérarchie visuelle (H1/H2) avec l’écran « Lumière du Dimanche », et peaufiner le PDF mensuel (gabarit fascicule multi-pages si besoin produit).
      </td>
    </tr>
    <tr>
      <td>PDF — dos (montage annuel des vignettes)</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Image Cloud <code>Images/thumbs/montage_{année}.png</code>, insertion avec garde-fous LayoutError ; affinements possibles (texte d’intro dos, taille image selon devices PDF).
      </td>
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
      <td>Paramètres IA (Sheets — standard MARPA) + secret sauce</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Admin : édition socle/surcouches dans <code>Paramètres_IA</code> (avec <code>Description</code> lisible) ; secret sauce jamais affichée en clair.
        Reste : gouvernance (qui peut éditer), sauvegarde/exports, et nettoyage éventuel d’historique.
      </td>
    </tr>
    <tr>
      <td><strong>Suivi Gemini + consolidation produit</strong></td>
      <td><span class="lv-st-partiel">Itératif</span></td>
      <td>
        Arbitrages qualité illustrations / prompts ; aligner la doc longue (<code>data/cahier_des_charges.md</code>) avec les choix réels (overlay, PDF, mobile).
        Tenir cette table à jour quand un chantier change de statut.
      </td>
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
      <td><strong>Refactor codebase (maintenabilité)</strong></td>
      <td><span class="lv-st-todo">À faire</span></td>
      <td>
        Réduire <code>app.py</code> (8k+ lignes) à un shell (styles + navigation + routage) et extraire les pages et l’admin en modules dédiés.
        Proposition : <code>ui/pages/*</code> (about/sunday/newsletter/account/memo/feedback) + <code>ui/admin/*</code> (1 fichier par tuile),
        puis scinder progressivement les gros modules <code>core/*</code> / <code>channel/*</code> par domaine.
      </td>
    </tr>
    <tr>
      <td>UX — <strong>overlay systématique</strong> pendant tout traitement serveur perceptible</td>
      <td><span class="lv-st-ok">Règle</span></td>
      <td>
        Dès qu’une action déclenche un traitement serveur (Sheets, Cloud, Vision, Vertex/Gemini, génération PDF, etc.),
        afficher un <strong>calque plein écran</strong> (overlay) jusqu’à la fin du traitement, pour éviter l’impression que « rien ne se passe ».
        Pattern : <code>overlay = loading_overlay(...)</code> puis <code>overlay.empty()</code> dans un <code>finally</code>.
      </td>
    </tr>
    <tr>
      <td>IA — « Passerelle catéchèse » (<strong>Stone Card</strong>) dans la synthèse + option PDF</td>
      <td><span class="lv-st-partiel">Livré base</span></td>
      <td>
        Section dédiée dans la synthèse + chapitre PDF séparé ; option d’exclusion PDF côté UI.
        Reste : enrichir le gabarit éditorial (validation rédactionnelle), affiner garde-fous si besoin terrain.
      </td>
    </tr>
    <tr>
      <td>Administration — <strong>simulateur vision mobile</strong></td>
      <td><span class="lv-st-ok">Livré v1</span></td>
      <td>
        Page dédiée <strong>« Simulateur mobile »</strong> (troisième ligne du menu Administration) : préréglages 320–428&nbsp;px + slider,
        boutons d’accès Dimanche&nbsp;/&nbsp;Mémo&nbsp;/&nbsp;À&nbsp;propos avec cadre ; iframe optionnelle si <code>PUBLIC_APP_URL</code>
        ou <code>st.context.url</code> disponible ; le même réglage de largeur s’applique au cadre téléphone si activé sur cette page.
        Complément recette&nbsp;: Chrome/Edge mode appareil pour clavier réaliste.
      </td>
    </tr>
  </tbody>
</table>

<dl class="lv-keylist">
  <dt>Trois points chirurgicaux UX mobile (référence verrouillée)</dt>
  <dd>
    <strong>1 — Navigation.</strong> <strong>≥1025&nbsp;px&nbsp;</strong>&nbsp;: quatre tuiles Rubriques en ligne, colonne Menu masquée.     <strong>≤1024&nbsp;px&nbsp;</strong>&nbsp;: uniquement le déclencheur <strong>«&nbsp;Menu&nbsp;»</strong> — rubriques + (si session admin)
    dans le panneau ; pas de tuiles dupliquées sous le logo (<code>@media max-width:&nbsp;1024px</code>).
    <strong>Iframe simulateur&nbsp;:</strong> <code>lumenvia_narrow_nav=1</code> dans l’URL (viewport parent). <strong>Téléphone déployé&nbsp;:</strong> même layout
    si <code>st.context.headers</code> («&nbsp;User-Agent&nbsp;» téléphone/Android/iPhone…) — sans cette détection, Streamlit peut laisser un viewport «&nbsp;bureau&nbsp;»
    où le CSS suffit rarement ; secours <code>lv_nav_five_cols</code> sous <code>max-width:&nbsp;1024px</code>.
    Connexion / déconnexion&nbsp;: ligne sous la navigation. Grille admin masquée ou sautée selon compact&nbsp;; le cadre mobile se pilote depuis la tuile Simulateur.
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
  <dd>Admin : page Simulateur mobile (+ cadre téléphone pour la session, largeur réglable).</dd>
  <dd>Stabiliser Vision sur le bon projet GCP et valider une analyse complète sans 403.</dd>
  <dd>Repasser sur le PDF mensuel et la couverture si tu veux un gabarit « fascicule » multi-pages.</dd>
  <dd>PWA : choix d’hébergement et socle technique pour exposer le manifest au navigateur.</dd>
</dl>

<dl class="lv-keylist">
  <dt>Écart documentaire — déjà relevé dans le dépôt (à refléter progressivement dans le Markdown)</dt>
  <dd>
    <strong>Règle projet</strong> : le cahier dans <code>data/cahier_des_charges.md</code> est encore minimal alors que l’app embarque déjà overlay obligatoire, cache AELF, pipelines d’images, admin Vision/PDF, etc.
    → soit export « snapshot » depuis l’admin (ligne tableau), soit enrichissement manuel du cahier.
  </dd>
  <dd>
    <strong>Graine de Parole / PDF mensuel</strong> : la règle <code>lumenvia.mdc</code> mentionnait l’encart résolutions « quand le générateur PDF sera branché » — le générateur existe ; la formulation mérite mise à jour dans la règle pour éviter une fausse « dette ».
  </dd>
  <dd>
    <strong>Newsletter / SMS</strong> : mentionnés dans la page « À propos » comme canaux ; vérifier pour chaque environnement ce qui est réellement câblé (Sheets, envoi, conformité) vs. pure intention produit.
  </dd>
  <dd>
    <strong>Typo &amp; PDF</strong> : la page web et le PDF « À propos » partagent le même texte source ; les finitions PDF (centrages, sauts) sont dans <code>pdf_liturgy_sunday.py</code> — à garder synchronisés si le texte marketing change.
  </dd>
</dl>
</div>
"""
    st.markdown(plan_html, unsafe_allow_html=True)


_CDC_MARKDOWN_PATH = Path("data/cahier_des_charges.md")


def render_admin_cahier_charges() -> None:
    """Document Markdown versionné + journal Sheets."""
    st.title("Cahier des charges")
    st.markdown(
        """
**Document principal** : fichier Markdown dans le dépôt (`data/cahier_des_charges.md`), éditable ci-dessous puis sauvegardé sur le serveur qui exécute Streamlit.

**Journal des évolutions** : entrées dans la table Google Sheets `admin_changelog` (traçabilité des décisions sans effacer l’historique).
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
            ov = loading_overlay("Enregistrement dans le journal (Google Sheets)…")
            gs = build_gspread_client(cfg.gcp_service_account)
            try:
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
            finally:
                ov.empty()

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
    if not (login_ok and pwd_ok):
        st.error(
            "Administration désactivée : configure `ADMIN_LOGIN` et `ADMIN_PASSWORD` dans `st.secrets` "
            "(Streamlit Cloud → Secrets) pour activer la connexion."
        )
        return
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
- **Dans l’app** : sur « La Lumière du Dimanche », l’image affichée est celle du **dimanche choisi** par l’utilisateur (fichier présent dans le Cloud au chemin du manifeste).
- **Communication** : la même illustration peut illustrer le **SMS**, l’**e-mail** ou la **newsletter** de la semaine pour laquelle tu fixes ce dimanche comme référence.

**Autres usages possibles** : visuel pour **réseaux sociaux** ou **Open Graph** du lien du jour ; **PDF** ou fascicule mensuel ; **diaporama** ou fond d’écran en paroisse ; **carte de partage** (PWA / lien) ; **miniature** dans un récap hebdomadaire ; **kit presse** ou **affiche** locale pour une grande solennité.

### Fréquence de production

Le manifeste est construit **pour une année civile** (script étape 2 avec `--year`). Une fois **toutes** les images générées et déposées sur le Cloud pour cette année, **tu n’as pas besoin d’y revenir** tant que tu restes sur cette même année — sauf **retouche ponctuelle**, **changement de charte**, ou passage à **l’année suivante** (nouveau manifeste + nouvelles images).

{f"**Année couverte par ce fichier** : **{year_hint}** ({len(targets)} dimanches)." if year_hint else f"**Dimanches dans ce manifeste** : {len(targets)}."}
        """.strip()
    )

    render_admin_illustration_gen_panel(data=data, manifest_path=manifest_path)


def render_admin_test_resources() -> None:
    st.title("Admin - diagnostique des ressources")
    cfg = load_config()
    st.write("Cette page sert à valider l’accès aux ressources configurées dans `secrets.toml`.")

    if not cfg.gcp_service_account:
        st.error("gcp_service_account manquant dans secrets.")
        return

    st.subheader("Identité / projet")
    sa = dict(cfg.gcp_service_account or {})
    sa_email = str(sa.get("client_email") or "").strip()
    sa_project_id = str(sa.get("project_id") or "").strip()
    sa_quota_project_id = str(sa.get("quota_project_id") or sa.get("project_id") or "").strip()
    diag = {
        "service_account": sa_email or "—",
        "project_id": sa_project_id or "—",
        "quota_project_id": sa_quota_project_id or "—",
        "gcs_bucket_name": str(cfg.gcs_bucket_name or "").strip() or "—",
        "gsheet_id_present": bool(str(cfg.gsheet_id or "").strip()),
    }
    st.code("\n".join([f"{k}: {v}" for k, v in diag.items()]))

    st.divider()
    st.subheader("Google Bucket")
    if not cfg.gcs_bucket_name:
        st.warning("gcs_bucket_name manquant.")
    else:
        with st.expander(f"Structure du bucket `gs://{cfg.gcs_bucket_name}`", expanded=False):
            try:
                gcs = build_gcs_client(cfg.gcp_service_account)
                gcs_project = str(getattr(gcs, "project", "") or "").strip()
                if gcs_project:
                    st.caption(f"Client Cloud — projet effectif : `{gcs_project}`")

                bucket_name = str(cfg.gcs_bucket_name).strip()
                bucket = gcs.bucket(bucket_name)

                # On liste un nombre raisonnable d’objets, puis on reconstruit une arborescence.
                # Objectif: diagnostic lisible (pas un inventaire exhaustif).
                names = [b.name for b in gcs.list_blobs(bucket, max_results=800)]
                if not names:
                    st.info("Aucun objet détecté (bucket vide ou droits insuffisants).")
                else:
                    def _add(tree: dict, parts: list[str]) -> None:
                        cur = tree
                        for p in parts:
                            if not p:
                                continue
                            cur = cur.setdefault(p, {})

                    tree: dict[str, dict] = {}
                    for n in names:
                        parts = [p for p in str(n).split("/") if p]
                        # On affiche uniquement la STRUCTURE de dossiers (pas les fichiers)
                        if len(parts) >= 2:
                            _add(tree, parts[: min(4, len(parts) - 1)])  # profondeur limitée, sans feuille fichier

                    def _render(cur: dict[str, dict], indent: int = 0, *, max_children: int = 40) -> list[str]:
                        out: list[str] = []
                        for i, k in enumerate(sorted(cur.keys())):
                            if i >= max_children:
                                out.append("  " * indent + "…")
                                break
                            # Ici on ne rend que des dossiers.
                            out.append("  " * indent + f"- {k}/")
                            out.extend(_render(cur[k], indent + 1, max_children=max_children))
                        return out

                    st.success(f"Cloud OK — bucket `{bucket_name}` ({len(names)} objet(s) échantillonnés)")
                    st.code("\n".join(_render(tree)), language="markdown")
                    st.caption("Affichage limité (profondeur/quantité) : c’est une vue de structure pour diagnostic.")
            except Exception as e:
                st.error(f"Cloud KO — {e}")

    st.divider()
    st.subheader("Google Sheet")
    if not cfg.gsheet_id:
        st.warning("gsheet_id manquant.")
    else:
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            sh = gs.open_by_key(cfg.gsheet_id)
            ws_titles = [w.title for w in sh.worksheets()]
            st.success(f"Sheets OK — {len(ws_titles)} onglet(s) accessibles.")
            if "Paramètres_IA" in ws_titles:
                ws = sh.worksheet("Paramètres_IA")
                header = ws.row_values(1)
                if "Description" in header:
                    st.success("Table `Paramètres_IA` OK — colonne `Description` présente.")
                else:
                    st.warning("Table `Paramètres_IA` : colonne `Description` absente (header à mettre à jour).")
            else:
                st.warning("Onglet `Paramètres_IA` absent (lance `init_sheets_db.py`).")
        except Exception as e:
            st.error(f"Sheets KO — {e}")

    st.divider()
    st.subheader("Dépendances runtime")
    st.caption("Vérifie que ce runtime Streamlit a bien les librairies nécessaires (PDF/Excel, etc.).")
    try:
        import openpyxl  # type: ignore

        st.success(f"openpyxl OK — version {getattr(openpyxl, '__version__', '?')} ({getattr(openpyxl, '__file__', '')})")
    except Exception as e:
        st.warning(f"openpyxl non importable dans CE runtime Streamlit : {e}")

    st.divider()
    st.subheader("IA : Gemini API TTS et VertexAI")
    st.caption(
        "VertexAI est la voie principale (via compte de service). "
        "La Gemini API (clé `GEMINI_API_KEY`) sert de **fallback** pour la TTS si Vertex refuse l’AUDIO (allowlist) "
        "ou en cas de quota/erreur transitoire."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        if not cfg.gemini_api_key:
            st.info("Gemini API : non configurée (`GEMINI_API_KEY` manquante).")
        else:
            if st.button("Tester Gemini TTS (court)", key="adm_test_gemini_tts"):
                ov = loading_overlay("Test Gemini TTS…")
                try:
                    from core.gemini_tts_api import GeminiTtsApiClient

                    t0 = time.perf_counter()
                    cli = GeminiTtsApiClient(api_key=cfg.gemini_api_key)
                    res = cli.generate_audio(
                        model="gemini-2.5-flash-preview-tts",
                        text="Test audio LumenVia. Un, deux, trois.",
                        voice_name="Kore",
                    )
                    dt = time.perf_counter() - t0
                    st.success(f"Gemini TTS OK — {len(res.audio_bytes)} octets en {dt:.2f}s ({res.mime_type})")
                    st.audio(res.audio_bytes, format=res.mime_type or "audio/wav")
                except Exception as e:
                    st.error(f"Gemini TTS KO — {e}")
                finally:
                    ov.empty()
    with col_b:
        if st.button("Tester VertexAI (texte court)", key="adm_test_vertex_text"):
            ov = loading_overlay("Test VertexAI (texte)…")
            try:
                from core.vertex_gemini import VertexGeminiClient

                t0 = time.perf_counter()
                vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
                res = vx.generate_text_auto(
                    preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                    prompt="Réponds uniquement par « OK ».",
                    max_output_tokens=32,
                )
                dt = time.perf_counter() - t0
                st.success(f"VertexAI OK — {res.model} en {dt:.2f}s")
                st.code((res.text or "").strip()[:400] or "—")
            except Exception as e:
                st.error(f"VertexAI KO — {e}")
            finally:
                ov.empty()

    st.caption(
        "Journal produit / décisions d’architecture : menu **Administration → Cahier des charges**."
    )

    st.divider()
    st.divider()
    st.subheader("Mes prompts à l’IA (secret sauce)")
    st.caption(
        "Le prompt final est composé de deux parties : "
        "A) un **socle + surcouches** versionnés dans Sheets (`Paramètres_IA`) ; "
        "B) une partie confidentielle (secret sauce) dans `st.secrets` (`IA_SECRET_SAUCE_MD`).\n\n"
        "Règles importantes :\n"
        "- **Append-only** : chaque modification crée une nouvelle ligne (Version + 1).\n"
        "- **Par Clé_Prompt, ce n’est pas cumulatif** : une seule version est **effective** (la dernière marquée Actif).\n"
        "- **Cumulatif entre clés uniquement si le code les assemble** : par exemple, `instructions_base_md` (socle) + "
        "`overlay_takeaways` / `overlay_no_takeaways` (selon options) + `overlay_catechese_bridge` (si coché). "
        "Ajouter une nouvelle Clé_Prompt ne change rien tant qu’elle n’est pas consommée dans `_build_prompt()`."
    )

    # Secret sauce : on n’affiche jamais le contenu en clair dans l’admin, seulement l’état.
    try:
        ss = str(st.secrets.get("IA_SECRET_SAUCE_MD") or "").strip()
    except Exception:
        ss = ""
    if ss:
        st.success(f"Secret sauce : configurée ({len(ss)} caractères).")
    else:
        st.warning("Secret sauce : absente (`IA_SECRET_SAUCE_MD` non défini).")

    with st.expander("Fallback local minimal (dépôt public) — `data/instructions_ia.md`", expanded=False):
        instr_path = Path("data/instructions_ia.md")
        if not instr_path.is_file():
            st.warning(f"Fichier introuvable : `{instr_path.as_posix()}`.")
        else:
            st.code(instr_path.read_text(encoding="utf-8").strip()[:2000] or "", language="markdown")
            st.caption("Ce fallback doit rester minimal dans le dépôt public (il ne doit pas contenir la matière du prompt).")

    if not (cfg.gsheet_id and cfg.gcp_service_account):
        st.info("Configure `gsheet_id` + `gcp_service_account` pour gérer les templates IA ici.")
    else:
        gs = build_gspread_client(cfg.gcp_service_account)
        try:
            rows = fetch_records(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="Paramètres_IA",
                limit=5000,
            )
        except Exception as e:
            rows = []
            st.warning(f"Lecture `Paramètres_IA` impossible : {e}")

        # Admin : on affiche uniquement les templates effectivement Actifs (pivot “latest”),
        # sans injecter des clés “théoriques” qui n’existent pas en base.
        latest = pick_effective_templates(rows, allowed_keys=None)
        existing_keys = sorted([k for k, v in latest.items() if (v.content_md or "").strip()])
        # Libellés lisibles : on prend la Description de la ligne EFFECTIVE (pivot latest),
        # pas “la première trouvée” dans l’historique (sinon incohérences visuelles).
        def _norm0(v: object) -> str:
            return str(v or "").strip()

        desc_by_key: dict[str, str] = {}
        for k, eff in latest.items():
            # retrouve la ligne correspondante dans rows pour récupérer Description
            # (en priorité via #ID, sinon via (clé, version)).
            chosen: str = ""
            eff_id = _norm0(getattr(eff, "id", ""))
            eff_ver = int(getattr(eff, "version", 0) or 0)
            for r in rows:
                if _norm0(r.get("Clé_Prompt")) != k:
                    continue
                rid = _norm0(r.get("#ID") or r.get("ID") or r.get("id"))
                if eff_id and rid and rid == eff_id:
                    chosen = _norm0(r.get("Description"))
                    break
            if not chosen:
                for r in rows:
                    if _norm0(r.get("Clé_Prompt")) != k:
                        continue
                    try:
                        rv = int(_norm0(r.get("Version") or 0) or 0)
                    except Exception:
                        rv = 0
                    if rv != eff_ver:
                        continue
                    chosen = _norm0(r.get("Description"))
                    if chosen:
                        break
            if chosen:
                desc_by_key[k] = chosen

        def _fmt_key(k: str) -> str:
            d = (desc_by_key.get(k) or _PROMPT_TEMPLATE_LABELS.get(k) or "").strip()
            return f"{k} — {d}" if d else k

        create_new = st.toggle("Créer un nouveau prompt (socle / surcouche)", value=False, key="adm_tpl_create_new")
        if create_new:
            picked = st.text_input(
                "Clé_Prompt (identifiant technique stable)",
                value="",
                key="adm_tpl_new_key",
                help="Exemples : `instructions_base_md` (socle), `overlay_takeaways` (surcouche), `retry_hardened_prefix` (préfixe de relance). "
                "Évite les espaces ; utilise des minuscules + underscores.",
            ).strip()
            current = ""
            current_desc = ""
        else:
            if not existing_keys:
                st.warning("Aucun prompt Actif trouvé en base (`Paramètres_IA`).")
                return
            picked = st.selectbox(
                "Choisir un prompt existant (Actif)",
                options=existing_keys,
                index=existing_keys.index("instructions_base_md") if "instructions_base_md" in existing_keys else 0,
                key="adm_tpl_key",
                format_func=_fmt_key,
            )
            current = (latest.get(picked).content_md if picked in latest else "").strip()
            current_desc = (desc_by_key.get(picked) or _PROMPT_TEMPLATE_LABELS.get(picked) or "").strip()

        edited_desc = st.text_input(
            "Description (affichage dans la liste)",
            value=current_desc,
            key=f"adm_tpl_desc__{picked}",
            help="Optionnel. Sert uniquement à rendre la liste plus claire (tu peux mettre un nom métier).",
        )
        edited = st.text_area(
            "Contenu (Markdown)",
            value=current,
            height=260,
            # IMPORTANT Streamlit: une key fixe “colle” le texte quand on change le selectbox.
            # Une key dépendante du template garde un état par template.
            key=f"adm_tpl_editor__{picked}",
            help="Append-only : enregistre une nouvelle version (Version + 1).",
        )
        notes = st.text_input("Notes (optionnel)", key="adm_tpl_notes", value="")
        active = st.checkbox("Activer ce prompt", value=True, key="adm_tpl_active", help="Si coché, l’ancien Actif de la même Clé_Prompt sera automatiquement désactivé.")
        date_effet = st.date_input("Date d'effet", value=date.today(), key="adm_tpl_date_effet")

        disabled_save = (not bool(edited.strip())) or (create_new and not bool(picked.strip()))
        if st.button("Enregistrer (nouvelle version dans Sheets)", type="primary", disabled=disabled_save, key="adm_tpl_save"):
            ov = loading_overlay("Enregistrement du template IA (Sheets)…")
            try:
                body = edited.strip()
                # Onglet MARPA: Paramètres_IA
                sh = gs.open_by_key(cfg.gsheet_id)
                ws = sh.worksheet("Paramètres_IA")
                header = ws.row_values(1)
                if not header:
                    raise RuntimeError("Onglet `Paramètres_IA` non initialisé (header vide). Lance init_sheets_db.")
                if "Description" not in header:
                    raise RuntimeError("Colonne `Description` manquante dans `Paramètres_IA`. Relance init_sheets_db.py ou mets à jour le header.")

                def _norm(s: object) -> str:
                    return str(s or "").strip()

                # Calcule la prochaine version à partir de la table (pas seulement “latest”),
                # car la table peut contenir plusieurs versions “Actif” à assainir.
                key_norm = _norm(picked)
                max_ver = 0
                for r in rows:
                    if _norm(r.get("Clé_Prompt")) != key_norm:
                        continue
                    try:
                        max_ver = max(max_ver, int(_norm(r.get("Version") or 0) or 0))
                    except Exception:
                        pass
                next_ver = int(max_ver + 1)
                de = str(date_effet)

                # MARPA (sans supprimer) : on met à jour EN PLACE les lignes Actif existantes
                # pour cette clé (Statut -> Inactif), puis on append uniquement la nouvelle version.
                if active:
                    try:
                        records = ws.get_all_records()  # lignes à partir de la ligne 2
                    except Exception:
                        records = []

                    def _make_concat(*, row_id: str, key: str, version: str, statut: str, date_effet: str) -> str:
                        return " | ".join([_norm(row_id), _norm(key), _norm(version), _norm(statut), _norm(date_effet)])

                    try:
                        col_statut = header.index("Statut") + 1
                        col_concat = header.index("Concaténation") + 1
                    except Exception:
                        col_statut = 0
                        col_concat = 0

                    # Update les cellules une par une (peu de lignes) pour rester robuste.
                    if col_statut and col_concat:
                        for i, r in enumerate(records):
                            if _norm(r.get("Clé_Prompt")) != key_norm:
                                continue
                            if not sheet_row_status_is_live(r.get("Statut")):
                                continue
                            # Row number dans Sheets (header=1, records commencent à 2)
                            row_num = i + 2
                            ws.update_cell(row_num, col_statut, "Inactif")

                            row_id = _norm(r.get("#ID") or r.get("ID") or r.get("id"))
                            ver_str = _norm(r.get("Version"))
                            de_str = _norm(r.get("Date_Effet")) or de
                            ws.update_cell(row_num, col_concat, _make_concat(row_id=row_id, key=key_norm, version=ver_str, statut="Inactif", date_effet=de_str))

                row_id = sha256(f"ia|{key_norm}|{next_ver}|{body}".encode("utf-8")).hexdigest()[:18]
                statut = "Actif" if active else "Inactif"
                concat = " | ".join([row_id, key_norm, str(next_ver), statut, de])
                row_map = {
                    "#ID": row_id,
                    "Clé_Prompt": key_norm,
                    "Description": str(edited_desc or "").strip(),
                    "Version": str(next_ver),
                    "Statut": statut,
                    "Date_Effet": de,
                    "Contenu_Markdown": body,
                    "Concaténation": concat,
                }
                ws.append_rows([[row_map.get(c, "") for c in header]], value_input_option="RAW")

                st.success("Paramètre IA enregistré.")
                # Force refresh du cache prompt
                try:
                    _load_prompt_templates_cached.clear()  # type: ignore[attr-defined]
                except Exception:
                    pass
                st.rerun()
            finally:
                ov.empty()

    # (Les dépendances runtime ont été déplacées plus haut.)


def render_admin_readings_cache() -> None:
    st.title("Cache lectures (AELF → Sheets)")
    st.caption(
        "Cette page permet de précharger les lectures liturgiques (AELF) dans la table `readings_cache`, "
        "sans doublons. Utile pour accélérer l’usage et stabiliser le rendu (web/PDF)."
    )
    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.error("Configure `gcp_service_account` et `gsheet_id` dans `.streamlit/secrets.toml`.")
        return

    zone = "france"
    today = date.today()
    year = st.number_input("Année", min_value=2020, max_value=2100, value=int(today.year), step=1)
    month = st.selectbox(
        "Mois (optionnel)",
        options=[("all", "Toute l’année")] + [(f"{i:02d}", f"{i:02d}") for i in range(1, 13)],
        format_func=lambda x: x[1],
        index=0,
        key="adm_readings_cache_month",
    )[0]

    def _normalize_aelf_text_for_cache_local(s: str | None) -> str:
        raw = (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return ""
        return re.sub(r"\s+", " ", raw).strip()

    def _sundays_in_year(y: int) -> list[date]:
        d = date(int(y), 1, 1)
        # weekday(): Monday=0 ... Sunday=6
        days_to_sun = (6 - d.weekday()) % 7
        d = d + timedelta(days=days_to_sun)
        out: list[date] = []
        while d.year == int(y):
            out.append(d)
            d = d + timedelta(days=7)
        return out

    def _sundays_in_month(y: int, m: int) -> list[date]:
        out: list[date] = []
        for d in _sundays_in_year(y):
            if d.month == int(m):
                out.append(d)
        return out

    targets = _sundays_in_year(year) if month == "all" else _sundays_in_month(year, int(month))
    st.metric("Dimanches à vérifier", len(targets))

    if st.button("Précharger dans `readings_cache`", type="primary", key="adm_readings_cache_run"):
        ov = loading_overlay("Préchargement des lectures…")
        try:
            from core.sheets_db import TableSpec, ensure_table, fetch_records, append_immutable_rows_bulk
            from core.aelf import AelfDayIdentity, AelfTexts

            gs = build_gspread_client(cfg.gcp_service_account)
            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=TableSpec(
                    name="readings_cache",
                    columns=with_concat(
                        [
                            *BASE_COLUMNS,
                            "date",
                            "zone",
                            "periode",
                            "semaine",
                            "annee",
                            "couleur",
                            "fete",
                            "jour_liturgique_nom",
                            "premiere_lecture",
                            "psaume",
                            "deuxieme_lecture",
                            "evangile",
                            "source",
                            "error",
                        ]
                    ),
                ),
            )

            existing = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="readings_cache", limit=6000)
            existing_dates = {
                str(r.get("date") or "").strip()
                for r in existing
                if str(r.get("zone") or "").strip() == zone
                and sheet_row_status_is_live(r.get("status"))
                and not str(r.get("error") or "").strip()
                and str(r.get("date") or "").strip().startswith(str(year))
            }

            to_fetch = [d for d in targets if d.isoformat() not in existing_dates]
            st.write(f"À récupérer : **{len(to_fetch)}** dimanche(s).")
            if not to_fetch:
                st.success("Rien à faire : tout est déjà en base pour cette sélection.")
                return

            rows: list[dict[str, str]] = []
            for d in to_fetch:
                ds = d.isoformat()
                try:
                    identity, texts = cached_aelf(ds, zone=zone, _identity_schema=4)
                    # Normalisation “bloc” pour stockage
                    rows.append(
                        {
                            "entity_id": sha256(f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                            "date": ds,
                            "zone": zone,
                            "periode": getattr(identity, "periode", None) or "",
                            "semaine": getattr(identity, "semaine", None) or "",
                            "annee": getattr(identity, "annee", None) or "",
                            "couleur": getattr(identity, "couleur", None) or "",
                            "fete": getattr(identity, "fete", None) or "",
                            "jour_liturgique_nom": getattr(identity, "jour_liturgique_nom", None) or "",
                            "premiere_lecture": _normalize_aelf_text_for_cache_local(getattr(texts, "premiere_lecture", None)),
                            "psaume": _normalize_aelf_text_for_cache_local(getattr(texts, "psaume", None)),
                            "deuxieme_lecture": _normalize_aelf_text_for_cache_local(getattr(texts, "deuxieme_lecture", None)),
                            "evangile": _normalize_aelf_text_for_cache_local(getattr(texts, "evangile", None)),
                            "source": "aelf_api_prefetch",
                            "error": "",
                        }
                    )
                except Exception as ex:
                    rows.append(
                        {
                            "entity_id": sha256(f"read|{ds}|{zone}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                            "date": ds,
                            "zone": zone,
                            "source": "aelf_api_prefetch",
                            "error": str(ex)[:900],
                        }
                    )

            added = append_immutable_rows_bulk(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="readings_cache",
                values_by_col_list=rows,
                chunk_size=120,
            )
            st.success(f"Préchargement terminé : **{added}** ligne(s) ajoutée(s).")
        finally:
            ov.empty()


def render_admin_accounts() -> None:
    st.title("Comptes inscrits")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    try:
        users = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=6000)
    except Exception as e:
        st.error(f"Lecture `users` impossible : {e}")
        return
    try:
        subs = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=6000)
    except Exception:
        subs = []

    # "Flash message" persistant (après rerun)
    flash = str(st.session_state.get("adm_addsub_flash") or "").strip()
    if flash:
        st.success(flash)
        st.session_state.pop("adm_addsub_flash", None)

    # Nonce pour forcer un "reset" visuel fiable des champs Streamlit après succès
    # (en changeant les keys des widgets plutôt que de dépendre d'un pop()).
    nonce = int(st.session_state.get("adm_addsub_nonce") or 0)

    with st.expander(
        "Ajouter des abonnés (lot de 5)",
        expanded=bool(st.session_state.get("adm_addsub_open") or False),
    ):
        def _norm_email(s: object) -> str:
            return str(s or "").strip().lower()

        def _email_ok(em: str) -> bool:
            return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)) if em else False

        def _phone_ok(ph: str) -> bool:
            if not ph:
                return True
            return bool(re.match(r"^\+\d{8,15}$", ph))

        # Formulaire en lot : 5 lignes
        with st.form("adm_add_subscribers_5"):
            col_a, col_b, col_c, col_d = st.columns([1.3, 1, 1, 1], gap="small")
            with col_a:
                st.markdown("**E-mail**")
            with col_b:
                st.markdown("**Prénom**")
            with col_c:
                st.markdown("**Nom**")
            with col_d:
                st.markdown("**Téléphone (optionnel)**")

            rows_in: list[dict[str, str]] = []
            for i in range(5):
                c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1], gap="small")
                with c1:
                    em = st.text_input("E-mail", label_visibility="collapsed", key=f"adm_addsub_em_{nonce}_{i}").strip()
                with c2:
                    fn = st.text_input("Prénom", label_visibility="collapsed", key=f"adm_addsub_fn_{nonce}_{i}").strip()
                with c3:
                    ln = st.text_input("Nom", label_visibility="collapsed", key=f"adm_addsub_ln_{nonce}_{i}").strip()
                with c4:
                    ph = st.text_input(
                        "Téléphone",
                        label_visibility="collapsed",
                        key=f"adm_addsub_ph_{nonce}_{i}",
                        placeholder="+33612345678",
                    ).strip()
                rows_in.append({"email": em, "first_name": fn, "last_name": ln, "phone_e164": ph})

            country = st.selectbox("Pays", options=["FR"], index=0, key=f"adm_addsub_country_{nonce}")
            length_pref = st.selectbox(
                "Préférence de longueur",
                options=["150", "250", "400"],
                index=1,
                key=f"adm_addsub_lenpref_{nonce}",
            )
            do_submit = st.form_submit_button("Créer ces abonnés", type="primary")

        if do_submit:
            # En cas d'erreur, on garde l'expander ouvert au rerun.
            st.session_state["adm_addsub_open"] = True
            # Nettoyage + validation
            cleaned: list[dict[str, str]] = []
            for r in rows_in:
                em_lc = _norm_email(r.get("email"))
                fn = str(r.get("first_name") or "").strip()
                ln = str(r.get("last_name") or "").strip()
                ph = str(r.get("phone_e164") or "").strip()
                if not (em_lc or fn or ln or ph):
                    continue  # ligne vide
                cleaned.append({"email": em_lc, "first_name": fn, "last_name": ln, "phone_e164": ph})

            if not cleaned:
                st.warning("Aucune ligne renseignée.")
            else:
                bad_lines: list[str] = []
                for idx, r in enumerate(cleaned, start=1):
                    em_lc = r["email"]
                    if not _email_ok(em_lc):
                        bad_lines.append(f"Ligne {idx} : e-mail invalide.")
                    if not r["first_name"] or not r["last_name"]:
                        bad_lines.append(f"Ligne {idx} : prénom/nom requis.")
                    if not _phone_ok(r.get("phone_e164") or ""):
                        bad_lines.append(f"Ligne {idx} : téléphone invalide (format +336...).")
                if bad_lines:
                    for m in bad_lines[:12]:
                        st.error(m)
                    if len(bad_lines) > 12:
                        st.error(f"... et {len(bad_lines) - 12} autre(s) erreur(s).")
                else:
                    ov = loading_overlay("Création des abonnés…")
                    try:
                        from core.sheets_db import append_immutable_rows_bulk

                        # Index existants (dernier état par e-mail / par user_entity_id)
                        by_email: dict[str, dict] = {}
                        for u in users:
                            em = _norm_email(u.get("email"))
                            if not em:
                                continue
                            prev = by_email.get(em)
                            if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
                                by_email[em] = u

                        latest_sub_by_uid: dict[str, dict] = {}
                        for s in subs:
                            if str(s.get("type") or "").strip() != "weekly_friday":
                                continue
                            uid0 = str(s.get("user_entity_id") or "").strip()
                            if not uid0:
                                continue
                            prev = latest_sub_by_uid.get(uid0)
                            if not prev or str(s.get("created_at") or "") > str(prev.get("created_at") or ""):
                                latest_sub_by_uid[uid0] = s

                        to_add_users: list[dict[str, str]] = []
                        to_add_subs: list[dict[str, str]] = []
                        seen_batch: set[str] = set()
                        already_users: list[str] = []
                        already_optin: list[str] = []

                        for r in cleaned:
                            em_lc = r["email"]
                            if em_lc in seen_batch:
                                continue
                            seen_batch.add(em_lc)
                            uid0 = sha256(em_lc.encode("utf-8")).hexdigest()[:24]

                            # User (si absent)
                            if em_lc not in by_email:
                                to_add_users.append(
                                    {
                                        "entity_id": uid0,
                                        "email": em_lc,
                                        "first_name": r["first_name"],
                                        "last_name": r["last_name"],
                                        "phone_e164": r.get("phone_e164") or "",
                                        "country": str(country or "").strip(),
                                        # Aligné avec “Nous rejoindre”
                                        "source": "newsletter",
                                    }
                                )
                                by_email[em_lc] = {"entity_id": uid0, "email": em_lc, "created_at": utc_now_iso()}
                            else:
                                already_users.append(em_lc)

                            # Subscription (si pas active)
                            last = latest_sub_by_uid.get(uid0)
                            if _subscription_is_active(last):
                                already_optin.append(em_lc)
                                continue
                            sub_entity = sha256(f"sub|{uid0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                            to_add_subs.append(
                                {
                                    "entity_id": sub_entity,
                                    "user_entity_id": uid0,
                                    "type": "weekly_friday",
                                    "zone": "france",
                                    "length_pref": str(length_pref or "250").strip(),
                                    "opt_in": "true",
                                    "active": "true",
                                }
                            )

                        added_u = append_immutable_rows_bulk(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="users",
                            values_by_col_list=to_add_users,
                            chunk_size=120,
                        )
                        added_s = append_immutable_rows_bulk(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="subscriptions",
                            values_by_col_list=to_add_subs,
                            chunk_size=120,
                        )
                        # Message + reset UI (champs vidés + expander replié)
                        msg = f"Abonnés ajoutés : {added_u} utilisateur(s) créé(s), {added_s} abonnement(s) ajouté(s)."
                        if already_users:
                            uniq = sorted(set(already_users))
                            msg += f"\nDéjà existants (non recréés) : {', '.join(uniq[:12])}" + ("…" if len(uniq) > 12 else "")
                        if already_optin:
                            uniq2 = sorted(set(already_optin))
                            msg += f"\nDéjà abonnés (opt-in actif) : {', '.join(uniq2[:12])}" + ("…" if len(uniq2) > 12 else "")
                        st.session_state["adm_addsub_flash"] = msg
                        st.session_state["adm_addsub_open"] = False
                        st.session_state["adm_addsub_nonce"] = nonce + 1
                        st.rerun()
                    finally:
                        ov.empty()

    # Filtre simple (côté UI) : sous-chaîne e-mail
    q = st.text_input("Filtrer (e-mail contient)", value="", key="adm_accounts_filter").strip().lower()

    # Admin canonical : login secret (si présent)
    try:
        adm_login, _adm_pwd = _admin_login_and_password()
    except Exception:
        adm_login = ""

    def _latest_by_email(rows: list[dict]) -> list[dict]:
        by: dict[str, dict] = {}
        for r in rows:
            em = str(r.get("email") or "").strip().lower()
            if not em:
                continue
            prev = by.get(em)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[em] = r
        return sorted(by.values(), key=lambda x: str(x.get("created_at") or ""), reverse=True)

    latest = _latest_by_email(users)
    if q:
        latest = [u for u in latest if q in str(u.get("email") or "").strip().lower()]

    def _latest_sub_by_user_entity_id(sub_rows: list[dict]) -> dict[str, dict]:
        by: dict[str, dict] = {}
        for r in sub_rows:
            if str(r.get("type") or "").strip() != "weekly_friday":
                continue
            uid = str(r.get("user_entity_id") or "").strip()
            if not uid:
                continue
            prev = by.get(uid)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[uid] = r
        return by

    latest_sub = _latest_sub_by_user_entity_id(subs)

    def _kind(u: dict) -> str:
        em = str(u.get("email") or "").strip().lower()
        src = str(u.get("source") or "").strip().lower()
        has_pwd = bool(str(u.get("password_hash_b64") or "").strip())
        if adm_login and em == adm_login:
            return "ADMIN"
        if src in ("dry_run", "test_emailing", "test"):
            return "TEST (DRY-RUN)"
        if src == "newsletter":
            return "NOUS REJOINDRE"
        if has_pwd:
            return "COMPTE"
        return "AUTRE"

    buckets: dict[str, list[dict]] = {"NOUS REJOINDRE": [], "ADMIN": [], "TEST (DRY-RUN)": [], "COMPTE": [], "AUTRE": []}
    for u in latest:
        buckets[_kind(u)].append(u)

    st.markdown(
        f"""
<div style="display:flex;gap:0.75rem;flex-wrap:wrap;justify-content:center;margin:0.5rem 0 0.75rem;">
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Nous rejoindre</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['NOUS REJOINDRE'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Admin</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['ADMIN'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Test (dry-run)</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['TEST (DRY-RUN)'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Comptes</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(buckets['COMPTE'])}</div>
  </div>
  <div style="border:1px solid rgba(212,175,55,0.35);padding:0.5rem 0.75rem;background:rgba(255,255,255,0.65);">
    <div style="text-align:center;font-weight:600;color:#6b5918;">Total</div>
    <div style="text-align:center;font-size:1.25rem;color:var(--liturgie-text);">{len(latest)}</div>
  </div>
</div>
        """.strip(),
        unsafe_allow_html=True,
    )

    def _render_table(title: str, rows: list[dict]) -> None:
        body_rows = []
        for u in rows[:400]:
            em = str(u.get("email") or "").strip().lower()
            created = str(u.get("created_at") or "").strip()
            src = str(u.get("source") or "").strip()
            uid = str(u.get("entity_id") or "").strip()
            opt_txt = "—"
            if uid and title.lower().startswith("nous rejoindre"):
                rec = latest_sub.get(uid)
                # Colonne dédiée si présente, sinon fallback sur `active`
                if rec and str(rec.get("opt_in") or "").strip():
                    opt_txt = "Oui" if str(rec.get("opt_in") or "").strip().lower() in ("true", "1", "oui", "yes") else "Non"
                else:
                    opt_txt = "Oui" if _subscription_is_active(rec) else "Non"
            body_rows.append(
                "<tr>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);'>{esc(em)}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{esc(src or '—')}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{esc(opt_txt)}</td>"
                f"<td style='padding:8px 10px;border-top:1px solid rgba(0,0,0,0.06);opacity:0.9;'>{esc(created or '—')}</td>"
                "</tr>"
            )
        html = f"""
<div style="margin:0.75rem 0 0.25rem;font-weight:700;color:#6b5918;text-align:center;">{esc(title)}</div>
<div style="overflow:auto;border:1px solid rgba(212,175,55,0.35);background:rgba(255,255,255,0.72);">
<table style="width:100%;border-collapse:collapse;font-size:0.95rem;">
  <thead>
    <tr style="background:rgba(212,175,55,0.10);">
      <th style="text-align:left;padding:9px 10px;">E-mail</th>
      <th style="text-align:left;padding:9px 10px;">Source</th>
      <th style="text-align:left;padding:9px 10px;">Opt-in</th>
      <th style="text-align:left;padding:9px 10px;">Créé le</th>
    </tr>
  </thead>
  <tbody>
    {''.join(body_rows) if body_rows else '<tr><td colspan="4" style="padding:10px;opacity:0.75;">Aucun.</td></tr>'}
  </tbody>
</table>
</div>
        """.strip()
        st.markdown(html, unsafe_allow_html=True)

    _render_table("Nous rejoindre", buckets["NOUS REJOINDRE"])
    _render_table("Admin", buckets["ADMIN"])
    _render_table("Test (dry-run)", buckets["TEST (DRY-RUN)"])
    _render_table("Comptes (mot de passe)", buckets["COMPTE"])
    if buckets["AUTRE"]:
        _render_table("Autres", buckets["AUTRE"])


def render_admin_emailing() -> None:
    st.title("Emailing — templates & automatisation")
    st.caption("Édite le contenu de l’e-mail hebdomadaire (vendredi soir).")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    from core.sheets_db import TableSpec, ensure_table

    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="email_templates",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "template_key",
                    "channel",
                    "language",
                    "subject",
                    "body",
                    "active",  # colonne facultative sur la feuille ; pas utilisée pour filtrer le template (seul `status` compte).
                    "status_note",
                ]
            ),
        ),
    )

    template_key = "weekly_friday_lumenvia"
    st.caption(
        "**Templates e-mail :** seule la colonne **`status`** (**Actif** / **Inactif**) détermine quelle ligne est la version "
        "courante (aperçu, enregistrement, envoi manuel, **et** choix du modèle côté campagne / scheduler). "
        "La colonne **`active`** sur cette table n’est **pas** utilisée par l’app pour ce choix (elle peut rester pour du "
        "pilotage manuel ou un usage futur lié au planning, mais si **`status`** est **Inactif**, la ligne est ignorée "
        "**sans** lire **`active`**)."
    )
    with st.expander("Paramètres du template (clé/canal/langue)", expanded=False):
        st.caption(f"Template : `{template_key}` (canal: email, langue: fr)")

    from core.emailing import (
        EmailTemplate,
        render_template,
        supported_tags,
        french_day_month_year,
        pick_latest_live_email_template,
        email_template_row_is_live,
    )

    try:
        rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
    except Exception:
        rows = []

    lang_fr = ("fr", "fr-fr", "france", "")
    current = pick_latest_live_email_template(rows, template_key=template_key, channel="email", language_in=lang_fr) or {}

    default_subject = "🕯️ Votre halte LumenVia : Préparez la célébration du dimanche {{date_dimanche}}"
    default_body = ""
    try:
        p = Path("data/emailing_template_raw.txt")
        if p.is_file():
            default_body = p.read_text(encoding="utf-8").strip()
    except Exception:
        default_body = ""

    with st.expander("Paramètres du template (clé/canal/langue)", expanded=False):
        subject = st.text_input(
            "Objet",
            value=str(current.get("subject") or default_subject).strip(),
            key="adm_email_tpl_subject",
        )
        body = st.text_area(
            "Corps (texte, avec balises {{...}})",
            value=str(current.get("body") or default_body).strip(),
            height=320,
            key="adm_email_tpl_body",
        )
        note = st.text_input("Note (optionnel)", value="", key="adm_email_tpl_note")

    with st.expander("Balises supportées", expanded=False):
        st.code("\n".join([f"{{{{{t}}}}}" for t in supported_tags()]), language="text")

    st.divider()
    with st.expander("Dimanche de référence (aperçu + envoi manuel)", expanded=False):
        # Dimanche cible (par défaut : prochain dimanche)
        try:
            today = date.today()
            next_sun = today + timedelta(days=(6 - today.weekday()) % 7)
        except Exception:
            next_sun = date.today()
        d_pick = st.date_input("Dimanche ciblé", value=next_sun, key="adm_email_sunday_pick")
        date_str = d_pick.isoformat()[:10]

        # Identité AELF
        try:
            ident0, _texts0 = cached_aelf(date_str, zone="france", _identity_schema=4)
        except Exception:
            ident0 = None

        # Liens signés (si objets présents)
        origin = _lumenvia_app_origin_url() or ""
        url_app = (origin.rstrip("/") + "/?sunday=" + date_str) if origin else ""
        url_pdf = ""
        url_audio = ""
        url_illu = ""
        try:
            gcs = build_gcs_client(cfg.gcp_service_account)
            # PDF
            p_pdf = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
            s_pdf = _gcs_signed_url(gcs=gcs, bucket_name=str(cfg.gcs_bucket_name).strip(), path=p_pdf) if cfg.gcs_bucket_name else None
            url_pdf = s_pdf or ""
            # Illustration (multi-extensions, comme ailleurs)
            year = date_str[:4]
            if cfg.gcs_bucket_name:
                cand = [f"Images/illustrations/{year}/{date_str}{ext}" for ext in (".webp", ".png", ".jpg", ".jpeg")]
                url_illu = (
                    _gcs_first_signed_url(
                        gcs=gcs,
                        bucket_name=str(cfg.gcs_bucket_name).strip(),
                        candidate_paths=cand,
                    )
                    or ""
                )
        except Exception:
            pass

        # Audio : on cherche la dernière génération puis son audio associé
        try:
            gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=6000)
            gens_d = [g for g in gens if str(g.get("date") or "").strip()[:10] == date_str and str(g.get("zone") or "").strip() == "france"]
            gens_d.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            gen_id = str((gens_d[0] or {}).get("entity_id") or "").strip() if gens_d else ""
            aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="audio", limit=6000)
            aud_d = [a for a in aud_rows if str(a.get("gen_entity_id") or "").strip() == gen_id]
            aud_d.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            p_audio = str((aud_d[0] or {}).get("gcs_path") or "").strip() if aud_d else ""
            if p_audio and cfg.gcs_bucket_name:
                try:
                    gcs = build_gcs_client(cfg.gcp_service_account)
                    url_audio = _gcs_signed_url(gcs=gcs, bucket_name=str(cfg.gcs_bucket_name).strip(), path=p_audio) or ""
                except Exception:
                    url_audio = ""
        except Exception:
            pass

        # Valeurs exemple (issues d'un dimanche réel)
        values = {
            "prenom": "Jean",
            "nom": "Dupont",
            "origin": origin,
            "date_dimanche": french_day_month_year(d_pick),
            "nom_du_dimanche": _liturgy_display_label(
                (getattr(ident0, "fete", None) or "").strip()
                or (_jour_liturgique(ident0) if ident0 else "")  # type: ignore[arg-type]
                or ""
            )
            or "—",
            "url_pdf": url_pdf,
            "url_audio": url_audio,
            "url_illustration": url_illu,
            "url_app": url_app,
            "optout_url": (origin.rstrip("/") + "/?route=join") if origin else "",
        }
        rendered = render_template(EmailTemplate(subject=subject, body=body), values=values)
        st.markdown(f"**Objet :** {rendered.subject}")
        st.code((rendered.body or "")[:4000] or "—")

    st.caption(
        "Astuce : pour rendre les CTA cliquables, mets directement des URLs dans le corps, par ex. "
        "`{{url_pdf}}`, `{{url_audio}}`, `{{url_app}}`."
    )

    st.divider()
    if st.button("Enregistrer le template", type="primary", disabled=not (subject.strip() and body.strip())):
        ov = loading_overlay("Enregistrement du template emailing…")
        try:
            # Inactivation (historique) de la version précédente active (si elle existe)
            try:
                rows2 = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
            except Exception:
                rows2 = []
            prev = pick_latest_live_email_template(rows2, template_key=template_key, channel="email", language_in=lang_fr)
            body_n = body.strip()
            subj_n = subject.strip()

            # Immuabilité : si le contenu n’a pas bougé, on n’écrit pas une nouvelle version.
            unchanged = prev and str(prev.get("subject") or "").strip() == subj_n and str(prev.get("body") or "").strip() == body_n
            if unchanged:
                st.info("Aucune modification détectée (objet + corps inchangés) — pas de nouvelle ligne.")
            else:
                # 1) Mettre les lignes actuellement **Actives** (même clé / canal / langue) en **Inactif** dans la feuille
                # (comme MARPA pour Paramètres_IA : append seul laisse l’historique encore « Actif »).
                from core.sheets_db import _resolve_table_name

                sh_etpl = gs.open_by_key(cfg.gsheet_id)
                ws_etpl = sh_etpl.worksheet(_resolve_table_name(sh=sh_etpl, table="email_templates"))
                header_etpl = ws_etpl.row_values(1)
                if not header_etpl:
                    raise RuntimeError("Onglet templates e-mail sans en-tête — relance init_sheets_db ou vérifie l’alias ETPL.")

                try:
                    rec_etpl = ws_etpl.get_all_records(numericise_ignore=["all"])
                except Exception:
                    rec_etpl = []

                def _tpl_row_lang_ok(lang_raw: object) -> bool:
                    lg = str(lang_raw or "").strip().lower()
                    return lg in lang_fr

                col_status_etpl = header_etpl.index("status") + 1 if "status" in header_etpl else 0
                col_concat_etpl = header_etpl.index("concat") + 1 if "concat" in header_etpl else 0
                if not col_status_etpl:
                    raise RuntimeError("Colonne `status` absente sur l’onglet templates e-mail.")

                for ix, rr in enumerate(rec_etpl):
                    if str(rr.get("template_key") or "").strip() != template_key:
                        continue
                    if str(rr.get("channel") or "").strip().lower() != "email":
                        continue
                    if not _tpl_row_lang_ok(rr.get("language")):
                        continue
                    if not email_template_row_is_live(rr):
                        continue

                    merged = dict(rr)
                    merged["status"] = SHEETS_ROW_STATUS_INACTIVE
                    row_num = ix + 2
                    ws_etpl.update_cell(row_num, col_status_etpl, SHEETS_ROW_STATUS_INACTIVE)
                    if col_concat_etpl:
                        ws_etpl.update_cell(row_num, col_concat_etpl, compute_concat(merged, header=header_etpl))

                # 2) Version suivante à partir de l’historique (toutes lignes série, pas seulement actives)
                max_tpl_ver = 0
                for r0 in rows2:
                    if str(r0.get("template_key") or "").strip() != template_key:
                        continue
                    if str(r0.get("channel") or "").strip().lower() != "email":
                        continue
                    if not _tpl_row_lang_ok(r0.get("language")):
                        continue
                    vtxt = str(r0.get("version") or "").strip()
                    if vtxt.isdigit():
                        max_tpl_ver = max(max_tpl_ver, int(vtxt))
                next_tpl_ver = int(max_tpl_ver + 1)

                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="email_templates",
                    values_by_col={
                        "entity_id": sha256(f"tpl|{template_key}|{next_tpl_ver}|{subj_n}|{body_n}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                        "template_key": template_key,
                        "channel": "email",
                        "language": "fr",
                        "subject": subj_n,
                        "body": body_n,
                        "version": next_tpl_ver,
                        "status": SHEETS_ROW_STATUS_ACTIVE,
                        "status_note": note.strip(),
                    },
                    version=next_tpl_ver,
                )
                st.success("Template enregistré.")
                st.rerun()
        finally:
            ov.empty()

    st.divider()
    st.subheader("Déclencher un envoi (manuel)")
    st.caption(
        "Par défaut, c’est un **dry-run** : envoi uniquement vers les coordonnées de test (secrets). "
        "Coche l’option pour envoyer à tous les inscrits opt-in."
    )

    # UI simplifiée : 3 cases uniquement
    send_email = st.checkbox("Envoyer e-mail", value=True, key="adm_email_send_email")
    send_sms = st.checkbox("Envoyer SMS", value=False, key="adm_email_send_sms")
    send_to_all = st.checkbox(
        "Envoyer à tous les inscrits opt-in (désactivé par défaut)",
        value=False,
        key="adm_email_send_to_all",
    )

    # Comportement implicite
    send_email_as_html = True
    use_lv_html_template = True
    send_email_html_only = False
    sms_short_mode = True
    def _is_email_ok(email: str) -> bool:
        email_lc = (email or "").strip().lower()
        return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_lc)) if email_lc else False

    # Dry-run : prioritaire depuis la table users (source = dry_run/test_emailing)
    # Un peu plus large que 6000 pour fiabiliser la recherche "par e-mail" en mode test.
    users_rows_for_dry = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=9000)
    dry_candidates = [
        u
        for u in users_rows_for_dry
        if str(u.get("source") or "").strip().lower() in ("dry_run", "test_emailing", "test")
        and _is_email_ok(str(u.get("email") or "").strip())
    ]
    dry_candidates.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    dry_user = dry_candidates[0] if dry_candidates else {}
    dry_email_in = str(dry_user.get("email") or "").strip()
    dry_phone_in = str(dry_user.get("phone_e164") or "").strip()
    # Fallback : secrets (option B à plat ou section [dry_run])
    dry_email_secret = ""
    dry_phone_secret = ""
    try:
        s = st.secrets
        dry_email_secret = str(s.get("EMAIL_DRY_RUN_TO") or "").strip()
        dry_phone_secret = str(s.get("SMS_DRY_RUN_TO") or "").strip()
        if not dry_email_secret and isinstance(s.get("dry_run"), dict):
            dry_email_secret = str((s.get("dry_run") or {}).get("EMAIL_DRY_RUN_TO") or "").strip()
        if not dry_phone_secret and isinstance(s.get("dry_run"), dict):
            dry_phone_secret = str((s.get("dry_run") or {}).get("SMS_DRY_RUN_TO") or "").strip()
    except Exception:
        pass

    if not dry_email_in and _is_email_ok(dry_email_secret):
        dry_email_in = dry_email_secret
    if not dry_phone_in and dry_phone_secret:
        dry_phone_in = dry_phone_secret

    # Destinataires de test (manuel unitaire) : sélection explicite possible
    test_opt_hotmail = st.checkbox(
        "Envoyer aussi à `jop28@hotmail.com` (test)",
        value=False,
        key="adm_email_test_hotmail",
        disabled=bool(send_to_all),
    )
    test_opt_gemini = st.checkbox(
        "Envoyer aussi à `jop28gemini@gmail.com` (test)",
        value=True,
        key="adm_email_test_gemini",
        disabled=bool(send_to_all),
    )

    def _latest_user_by_email(email_lc: str) -> dict:
        em0 = str(email_lc or "").strip().lower()
        if not em0:
            return {}
        best: dict = {}
        best_ts = ""
        for u in users_rows_for_dry:
            if str(u.get("email") or "").strip().lower() != em0:
                continue
            # En e-mailing, on veut la fiche "live" (immuabilité : une seule version Actif).
            if not sheet_row_status_is_live(u.get("status")):
                continue
            ts = str(u.get("created_at") or "")
            if not best or ts > best_ts:
                best = u
                best_ts = ts
        return best

    selected_test_emails: list[str] = []
    if test_opt_hotmail:
        selected_test_emails.append("jop28@hotmail.com")
    if test_opt_gemini:
        selected_test_emails.append("jop28gemini@gmail.com")
    # Si rien n'est coché, on retombe sur le destinataire dry-run automatique (users/secrets)
    if not selected_test_emails and _is_email_ok(dry_email_in):
        selected_test_emails = [dry_email_in.strip().lower()]

    if not selected_test_emails and not dry_phone_in:
        st.warning(
            "Aucun destinataire de test trouvé. "
            "Coche au moins un destinataire ci-dessus, "
            "ou ajoute un e‑mail de test dans `users` (source=test), "
            "ou configure une adresse de test dans les secrets."
        )

    st.markdown("**Destinataires de test (aperçu)**")
    preview_lines: list[str] = []
    for em in selected_test_emails:
        u0 = _latest_user_by_email(em)
        fn0 = str(u0.get("first_name") or "Test").strip() or "Test"
        ln0 = str(u0.get("last_name") or "JOPAI").strip() or "JOPAI"
        src0 = str(u0.get("source") or "").strip() or "—"
        preview_lines.append(f"{em}\t{fn0}\t{ln0}\t{src0}")
    if dry_phone_in and not selected_test_emails:
        preview_lines.append(f"phone_e164:\t{dry_phone_in}")
    st.code(("\n".join(preview_lines) if preview_lines else "—")[:9000])
    debug_verbose = False

    # Garde-fous supplémentaires si envoi "tous opt-in"
    excluded_emails: set[str] = set()
    limit_to_n = 0
    confirm_all_ok = True
    confirm_phrase_ok = True
    if send_to_all:
        st.warning(
            "Tu es sur le point de cibler **tous les inscrits opt-in**. "
            "Prévisualise la liste, ajuste si besoin, puis confirme avant de pouvoir envoyer.",
            icon="⚠️",
        )
        with st.expander("Aperçu des destinataires (avant envoi)", expanded=True):
            users_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            subs_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=8000)
            rec_preview = lumenvia_manual_broadcast_users(
                users_rows=users_preview,
                subs_rows=subs_preview,
                send_to_all=True,
            )
            em_list = sorted(
                {
                    str(u.get("email") or "").strip().lower()
                    for u in rec_preview
                    if str(u.get("email") or "").strip()
                }
            )
            st.caption(f"Destinataires détectés : **{len(rec_preview)}** (e-mails valides : **{len(em_list)}**)")

            # Exclusions (optionnel)
            excl_pick = st.multiselect(
                "Exclure des e-mails (optionnel)",
                options=em_list,
                default=[],
                key="adm_email_excl_pick",
            )
            excl_paste = st.text_area(
                "Ou coller une liste d’e-mails à exclure (un par ligne, optionnel)",
                value="",
                height=80,
                key="adm_email_excl_paste",
            )
            excluded_emails = {str(e or "").strip().lower() for e in (excl_pick or []) if str(e or "").strip()}
            for ln in (excl_paste or "").splitlines():
                s0 = str(ln or "").strip().lower()
                if s0 and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s0):
                    excluded_emails.add(s0)

            filtered_preview = (
                [u for u in rec_preview if str(u.get("email") or "").strip().lower() not in excluded_emails]
                if excluded_emails
                else list(rec_preview)
            )

            limit_to_n = int(
                st.number_input(
                    "Limiter l’envoi aux N premiers (optionnel, utile pour un test)",
                    min_value=0,
                    max_value=max(0, len(filtered_preview)),
                    value=0,
                    step=1,
                    key="adm_email_limit_to_n",
                )
            )

            # Mini table (limite) : qui va recevoir
            show_n = st.slider(
                "Afficher les N premiers destinataires (aperçu)",
                min_value=10,
                max_value=300,
                value=60,
                step=10,
                key="adm_email_preview_n",
            )
            lines = []
            for u in filtered_preview[: int(show_n)]:
                em0 = str(u.get("email") or "").strip().lower()
                fn0 = str(u.get("first_name") or "").strip()
                ln0 = str(u.get("last_name") or "").strip()
                lines.append(f"{em0}\t{fn0}\t{ln0}")
            st.code(("\n".join(lines) if lines else "—")[:9000])

            final_list = filtered_preview[: int(limit_to_n)] if limit_to_n > 0 else filtered_preview
            st.markdown(f"**Après exclusions/limite :** {len(final_list)} destinataire(s).")

            confirm_all_ok = st.checkbox(
                "J’ai vérifié la liste et je confirme vouloir envoyer à ces destinataires.",
                value=False,
                key="adm_email_confirm_checked",
            )
            phrase = st.text_input(
                "Pour activer l’envoi, tape exactement : ENVOYER",
                value="",
                key="adm_email_confirm_phrase",
            ).strip()
            confirm_phrase_ok = phrase == "ENVOYER"

    can_execute = (confirm_all_ok and confirm_phrase_ok) if send_to_all else True
    can_execute = can_execute and (bool(send_to_all) or bool(selected_test_emails) or bool(dry_phone_in))

    if st.button(
        "Lancer l’envoi",
        type="primary",
        key="adm_email_send_run",
        disabled=(not (send_email or send_sms)) or (not can_execute),
    ):
        ov = loading_overlay("Préparation de l’envoi…")
        try:
            import traceback

            try:
                _tpl_mail_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
            except Exception:
                _tpl_mail_rows = []
            _tpl_live_mail = pick_latest_live_email_template(
                _tpl_mail_rows, template_key=template_key, channel="email", language_in=("fr", "fr-fr", "france", "")
            )
            if not _tpl_live_mail:
                st.warning(
                    "Aucune ligne avec **`status` = Actif** pour ce template (clé `weekly_friday_lumenvia`, e-mail, FR). "
                    "L’envoi utilise le **texte du formulaire** ci-dessus en secours — corrige **`status`** sur l’onglet templates."
                )
            subject_rt = (str(_tpl_live_mail.get("subject") or "").strip() if _tpl_live_mail else "") or subject
            body_rt = (str(_tpl_live_mail.get("body") or "").strip() if _tpl_live_mail else "") or body

            # recipients
            users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=6000)
            subs_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=6000)

            def _latest_sub_by_uid() -> dict[str, dict]:
                by: dict[str, dict] = {}
                for r in subs_rows:
                    if str(r.get("type") or "").strip() != "weekly_friday":
                        continue
                    uid0 = str(r.get("user_entity_id") or "").strip()
                    if not uid0:
                        continue
                    prev = by.get(uid0)
                    if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                        by[uid0] = r
                return by

            latest_sub = _latest_sub_by_uid()

            by_uid_user: dict[str, dict] = {}
            for u in users_rows:
                uid0 = str(u.get("entity_id") or "").strip()
                if not uid0:
                    continue
                prev = by_uid_user.get(uid0)
                if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
                    by_uid_user[uid0] = u

            recipients: list[tuple[str, dict]] = []
            if send_to_all:
                for uid0, subr in latest_sub.items():
                    if not sheet_row_status_is_live(subr.get("status")):
                        continue
                    if str(subr.get("opt_in") or "").strip().lower() not in ("true", "1", "oui", "yes"):
                        continue
                    if str(subr.get("active") or "").strip().lower() not in ("true", "1", "oui", "yes", "active"):
                        continue
                    recipients.append((uid0, by_uid_user.get(uid0) or {}))
                # Applique exclusions / limite (si demandées dans l’aperçu)
                if excluded_emails:
                    recipients = [
                        (uid0, u)
                        for (uid0, u) in recipients
                        if str((u or {}).get("email") or "").strip().lower() not in excluded_emails
                    ]
                if limit_to_n > 0:
                    recipients = recipients[: int(limit_to_n)]
            else:
                # Destinataires de test sélectionnés (1 ou 2) + fallback dry-run
                recipients = []
                for em in selected_test_emails:
                    u0 = _latest_user_by_email(em)
                    recipients.append(
                        (
                            "dry_run",
                            {
                                "email": em.strip(),
                                "phone_e164": str(u0.get("phone_e164") or "").strip(),
                                "first_name": str(u0.get("first_name") or "Test").strip() or "Test",
                                "last_name": str(u0.get("last_name") or "JOPAI").strip() or "JOPAI",
                            },
                        )
                    )
                if not recipients:
                    recipients = [
                        (
                            "dry_run",
                            {
                                "email": dry_email_in.strip(),
                                "phone_e164": dry_phone_in.strip(),
                                "first_name": "Test",
                                "last_name": "JOPAI",
                            },
                        )
                    ]

            from core.outbound import SmtpConfig, TwilioConfig, send_smtp_email, send_twilio_sms
            from core.sheets_db import TableSpec, ensure_table

            def _secret_get(*keys: str) -> str:
                """
                Lit une valeur depuis st.secrets en supportant:
                - clés racine (ex: SMTP_HOST)
                - sous-sections (ex: [smtp] SMTP_HOST=...)
                """
                try:
                    s = st.secrets
                except Exception:
                    return ""
                # 1) racine
                for k in keys:
                    try:
                        v = s.get(k)  # type: ignore[attr-defined]
                    except Exception:
                        v = None
                    if v is not None and str(v).strip():
                        return str(v).strip()
                # 2) sous-sections connues
                sections = ("smtp", "twilio", "dry_run")
                for sec in sections:
                    try:
                        block = s.get(sec)  # type: ignore[attr-defined]
                    except Exception:
                        block = None
                    if not isinstance(block, dict):
                        continue
                    for k in keys:
                        v = block.get(k)
                        if v is not None and str(v).strip():
                            return str(v).strip()
                return ""

            # Outbound log
            ensure_table(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table=TableSpec(
                    name="outbound_messages",
                    columns=with_concat(
                        [
                            *BASE_COLUMNS,
                            "channel",
                            "template_key",
                            "user_entity_id",
                            "email",
                            "phone_e164",
                            "date_dimanche",
                            "status_detail",
                            "scheduled_at",
                            "sent_at",
                            "error",
                        ]
                    ),
                ),
            )

            # SMTP config
            try:
                smtp_cfg = SmtpConfig(
                    host=_secret_get("SMTP_HOST"),
                    port=int(_secret_get("SMTP_PORT") or 587),
                    username=_secret_get("SMTP_USER"),
                    password=_secret_get("SMTP_PASSWORD"),
                    from_email=_secret_get("SMTP_FROM"),
                    use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower()
                    not in ("0", "false", "no", "off"),
                )
            except Exception:
                smtp_cfg = SmtpConfig(host="", port=587, username="", password="", from_email="")

            # Twilio config
            try:
                tw_cfg = TwilioConfig(
                    account_sid=_secret_get("TWILIO_ACCOUNT_SID"),
                    auth_token=_secret_get("TWILIO_AUTH_TOKEN"),
                    from_phone_e164=_secret_get("TWILIO_FROM", "TWILIO_FROM_NUMBER"),
                )
            except Exception:
                tw_cfg = TwilioConfig(account_sid="", auth_token="", from_phone_e164="")

            ok = 0
            err = 0
            debug_rows: list[dict[str, str]] = []

            def _linkify_html(text: str) -> str:
                def repl(m: re.Match) -> str:
                    u = m.group(0)
                    return f'<a href="{u}" target="_blank" rel="noopener noreferrer">{u}</a>'

                return re.sub(r"(https?://[^\s<]+)", repl, text or "")

            def _body_to_html(body0: str) -> str:
                b = (body0 or "").strip()
                if re.search(r"(?is)<\s*(html|body|div|p|table|br|a)\b", b):
                    return b
                b = _linkify_html(b)
                paras = [p.strip() for p in b.split("\n\n") if p.strip()]
                out: list[str] = []
                for p in paras:
                    out.append("<p>" + p.replace("\n", "<br>\n") + "</p>")
                inner = ("\n".join(out) if out else "<p></p>")
                dn_ml = html_escape(LUMENVIA_DEVELOPMENT_NOTICE)
                return (
                    "<!doctype html>\n"
                    "<html><head><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                    "<style>"
                    "body{font-family:Arial,Helvetica,sans-serif;line-height:1.45;color:#0b2745;}"
                    "p{margin:0 0 12px 0;}"
                    "a{color:#0d9488;text-decoration:underline;}"
                    "img{max-width:100%;height:auto;display:block;margin:10px 0;}"
                    "</style>"
                    "</head><body>\n"
                    f"{inner}\n"
                    f'<p style="color:#7F8C8D;font-size:10px;line-height:1.4;margin-top:18px;"><em>{dn_ml}</em></p>\n'
                    "</body></html>"
                )

            def _lv_email_html(*, subject0: str, values0: dict[str, str], intro_text: str) -> str:
                prenom = (values0.get("prenom") or "").strip() or "—"
                nom = (values0.get("nom") or "").strip() or ""
                origin0 = (values0.get("origin") or "").strip()
                date_dim = (values0.get("date_dimanche") or "").strip()
                nom_dim = (values0.get("nom_du_dimanche") or "").strip()
                url_pdf0 = (values0.get("url_pdf") or "").strip()
                url_audio0 = (values0.get("url_audio") or "").strip()
                url_app0 = (values0.get("url_app") or "").strip()
                url_illu0 = (values0.get("url_illustration") or "").strip()
                optout0 = (values0.get("optout_url") or "").strip()
                email0 = (values0.get("email") or "").strip().lower()

                pref_url = ""
                if origin0 and email0:
                    try:
                        from urllib.parse import quote_plus as _q
                    except Exception:  # pragma: no cover
                        _q = None  # type: ignore[assignment]
                    enc = _q(email0) if _q else email0
                    pref_url = origin0.rstrip("/") + "/?route=join&email=" + enc

                def btn(label: str, url: str) -> str:
                    if not url:
                        return ""
                    return (
                        f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
                        'style="display:inline-block;background:#0d9488;color:white;'
                        'padding:10px 14px;border-radius:10px;text-decoration:none;'
                        'font-weight:700;margin:6px 0;">'
                        f"{label}</a>"
                    )

                # Le template newsletter est rédigé "Bonjour {{prenom}}," : on force donc le prénom seul.
                who = (prenom or "—").strip()
                # On retire les lignes techniques / URLs signées du corps (l'illustration est rendue dans une card).
                raw_lines = [ln.strip() for ln in (intro_text or "").replace("\r\n", "\n").split("\n")]
                raw_lines = [ln for ln in raw_lines if ln]
                filtered: list[str] = []
                for ln in raw_lines:
                    if re.match(r"(?i)^bonjour\b", ln):
                        continue
                    if re.match(r"(?i)^illustration\s*:\s*https?://", ln):
                        continue
                    # Évite d'afficher des URLs signées interminables en clair
                    if "X-Goog-Algorithm=" in ln or "X-Goog-Credential=" in ln or "X-Goog-Signature=" in ln:
                        continue
                    if re.search(r"(?is)\{\{\s*affichage.*illustration", ln):
                        continue
                    filtered.append(ln)
                if not filtered:
                    filtered = ["La fin de semaine approche : voici votre préparation dominicale."]

                def _is_list_unsubscribe_line(ln: str) -> bool:
                    s = (ln or "").strip()
                    if not s:
                        return False
                    if re.search(r"(?i)vous recevez cet e-mail", s):
                        return True
                    if re.search(r"(?i)préférences ou vous désabonner", s):
                        return True
                    if re.search(r"(?i)membre de la communauté\s+LumenVia", s) and re.search(
                        r"(?i)cliquez\s+ici", s
                    ):
                        return True
                    return False

                def _is_feedback_survey_bullet(ln: str) -> bool:
                    """True uniquement pour la ligne CTA questionnaire (pas « L'Expérience Sonore »)."""
                    if "👉" not in ln:
                        return False
                    if re.match(r"(?i)^l['’]exp[eé]rience\s+sonore\b", ln.strip()):
                        return False
                    return bool(
                        re.search(r"(?i)donner\s+(mon\s+)?avis|avis\s+sur\s+cette\s+expérience", ln)
                        or re.search(r"(?i)questionnaire", ln)
                    )

                legal_notice_line = ""

                # Ordre du template : paragraphes et puces entrelacés (évite tout le prose puis toute la liste).
                segments: list[tuple[str, str]] = []
                for ln in filtered:
                    if _is_list_unsubscribe_line(ln):
                        legal_notice_line = ln.strip()
                        continue
                    raw = ln.strip()
                    lead = raw.lstrip("-•").lstrip()
                    if re.match(
                        r"(?i)^(la synth[eè]se|l['’]exp[eé]rience\s+sonore|l['’]illustration)\b",
                        lead,
                    ) or raw.startswith(("-", "•")):
                        segments.append(("li", lead))
                        continue
                    if _is_feedback_survey_bullet(raw):
                        # Pas en <ul>/<li> : évite une puce • avant le CTA questionnaire (absente du template).
                        segments.append(("cta", lead))
                        continue
                    segments.append(("p", raw))

                _wrap_lo: int | None = None
                _wrap_hi: int | None = None
                for _wi, (_wk, _wch) in enumerate(segments):
                    if _wk == "p" and re.match(
                        r"(?i)^beau\s+chemin\s+vers\s+dimanche",
                        (_wch or "").strip(),
                    ):
                        _wrap_lo = _wi
                        break
                if _wrap_lo is not None:
                    _wrap_hi = _wrap_lo
                    _wj = _wrap_lo + 1
                    while _wj < len(segments):
                        _nk, _ = segments[_wj]
                        if _nk in ("p", "cta"):
                            _wrap_hi = _wj
                            _wj += 1
                            continue
                        break

                def _esc(s: str) -> str:
                    return (
                        (s or "")
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace('"', "&quot;")
                    )

                def _bullet_html(b: str) -> str:
                    bb0 = (b or "").strip()
                    if "👉" in bb0:
                        left, right = bb0.split("👉", 1)
                        left = left.strip()
                        right = right.strip()
                        href = ""
                        if url_pdf0 and re.search(r"(?i)synth[èe]se.*pdf|pdf", bb0):
                            href = url_pdf0
                        elif url_audio0 and re.search(r"(?i)audio|[ée]couter", bb0):
                            href = url_audio0
                        elif url_illu0 and re.search(r"(?i)image|illustration", bb0):
                            href = url_illu0
                        fb_url = lumenvia_feedback_survey_abs_url(origin0, recipient_email=email0 or None)
                        if not href and fb_url and re.search(
                            r"(?i)donner\s+mon\s+avis|avis\s+sur\s+cette\s+expérience|donner\s+votre\s+avis",
                            right,
                        ):
                            href = fb_url
                        if href:
                            if left:
                                return (
                                    f"{_esc(left)}<br>"
                                    f"👉 <a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\"><strong>{_esc(right)}</strong></a>"
                                ).strip()
                            return (
                                f"👉 <a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\"><strong>{_esc(right)}</strong></a>"
                            ).strip()
                        if left:
                            return f"{_esc(left)}<br>👉 <strong>{_esc(right)}</strong>".strip()
                        return f"👉 <strong>{_esc(right)}</strong>".strip()
                    return _esc(bb0)

                intro_html = ""
                _ul_items: list[str] = []
                _max_intro_paras = 40
                _max_intro_li = 16
                _max_intro_cta = 6
                _p_used = 0
                _li_used = 0
                _cta_used = 0

                def _flush_ul() -> None:
                    nonlocal intro_html, _ul_items
                    if not _ul_items:
                        return
                    blk = "".join([f"<li style=\"margin:8px 0;\">{x}</li>" for x in _ul_items])
                    intro_html += f"<ul style=\"margin:10px 0 6px 18px;padding:0;\">{blk}</ul>"
                    _ul_items = []

                _wrap_div_open = (
                    '<div style="border:1px solid #e7e5e4;border-radius:14px;padding:14px 16px;'
                    'margin:14px 0;background:#fdfcfa;">'
                )

                for _seg_i, (kind, chunk) in enumerate(segments):
                    if _wrap_lo is not None and _seg_i == _wrap_lo:
                        intro_html += _wrap_div_open
                    if kind == "p":
                        _flush_ul()
                        if _p_used < _max_intro_paras:
                            pp = lumenvia_wrap_feedback_cta_with_link(
                                (chunk or "").strip(),
                                origin_for_href=origin0,
                                recipient_email=email0 or None,
                            )
                            pp = _linkify_html(pp)
                            # Met en valeur JOPAI© comme dans le footer (couleurs/typo).
                            pp = re.sub(
                                r"(?i)\bJOPAI\b",
                                '<span class="jopai-inline"><span class="jop">JOP</span><span class="ai">AI</span><sup class="ai">©</sup></span>',
                                pp,
                            )
                            for kw in ("LumenVia", "PDF", "Audio", "Illustration", "messe", "Parole"):
                                pp = re.sub(
                                    rf"(?i)\b{re.escape(kw)}\b",
                                    lambda m: f"<strong>{m.group(0)}</strong>",
                                    pp,
                                )
                            _in_fb = (
                                _wrap_lo is not None
                                and _wrap_hi is not None
                                and _wrap_lo <= _seg_i <= _wrap_hi
                            )
                            if _in_fb:
                                _psty = (
                                    "margin:0;"
                                    if _seg_i == _wrap_hi
                                    else "margin:0 0 10px 0;"
                                )
                                intro_html += f'<p style="{_psty}">{pp}</p>'
                            else:
                                intro_html += f"<p>{pp}</p>"
                            _p_used += 1
                    elif kind == "cta":
                        _flush_ul()
                        if _cta_used < _max_intro_cta:
                            _cta_margin = (
                                "10px 0 0 0"
                                if (
                                    _wrap_lo is not None
                                    and _wrap_hi is not None
                                    and _wrap_lo <= _seg_i <= _wrap_hi
                                )
                                else "8px 0 0 0"
                            )
                            intro_html += (
                                f'<p style="margin:{_cta_margin};padding:0;">{_bullet_html(chunk)}</p>'
                            )
                            _cta_used += 1
                    else:
                        if _li_used < _max_intro_li:
                            _ul_items.append(_bullet_html(chunk))
                            _li_used += 1
                    if _wrap_hi is not None and _seg_i == _wrap_hi:
                        intro_html += "</div>"

                _flush_ul()

                prefs_link = (pref_url or optout0 or "").strip()

                def _legal_subscription_notice_html(line: str, link: str) -> str:
                    s = (line or "").strip()
                    if not s:
                        return ""
                    esc = html_escape(s)
                    if link:
                        esc = re.sub(
                            r"(?i)cliquez\s+ici\b",
                            lambda m: (
                                f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
                                'style="color:#0d9488;text-decoration:underline;">'
                                f"{html_escape(m.group(0))}</a>"
                            ),
                            esc,
                        )
                    return (
                        f"<p style=\"color:#64748b;font-size:12px;line-height:1.45;margin:16px 0 0 0;\">{esc}</p>"
                    )

                # Citation mise en valeur (seulement si absente du corps — le template Sheets peut déjà la porter)
                quote_txt = (
                    "LumenVia n'est pas là pour remplacer la rencontre, mais pour la préparer, "
                    "afin que chaque messe devienne une rencontre plus consciente avec le Christ."
                )
                if not re.search(r"remplacer la rencontre", intro_html, flags=re.I):
                    intro_html += (
                        "<p style=\"margin-top:14px;padding:10px 12px;border-left:4px solid #0d9488;"
                        "background:#f0fdfa;color:#0b2745;border-radius:10px;\">"
                        f"<em>{html_escape(quote_txt)}</em></p>"
                    )

                cards = []
                if url_pdf0:
                    cards.append(
                        "<div style=\"border:1px solid #e7e5e4;border-radius:14px;padding:14px;margin:12px 0;\">"
                        "<div style=\"font-size:16px;font-weight:800;\">Synthèse illustrée (PDF)</div>"
                        "<div style=\"color:#334155;margin:6px 0;\">Retrouvez le fil rouge qui relie les textes.</div>"
                        f"{btn('Télécharger le PDF', url_pdf0)}"
                        "</div>"
                    )
                if url_audio0:
                    cards.append(
                        "<div style=\"border:1px solid #e7e5e4;border-radius:14px;padding:14px;margin:12px 0;\">"
                        "<div style=\"font-size:16px;font-weight:800;\">Expérience sonore (Audio)</div>"
                        "<div style=\"color:#334155;margin:6px 0;\">Écoutez la synthèse en chemin.</div>"
                        f"{btn('Écouter l’audio', url_audio0)}"
                        "</div>"
                    )
                if url_illu0:
                    cards.append(
                        "<div style=\"border:1px solid #e7e5e4;border-radius:14px;padding:14px;margin:12px 0;\">"
                        "<div style=\"font-size:16px;font-weight:800;\">Illustration de la semaine</div>"
                        f"<div style=\"margin:14px 0;text-align:center;\">"
                        f"<a href=\"{url_app0 or url_illu0}\" target=\"_blank\" rel=\"noopener noreferrer\">"
                        f"<img src=\"{url_illu0}\" alt=\"Illustration\" style=\"border-radius:12px;max-width:260px;width:100%;\">"
                        "</a></div>"
                        f"{btn('Voir l’illustration', url_illu0)}"
                        "</div>"
                    )

                footer_links = []
                # Liens de footer (cibles fixes)
                if origin0:
                    footer_links.append(
                        f'<a href="{origin0.rstrip("/")}/?route=about" target="_blank" rel="noopener noreferrer">Accéder à LumenVia</a>'
                    )
                footer_html = " • ".join(footer_links)

                # Évite d'afficher 2 fois la date si l'objet la contient déjà (cas par défaut :
                # "… dimanche {{date_dimanche}}" + sous-titre "… — {{date_dimanche}}").
                _sub_date = ""
                try:
                    if date_dim and date_dim not in (subject0 or ""):
                        _sub_date = date_dim
                except Exception:
                    _sub_date = date_dim if date_dim else ""
                h2 = (nom_dim + (" — " + _sub_date if _sub_date else "")).strip(" —")
                parts: list[str] = []
                parts.append("<!doctype html>")
                parts.append("<html><head><meta charset=\"utf-8\">")
                parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
                parts.append("<style>")
                parts.append("body{font-family:Montserrat,Helvetica,Arial,sans-serif;line-height:1.55;color:#2F3640;background:#ffffff;}")
                parts.append(".wrap{max-width:640px;margin:0 auto;padding:18px;}")
                parts.append(".title{font-family:'Playfair Display',Georgia,'Times New Roman',serif;font-size:20px;font-weight:900;margin:0 0 6px 0;color:#2F3640;}")
                parts.append(".sub{color:#334155;margin:0 0 14px 0;}")
                parts.append(".hr{height:1px;background:#e7e5e4;margin:14px 0;}")
                parts.append("a{color:#2F3640;}")
                # Identité JOPAI© (immuable) dans l'e-mail
                parts.append(".jopai{font-family:Montserrat,Helvetica,Arial,sans-serif;font-size:12px;letter-spacing:0.3px;}")
                parts.append(".jopai .jop{font-weight:800;color:#0d9488;}")
                parts.append(".jopai .ai{font-style:italic;color:#0b2745;}")
                parts.append(".jopai .rest{color:#0b2745;}")
                parts.append(".jopai-inline{font-family:Montserrat,Helvetica,Arial,sans-serif;letter-spacing:0.3px;white-space:nowrap;}")
                parts.append(".jopai-inline .jop{font-weight:800;color:#0d9488;}")
                parts.append(".jopai-inline .ai{font-style:italic;color:#0b2745;}")
                parts.append("</style></head><body><div class=\"wrap\">")
                parts.append(f"<p><strong>Bonjour {who},</strong></p>")
                parts.append(intro_html)
                parts.append("".join(cards))
                if footer_html:
                    parts.append("<div class=\"hr\"></div>")
                    parts.append(f"<p style=\"color:#475569;font-size:12px;\">{footer_html}</p>")
                if legal_notice_line:
                    parts.append(_legal_subscription_notice_html(legal_notice_line, prefs_link))
                parts.append("<div class=\"hr\"></div>")
                parts.append(
                    "<div class=\"jopai\">"
                    "<span class=\"jop\">JOP</span><span class=\"ai\">AI</span><sup class=\"ai\">©</sup>"
                    "<span class=\"rest\"> LumenVia - 2026 | TOUS DROITS RESERVES</span>"
                    "</div>"
                )
                dn_email = html_escape(LUMENVIA_DEVELOPMENT_NOTICE)
                parts.append(
                    f"<p style=\"color:#7F8C8D;font-size:10px;line-height:1.4;margin:14px 0 0 0;\"><em>{dn_email}</em></p>"
                )
                parts.append("</div></body></html>")
                return "".join(parts)

            def _inject_illustration_placeholder(*, text: str, url_illustration: str, as_html: bool) -> str:
                """
                Remplace le placeholder libre du template docx par un rendu concret.
                - HTML: injecte <img> + lien
                - texte: injecte l'URL si dispo
                """
                s = str(text or "")
                # Tolère plusieurs variantes observées (apostrophes différentes / espaces).
                variants = (
                    "{{affichage de l’illustration de la semaine}}",
                    "{{affichage de l'illustration de la semaine}}",
                    "{{affichage de l’illustration de la semaine }}",
                    "{{affichage de l'illustration de la semaine }}",
                )
                if not any(v in s for v in variants):
                    return s
                u = (url_illustration or "").strip()
                if not u:
                    rep = "" if as_html else ""
                else:
                    if as_html:
                        rep = (
                            f'<p><a href="{u}" target="_blank" rel="noopener noreferrer">Voir l’illustration de la semaine</a></p>'
                            f'<p><img src="{u}" alt="Illustration de la semaine"></p>'
                        )
                    else:
                        rep = f"Illustration : {u}"
                for v in variants:
                    s = s.replace(v, rep)
                return s
            for uid0, urec in recipients[:500]:
                to_email = str(urec.get("email") or "").strip()
                to_phone = str(urec.get("phone_e164") or "").strip()
                values2 = dict(values)
                values2["prenom"] = str(urec.get("first_name") or "—").strip() or "—"
                values2["nom"] = str(urec.get("last_name") or "—").strip() or "—"
                values2["email"] = to_email
                # Lien préférences: pré-remplit l'email sur "Nous rejoindre"
                try:
                    from urllib.parse import quote_plus
                except Exception:  # pragma: no cover
                    quote_plus = None  # type: ignore[assignment]
                if values2.get("origin") and to_email:
                    enc = quote_plus(to_email) if quote_plus else to_email
                    values2["optout_url"] = values2["origin"].rstrip("/") + "/?route=join&email=" + enc
                rendered2 = render_template(EmailTemplate(subject=subject_rt, body=body_rt), values=values2)
                # Placeholder "docx" (non-tag) : illustration
                rendered2 = EmailTemplate(
                    subject=rendered2.subject,
                    body=_inject_illustration_placeholder(
                        text=rendered2.body,
                        url_illustration=str(values2.get("url_illustration") or ""),
                        as_html=False,
                    ),
                )
                # Nettoyage des artefacts du template (copier/coller mail) : on ne retire « Objet : … »
                # que si cette ligne redit essentiellement l’objet réel (sinon on garde variantes / mentions de test).
                body_clean = (rendered2.body or "").replace("\r\n", "\n").strip()

                def _maybe_strip_objet_preamble(*, body: str, subject: str) -> str:
                    b = body
                    m = re.match(r"(?im)^\s*Objet\s*:\s*(.+?)\s*\n+", b)
                    if not m:
                        return b
                    obj_line = str(m.group(1) or "").strip()
                    subj = str(subject or "").strip()

                    def _squash(s: str) -> str:
                        return re.sub(r"\s+", " ", s).replace("—", "-").strip().lower()

                    if _squash(obj_line) == _squash(subj) or (
                        subj and _squash(subj) in _squash(obj_line) and len(_squash(obj_line)) - len(_squash(subj)) <= 2
                    ):
                        return b[m.end() :].lstrip()
                    return b

                body_clean = _maybe_strip_objet_preamble(body=body_clean, subject=rendered2.subject)
                body_clean = re.sub(r"(?im)^\s*Corps du message\s*:\s*\n*", "", body_clean)
                rendered2 = EmailTemplate(subject=rendered2.subject, body=body_clean.strip())

                if send_email:
                    try:
                        if not smtp_cfg.host or not smtp_cfg.from_email:
                            raise RuntimeError("SMTP non configuré (SMTP_HOST/SMTP_FROM).")
                        if not to_email:
                            raise RuntimeError("E-mail destinataire manquant.")
                        html_src = _inject_illustration_placeholder(
                            text=rendered2.body,
                            url_illustration=str(values2.get("url_illustration") or ""),
                            as_html=True,
                        )
                        if send_email_as_html and use_lv_html_template:
                            html2 = _lv_email_html(
                                subject0=rendered2.subject,
                                values0={k: str(v) for k, v in values2.items()},
                                intro_text=rendered2.body,
                            )
                        else:
                            html2 = _body_to_html(html_src) if send_email_as_html else None
                        _notice_txt_mail = rendered2.body.strip()
                        _notice_txt_mail = (_notice_txt_mail + "\n\n—\n") if _notice_txt_mail else ""
                        _notice_txt_mail += LUMENVIA_DEVELOPMENT_NOTICE
                        send_smtp_email(
                            cfg=smtp_cfg,
                            to_email=to_email,
                            subject=rendered2.subject,
                            body_text=_notice_txt_mail,
                            body_html=html2,
                            html_only=bool(send_email_html_only and send_email_as_html),
                        )
                        ok += 1
                        if debug_verbose:
                            debug_rows.append(
                                {
                                    "channel": "email",
                                    "status": "ok",
                                    "uid": str(uid0),
                                    "to": to_email,
                                    "detail": "sent",
                                }
                            )
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|email|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "email",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": to_email,
                                "phone_e164": "",
                                "date_dimanche": date_str,
                                "status_detail": "sent",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": utc_now_iso(),
                                "error": "",
                            },
                        )
                    except Exception as e:
                        err += 1
                        tb = traceback.format_exc()
                        debug_rows.append(
                            {
                                "channel": "email",
                                "status": "error",
                                "uid": str(uid0),
                                "to": (to_email or "—"),
                                "detail": f"{type(e).__name__}: {str(e)}",
                            }
                        )
                        if debug_verbose:
                            st.error(f"Erreur EMAIL → {to_email or '—'}")
                            st.code(tb[:8000] or (str(e)[:2000] or "—"))
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|email|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "email",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": to_email,
                                "phone_e164": "",
                                "date_dimanche": date_str,
                                "status_detail": "error",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": "",
                                "error": str(e)[:900],
                            },
                        )

                if send_sms:
                    try:
                        if not tw_cfg.account_sid or not tw_cfg.from_phone_e164:
                            raise RuntimeError("Twilio non configuré (TWILIO_*).")
                        if not to_phone:
                            raise RuntimeError("Téléphone destinataire manquant.")
                        if not re.match(r"^\+[1-9]\d{6,14}$", to_phone):
                            raise RuntimeError("Téléphone invalide (format E.164 attendu, ex: +33612345678).")
                        from core.outbound import fetch_twilio_message_status

                        sms_body = rendered2.body
                        if sms_short_mode:
                            # SMS ultra-minimaliste pour éviter tout filtrage opérateur (Twilio 30044).
                            # Aucun lien, pas d’emoji, pas de ponctuation exotique.
                            sms_body = "Message de JOPAI LumenVia"

                        sid = send_twilio_sms(cfg=tw_cfg, to_phone_e164=to_phone, body_text=sms_body)
                        st_tw = fetch_twilio_message_status(cfg=tw_cfg, sid=sid) if sid else {"status": "", "error_code": "", "error_message": ""}
                        if debug_verbose and st_tw.get("status") == "not_found":
                            st.warning(
                                "Twilio: SID introuvable via l'API. "
                                "Tu consultes probablement un autre projet/compte dans la console Twilio. "
                                f"Compte utilisé par l'app: …{str(tw_cfg.account_sid or '')[-6:]} "
                                f"(from: {tw_cfg.from_phone_e164 or '—'})."
                            )
                        ok += 1
                        if debug_verbose:
                            debug_rows.append(
                                {
                                    "channel": "sms",
                                    "status": "ok",
                                    "uid": str(uid0),
                                    "to": to_phone,
                                    "detail": (
                                        f"sid={sid} status={st_tw.get('status') or '—'}"
                                        + (f" err={st_tw.get('error_code')}" if st_tw.get("error_code") else "")
                                        + (f" msg={st_tw.get('error_message')}" if st_tw.get("error_message") else "")
                                    )
                                    if sid
                                    else "sent",
                                }
                            )
                            st.caption(f"SMS envoyé (contenu) : {sms_body}")
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|sms|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "sms",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": "",
                                "phone_e164": to_phone,
                                "date_dimanche": date_str,
                                "status_detail": (
                                    f"sid={sid} status={st_tw.get('status') or '—'}"
                                    + (f" err={st_tw.get('error_code')}" if st_tw.get("error_code") else "")
                                    + (f" msg={st_tw.get('error_message')}" if st_tw.get("error_message") else "")
                                )
                                if sid
                                else "sent",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": utc_now_iso(),
                                "error": "",
                            },
                        )
                    except Exception as e:
                        err += 1
                        tb = traceback.format_exc()
                        debug_rows.append(
                            {
                                "channel": "sms",
                                "status": "error",
                                "uid": str(uid0),
                                "to": (to_phone or "—"),
                                "detail": f"{type(e).__name__}: {str(e)}",
                            }
                        )
                        if debug_verbose:
                            st.error(f"Erreur SMS → {to_phone or '—'}")
                            st.code(tb[:8000] or (str(e)[:2000] or "—"))
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="outbound_messages",
                            values_by_col={
                                "entity_id": sha256(f"msg|sms|{uid0}|{date_str}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                "channel": "sms",
                                "template_key": template_key,
                                "user_entity_id": uid0,
                                "email": "",
                                "phone_e164": to_phone,
                                "date_dimanche": date_str,
                                "status_detail": "error",
                                "scheduled_at": utc_now_iso(),
                                "sent_at": "",
                                "error": str(e)[:900],
                            },
                        )

            st.success(f"Terminé : {ok} envoi(s) OK, {err} erreur(s).")
            if debug_verbose and debug_rows:
                st.markdown("**Debug (résumé)**")
                st.code(
                    ("\n".join([f"{r['channel']}\t{r['status']}\t{r['to']}\t{r['detail']}" for r in debug_rows])[:9000])
                    or "—"
                )
        finally:
            ov.empty()


def lumenvia_manual_broadcast_users(
    *,
    users_rows: list[dict],
    subs_rows: list[dict],
    send_to_all: bool,
) -> list[dict]:
    """Feuilles `users` + `subscriptions` → liste des utilisateurs potentiels pour l’expédition manuelle (dry-run inclus)."""

    def _latest_sub_by_uid() -> dict[str, dict]:
        by: dict[str, dict] = {}
        for r in subs_rows:
            if str(r.get("type") or "").strip() != "weekly_friday":
                continue
            uid0 = str(r.get("user_entity_id") or "").strip()
            if not uid0:
                continue
            prev = by.get(uid0)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                by[uid0] = r
        return by

    latest_sub = _latest_sub_by_uid()
    by_uid_user: dict[str, dict] = {}
    for u in users_rows:
        uid0 = str(u.get("entity_id") or "").strip()
        if not uid0:
            continue
        prev = by_uid_user.get(uid0)
        if not prev or str(u.get("created_at") or "") > str(prev.get("created_at") or ""):
            by_uid_user[uid0] = u

    if send_to_all:
        ordered: list[dict] = []
        for uid0, subr in latest_sub.items():
            if not sheet_row_status_is_live(subr.get("status")):
                continue
            if str(subr.get("opt_in") or "").strip().lower() not in ("true", "1", "oui", "yes"):
                continue
            if str(subr.get("active") or "").strip().lower() not in ("true", "1", "oui", "yes", "active"):
                continue
            urec = by_uid_user.get(uid0) or {}
            if urec:
                ordered.append(urec)
        return ordered

    dry_users = [
        u for u in users_rows if str(u.get("source") or "").strip().lower() == "dry_run" and str(u.get("email") or "").strip()
    ]
    dry_users.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    u0 = dry_users[0] if dry_users else {}
    return [u0] if u0 else []


def render_admin_scheduler() -> None:
    st.title("Scheduler")
    st.markdown(
        """
<div style="text-align:center;margin-top:0.85rem;margin-bottom:12px;padding-top:4px;color:#0b2745;opacity:0.92;line-height:1.45;">
<em>Planifie et déclenche des envois (structure générique pour hebdo/quotidien).</em>
</div>
        """.strip(),
        unsafe_allow_html=True,
    )
    st.caption(
        "Tant que le **déclenchement automatique dans le cloud** n’est pas câblé, rien ne part au créneau tout seul. "
        "Pour **tester** : tuiles **Emailing** (envoi manuel, jeu d’options plus simple) ou **Exécuter maintenant** dans "
        "**Déclencher une campagne (manuel)** ci-dessous."
    )
    with st.expander("Précision sur les statuts des campagnes", expanded=False):
        st.markdown(
            """
**`enabled`** — la campagne est en service ou non pour la prévision et les interrupteurs dans la liste.  
**`status`** — statut **de cette ligne** dans Sheets (historique immutable) ; **l’app affiche toujours la ligne la plus récente**
par identifiant de campagne.
            """.strip()
        )

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id:
        st.warning("Configuration Google Sheets manquante.")
        return

    from core.sheets_db import TableSpec, ensure_table, with_concat, BASE_COLUMNS

    gs = build_gspread_client(cfg.gcp_service_account)

    # Assure les tables
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="AliasTables",
            columns=["#ID", "Statut", "Version", "Nom Complet Table", "Acronyme Table", "Description"],
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="CMPG",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "name",
                    "enabled",
                    "timezone",
                    "schedule_kind",
                    "schedule_spec",
                    "audience_kind",
                    "audience_spec",
                    "send_email",
                    "send_sms",
                    "email_template_key",
                    "sms_template_key",
                    "content_pdf",
                    "content_audio",
                    "content_illustration",
                    "content_app_link",
                ]
            ),
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="RUNS",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "campaign_key",
                    "run_kind",
                    "status_detail",
                    "started_at",
                    "finished_at",
                    "recipients_ok",
                    "recipients_err",
                    "error",
                ]
            ),
        ),
    )
    ensure_table(
        gspread_client=gs,
        spreadsheet_id=cfg.gsheet_id,
        table=TableSpec(
            name="AUDC",
            columns=with_concat(
                [
                    *BASE_COLUMNS,
                    "audience_key",
                    "libelle",
                    "description",
                    "spec_aide",
                ]
            ),
        ),
    )

    from core.emailing import pick_latest_live_email_template

    # Seed audiences si table vide
    try:
        aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="AUDC", limit=2000)
    except Exception:
        aud_rows = []
    if not aud_rows:
        seed = [
            {
                "entity_id": sha256(f"audc|dry_run|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "dry_run",
                "libelle": "Test (dry-run)",
                "description": "Envoi uniquement au compte de test (source=dry_run), ou au destinataire de test des secrets si disponible.",
                "spec_aide": "",
            },
            {
                "entity_id": sha256(f"audc|weekly_friday_optin|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "weekly_friday_optin",
                "libelle": "Tous les inscrits opt-in",
                "description": "Envoi à tous les inscrits ayant opt-in=true et active=true (lettre du vendredi).",
                "spec_aide": "",
            },
            {
                "entity_id": sha256(f"audc|by_country|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_country",
                "libelle": "Filtrer par pays",
                "description": "Envoi uniquement aux utilisateurs dont country correspond.",
                "spec_aide": "Ex: FR",
            },
            {
                "entity_id": sha256(f"audc|by_source|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_source",
                "libelle": "Filtrer par source",
                "description": "Envoi aux utilisateurs dont source est dans la liste.",
                "spec_aide": "Ex: newsletter,admin,dry_run",
            },
            {
                "entity_id": sha256(f"audc|by_email_list|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                "audience_key": "by_email_list",
                "libelle": "Liste d’e-mails",
                "description": "Envoi aux e-mails listés (1 par ligne).",
                "spec_aide": "Ex:\nnom@domaine.fr\nprenom@domaine.fr",
            },
        ]
        append_immutable_rows_bulk(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="AUDC", values_by_col_list=seed)
        aud_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="AUDC", limit=2000)

    st.subheader("Campagnes")
    try:
        rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="CMPG", limit=2000)
    except Exception:
        rows = []

    default_key = "weekly_friday_lumenvia"
    with st.expander("Campagnes (activer / désactiver)", expanded=False):
        # Dernière version par campaign_key
        latest_by_key: dict[str, dict] = {}
        for r in rows:
            k = str(r.get("campaign_key") or "").strip()
            if not k:
                continue
            prev = latest_by_key.get(k)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                latest_by_key[k] = r

        keys = sorted(latest_by_key.keys())
        def _is_true(v: object) -> bool:
            return str(v or "").strip().lower() in ("true", "1", "yes", "oui", "active")

        def _next_trigger_label(camp: dict) -> str:
            try:
                from datetime import datetime, time as dtime, timedelta
                try:
                    from zoneinfo import ZoneInfo  # py3.9+
                except Exception:
                    ZoneInfo = None  # type: ignore[assignment]

                tz = str(camp.get("timezone") or "Europe/Paris").strip() or "Europe/Paris"
                tzinfo = ZoneInfo(tz) if ZoneInfo else None
                now = datetime.now(tzinfo) if tzinfo else datetime.now()

                kind = str(camp.get("schedule_kind") or "").strip().lower()
                spec = str(camp.get("schedule_spec") or "").strip().lower()
                if kind == "weekly":
                    # ex: "ven 19:00"
                    day = (spec.split(" ", 1)[0] if " " in spec else "").strip() or "ven"
                    hm = (spec.split(" ", 1)[1] if " " in spec else "19:00").strip()
                    hh, mm = (hm.split(":", 1) + ["0"])[:2]
                    target_t = dtime(int(hh), int(mm))
                    day_map = {"lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6}
                    target_wd = day_map.get(day, 4)
                    # prochain jour cible
                    delta_days = (target_wd - now.weekday()) % 7
                    cand = now.replace(hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0) + timedelta(days=delta_days)
                    if cand <= now:
                        cand = cand + timedelta(days=7)
                    return cand.strftime("%a %d/%m %H:%M")
                if kind == "daily":
                    # ex: "19:00"
                    hm = spec or "19:00"
                    hh, mm = (hm.split(":", 1) + ["0"])[:2]
                    target_t = dtime(int(hh), int(mm))
                    cand = now.replace(hour=target_t.hour, minute=target_t.minute, second=0, microsecond=0)
                    if cand <= now:
                        cand = cand + timedelta(days=1)
                    return cand.strftime("%d/%m %H:%M")
                return "—"
            except Exception:
                return "—"

        def _clone_campaign(*, base: dict, enabled_value: bool) -> None:
            k0 = str(base.get("campaign_key") or "").strip()
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="CMPG",
                values_by_col={
                    "entity_id": sha256(f"cmpg|{k0}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "campaign_key": k0,
                    "name": str(base.get("name") or k0).strip(),
                    "enabled": "true" if enabled_value else "false",
                    "timezone": str(base.get("timezone") or "Europe/Paris").strip(),
                    "schedule_kind": str(base.get("schedule_kind") or "manual").strip(),
                    "schedule_spec": str(base.get("schedule_spec") or "").strip(),
                    "audience_kind": str(base.get("audience_kind") or "dry_run").strip(),
                    "audience_spec": str(base.get("audience_spec") or "").strip(),
                    "send_email": str(base.get("send_email") or "true").strip(),
                    "send_sms": str(base.get("send_sms") or "false").strip(),
                    "email_template_key": str(base.get("email_template_key") or "weekly_friday_lumenvia").strip(),
                    "sms_template_key": str(base.get("sms_template_key") or "").strip(),
                    "content_pdf": str(base.get("content_pdf") or "true").strip(),
                    "content_audio": str(base.get("content_audio") or "true").strip(),
                    "content_illustration": str(base.get("content_illustration") or "true").strip(),
                    "content_app_link": str(base.get("content_app_link") or "true").strip(),
                },
            )

        st.caption(
            "Note : le déclenchement automatique n’est pas encore branché côté cloud. "
            "La “prochaine fois” affichée est une prévision basée sur la règle."
        )

        for k in keys[:80]:
            camp = latest_by_key.get(k) or {}
            name = str(camp.get("name") or k).strip() or k
            enabled0 = _is_true(camp.get("enabled") or "false")
            nxt = _next_trigger_label(camp) if enabled0 else "—"

            c_left, c_right = st.columns([5, 1], gap="small")
            with c_left:
                st.markdown(
                    f"**{name}**  \n<small style='color:#475569'>Prochain déclenchement (prévision) : <strong>{nxt}</strong></small>",
                    unsafe_allow_html=True,
                )
            with c_right:
                # Toggle: si changement, on écrit une nouvelle version CMPG.
                cur = st.toggle("On/Off", value=enabled0, key=f"adm_sched_onoff_{k}", label_visibility="collapsed")
                if cur != enabled0:
                    _clone_campaign(base=camp, enabled_value=bool(cur))
                    st.rerun()

    # Dernière version par campaign_key (reutilisé ci-dessous)
    latest_by_key: dict[str, dict] = {}
    for r in rows:
        k = str(r.get("campaign_key") or "").strip()
        if not k:
            continue
        prev = latest_by_key.get(k)
        if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
            latest_by_key[k] = r
    keys = sorted(latest_by_key.keys()) or [default_key]

    with st.expander("Créer une nouvelle campagne", expanded=False):
        st.caption("Crée une campagne si elle n’existe pas encore. Ensuite, tu peux la paramétrer et la déclencher.")
        new_key = st.text_input(
            "Identifiant campagne",
            value="",
            key="adm_sched_new_key",
            placeholder="ex. hebdom_vendredi_2026",
        ).strip()
        new_name = st.text_input(
            "Nom (affiché)",
            value="",
            key="adm_sched_new_name",
            placeholder="ex. Hebdo — préparation dominicale",
        ).strip()
        if st.button("Créer la campagne", type="secondary"):
            if not new_key:
                st.error("Saisis un identifiant campagne (champ obligatoire).")
            else:
                exists = str(new_key) in latest_by_key
                if exists:
                    st.info("Cette campagne existe déjà.")
                else:
                    append_immutable_row(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="CMPG",
                        values_by_col={
                            "entity_id": sha256(f"cmpg|{new_key}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                            "campaign_key": new_key,
                            "name": new_name or new_key,
                            "enabled": "true",
                            "timezone": "Europe/Paris",
                            "schedule_kind": "weekly",
                            "schedule_spec": "ven 19:00",
                            "audience_kind": "weekly_friday_optin",
                            "audience_spec": "",
                            "send_email": "true",
                            "send_sms": "true",
                            "email_template_key": "weekly_friday_lumenvia",
                            "sms_template_key": "weekly_friday_lumenvia_sms",
                            "content_pdf": "true",
                            "content_audio": "true",
                            "content_illustration": "true",
                            "content_app_link": "true",
                        },
                    )
                    st.success("Campagne créée.")
            st.rerun()

    with st.expander("Paramétrer une campagne", expanded=False):
        camp_sel = st.selectbox("Choisir une campagne", options=keys, index=0, key="adm_sched_pick")
        camp = latest_by_key.get(camp_sel) or {}

        st.markdown("**Réglages**")
        c1, c2, c3 = st.columns([1, 1, 1], gap="small")
        with c1:
            enabled = st.checkbox("Activée", value=str(camp.get("enabled") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_enabled")
            timezone = st.text_input("Fuseau horaire", value=str(camp.get("timezone") or "Europe/Paris"), key="adm_sched_tz").strip()
        with c2:
            kind_code = str(camp.get("schedule_kind") or "weekly").strip() or "weekly"
            kind_labels = {"manual": "Manuel", "weekly": "Hebdomadaire", "daily": "Quotidien"}
            kind_order = ["manual", "weekly", "daily"]
            kind_pick = st.selectbox(
                "Fréquence",
                options=kind_order,
                format_func=lambda k: kind_labels.get(k, k),
                index=kind_order.index(kind_code) if kind_code in kind_order else 1,
                key="adm_sched_kind",
            )
            schedule_kind = kind_pick
            # Règle: guidée (pas de saisie libre)
            _days = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
            _day_labels = {
                "lun": "Lundi",
                "mar": "Mardi",
                "mer": "Mercredi",
                "jeu": "Jeudi",
                "ven": "Vendredi",
                "sam": "Samedi",
                "dim": "Dimanche",
            }
            spec0 = str(camp.get("schedule_spec") or "ven 19:00").strip().lower()
            day0 = spec0.split(" ", 1)[0] if spec0 else "ven"
            if day0 not in _days:
                day0 = "ven"
            import datetime as _dt

            t0 = _dt.time(19, 0)
            try:
                if ":" in spec0:
                    hhmm = spec0.split(" ", 1)[1].strip() if " " in spec0 else "19:00"
                    hh, mm = hhmm.split(":", 1)
                    t0 = _dt.time(int(hh), int(mm))
            except Exception:
                t0 = _dt.time(19, 0)

            if schedule_kind == "weekly":
                d_pick = st.selectbox("Jour d’envoi", options=_days, index=_days.index(day0), format_func=lambda d: _day_labels.get(d, d), key="adm_sched_day")
                t_pick = st.time_input("Heure d’envoi", value=t0, key="adm_sched_time")
                try:
                    from datetime import datetime
                    import time as _time

                    now_loc = datetime.now().strftime("%H:%M:%S")
                    tz_name = _time.tzname[0] if _time.tzname else "—"
                    st.caption(f"Actuellement : {now_loc} ({tz_name})")
                except Exception:
                    st.caption("Actuellement : —")
                schedule_spec = f"{d_pick} {t_pick.hour:02d}:{t_pick.minute:02d}"
            elif schedule_kind == "daily":
                t_pick = st.time_input("Heure d’envoi", value=t0, key="adm_sched_time_d")
                try:
                    from datetime import datetime
                    import time as _time

                    now_loc = datetime.now().strftime("%H:%M:%S")
                    tz_name = _time.tzname[0] if _time.tzname else "—"
                    st.caption(f"Actuellement : {now_loc} ({tz_name})")
                except Exception:
                    st.caption("Actuellement : —")
                schedule_spec = f"{t_pick.hour:02d}:{t_pick.minute:02d}"
            else:
                schedule_spec = ""
        with c3:
            send_email = st.checkbox("Envoyer e‑mail", value=str(camp.get("send_email") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_send_email")
            send_sms = st.checkbox("Envoyer SMS", value=str(camp.get("send_sms") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_send_sms")

    with st.expander("Audience (qui reçoit ?)", expanded=False):
        aud_active = [r for r in aud_rows if sheet_row_status_is_live(r.get("status"))]
        aud_latest: dict[str, dict] = {}
        for r in aud_active:
            k = str(r.get("audience_key") or "").strip()
            if not k:
                continue
            prev = aud_latest.get(k)
            if not prev or str(r.get("created_at") or "") > str(prev.get("created_at") or ""):
                aud_latest[k] = r
        aud_keys = sorted(aud_latest.keys())
        default_aud = str(camp.get("audience_kind") or "dry_run").strip() or "dry_run"
        aud_kind = st.selectbox(
            "Critère principal",
            options=aud_keys or ["dry_run"],
            index=(aud_keys.index(default_aud) if default_aud in aud_keys else 0),
            key="adm_sched_aud_kind",
            format_func=lambda k: str((aud_latest.get(k) or {}).get("libelle") or k),
        )
        aud_meta = aud_latest.get(aud_kind) or {}
        desc_txt = str(aud_meta.get("description") or "").strip()
        if desc_txt:
            st.markdown(
                f"""
<div style="border-left:4px solid #0d9488;background:#f0fdfa;color:#0b2745;
padding:10px 12px;border-radius:10px;margin:6px 0 10px 0;">
{desc_txt}
</div>
                """.strip(),
                unsafe_allow_html=True,
            )

        # Valeurs du critère : assistées (pas de saisie libre)
        aud_spec = ""
        if aud_kind in ("by_country", "by_source", "by_email_list"):
            try:
                users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            except Exception:
                users_rows = []
            if aud_kind == "by_country":
                countries = sorted({str(u.get("country") or "").strip() for u in users_rows if str(u.get("country") or "").strip()})
                pick = st.selectbox("Pays", options=countries or ["FR"], index=0, key="adm_sched_country")
                aud_spec = pick
            elif aud_kind == "by_source":
                sources = sorted({str(u.get("source") or "").strip() for u in users_rows if str(u.get("source") or "").strip()})
                picks = st.multiselect("Source(s)", options=sources, default=[], key="adm_sched_sources")
                aud_spec = ",".join(picks)
            elif aud_kind == "by_email_list":
                emails = sorted({str(u.get("email") or "").strip().lower() for u in users_rows if str(u.get("email") or "").strip()})
                picks = st.multiselect("E‑mails (multi‑sélection)", options=emails[:100], default=[], key="adm_sched_emails")
                aud_spec = "\n".join(picks)

        # Affichage explicite du destinataire dry-run (comme la page emailing)
        if aud_kind == "dry_run":
            try:
                users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            except Exception:
                users_rows = []

            def _is_email_ok(email: str) -> bool:
                em = (email or "").strip().lower()
                return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)) if em else False

            dry_users = [
                u
                for u in users_rows
                if str(u.get("source") or "").strip().lower() in ("dry_run", "test_emailing", "test")
                and _is_email_ok(str(u.get("email") or "").strip())
            ]
            dry_users.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            u0 = dry_users[0] if dry_users else {}
            dry_email = str(u0.get("email") or "").strip()
            dry_phone = str(u0.get("phone_e164") or "").strip()
            if not dry_email:
                try:
                    s = st.secrets
                    dry_email = str(s.get("EMAIL_DRY_RUN_TO") or "").strip()
                    dry_phone = str(s.get("SMS_DRY_RUN_TO") or "").strip()
                except Exception:
                    pass
            st.markdown("**Destinataire de test (dry-run)**")
            st.code(f"email: {dry_email or '—'}\nphone_e164: {dry_phone or '—'}")

    with st.expander("Contenu (quoi envoyer ?)", expanded=False):
        k1, k2, k3, k4 = st.columns([1, 1, 1, 1], gap="small")
        with k1:
            content_pdf = st.checkbox("PDF", value=str(camp.get("content_pdf") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_pdf")
        with k2:
            content_audio = st.checkbox("Audio", value=str(camp.get("content_audio") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_audio")
        with k3:
            content_illustration = st.checkbox("Illustration", value=str(camp.get("content_illustration") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_illu")
        with k4:
            content_app = st.checkbox("Lien app", value=str(camp.get("content_app_link") or "true").strip().lower() in ("true", "1", "yes", "oui"), key="adm_sched_c_app")

        st.markdown("**Templates (formats)**")
        # Templates e-mail: sélection parmi les clés existantes
        try:
            tpl_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)
        except Exception:
            tpl_rows = []
        email_keys = sorted({str(r.get("template_key") or "").strip() for r in tpl_rows if str(r.get("channel") or "").strip().lower() == "email" and str(r.get("template_key") or "").strip()})
        sms_keys = sorted({str(r.get("template_key") or "").strip() for r in tpl_rows if str(r.get("channel") or "").strip().lower() == "sms" and str(r.get("template_key") or "").strip()})
        cur_email_key = str(camp.get("email_template_key") or "weekly_friday_lumenvia").strip()
        cur_sms_key = str(camp.get("sms_template_key") or "weekly_friday_lumenvia_sms").strip()
        email_tpl_key = st.selectbox(
            "Template e‑mail",
            options=email_keys or [cur_email_key],
            index=(email_keys.index(cur_email_key) if cur_email_key in email_keys else 0),
            key="adm_sched_tpl_email",
        ).strip()
        sms_tpl_key = st.selectbox(
            "Template SMS",
            options=sms_keys or [cur_sms_key],
            index=(sms_keys.index(cur_sms_key) if cur_sms_key in sms_keys else 0),
            key="adm_sched_tpl_sms",
            help="Si aucun template SMS n’existe encore, on garde la clé actuelle.",
        ).strip()

        if st.button("Enregistrer les réglages", type="primary"):
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="CMPG",
                values_by_col={
                    "entity_id": sha256(f"cmpg|{camp_sel}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                    "campaign_key": camp_sel,
                    "name": str(camp.get("name") or camp_sel),
                    "enabled": "true" if enabled else "false",
                    "timezone": timezone or "Europe/Paris",
                    "schedule_kind": schedule_kind,
                    "schedule_spec": schedule_spec,
                    "audience_kind": aud_kind,
                    "audience_spec": aud_spec,
                    "send_email": "true" if send_email else "false",
                    "send_sms": "true" if send_sms else "false",
                    "email_template_key": email_tpl_key,
                    "sms_template_key": sms_tpl_key,
                    "content_pdf": "true" if content_pdf else "false",
                    "content_audio": "true" if content_audio else "false",
                    "content_illustration": "true" if content_illustration else "false",
                    "content_app_link": "true" if content_app else "false",
                },
            )
            st.success("Réglages enregistrés.")
            st.rerun()

    def _manual_campaign_snapshot_fr(cp: dict) -> str:
        """Une ligne lisible pour l’admin (sans empiler tous les champs en colonne)."""
        if not cp or not str(cp.get("campaign_key") or "").strip():
            return "Aucune version en Sheets pour cet identifiant — crée ou paramètre une campagne d’abord."
        ck = str(cp.get("campaign_key") or "").strip()
        nm = str(cp.get("name") or ck).strip()
        tf = lambda v: str(v or "").strip().lower() in ("true", "1", "oui", "yes")
        en = tf(cp.get("enabled"))
        tz = str(cp.get("timezone") or "Europe/Paris").strip()
        kind = str(cp.get("schedule_kind") or "manual").strip().lower()
        kind_fr = {"manual": "manuel", "weekly": "hebdomadaire", "daily": "quotidien"}.get(kind, kind)
        spec = str(cp.get("schedule_spec") or "").strip()
        aud_k = str(cp.get("audience_kind") or "").strip() or "—"
        raw_aud = str(cp.get("audience_spec") or "").strip().replace("\n", ", ")
        if len(raw_aud) > 140:
            raw_aud = raw_aud[:137].rstrip() + "…"
        row_st = str(cp.get("status") or "").strip()
        chunks = [
            f"nom « {nm} »",
            f"identifiant `{ck}`",
            "en service (`enabled`=oui)" if en else "hors service (`enabled`=non)",
            f"fuseau {tz}",
            f"cadence {kind_fr}",
        ]
        if spec:
            chunks.append(f"créneau `{spec}`")
        chunks.append(f"audience `{aud_k}`")
        if raw_aud:
            chunks.append(f"paramètre audience « {raw_aud} »")
        chunks.append(f"envoi mail {'oui' if tf(cp.get('send_email')) else 'non'}")
        chunks.append(f"envoi SMS {'oui' if tf(cp.get('send_sms')) else 'non'}")
        ek = str(cp.get("email_template_key") or "").strip()
        sk = str(cp.get("sms_template_key") or "").strip()
        if ek:
            chunks.append(f"modèle mail `{ek}`")
        if sk:
            chunks.append(f"modèle SMS `{sk}`")
        media: list[str] = []
        if tf(cp.get("content_pdf")):
            media.append("PDF")
        if tf(cp.get("content_audio")):
            media.append("audio")
        if tf(cp.get("content_illustration")):
            media.append("illustration")
        if tf(cp.get("content_app_link")):
            media.append("lien app")
        chunks.append("contenus : " + (", ".join(media) if media else "aucun coché en base"))
        if row_st:
            chunks.append(f"statut de cette ligne Sheets `{row_st}` (historique immutable)")
        return ", ".join(chunks)

    with st.expander("Déclencher une campagne (manuel)", expanded=False):
        camp_key = st.selectbox(
            "Campagne",
            options=keys or [default_key],
            index=(keys.index(camp_sel) if camp_sel in keys else 0),
            key="adm_sched_key_sel",
        )
        camp_snap = latest_by_key.get(str(camp_key).strip()) or {}

        mode = st.selectbox(
            "Mode d’envoi",
            options=["test_dry_run", "tous_opt_in"],
            index=0,
            format_func=lambda x: "Test (dry-run)" if x == "test_dry_run" else "Tous les inscrits opt-in",
            key="adm_sched_mode",
        )
        send_to_all = mode == "tous_opt_in"
        if send_to_all:
            st.warning(
                "Mode **Tous les inscrits opt-in** : tu vas cibler l’ensemble des abonnés actifs (selon `subscriptions`). "
                "Vérifie la liste ci-dessous avant d’envoyer.",
                icon="⚠️",
            )

        # Dimanche ciblé : force un dimanche
        today = date.today()
        next_sun = today + timedelta(days=(6 - today.weekday()) % 7)
        sunday = st.date_input("Dimanche ciblé", value=next_sun, key="adm_sched_sunday")
        if sunday.weekday() != 6:
            fixed = sunday + timedelta(days=(6 - sunday.weekday()) % 7)
            st.warning(f"Date ajustée au dimanche suivant : {fixed.isoformat()}")
            sunday = fixed
        date_str = sunday.isoformat()[:10]

        _tf_mail = lambda v: str(v or "").strip().lower() in ("true", "1", "oui", "yes")
        users_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
        subs_preview = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=8000)
        rec_preview = lumenvia_manual_broadcast_users(
            users_rows=users_preview, subs_rows=subs_preview, send_to_all=send_to_all
        )
        chan_em = _tf_mail(camp_snap.get("send_email"))
        chan_sm = _tf_mail(camp_snap.get("send_sms"))
        n_em = (
            sum(1 for u in rec_preview if str(u.get("email") or "").strip())
            if chan_em
            else 0
        )
        n_sm = (
            sum(1 for u in rec_preview if str(u.get("phone_e164") or "").strip())
            if chan_sm
            else 0
        )
        n_touch = len(rec_preview)
        mode_lbl = "tous les inscrits opt-in actifs (abonnement hebdo vendredi)" if send_to_all else "dry-run (compte source=dry_run)"
        recap = (
            _manual_campaign_snapshot_fr(camp_snap)
            + f"\n\n**Portée (mode actuel · {mode_lbl}) : {n_touch} destinataire(s) visé(s), "
            f"**{n_em}** envoi(s) e-mail prévu(x) et **{n_sm}** envoi(s) SMS prévu(x) "
            f"**(chaînes « envoi » de la campagne + coordonnée renseignée).**"
        )
        st.info(recap)

        # Sécurité / ajustement liste : uniquement pour l’envoi “tous opt-in”
        filtered_preview = list(rec_preview)
        excluded_emails: set[str] = set()
        confirm_all_ok = True
        confirm_phrase_ok = True
        if send_to_all:
            with st.expander("Aperçu des destinataires (avant envoi)", expanded=True):
                em_list = sorted(
                    {
                        str(u.get("email") or "").strip().lower()
                        for u in rec_preview
                        if str(u.get("email") or "").strip()
                    }
                )
                st.caption(f"E-mails détectés : **{len(em_list)}**")

                # Exclusions : multi-select + copier/coller
                excl_pick = st.multiselect(
                    "Exclure des e-mails (optionnel)",
                    options=em_list,
                    default=[],
                    key="adm_sched_excl_pick",
                )
                excl_paste = st.text_area(
                    "Ou coller une liste d’e-mails à exclure (un par ligne, optionnel)",
                    value="",
                    height=80,
                    key="adm_sched_excl_paste",
                )
                excluded_emails = {str(e or "").strip().lower() for e in (excl_pick or []) if str(e or "").strip()}
                for ln in (excl_paste or "").splitlines():
                    s = str(ln or "").strip().lower()
                    if s and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
                        excluded_emails.add(s)

                if excluded_emails:
                    filtered_preview = [
                        u
                        for u in filtered_preview
                        if str(u.get("email") or "").strip().lower() not in excluded_emails
                    ]

                # Mini table (limite) : qui va recevoir
                show_n = st.slider(
                    "Afficher les N premiers destinataires (aperçu)",
                    min_value=10,
                    max_value=300,
                    value=60,
                    step=10,
                    key="adm_sched_preview_n",
                )
                lines = []
                for u in filtered_preview[: int(show_n)]:
                    em0 = str(u.get("email") or "").strip().lower()
                    fn0 = str(u.get("first_name") or "").strip()
                    ln0 = str(u.get("last_name") or "").strip()
                    lines.append(f"{em0}\t{fn0}\t{ln0}")
                st.code(("\n".join(lines) if lines else "—")[:9000])

                # Confirmations
                final_em = sum(1 for u in filtered_preview if str(u.get("email") or "").strip()) if chan_em else 0
                final_sm = sum(1 for u in filtered_preview if str(u.get("phone_e164") or "").strip()) if chan_sm else 0
                final_touch = len(filtered_preview)
                st.markdown(
                    f"**Après exclusions :** {final_touch} destinataire(s), "
                    f"**{final_em}** e-mail(s) et **{final_sm}** SMS."
                )

                confirm_all_ok = st.checkbox(
                    "J’ai vérifié la liste et je confirme vouloir envoyer à ces destinataires.",
                    value=False,
                    key="adm_sched_confirm_checked",
                )
                phrase = st.text_input(
                    "Pour activer l’envoi, tape exactement : ENVOYER",
                    value="",
                    key="adm_sched_confirm_phrase",
                ).strip()
                confirm_phrase_ok = phrase == "ENVOYER"

        can_execute = (confirm_all_ok and confirm_phrase_ok) if send_to_all else True

        if st.button("Exécuter maintenant", type="primary", disabled=not can_execute):
            # Exécution : réutilise la logique d’envoi (hebdo opt-in)
            started = utc_now_iso()
            ok0 = 0
            err0 = 0
            run_id = sha256(f"run|{camp_key}|{date_str}|{started}".encode("utf-8")).hexdigest()[:24]
            ov = loading_overlay("Envoi en cours…")
            # Récupère campagne (dernière)
            camp_rows = [r for r in rows if str(r.get("campaign_key") or "").strip() == camp_key]
            camp_rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            camp = camp_rows[0] if camp_rows else {}
            do_email = str(camp.get("send_email") or "true").strip().lower() in ("true", "1", "oui", "yes")
            do_sms = str(camp.get("send_sms") or "true").strip().lower() in ("true", "1", "oui", "yes")
            email_tpl_key = str(camp.get("email_template_key") or "weekly_friday_lumenvia").strip()

            users_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="users", limit=8000)
            subs_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="subscriptions", limit=8000)
            recipients = lumenvia_manual_broadcast_users(
                users_rows=users_rows, subs_rows=subs_rows, send_to_all=send_to_all
            )
            # Applique les exclusions choisies dans l’aperçu (sécurité)
            if send_to_all and excluded_emails:
                recipients = [u for u in recipients if str(u.get("email") or "").strip().lower() not in excluded_emails]

            # template actif — même filtres vivants/langue FR que la page Emailing
            tpl_rows = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="email_templates", limit=0)

            tpl0 = pick_latest_live_email_template(
                tpl_rows,
                template_key=email_tpl_key,
                channel="email",
                language_in=("fr", "fr-fr", "france", ""),
            )
            tpl = tpl0 if tpl0 is not None else {}
            subj = str(tpl.get("subject") or "").strip()
            body = str(tpl.get("body") or "").strip()

            # Liens (signés)
            origin = _lumenvia_app_origin_url() or ""
            url_app = (origin.rstrip("/") + "/?route=about") if origin else ""
            url_pdf = ""
            url_audio = ""
            url_illu = ""
            try:
                gcs = build_gcs_client(cfg.gcp_service_account)
                p_pdf = f"Fascicules/{date_str}/lumenvia_dimanche_{date_str}.pdf"
                url_pdf = _gcs_signed_url(gcs=gcs, bucket_name=str(cfg.gcs_bucket_name).strip(), path=p_pdf) or ""
                year = date_str[:4]
                cand = [f"Images/illustrations/{year}/{date_str}{ext}" for ext in (".webp", ".png", ".jpg", ".jpeg")]
                url_illu = _gcs_first_signed_url(gcs=gcs, bucket_name=str(cfg.gcs_bucket_name).strip(), candidate_paths=cand) or ""
            except Exception:
                pass

            # SMTP/Twilio config réutilise _secret_get de la page Emailing (simple)
            def _secret_get(*keys: str) -> str:
                try:
                    s = st.secrets
                except Exception:
                    return ""
                for k in keys:
                    v = s.get(k)
                    if v is not None and str(v).strip():
                        return str(v).strip()
                return ""

            from core.outbound import SmtpConfig, TwilioConfig, send_smtp_email, send_twilio_sms
            smtp_cfg = SmtpConfig(
                host=_secret_get("SMTP_HOST"),
                port=int(_secret_get("SMTP_PORT") or 587),
                username=_secret_get("SMTP_USER"),
                password=_secret_get("SMTP_PASSWORD"),
                from_email=_secret_get("SMTP_FROM"),
                use_tls=str(_secret_get("SMTP_USE_TLS") or "true").strip().lower() not in ("0", "false", "no", "off"),
            )
            tw_cfg = TwilioConfig(
                account_sid=_secret_get("TWILIO_ACCOUNT_SID"),
                auth_token=_secret_get("TWILIO_AUTH_TOKEN"),
                from_phone_e164=_secret_get("TWILIO_FROM", "TWILIO_FROM_NUMBER"),
            )

            from core.emailing import EmailTemplate, render_template, french_day_month_year

            vals_base = {
                "origin": origin,
                "date_dimanche": french_day_month_year(sunday),
                "url_pdf": url_pdf,
                "url_audio": url_audio,
                "url_illustration": url_illu,
                "url_app": url_app,
            }

            for urec in recipients[:2000]:
                uid0 = str(urec.get("entity_id") or "").strip() or "recipient"
                em = str(urec.get("email") or "").strip()
                ph = str(urec.get("phone_e164") or "").strip()
                vals = dict(vals_base)
                vals["prenom"] = str(urec.get("first_name") or "—").strip() or "—"
                vals["nom"] = str(urec.get("last_name") or "—").strip() or "—"
                vals["email"] = em
                rendered = render_template(EmailTemplate(subject=subj, body=body), values={k: str(v) for k, v in vals.items()})

                # Envoi email (HTML gabarit)
                if do_email and em and smtp_cfg.host and smtp_cfg.from_email:
                    try:
                        html2 = ""  # le gabarit est généré dans render_admin_emailing; ici simple fallback texte
                        bt = rendered.body.strip()
                        bt = (bt + "\n\n—\n") if bt else ""
                        bt += LUMENVIA_DEVELOPMENT_NOTICE
                        send_smtp_email(cfg=smtp_cfg, to_email=em, subject=rendered.subject, body_text=bt, body_html=html2 or None)
                        ok0 += 1
                    except Exception:
                        err0 += 1

                # Envoi SMS (minimaliste)
                if do_sms and ph and tw_cfg.account_sid and tw_cfg.from_phone_e164:
                    try:
                        send_twilio_sms(cfg=tw_cfg, to_phone_e164=ph, body_text="Message de JOPAI LumenVia")
                        ok0 += 1
                    except Exception:
                        err0 += 1

            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="RUNS",
                values_by_col={
                    "entity_id": run_id,
                    "campaign_key": camp_key,
                    "run_kind": "manual",
                    "status_detail": "done",
                    "started_at": started,
                    "finished_at": utc_now_iso(),
                    "recipients_ok": str(ok0),
                    "recipients_err": str(err0),
                    "error": "",
                },
            )
            st.success(f"Terminé : {ok0} OK, {err0} erreur(s).")
            ov.empty()


def _build_prompt(
    *,
    instructions: str,
    length_words: int,
    include_takeaways: bool,
    include_catechese_bridge: bool,
    templates: dict[str, str] | None = None,
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
    tpls = dict(templates or {})
    default_takeaways = (
        "\nInclure une sous-section titrée exactement « Le Psaume : Ma réponse » : uniquement à partir du texte du psaume fourni, "
        "explique comment ce psaume permet de répondre en prière aux lectures (sans sources externes).\n"
        "Structurer aussi la synthèse pour mettre en relief la promesse / préfiguration (Première lecture, AT si applicable) "
        "et son accomplissement ou réponse dans l’Évangile, strictement à partir des textes fournis.\n"
        "Terminer par une section « À retenir » avec 3 à 5 puces commençant par un verbe.\n"
    )
    default_no_takeaways = (
        "\nMettre en relief la promesse / préfiguration (Première lecture) et l’accomplissement (Évangile), strictement à partir des textes fournis.\n"
    )
    psalm_block = (tpls.get("overlay_takeaways") or default_takeaways) if include_takeaways else (
        tpls.get("overlay_no_takeaways") or default_no_takeaways
    )

    catechese_block = ""
    if include_catechese_bridge:
        catechese_block = tpls.get("overlay_catechese_bridge") or (
            "\nAjouter à la fin une section titrée exactement : « Passerelle catéchèse — L’écho des paraboles ».\n"
            "Cette section doit être une “Stone Card” structurée en 5 sous-parties (titres exacts) :\n"
            "Important : ne mets pas de numérotation (pas de « 1) », « 2) », etc.).\n"
            "Important : n'utilise aucun emoji, aucune puce décorative, aucun symbole (ni carrés, ni ronds), et aucun caractère isolé en préfixe.\n"
            "Chaque sous-partie doit commencer par le TITRE SEUL sur une ligne (ex: « La Scène Visuelle »), puis le texte sur les lignes suivantes.\n"
            "« L’Essentiel » : une seule phrase percutante (le cœur du message), fidèle aux textes.\n"
            "« La Scène Visuelle » : décrire la scène comme un tableau vivant (sensoriel) sans inventer de paroles.\n"
            "« Le Mot-Clé » : choisir 1 concept (ex. Grâce, Alliance…) et le définir simplement.\n"
            "« L’Analogie du Quotidien » : une analogie moderne, digne, non trivialisante, qui éclaire le texte sans le remplacer.\n"
            "« Le Pas de la Semaine » : un défi concret à vivre (école, famille, paroisse).\n"
            "Garde-fous :\n"
            "- Prudence interprétative : ne pas inventer de paroles du Christ ni changer le sens de l’Écriture.\n"
            "- Ton d’accompagnement respectueux ; pas de langage culpabilisant.\n"
            "- Si un point théologique est complexe/controversé, inviter à en parler avec un animateur/catéchiste.\n"
        )

    return f"""
{instructions}

Paramètres:
- length_words: {length_words}
- include_takeaways: {takeaways}
- include_catechese_bridge: {"true" if include_catechese_bridge else "false"}
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
{catechese_block}
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


def _chunk_text_for_tts(text: str, *, max_chars: int = 1400) -> list[str]:
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


_CATECHESE_SECTION_TITLE = "Passerelle catéchèse — L’écho des paraboles"


def _strip_catechese_bridge(text: str | None) -> str | None:
    """Retire la section « Passerelle catéchèse… » du Markdown si présente (pour option PDF)."""
    if not text:
        return text
    s = str(text)
    # Retire depuis un titre Markdown contenant le libellé jusqu’à la fin ou jusqu’au prochain titre niveau 2/3.
    # Supporte: "## Passerelle..." ou "**Passerelle..." (selon style modèle).
    pat = re.compile(
        r"(?is)\n{0,2}(?:#{2,3}\s*|\\*\\*\\s*)"
        + re.escape(_CATECHESE_SECTION_TITLE)
        + r".*?(?=(?:\n#{2,3}\s)|\\Z)"
    )
    out = re.sub(pat, "\n", s).strip()
    return out


def _gcs_signed_url(
    *,
    gcs: object,
    bucket_name: str,
    path: str,
    expires_s: int = 7 * 24 * 3600,
) -> str | None:
    """URL signée (V4) pour accès anonyme temporaire à un objet privé."""
    try:
        bucket = gcs.bucket(bucket_name)
        blob = bucket.blob(path)
        if not blob.exists():
            return None
        return blob.generate_signed_url(
            version="v4",
            expiration=int(expires_s),
            method="GET",
        )
    except Exception:
        return None


def _gcs_first_signed_url(
    *,
    gcs: object,
    bucket_name: str,
    candidate_paths: list[str],
    expires_s: int = 7 * 24 * 3600,
) -> str | None:
    for p in candidate_paths:
        path = str(p or "").strip()
        if not path:
            continue
        u = _gcs_signed_url(gcs=gcs, bucket_name=bucket_name, path=path, expires_s=expires_s)
        if u:
            return u
    return None


def _inject_admin_phone_preview_css() -> None:
    """Admin uniquement : largeur réglable + cadre arrondi type smartphone pour recette bureau."""
    if not st.session_state.get("admin_authenticated"):
        return
    if not st.session_state.get("admin_phone_preview"):
        return
    try:
        wpx = int(st.session_state.get("admin_mobile_preview_width", 390) or 390)
    except Exception:
        wpx = 390
    wpx = max(280, min(560, wpx))
    st.markdown(
        f"""
<style>
/* Aperçu smartphone — activé depuis la tuile Simulateur mobile (cadre téléphone) */
[data-testid="stAppViewContainer"] {{
  background: linear-gradient(165deg, #4a4a52 0%, #1e1e22 55%, #121214 100%) !important;
  min-height: 100vh !important;
}}
[data-testid="stHeader"] {{
  background: transparent !important;
}}
section[data-testid="stMain"] {{
  max-width: {wpx}px !important;
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
}}
section[data-testid="stMain"] .block-container {{
  padding-left: max(0.65rem, env(safe-area-inset-left, 0px)) !important;
  padding-right: max(0.65rem, env(safe-area-inset-right, 0px)) !important;
}}
</style>
        """,
        unsafe_allow_html=True,
    )


def _lumenvia_narrow_nav_from_query() -> bool:
    """`?lumenvia_narrow_nav=1` : iframe où le viewport CSS ne reflète pas la largeur utile."""
    try:
        v = str(st.query_params.get("lumenvia_narrow_nav") or "").strip().lower()
    except Exception:
        v = ""
    return v in ("1", "true", "yes", "on")


def _lumenvia_app_origin_url() -> str | None:
    """Origine HTTPS de l'app pour iframe simulateur (`PUBLIC_APP_URL` ou URL courante Streamlit)."""
    try:
        s = st.secrets
        base = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip().rstrip("/")
    except Exception:
        base = ""
    if base:
        return base
    try:
        u = str(getattr(st.context, "url", "") or "").strip()
        if not u:
            return None
        from urllib.parse import urlparse

        p = urlparse(u)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception:
        pass
    return None


_FEEDBACK_SURVEY_CTA_RE = re.compile(
    r"👉\s*(?:\[\s*)?Donner\s+mon\s+avis\s+sur\s+cette\s+expérience(?:\s*\])?",
    flags=re.IGNORECASE,
)


def lumenvia_feedback_survey_abs_url(origin: str | None, *, recipient_email: str | None = None) -> str:
    """URL absolue page « Donner votre avis » (lien depuis e-mails).

    Si ``recipient_email`` est un e-mail plausible, ajoute ``&email=`` pour préremplir le formulaire.
    """
    base_raw = ((origin or "").strip() if origin else "") or (_lumenvia_app_origin_url() or "").strip()
    base = base_raw.rstrip("/")
    if not base:
        return ""
    em = str(recipient_email or "").strip().lower()
    try:
        from urllib.parse import quote_plus
    except Exception:  # pragma: no cover
        quote_plus = None  # type: ignore[assignment]
    url = f"{base}/?route=feedback"
    try:
        if em and bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em)) and quote_plus:
            url += f"&email={quote_plus(em)}"
    except Exception:
        pass
    return url


def lumenvia_wrap_feedback_cta_with_link(
    fragment: str, *, origin_for_href: str | None = None, recipient_email: str | None = None
) -> str:
    """Encapsule la phrase 👉 … Donner mon avis … en lien `<a>` (fragment HTML léger ou texte)."""
    txt = fragment or ""
    if not txt or "👉" not in txt or "avis" not in txt.lower():
        return txt
    url = lumenvia_feedback_survey_abs_url(origin_for_href, recipient_email=recipient_email)
    if not url:
        return txt

    def repl(m: re.Match) -> str:
        lbl = (m.group(0) or "").strip()
        return (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
            'style="color:#0d9488;font-weight:700;text-decoration:underline;">'
            f"{lbl}</a>"
        )

    return _FEEDBACK_SURVEY_CTA_RE.sub(repl, txt)


def render_admin_mobile_simulator() -> None:
    """Panneau recette : prévisualisation iframe + paramètres du cadre appliqué à toute la session."""
    if _lumenvia_phone_like_user_agent():
        st.info("Simulateur mobile masqué sur téléphone : utilise l’app directement sur cet écran.")
        return
    st.title("Simulateur vision mobile")
    st.markdown(
        """
Recette depuis un **ordinateur** : même session Streamlit que l’écran suivant après navigation.

**Deux modes :**
1. **Cadre sur l’app** : même onglet, réduit la zone principale façon téléphone (`max-width`), utile pour *Menu*,
   *La Lumière du Dimanche*, *Mon Aide‑Mémoire* (dont expander « Mes mémos ») et les saisies.
2. **iframe** ci‑dessous : **nouvelle connexion Streamlit** (session distincte).

Le **clavier virtuel** réel du téléphone n’est pas reproductible ici ; pour un faux clavier utilise
**Chrome / Edge → F12 → mode appareil** en complément si besoin.
        """.strip()
    )
    st.toggle(
        "Cadre téléphone sur l’app (session)",
        key="admin_phone_preview",
        help="Réduit la zone principale comme sur un téléphone (largeur ci‑dessous). À activer ici puis naviguer dans l’admin ou les pages métier.",
    )

    if "admin_mobile_preview_width" not in st.session_state:
        st.session_state["admin_mobile_preview_width"] = 390

    p1, p2, p3, p4 = st.columns(4)
    if p1.button("320 px · petit tel.", key="adm_mob_preset_320"):
        st.session_state["admin_mobile_preview_width"] = 320
        st.rerun()
    if p2.button("360 px · classique", key="adm_mob_preset_360"):
        st.session_state["admin_mobile_preview_width"] = 360
        st.rerun()
    if p3.button("390 px · iPhone", key="adm_mob_preset_390"):
        st.session_state["admin_mobile_preview_width"] = 390
        st.rerun()
    if p4.button("428 px · large", key="adm_mob_preset_428"):
        st.session_state["admin_mobile_preview_width"] = 428
        st.rerun()

    w = st.slider(
        "Largeur du cadre (px)",
        min_value=280,
        max_value=560,
        value=int(st.session_state.get("admin_mobile_preview_width", 390)),
        step=2,
        help="Largeur du cadre quand « Cadre téléphone sur l’app » est activé, et pour l’iframe ci‑dessous.",
    )
    st.session_state["admin_mobile_preview_width"] = int(w)
    st.caption(
        "Active d’abord **Cadre téléphone sur l’app** ci‑dessus, puis navigue ; ou utilise un bouton "
        "**+ cadre** pour l’allumer automatiquement."
    )

    st.subheader("Ouvrir une page métier avec le cadre")
    oc1, oc2, oc3 = st.columns(3)
    with oc1:
        if st.button("La Lumière du Dimanche + cadre", key="adm_mob_go_sunday", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "sunday"
            st.rerun()
    with oc2:
        if st.button("Mon Aide‑Mémoire + cadre", key="adm_mob_go_memo", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "memo"
            st.rerun()
    with oc3:
        if st.button("À propos + cadre", key="adm_mob_go_about", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "about"
            st.rerun()

    st.divider()
    st.subheader("Aperçu iframe (session distincte)")
    origin = _lumenvia_app_origin_url()
    if not origin:
        st.info(
            "Pour charger l’iframe, définis `PUBLIC_APP_URL` (ou `public_app_url`) dans les secrets avec l’URL publique "
            "de déploiement (ex. ton app Streamlit Cloud). Sinon Streamlit doit exposer `st.context.url` sur ton hébergement."
        )
    else:
        base_src = origin.rstrip("/") + "/"
        sep = "&" if "?" in base_src else "?"
        src = html_escape(base_src + sep + "lumenvia_narrow_nav=1", quote=True)
        iw = max(280, min(560, int(st.session_state.get("admin_mobile_preview_width", 390) or 390)))
        iframe_html = f"""
<div style="display:flex;justify-content:center;background:linear-gradient(165deg,#3a3a42,#121214);padding:1rem;border-radius:12px;">
  <iframe
    src="{src}"
    title="LumenVia — aperçu mobile"
    style="width:{iw}px;height:760px;border:12px solid #0d0d0f;border-radius:36px;box-sizing:border-box;background:#fdfbf7;"
    loading="lazy"
    referrerpolicy="strict-origin-when-cross-origin"
  ></iframe>
</div>
"""
        components.html(iframe_html, height=840, scrolling=True)
        # (supprimé) texte d’explication sous l’iframe


def main() -> None:
    set_page_style()
    if _lumenvia_narrow_nav_from_query():
        st.session_state["lumenvia_narrow_nav"] = True
    # À appliquer avant tout widget lié à `admin_phone_preview` (ex. simulateur mobile).
    if st.session_state.pop("_lumenvia_enable_phone_preview", False):
        st.session_state["admin_phone_preview"] = True
    _inject_admin_phone_preview_css()

    if "route" not in st.session_state:
        st.session_state.route = "about"

    # Liens admin optionnels : ?admin=1 (ressources), ?admin=login, ?admin=step3, ?admin=mob (simulateur mobile)
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
    elif adm in ("mob", "mobile"):
        if st.session_state.get("admin_authenticated"):
            st.session_state.route = "admin_mobile_sim"
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
        try:
            if "open_cal" in st.query_params:
                del st.query_params["open_cal"]
        except Exception:
            pass

    try:
        rte_q = str(params.get("route") or "").strip().lower()
    except Exception:
        rte_q = ""
    if rte_q in ("feedback", "avis"):
        st.session_state.route = "feedback"
    elif rte_q in ("account", "compte"):
        # Liens e-mailing : /?route=account&email=... → ouvrir “Mon compte”
        st.session_state.route = "account"
    elif rte_q in ("join", "nous_rejoindre", "nousrejoindre"):
        st.session_state.route = "join"
    elif rte_q in ("reset_password", "reset", "pwd_reset"):
        st.session_state.route = "reset_password"

    if adm in ("1", "login", "step3", "cdc", "mob", "mobile"):
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
    elif route == "feedback":
        render_feedback()
    elif route == "account":
        render_join()
    elif route == "reset_password":
        render_reset_password()
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
    elif route == "admin_vision":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_vision"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_vision_text()
    elif route == "admin_accounts":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_accounts"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_accounts()
    elif route == "admin_emailing":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_emailing"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_emailing()
    elif route == "admin_scheduler":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_scheduler"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_scheduler()
    elif route == "admin_readings_cache":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_readings_cache"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_readings_cache()
    elif route == "admin_mobile_sim":
        if not st.session_state.get("admin_authenticated"):
            st.warning("Accès réservé — identifie-toi avec le compte administrateur.")
            if st.button("Aller à la connexion admin", key="goto_admin_login_mobile_sim"):
                st.session_state.route = "admin_login"
                st.rerun()
        else:
            render_admin_mobile_simulator()
    else:
        render_about()


if __name__ == "__main__":
    main()

