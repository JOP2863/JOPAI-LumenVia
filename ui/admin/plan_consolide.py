"""Admin — Plan consolidé (HTML statique)."""

from __future__ import annotations

import streamlit as st


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
      <td>Pilotage éditorial continu ; pas de sources hors AELF.</td>
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
