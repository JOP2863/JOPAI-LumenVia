# Stratégie de migration et refactorisation — LumenVia

**Objectif du chantier** : réduire `app.py` (plus de huit mille lignes) à un **shell minimal** (styles + navigation + routage + garde session), puis extraire les écrans publics et l’administration en **modules dédiés**, sans régression sur le modèle de données Google Sheets (append-only).

**Lien avec le plan consolidé** : cette initiative correspond à la ligne *« Refactor codebase (maintenabilité) »*. La progression concrète est suivie via les cases à cocher de la page admin **Refactor (code)** et le fichier versionné `data/refactor_migration_progress.json`.

---

## 1. Structure cible (rappel)

| Zone | Rôle |
|------|------|
| `app.py` | Point d’entrée : routage, bootstrap CSS, validation de session minimale. |
| `core/` | Moteur : accès Sheets (`sheets_db`), GCS, auth, règles métier — **pas de widgets Streamlit**. |
| `ui/` | Châssis : styles globaux, composants réutilisables (overlay, footer), navigation. |
| `ui/pages/` ou `pages/` | Pages publiques **maigres** : uniquement composition UI + appels à des fonctions nommées du `core/`. |
| `ui/admin/` | Une tuile admin ≈ un fichier (emailing, scheduler, comptes, …). |
| `tools/` / `utils/` | Maintenance, pipelines, audits locaux. |

**Note** : dans ce dépôt il n’existe pas de dossier `channel/*` ; la mention du plan consolidé visait une généralisation — le découpage portera surtout sur `core/*` par domaine si nécessaire.

---

## 2. Trois garde-fous cardinaux (non négociables)

### 2.1 Page maigre (*Thin Page*)

Les fichiers sous `ui/pages/` (ou équivalent) **ne doivent pas** :

- appeler directement `fetch_records`, `append_immutable_row`, `build_gspread_client`, etc. ;
- manipuler des DataFrames ou listes de dicts « brutes » comme couche données ;
- contenir de la logique CRUD Sheets.

Ils **doivent** uniquement appeler des **fonctions nommées** du `core/` dont l’intention métier est claire (ex. préparer un bundle dimanche pour affichage). Si une page « grossit », descendre la logique dans `core/` derrière une façade stable.

### 2.2 Navigation cognitive du chantier

**Avant** chaque extraction significative d’un bloc hors de `app.py` :

1. Identifier la tâche dans la checklist admin (ou la ajouter si absente).
2. Marquer la tâche comme **en cours** via le sélecteur « Tâche en cours » sur la même page admin.
3. Ne passer la tâche en **cochée / terminée** qu’une fois le module extrait et mergé de façon stable.

Cela évite les extractions à moitié et les doublons de travail entre sessions.

### 2.3 Immuabilité Sheets (append-only)

Toute **nouvelle** fonction dans `core/` qui écrit en Sheets doit :

- respecter le protocole append-only / versions (**$N+1$**) ;
- respecter les colonnes obligatoires et le **`concat`** ;
- gérer correctement **`status`** et les lignes **actives** (`SHEETS_ROW_STATUS_*`, helpers `sheet_row_status_is_live`, etc.).

Aucune « simplification » ne doit introduire de mise à jour destructive déguisée.

---

## 3. Phases de migration (ordre recommandé)

### Phase A — Fondations UI

- Extraire styles globaux et injections (viewport, footer, thème) vers un module `ui/` dédié.
- Extraire `loading_overlay` et patterns chrome récurrents vers `ui/components.py` (ou équivalent).
- Extraire navigation (`top_nav`, barre admin, popover) vers `ui/navigation.py`.

**Critère de fin de phase** : `app.py` allégé sur ces blocs ; comportement visuel inchangé.

### Phase B — Pages publiques (une extraction à la fois)

Ordre suggéré (du plus simple au plus couplé) :

1. À propos  
2. Feedback / join / compte / reset password  
3. Mémo  
4. Dimanche (le plus volumineux — possibilité de sous-modules `core/` + squelette page maigre)
5. **QA régression admin sur Dimanche** (à faire avant de considérer l’extraction « Dimanche » comme définitivement validée) : avec un compte administrateur, sur une date de test, enchaîner au moins une fois **Compléter les manquants** et **Tout régénérer** (synthèse Vertex, audios, écritures Sheets/GCS ; quotas et réseau requis). Les smoke tests légers (navigation, lectures, PDF téléchargé) ne suffisent pas à couvrir ce flux.

### Phase C — Administration (une tuile à la fois)

Pour chaque `render_admin_*` dans `app.py`, créer un module sous `ui/admin/` et ne laisser dans `app.py` qu’un import + dispatch.

Prioriser les domaines les moins couplés aux autres pour valider le pattern (ex. plan consolidé / cahier si légers), puis emailing, scheduler, comptes, etc.

### Phase D — Core par domaine (optionnel, après stabilisation UI)

- **En premier** : vigilance de granularité (`granularity_gauss_audit`) — radar admin pour cadrer la suite (pas le contraire).
- **Ensuite** : regrouper ou scinder des fichiers `core/*` trop larges **par domaine métier** (liturgie, comptes, outbound), sans changer le contrat Sheets ; puis réduction de `app.py` au shell (`final_shell`).

