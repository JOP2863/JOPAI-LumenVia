"""Admin — Plan consolidé (HTML + tri colonnes asc/desc)."""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from typing import Literal

import streamlit as st

SortCol = Literal["theme", "status", "notes", ""]
SortDir = Literal["asc", "desc"]

_COL_LABELS: dict[str, str] = {
    "theme": "Thème",
    "status": "Statut",
    "notes": "Reste à faire / notes",
}

# Ordre métier des statuts (clés déjà « folded » sans accents)
_STATUS_RANK: dict[str, int] = {
    "livre": 10,
    "livre v1": 11,
    "livre v2": 12,
    "livre base": 13,
    "en donnees": 20,
    "regle": 21,
    "en cours": 30,
    "iteratif": 31,
    "a finaliser": 40,
    "a faire": 50,
    "a cadrer": 51,
}


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).casefold()


def _status_sort_key(label: str) -> tuple[int, str]:
    folded = _fold(label)
    rank = _STATUS_RANK.get(folded)
    if rank is None:
        for k, v in _STATUS_RANK.items():
            if folded.startswith(k):
                rank = v
                break
    if rank is None:
        rank = 99
    return (rank, folded)


def _parse_plan_rows(tbody_html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for i, tr in enumerate(re.findall(r"<tr\b.*?</tr>", tbody_html, flags=re.I | re.S)):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, flags=re.I | re.S)
        if len(cells) < 3:
            continue
        theme_txt = _strip_tags(cells[0])
        status_txt = _strip_tags(cells[1])
        notes_txt = _strip_tags(cells[2])
        rows.append(
            {
                "tr": tr,
                "theme": theme_txt,
                "status": status_txt,
                "notes": notes_txt,
                "ord": str(i).zfill(4),
            }
        )
    return rows


def _sort_rows(rows: list[dict[str, str]], *, col: SortCol, direction: SortDir) -> list[dict[str, str]]:
    if not col:
        return list(rows)
    reverse = direction == "desc"

    def key_fn(r: dict[str, str]):
        if col == "status":
            return _status_sort_key(r["status"])
        if col == "notes":
            return _fold(r["notes"])
        return _fold(r["theme"])

    return sorted(rows, key=key_fn, reverse=reverse)


def _th_html(col: SortCol) -> str:
    """En-tête HTML avec indicateur de tri visible dans le tableau."""
    base = _COL_LABELS.get(col or "", "")
    cur = str(st.session_state.get("plan_sort_col") or "")
    if cur != col:
        mark = '<span class="lv-plan-sort-idle" title="Non trié"> ↕</span>'
        cls = ""
    else:
        direction = str(st.session_state.get("plan_sort_dir") or "asc")
        arrow = "▲" if direction == "asc" else "▼"
        mark = f'<span class="lv-plan-sort-active" title="Tri {direction}"> {arrow}</span>'
        cls = ' class="lv-plan-th-sorted"'
    return f"<th{cls}>{html_lib.escape(base)}{mark}</th>"


