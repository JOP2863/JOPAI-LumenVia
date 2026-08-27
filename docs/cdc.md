# JOPAI-LumenVia — Cahier des charges

| Champ | Valeur |
|-------|--------|
| **pod_id** | `JOPAI-LumenVia` |
| **Profil** | `local-first` |
| **Famille** | Arts, Culture et Patrimoine |
| **Génome** | `3.1.0` |
| **Runtime cible** | Node/TypeScript · PostgreSQL · objet S3 |
| **Généré par** | JOPAI-NEXUS incubateur |
| **Date** | 2026-08-26 |

## 1. Expression du besoin

_À compléter._

## 2. Cible utilisateur

À définir

## 3. Trajectoire

migration
**Mode migration** : le pack oriente l'agent du **dépôt fils** vers la mise en conformité (Node/TypeScript + PostgreSQL + S3). NEXUS ne génère pas le code. Voir `PROMPT_CURSOR.md` § Mode migration.

## 4. Périmètre MVP

Premier jet **produit** : serveur TypeScript, couches UI / métier / persistance séparées
(« Architecture applicative (UI, métier, persistance) »), PostgreSQL + schéma versionné, accès objet S3
(« Cible d'infrastructure souveraine »). UI opérateur = nom + action (pas d'URL techniques).

Streamlit / Google Sheets = atelier de maquetage uniquement (« Atelier de maquetage — Streamlit et Google Sheets ») —
pas la vérité métier de production.

## 5. Marqueurs engagés

Voir `docs/arbitrages.yaml` — engagements figés à l'initialisation (§ 2.9 fiche d'écart).

## 6. Utilitaires retenus

_Aucun pour l'instant._