---

## 4. Fichiers de suivi versionnés

| Fichier | Rôle |
|---------|------|
| `docs/admin/refactor_migration_strategy.md` | Ce document — stratégie et règles. |
| `data/refactor_migration_progress.json` | Cases cochées + id de la tâche « en cours » (suivi d’équipe via Git). |

---

## 5. Définition de « terminé » pour le chantier global

Le shell `app.py` se limite à configuration de page, routage, styles/navigation importés, et points d’entrée session ; les pages publiques et l’admin vivent dans `ui/pages/` et `ui/admin/` ; aucune page maigre n’appelle Sheets directement ; les nouveaux points d’accès données respectent l’immuabilité append-only. À ce moment-là, la ligne *Refactor codebase* du plan consolidé pourra passer à **Livré**. La **vigilance de granularité** (§7) est en **tête de Phase D** dans la checklist (`granularity_gauss_audit`) : boussole avant `core_split` et `final_shell`, cochable dès le radar admin livré.

---

## 6. Phase E — Auto-audit final (checklist admin `closure_audit_self_review`)

**Quand** : uniquement **après** stabilisation du refactor (shell `app.py`, extractions admin/pages, core clarifié selon les choix du chantier). Ne pas court-circuiter les étapes techniques pour répondre à ces questions : elles servent à **documenter la conformité** du résultat.

Chaque bloc ci-dessous doit faire l’objet d’une **réponse rédigée** (référence fichiers/fonctions si utile), sans checklist vide.

### 6.1 Audit « Page maigre » (*Thin Page*)

- Dans les nouveaux fichiers sous `ui/pages/`, prouver qu’il n’existe **plus aucun** appel direct à `fetch_records` ni manipulation de DataFrames ou dicts « bruts » comme couche données ; décrire comment la récupération des données liturgiques est **déléguée au `core/`**.

### 6.2 Vérification immuabilité Sheets ($N+1$)

- Pour le module Feedback extrait : confirmer que **toute écriture en base** utilise exclusivement `append_immutable_row` (ou équivalent append-only documenté).
- Décrire comment est assurée **l’exclusivité du statut « Actif »** lors d’une mise à jour de profil (ou équivalent métier).

### 6.3 Audit « Zéro-trace »

- Lors du découpage du monolithe, avoir identifié d’éventuels **e-mails réels** ou **secrets** (clés API) codés en dur ; confirmer qu’ils sont désormais **abstraits** via `st.secrets` ou libellés neutres.

### 6.4 Optimisation FinOps — page Dimanche

- Expliquer comment le mécanisme de **`source_hash`** dans le `core/` évite de régénérer inutilement des synthèses IA ou des fichiers audio lorsque la source AELF **n’a pas changé**.

### 6.5 Navigation cognitive

- Indiquer la **taille finale** (lignes ou ordre de grandeur) du fichier `app.py`.
- Signaler s’il reste une **fonction métier** dont la compréhension nécessiterait encore **plus de ~30 secondes** de lecture linéaire ; si oui, laquelle et pourquoi.

---

## 7. Vigilance de granularité — Index gaussien (Plan constitutionnel JOPAI© V16.10)

**Statut** : **livré** — checklist refactor (`granularity_gauss_audit`) ; entrée admin « Radar granularité », route `admin_granularity`.

**Objectif** : outil d’audit admin mesurant la **répartition du poids** du code (LOC) dans le pod pour contrôler la **navigation cognitive** (règle des ~30 secondes).

**Découpage respectant la page maigre** :

| Zone | Rôle |
|------|------|
| `core/system_audit.py` | Scan récursif, comptage LOC, agrégats, moyenne / écart-type sur le « Corps », seuils « hors-Gauss », données pour courbe théorique — **sans Streamlit**. |
| `ui/admin/granularity_audit.py` | Composition UI : histogramme, courbe Gauss superposée, liste d’alertes hypertrophie — couleurs charte Turquoise `#0d9488`, Bleu pétrole `#0b2745`. |
| `ui/navigation.py` + `app.py` | Menu admin et routage vers l’écran radar. |

**Périmètre de scan (spec)** : `app.py`, `core/`, `ui/`, `pages/` si présent, `tools/`, `utils/` ; exclusion `.git`, `__pycache__`, `.streamlit` ; fichiers `.py` ; traitement Markdown optionnel pour le CDC comme document hors Python.

**Modélisation** : classes constitutionnelles — **Sommet (léger)** : `app.py`, `ui/navigation.py` ; **Corps (médian)** : `ui/pages/`, `ui/admin/`, `core/` — moyenne et σ sur les tailles de fichiers du Corps ; **alerting** : modules dont la taille dépasse significativement la moyenne (ex. > 2σ) listés avec mention « Risque de navigation cognitive » et suggestion de découpage.

**Livrables** : case **Phase D** — `granularity_gauss_audit` (en tête de phase, avant `core_split`) ; ligne **plan consolidé** à jour **Livré**.
