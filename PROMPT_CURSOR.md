# PROMPT_CURSOR — JOPAI-LumenVia

## Contexte
- POD **JOPAI-LumenVia** · profil **local-first**
- Génome **3.1.0** — loi machine : `../JOPAI/genome-dist/v3.1.0/genome_markers.yaml`
- Pack produit par l'incubateur NEXUS (« L'Incubateur de POD ») — **NEXUS ne génère pas le code** ;
  c'est l'agent Cursor du **dépôt fils** qui implémente.
- Cible runtime produit : **Node/TypeScript + PostgreSQL + objet S3**
  (« Architecture applicative (UI, métier, persistance) », « Cible d'infrastructure souveraine »).
- Étalon d'arborescence (sans copier le métier) : `JOPAI-Yachting`
  (`package.json`, `src/server/index.ts`, `src/ui`, `src/domain`, `src/data`).
- Streamlit / Google Sheets = NEXUS, Constitution, maquetage (« Atelier de maquetage — Streamlit et Google Sheets ») seulement.

## Marqueurs MARQ-* retenus
- MARQ-APP-ARCH
- MARQ-APP-FOUND
- MARQ-APP-PLAN
- MARQ-APP-REQ
- MARQ-APP-UX
- MARQ-DATA-NAMING
- MARQ-DATA-SOCLE
- MARQ-APP-DOC-MAINT
- MARQ-APP-ISGC
- MARQ-APP-MEDIA
- MARQ-APP-MOBILE
- MARQ-APP-OBS
- MARQ-SEC-CARTO
- MARQ-SEC-CICD
- MARQ-SEC-DNS
- MARQ-SEC-INFRA
- MARQ-SEC-MAIL

## Utilitaires à intégrer (ne pas dupliquer le catalogue NEXUS)
- _Aucun_

## Livrables attendus
- `package.json` + lockfile Node (npm/pnpm/yarn)
- Point d'entrée serveur TypeScript (ex. `src/server/index.ts`)
- Couches séparées : UI (`src/ui`) · métier / domaine (`src/domain`) · persistance (`src/data`)
- Schéma SQL PostgreSQL versionné (ex. `docs/schema_postgresql.sql`)
- Accès objet S3 (adapter stockage — secrets hors Git)
- `docs/cdc.md` · `docs/arbitrages.yaml` · `docs/avancement.yaml`
- `docs/galaxie_manifest.yaml` — carte d'identité galaxie (§ 5.1.8)
- `.cursor/rules/lumenvia-core.mdc`

## Interdictions
- Ne pas implémenter l'intégralité du génome
- Pas de dépendance runtime vers JOPAI-NEXUS
- INTERDIT de modifier le dépôt JOPAI-NEXUS (registre galaxie)
- Zéro secret dans Git
- **Streamlit** / **Google Sheets** / **GCS** / **JSON fichier** comme vérité métier de **production**
- **Streamlit Cloud** comme hébergement produit
- UI opérateur avec URL techniques visibles (MARQ-APP-UX : nom + action)

## Mode migration — mise en conformité du dépôt existant
- Auditer le dépôt **existant** face aux marqueurs **retenus** dans `docs/arbitrages.yaml`
- Viser la bascule **Node/TypeScript + PostgreSQL + objet S3** (pas « ajouter des pages Streamlit »)
- Streamlit / Sheets peuvent rester **maquette** (MARQ-APP-MAQ) jusqu'à bascule runtime
- **INTERDIT** de classer IMPLÉMENTÉ sans preuve (fichier + surface consultable si le marqueur l'exige — MARQ-APP-ECART)
- L'agent qui code est celui du **dépôt fils**, pas NEXUS
- Documenter les écarts dans `docs/avancement.yaml` et régénérer la fiche d'écart
