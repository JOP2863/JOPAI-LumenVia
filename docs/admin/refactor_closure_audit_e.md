# Phase E — Auto-audit de clôture (chantier refactor LumenVia)

**Référence** : `docs/admin/refactor_migration_strategy.md` §6.  
**Date de rédaction** : mai 2026.  
**État du dépôt** : snapshot au moment du passage à la Phase E (après livraison du shell `app.py` et extraction `core/` / `ui/`).

---

## 6.1 Audit « Page maigre » (*Thin Page*)

**Objectif constitutionnel** : les fichiers sous `ui/pages/` ne devraient pas constituer une couche données « brute » (pas d’appels directs à `fetch_records`, pas de manipulation de DataFrames comme API métier).

**Constat actuel (honnête)** : plusieurs pages appellent encore `fetch_records` pour des flux interactifs (liste mémo, jointures compte / abonnements, cache lectures optionnel sur Dimanche). Exemples repérés :

- `ui/pages/memo.py` — lectures `memos` / `generations` pour l’historique et les formulaires.
- `ui/pages/join_account.py` — lectures `users`, `subscriptions`, `password_resets` pour inscription, compte et réinitialisation ; les **écritures** passent par `append_immutable_row` avec logique MARPA.
- `ui/pages/sunday.py` — lecture optionnelle de `readings_cache` pour affichage / perf.

**Ce qui est bien délégué au `core/` pour la liturgie dominicale** : récupération et normalisation AELF (via `cached_aelf` / caches Streamlit), préparation TTS, PDF, fetch GCS / agrégats Sheets pour médias déjà produits — désormais dans `core/sunday_existing_outputs.py`, `core/sunday_readings_tts.py`, `core/synthesis_vertex_prompt.py`, etc., avec `app.py` comme façade `import app as ap` pour compatibilité.

**Conclusion** : la **direction** du refactor (logique lourde hors pages) est alignée pour le dimanche et les pipelines ; il reste une **dette maigre** sur quelques pages encore couplées à Sheets pour des écrans CRUD/listes. Piste : introduire des façades `core/memo_service.py`, `core/account_queries.py`, etc., puis retirer `fetch_records` des pages.

---

## 6.2 Vérification MARPA ($N+1$)

**Feedback** (`ui/pages/feedback.py`) : les envois d’expérience utilisateur passent par `append_immutable_row` vers la table prévue (pas de mise à jour destructive sur une ligne existante pour créer une nouvelle réponse).

**Profil / compte** (`ui/pages/join_account.py`) : les mises à jour de profil suivent le modèle append-only documenté — avant une nouvelle ligne « vivante », les lignes `users` précédentes encore marquées actives sont **marquées inactives** via mise à jour de cellule `status` sur la ligne historique, puis **nouvelle ligne** avec `append_immutable_row` et `concat` / colonnes obligatoires ; réutilisation de `sheet_row_status_is_live`, `SHEETS_ROW_STATUS_INACTIVE`, `compute_concat` (voir commentaires et blocs autour des inscriptions et « Mon compte »).

**Exclusivité du statut « Actif »** : la logique « une seule ligne active » pour l’utilisateur repose sur le filtrage `sheet_row_status_is_live` à la lecture et sur l’inactivation explicite des anciennes lignes avant append de la nouvelle version (pattern MARPA du projet).

---

## 6.3 Audit « Zéro-trace »

**Secrets et clés** : configuration runtime via `st.secrets` / variables d’environnement et clients GCP dérivés du JSON compte de service ; pas de clés API type Google AI (`AIza…`) ni jetons OpenAI codés en dur dans le dépôt Python (contrôle par recherche sur motifs type clés).

**E-mails** : les gabarits et liens utilisent des placeholders ou des constructions à partir de l’origine app (`lumenvia_app_origin_url`, `build_feedback_survey_url`) — pas d’adresses personnelles codées en dur comme destinataires de prod dans les modules refactorés.

**Limite** : tout dépôt peut contenir des exemples dans `docs/` ou des données de test ; l’audit continu doit rester une revue avant release.

---

## 6.4 Optimisation FinOps — page Dimanche

**Mécanisme `source_hash`** : lors d’une génération complète (voir `ui/sunday_admin_flows.py`), un hash SHA-256 est calculé à partir de la date et des textes canoniques des lectures AELF (première lecture, psaume, évangile — chaîne concaténée). Ce `source_hash` est **persisté** dans la ligne `generations` (colonne prévue au schéma, voir usage dans `append_immutable_row` avec `"source_hash": source_hash`).

**Effet FinOps** : l’identifiant de génération `gen_entity_id` dérive également de `(date, zone, source_hash)`, ce qui **aligne** une même « version » de contenu source avec une entité de génération ; combiné aux caches locaux (`core/local_bundle_cache.py`, snapshots AELF) et aux chemins GCS versionnés par génération, on évite de traiter comme nouvelle prod une sortie identique à une source inchangée lorsque les flux réutilisent ces identifiants et fichiers déjà présents.

*(Si une régénération forcée est demandée explicitement par l’admin, les coûts Vertex/TTS peuvent se reproduire : c’est un choix opérationnel, pas une défaillance du hash.)*

---

## 6.5 Navigation cognitive

**Taille de `app.py`** : ordre de grandeur **~220 lignes** (shell : imports de façade, URL publiques/feedback, injection CSS aperçu téléphone, lecture query params, `top_nav`, `dispatch_route`).

**Fonctions encore « lourdes » à la lecture (plus de ~30 secondes si on suit tout un flux)** : le shell lui-même est court ; la complexité résiduelle est dans les **modules volumineux** (`ui/sunday_admin_flows.py`, certaines pages Dimanche / admin), pas dans `app.py`. Le **radar de granularité** (`ui/admin/granularity_audit.py`, `core/system_audit.py`) sert de filet pour prioriser les prochains découpes.

---

## Suite

- **Checklist** : l’étape **`closure_audit_self_review`** est cochée ; le chantier refactor tel que défini dans la stratégie est **clôturé** au niveau du suivi (`data/refactor_migration_progress.json`, `current_step_id` vide).
- **Amélioration continue** : la dette « page maigre » résiduelle (façades `core/` pour requêtes encore dans `ui/pages/`) peut faire l’objet de **chantiers séparés** si vous souhaitez pousser plus loin la séparation UI / données.