def render_admin_plan_consolide() -> None:
    """Vue synthèse : protocole LumenVia + reste à faire (alignement retours Gemini)."""
    st.title("Plan consolidé")
    st.caption(
        "Synthèse du protocole (`.cursor/rules/lumenvia.mdc`), de l’état du code et des chantiers — "
        "y compris les écarts repérés par rapport à ce qui est déjà documenté (cahier, règles, écran admin)."
    )

    if "plan_sort_col" not in st.session_state:
        st.session_state.plan_sort_col = ""
    if "plan_sort_dir" not in st.session_state:
        st.session_state.plan_sort_dir = "asc"

    st.markdown(
        """
<style>
div[class*="st-key-plan_ico_"] button {
  min-height: 32px !important;
  height: 32px !important;
  padding: 0 !important;
  letter-spacing: normal !important;
  text-transform: none !important;
  font-size: 0.95rem !important;
  font-weight: 700 !important;
  background: rgba(212, 175, 55, 0.14) !important;
  color: #342E29 !important;
  border: 1px solid rgba(212, 175, 55, 0.45) !important;
}
div[class*="st-key-plan_ico_"] button p,
div[class*="st-key-plan_ico_"] button span {
  color: #342E29 !important;
  line-height: 1 !important;
}
div[class*="wrap_active"] button {
  background: rgba(212, 175, 55, 0.42) !important;
  border-color: #D4AF37 !important;
}
</style>
        """.strip(),
        unsafe_allow_html=True,
    )

    def _set_sort(col: SortCol, direction: SortDir) -> None:
        st.session_state.plan_sort_col = col
        st.session_state.plan_sort_dir = direction
        st.rerun()

    def _ico_btn(col: SortCol, direction: SortDir, *, key: str) -> None:
        active = (
            str(st.session_state.get("plan_sort_col") or "") == col
            and str(st.session_state.get("plan_sort_dir") or "") == direction
        )
        label = "▲" if direction == "asc" else "▼"
        # Conteneur clé pour le CSS « actif »
        wrap_key = f"{key}_wrap_active" if active else f"{key}_wrap"
        with st.container(key=wrap_key):
            if st.button(label, key=key, use_container_width=True, help=f"Trier {_COL_LABELS[col]} ({'asc' if direction == 'asc' else 'desc'})"):
                _set_sort(col, direction)

    # Une seule ligne d’icônes alignée sur les 3 colonnes du tableau (pas de barre select/radio)
    hc1, hc2, hc3, hc4 = st.columns([2.2, 1.15, 3.0, 0.7], gap="small")
    with hc1:
        a1, a2 = st.columns(2, gap="small")
        with a1:
            _ico_btn("theme", "asc", key="plan_ico_theme_asc")
        with a2:
            _ico_btn("theme", "desc", key="plan_ico_theme_desc")
    with hc2:
        b1, b2 = st.columns(2, gap="small")
        with b1:
            _ico_btn("status", "asc", key="plan_ico_status_asc")
        with b2:
            _ico_btn("status", "desc", key="plan_ico_status_desc")
    with hc3:
        c1, c2 = st.columns(2, gap="small")
        with c1:
            _ico_btn("notes", "asc", key="plan_ico_notes_asc")
        with c2:
            _ico_btn("notes", "desc", key="plan_ico_notes_desc")
    with hc4:
        if st.button("✕", key="plan_ico_reset", use_container_width=True, help="Ordre d’origine"):
            st.session_state.plan_sort_col = ""
            st.session_state.plan_sort_dir = "asc"
            st.rerun()

    thead_html = (
        "<tr>"
        + _th_html("theme")
        + _th_html("status")
        + _th_html("notes")
        + "</tr>"
    )

    plan_html = f"""
<style>
.lv-plan-wrap {{ font-family: Lora, Georgia, serif; color: #342E29; font-size: 0.92rem; }}
.lv-plan-table {{ width: 100%; border-collapse: collapse; margin: 0.75rem 0 1.25rem 0; }}
.lv-plan-table th {{
  text-align: left; padding: 10px 12px; background: rgba(212, 175, 55, 0.18);
  border: 1px solid rgba(212, 175, 55, 0.45); font-weight: 600;
}}
.lv-plan-table th.lv-plan-th-sorted {{
  background: rgba(212, 175, 55, 0.38);
  box-shadow: inset 0 -3px 0 #D4AF37;
}}
.lv-plan-sort-active {{ color: #1b5e20; font-weight: 700; }}
.lv-plan-sort-idle {{ color: rgba(52, 46, 41, 0.45); font-weight: 500; }}
.lv-plan-table td {{
  vertical-align: top; padding: 10px 12px; border: 1px solid rgba(52, 46, 41, 0.15);
  background: rgba(255, 255, 255, 0.65);
}}
.lv-plan-table tr:nth-child(even) td {{ background: rgba(253, 251, 247, 0.95); }}
.lv-st-ok {{ color: #1b5e20; font-weight: 600; }}
.lv-st-partiel {{ color: #bf360c; font-weight: 600; }}
.lv-st-todo {{ color: #6a1b9a; font-weight: 600; }}
.lv-keylist {{ margin-top: 1rem; padding: 12px 14px; border-left: 3px solid #D4AF37; background: rgba(255,255,255,0.75); }}
.lv-keylist dt {{ font-weight: 600; margin-top: 8px; color: #342E29; }}
.lv-keylist dd {{ margin: 4px 0 0 0; padding-left: 0.5rem; border-left: 2px solid rgba(212, 175, 55, 0.35); }}
</style>
<div class="lv-plan-wrap">
<table class="lv-plan-table">
  <thead>
    {thead_html}
  </thead>
  <tbody>
"""
    plan_body = """
    <tr>
      <td><strong>Déploiement public (Git + Streamlit Cloud) — sécurité</strong></td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Déploiement réalisé après durcissement du dépôt et configuration Streamlit Cloud (Secrets). Socle maintenu :
        <ul>
          <li><code>.gitignore</code> : <code>.streamlit/secrets.toml</code>, <code>.env*</code>, clés, comptes de service, <code>.venv/</code>, caches.</li>
          <li>Admin via <code>st.secrets</code> (pas d’identifiants par défaut en dur).</li>
          <li>Prompts IA dans Sheets (<code>Paramètres_IA</code> / <strong>AIP</strong>) + secret sauce (<code>IA_SECRET_SAUCE_MD</code>).</li>
          <li>Fallback local <code>data/instructions_ia.md</code> minimal (repo public).</li>
        </ul>
        Vigilance continue à chaque contribution ; révision historique Git ponctuelle si besoin d’audit.
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
      <td>
        Extensions possibles (autres médias) si le produit le demande.
        URL de base de l’API AELF surchargeable via secrets (<code>AELF_BASE_URL</code> ou section <code>[aelf]</code> ; défaut <code>api.aelf.org</code>, pas de clé API).
      </td>
    </tr>
    <tr>
      <td><strong>Multi-langues — lectures natives (pas de traduction maison)</strong></td>
      <td><span class="lv-st-partiel">Livré v1</span></td>
      <td>
        Règle produit : uniquement des API qui livrent les <strong>textes complets</strong> de la messe dans la langue
        (pas de traduction IA/maison à partir de l’AELF ; exclus : évangile-seul, calendrier-seul / Romcal).
        Priorité langues : <strong>FR → DE → EN → ES → IT</strong>.
        Socle : <code>pref_langue</code> + LGP + chemins GCS <code>&#123;LANG&#125;/</code>.
        <strong>FR</strong> : AELF production (non remplacé).
        <strong>DE / EN / ES / IT</strong> : Evangelizo Reader Feed (<code>cached_liturgy_day</code>) —
        page Dimanche (sélecteur + drapeaux) + admin génération + écriture RDC automatique ;
        codes Reader : DE / AM (EN) / SP (ES) / IT ; horizon ±30&nbsp;j.
        Universalis = Lab / secours (pas la route produit EN).
        Checklist licences : <code>data/evangelizo_license_checklist.json</code> + Universalis.
        Reste : confirmation écrite Evangelizo pour e-mail/TTS/PDF larges + templates e-mail localisés.
      </td>
    </tr>
    <tr>
      <td><strong>Atelier audio — ambiance TTS (intro / outro / bed)</strong></td>
      <td><span class="lv-st-ok">Livré v1</span></td>
      <td>
        Tuile admin <strong>Atelier audio</strong> (<code>admin_audio_atelier</code>) : upload WAV → GCS <code>Audio/ambiance/</code>
        + table <code>audio_ambiance</code> (<strong>AAMB</strong>).
        Mix automatique à la génération TTS (lectures / synthèse) : intro → voix (± bed bas) → outro.
        Choix : clips <strong>actifs</strong> filtrés par cible/langue ; bouton <strong>Mettre en priorité</strong> ;
        retrait = statut <strong>Inactif</strong> (pas d’effacement GCS).
        Licences acceptées : <strong>CC0</strong> / <strong>domaine public</strong> / <strong>CC-BY</strong> (attribution).
        Checklist : <code>data/audio_ambiance_license_checklist.json</code>.
        Reste V2 : formats hors WAV, preview du mix complet, attribution visible côté auditeur.
      </td>
    </tr>
    <tr>
      <td><strong>Multi-langues — interface du site (chrome UI)</strong></td>
      <td><span class="lv-st-todo">À faire (plus tard)</span></td>
      <td>
        <strong>Décision actuelle :</strong> le contenu de l’application elle-même
        (navigation, libellés, pages À propos / compte / admin, messages UX)
        reste <strong>uniquement en français</strong> pour le moment.
        Les contenus liturgiques / synthèse / audio / PDF suivent déjà <code>pref_langue</code> ;
        le chrome UI, non.
        <strong>Chantier futur :</strong> i18n de l’interface selon la
        <code>pref_langue</code> du compte (session + feuille <code>users</code>) —
        catalogues de chaînes (FR pivot → DE / EN / ES / IT), bascule à la connexion,
        fallback FR si clé manquante. Hors scope immédiat : ne pas bloquer lectures / génération multi-langues.
      </td>
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
        L’UI «&nbsp;Planificateur d’envoi&nbsp;» définit déjà les campagnes et le mode manuel&nbsp;; il manque le worker planifié hors session Streamlit.
      </td>
    </tr>
    <tr>
      <td><strong>Captation des retours après mailing (mini-questionnaire)</strong></td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Page «&nbsp;Donner votre avis&nbsp;» (<code>?route=feedback</code>), table <code>experience_feedback</code> (<strong>RSTN</strong>), accès connecté ou lien <code>?email=</code>.
        Lien cliquable dans l’e-mail lorsque le template contient la phrase <em>👉 Donner mon avis sur cette expérience</em>.
        Admin <strong>Sondage synthèse</strong> : agrégat des réponses + IA Vertex → historique <code>feedback_insights</code> (<strong>FBIN</strong>), export Excel des bruts.
        Optionnel plus tard : paramètres d’URL enrichis dans les campagnes (campagne, dimanche ciblé) ou export terrain type Forms.
      </td>
    </tr>
    <tr>
      <td>Authentification — récupération « mot de passe oublié »</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Flux opérationnel : demande depuis la connexion &rarr; e-mail SMTP &rarr; lien
        <code>?route=reset_password&amp;email=&amp;token=</code> &rarr; saisie du nouveau mot de passe.
        Jetons append-only dans <code>password_resets</code> (<strong>PWRT</strong>, aligné <code>AliasTables</code>), durée limitée, PBKDF2 (<code>hash_password</code> / <code>verify_password</code>).
      </td>
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
      <td>Typologie biblique / section « Le Psaume » (<code>data/instructions_ia.md</code>)</td>
      <td><span class="lv-st-ok">En données</span></td>
      <td>Pilotage éditorial continu ; lectures FR via AELF ; autres langues uniquement via API natives validées (pas de traduction maison).</td>
    </tr>
    <tr>
      <td>Paramètres IA (Google Sheets, append-only) + secret sauce</td>
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
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Version jugée <strong>bonne pour le service</strong> : navigation &lt; 1024&nbsp;px (popover <code>Menu</code>), viewport, lectures liturgiques,
        mémos / clavier (<code>padding-bottom</code> + <code>:has(textarea:focus)</code>), simulateur admin pour recette.
        Référence : <strong>points chirurgicaux</strong> ci-dessous. Améliorations futures possibles : extraction CSS dédiée, micro-ajustements largeur « app », polish ponctuel des expanders.
      </td>
    </tr>
    <tr>
      <td><strong>Refactor codebase (maintenabilité)</strong></td>
      <td><span class="lv-st-todo">À faire</span></td>
      <td>
        Réduire <code>app.py</code> (8k+ lignes) à un shell (styles + navigation + routage) et extraire les pages et l’admin en modules dédiés.
        Proposition : <code>ui/pages/*</code> (about/sunday/newsletter/account/memo/feedback) + <code>ui/admin/*</code> (1 fichier par tuile),
        puis scinder progressivement les gros modules <code>core/*</code> / <code>channel/*</code> par domaine.
        Suivi : page admin <strong>Refactor (code)</strong> + <code>data/refactor_migration_progress.json</code>.
      </td>
    </tr>
    <tr>
      <td><strong>Vigilance &amp; Tests Automatisés (Recette Continue)</strong></td>
      <td><span class="lv-st-todo">À cadrer</span></td>
      <td>
        Nouvelle tuile admin <strong>Recette continue</strong> : cheminement de self-diagnostic du pod, sans tests lourds au chargement.
        Périmètre : secrets, connectivité Google Sheets / GCS, quotas IA, intégrité des tables Sheets et résolution des prompts vitaux AIP via
        <code>pick_effective_templates</code>. Première persistance prévue dans <code>admin_changelog</code> / <strong>ADLG</strong> ;
        table <strong>TST</strong> seulement si l’historique de scores devient nécessaire.
        Suivi : <code>data/continuous_reception_progress.json</code>.
      </td>
    </tr>
    <tr>
      <td><strong>Vigilance de granularité (Index gaussien)</strong> — Constitution JOPAI© V16.10</td>
      <td><span class="lv-st-ok">Livré</span></td>
      <td>
        Écran admin <strong>Radar — granularité</strong> (<code>?route=admin_granularity</code>) : moteur <code>core/system_audit.py</code>,
        UI <code>ui/admin/granularity_audit.py</code>, menu + routage. Histogramme + Gauss théorique (charte Turquoise <code>#0d9488</code> / Bleu pétrole <code>#0b2745</code>),
        seuil μ + 2σ sur le corps (<code>core/</code>, <code>ui/pages/</code>, <code>ui/admin/</code>), liste d’alertes « Risque de navigation cognitive ».
        Sert de boussole avant / pendant le découpage Phase D (<code>core_split</code>, cocher la case refactor <code>granularity_gauss_audit</code>).
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
      <td>IA — « Passerelle catéchèse » dans la synthèse + option PDF</td>
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
  <dd>Vigilance de granularité (Gauss) : <strong>livré</strong> — radar admin ; utiliser les alertes pour prioriser le découpage Phase D.</dd>
  <dd>Responsive : considéré livré pour le service ; affiner au fil des retours terrain si besoin.</dd>
  <dd>Admin : simulateur mobile livré ; compléter au besoin par Chrome / Edge mode appareil pour clavier réaliste.</dd>
  <dd>Stabiliser Vision sur le bon projet GCP et valider une analyse complète sans 403.</dd>
  <dd>Repasser sur le PDF mensuel et la couverture si tu veux un gabarit « fascicule » multi-pages.</dd>
  <dd>PWA : choix d’hébergement et socle technique pour exposer le manifest au navigateur.</dd>
  <dd>
    Multi-langues : EN Universalis (adapter + ToS) → DE/ES/IT (trouver API) →
    consommation <code>pref_langue</code> (dimanche / e-mail / TTS).
  </dd>
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
    plan_html = plan_html + plan_body
    m = re.search(r"(<tbody>)(.*?)(</tbody>)", plan_html, flags=re.I | re.S)
    if m:
        sort_col = str(st.session_state.get("plan_sort_col") or "")
        sort_dir = str(st.session_state.get("plan_sort_dir") or "asc")
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"
        rows = _parse_plan_rows(m.group(2))
        ordered = _sort_rows(
            rows,
            col=sort_col if sort_col in ("theme", "status", "notes") else "",  # type: ignore[arg-type]
            direction=sort_dir,  # type: ignore[arg-type]
        )
        new_tbody = "\n".join(r["tr"] for r in ordered)
        plan_html = plan_html[: m.start(2)] + "\n" + new_tbody + "\n" + plan_html[m.end(2) :]

    st.markdown(plan_html, unsafe_allow_html=True)
