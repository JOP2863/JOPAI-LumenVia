# Instructions IA — fallback local minimal (dépôt public)

Ce fichier ne doit **pas** contenir la matière complète des instructions IA.

- **Source de vérité (Partie A)** : Google Sheets `Paramètres_IA`, clé `instructions_base_md` (append-only, versionnée, datée).
- **Complément confidentiel (Partie B)** : `st.secrets["IA_SECRET_SAUCE_MD"]`.

Si tu lis ceci en local, c’est que `Paramètres_IA` n’est pas accessible ou pas encore initialisé.  
Dans ce cas, l’application doit fonctionner en **mode dégradé** (message d’erreur/indisponibilité) plutôt que de ré-embarquer le prompt complet dans le repo.
