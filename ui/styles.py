"""Styles globaux LumenVia : config page, injections viewport/footer, CSS charte, pied de page fixe."""

from __future__ import annotations

from html import escape as html_escape
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from core.dev_notice import LUMENVIA_DEVELOPMENT_NOTICE


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

  function scrollBehaviorPreferInstant() {
    try {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return true;
    } catch (e0) {}
    try {
      if (window.matchMedia("(max-width: 900px)").matches) return true;
    } catch (e1) {}
    try {
      if (window.matchMedia("(pointer: coarse)").matches) return true;
    } catch (e2) {}
    return false;
  }

  function bumpScroll(detailsEl) {
    if (!detailsEl || !detailsEl.open) return;
    var instant = scrollBehaviorPreferInstant();
    var sb = instant ? "auto" : "smooth";
    var reserve = footerReservePx();
    function step() {
      try {
        detailsEl.scrollIntoView({ behavior: sb, block: "nearest", inline: "nearest" });
        var rect = detailsEl.getBoundingClientRect();
        var vh = rootWin.innerHeight || doc.documentElement.clientHeight || 720;
        var bottomLimit = vh - reserve;
        var overflow = rect.bottom - bottomLimit;
        if (overflow <= 6) return;
        var dy = overflow + 8;
        var se = doc.scrollingElement || doc.documentElement || doc.body;
        try {
          se.scrollBy({ top: dy, behavior: sb });
        } catch (e2) {
          rootWin.scrollBy(0, dy);
        }
      } catch (e) {}
    }
    if (instant) {
      requestAnimationFrame(step);
    } else {
      requestAnimationFrame(function () {
        requestAnimationFrame(step);
      });
    }
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
          var delays = window.matchMedia("(max-width: 900px)").matches ? [120] : [90, 320];
          for (var di = 0; di < delays.length; di++) {
            (function (ms) {
              window.setTimeout(function () {
                onDetails(det);
              }, ms);
            })(delays[di]);
          }
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
  /* Tuiles Menu + grille Administration (bureau) : intercolonnes un peu plus serrées qu’avec gap Streamlit seul */
  div[class*="st-key-lv_nav_web_one_row"] > div[data-testid="stHorizontalBlock"],
  div[class*="st-key-lv_admin_desktop_shell"] div[data-testid="stHorizontalBlock"] {
    gap: clamp(0.2rem, 0.65vw, 0.45rem) !important;
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

/* Cases à cocher : réduire la marge verticale entre lignes (reruns Streamlit plus « légères » visuellement) */
[data-testid="stCheckbox"] {
  margin-bottom: 0.12rem !important;
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

/* Identité du jour — titre + cadre livrables (couleur liturgique de la semaine) */
h2.lv-sunday-identity-heading {
  font-family: 'Cormorant Garamond', serif !important;
  text-align: center !important;
  color: var(--liturgie-text) !important;
  font-size: clamp(1.35rem, 2.2vw, 1.65rem) !important;
  font-weight: 600 !important;
  margin: 0 0 0 !important;
  padding: 0 0 0.55rem !important;
  border-bottom: 2.5px solid var(--liturgie-accent) !important;
}

/* Cadre extérieur : même liseré que le filet sous le titre (remplace le gris Streamlit) */
div[class*="st-key-lv_sunday_deliverables_box"] {
  margin: 0 0 1.15rem !important;
  max-width: 100% !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] > div[data-testid="stVerticalBlockBorderWrapper"] {
  border: 2.5px solid var(--liturgie-accent) !important;
  border-top: none !important;
  border-radius: 0 !important;
  padding: clamp(0.85rem, 2.2vw, 1.3rem) clamp(0.75rem, 2.5vw, 1.25rem) !important;
  margin-top: 0 !important;
  background: rgba(255, 253, 248, 0.55) !important;
  box-shadow: none !important;
}

/* Lecteurs audio pleine largeur dans le cadre */
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="stAudio"] {
  width: 100% !important;
  max-width: 100% !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="stAudio"] audio {
  width: 100% !important;
  max-width: 100% !important;
  display: block !important;
}

/* PDF + texte : même hauteur, sans cadre interne */
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="stHorizontalBlock"] {
  align-items: stretch !important;
  margin: 0.55rem 0 !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="column"] {
  display: flex !important;
  flex-direction: column !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="column"] > div[data-testid="stVerticalBlock"] {
  flex: 1 1 auto !important;
  display: flex !important;
  flex-direction: column !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] button[kind="secondary"],
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="stExpander"] {
  flex: 1 1 auto !important;
  height: 100% !important;
  margin: 0 !important;
}
div[class*="st-key-lv_sunday_deliverables_box"] [data-testid="stExpander"] summary {
  min-height: 64px !important;
  height: 100% !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  white-space: pre-line !important;
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

/*
  Clic / tap « une fois suffit » sur les boutons (formulaires Streamlit inclus).
  - touch-action: manipulation évite le délai tactiles ~300ms (legacy WebKit) et le double-tap-zoom
    qui donnent l’impression qu’il faut appuyer deux fois sur Connexion / Envoyer / etc.
*/
[data-testid="stAppViewContainer"] button,
[data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button {
  touch-action: manipulation !important;
}
@media (max-width: 1024px) {
  [data-testid="stAppViewContainer"] button[kind="primary"],
  [data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button {
    min-height: 48px !important;
    padding-top: 0.45rem !important;
    padding-bottom: 0.45rem !important;
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

