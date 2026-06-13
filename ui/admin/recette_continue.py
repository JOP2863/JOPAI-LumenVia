"""Admin — chantier Recette continue : stratégie, cockpit et checklist."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROGRESS_PATH = _REPO_ROOT / "data" / "continuous_reception_progress.json"


@dataclass(frozen=True)
class RecetteStep:
    """Une étape de chantier, suivie manuellement depuis l'administration."""

    id: str
    label: str
    note: str = ""

    def label_with_note(self) -> str:
        if not (self.note or "").strip():
            return self.label
        return f"{self.label} — {self.note.strip()}"


RECETTE_PHASES: tuple[tuple[str, tuple[RecetteStep, ...]], ...] = (
    (
        "Phase 0 — Cadrage du protocole",
        (
            RecetteStep(
                "protocol_scope",
                "Définir le périmètre de santé du pod : secrets, Sheets, GCS, IA, intégrité Sheets et AIP",
            ),
            RecetteStep(
                "manual_first",
                "Garder les tests réseau déclenchés manuellement au départ",
                "pas d'alourdissement au démarrage Streamlit",
            ),
        ),
    ),
    (
        "Phase A — Smoke tests locaux",
        (
            RecetteStep("secrets_presence", "Vérifier la présence des secrets critiques"),
            RecetteStep("gsheet_connectivity", "Tester la connectivité Google Sheets et l'accès aux onglets vitaux"),
            RecetteStep("gcs_connectivity", "Tester la connectivité GCS et la structure du bucket"),
            RecetteStep("ia_quota_smoke", "Valider un smoke test IA court : Vertex texte et Gemini TTS si configuré"),
        ),
    ),
    (
        "Phase B — Intégrité Sheets / AIP",
        (
            RecetteStep(
                "sheets_active_duplicates",
                "Détecter les doublons Actif par clé métier",
                "priorité Paramètres_IA / AIP",
            ),
            RecetteStep(
                "aip_effective_templates",
                "Contrôler que pick_effective_templates renvoie un gagnant pour chaque clé vitale",
            ),
            RecetteStep(
                "append_only_trace",
                "Documenter les écarts sans update destructeur",
                "alignement Google Sheets append-only",
            ),
        ),
    ),
    (
        "Phase C — Moteur léger",
        (
            RecetteStep(
                "diagnostic_engine_contract",
                "Définir un résultat structuré : statut, score, durée, détails, recommandations",
            ),
            RecetteStep(
                "diagnostic_engine_extract",
                "Extraire les contrôles réutilisables depuis Test ressources vers utils/diagnostic_test.py",
            ),
            RecetteStep(
                "admin_trigger",
                "Brancher un bouton admin qui exécute le diagnostic sans recharger toute la page",
            ),
        ),
    ),
    (
        "Phase D — Persistance des résultats",
        (
            RecetteStep("adlg_first", "Journaliser les exécutions majeures dans admin_changelog / ADLG"),
            RecetteStep("tst_table_decision", "Décider si une table TST dédiée est nécessaire pour l'historique chiffré"),
            RecetteStep("health_score", "Stabiliser un score de santé lisible pour le pod"),
        ),
    ),
    (
        "Phase E — Heartbeat futur",
        (
            RecetteStep(
                "heartbeat_entrypoint",
                "Préparer un déclenchement heartbeat ou scheduler hors session utilisateur",
            ),
            RecetteStep(
                "heartbeat_idempotence",
                "Garantir l'idempotence et éviter les appels IA/GCS redondants",
            ),
        ),
    ),
)

_STRATEGY_MD = """
### Vigilance & Tests Automatisés (Recette Continue)

Chaque pod doit pouvoir répondre à une question simple : **puis-je servir le patrimoine LumenVia maintenant, sans surprise silencieuse ?**

Le chantier ne remplace pas la page `Test ressources`, qui reste l'atelier de dépannage manuel. Il ajoute un cheminement de recette continue : d'abord une checklist et un cockpit, puis un moteur léger réutilisable par l'administration et, plus tard, par un heartbeat.

#### Protocole cible

- **Autonomie de recette locale** : vérifier les secrets critiques, la connectivité Google Sheets, la connectivité GCS et un quota IA minimal sans lancer de génération lourde.
- **Intégrité Sheets** : détecter les doublons `Actif`, en priorité sur `Paramètres_IA` / `AIP`, pour préserver la règle d'exclusivité.
- **Résolution IA** : confirmer que `pick_effective_templates` produit une ligne gagnante pour chaque clé vitale de prompt.
- **Moteur lightweight** : encapsuler les contrôles dans `utils/diagnostic_test.py`, déclenchable à la demande puis par heartbeat.
- **Persistance** : commencer par une trace synthétique dans `admin_changelog` / `ADLG`, puis créer une table `TST` seulement si l'historique de scores devient un vrai besoin produit.
""".strip()


def _default_progress() -> dict:
    return {"version": 1, "completed_ids": [], "current_step_id": None}


def load_progress() -> dict:
    try:
        if not _PROGRESS_PATH.is_file():
            return _default_progress()
        raw = json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _default_progress()
        raw.setdefault("version", 1)
        raw.setdefault("completed_ids", [])
        if raw.get("completed_ids") is None:
            raw["completed_ids"] = []
        if "current_step_id" not in raw:
            raw["current_step_id"] = None
        return raw
    except Exception:
        return _default_progress()


def save_progress(data: dict) -> tuple[bool, str | None]:
    """Écrit le JSON sur disque ; retourne une erreur lisible si le serveur est read-only."""
    data = dict(data)
    data["completed_ids"] = sorted({str(x) for x in (data.get("completed_ids") or []) if str(x).strip()})
    cur = data.get("current_step_id")
    data["current_step_id"] = str(cur).strip() if cur else None
    try:
        _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROGRESS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True, None
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _all_step_ids() -> list[str]:
    return [step.id for _, steps in RECETTE_PHASES for step in steps]


@st.fragment
def _recette_controls_fragment() -> None:
    st.subheader("Étape active")
    prog = load_progress()
    all_ids = [""] + _all_step_ids()
    labels: dict[str, str] = {"": "— Aucune —"}
    for _, steps in RECETTE_PHASES:
        for step in steps:
            labels[step.id] = step.label_with_note()

    cur = prog.get("current_step_id") or ""
    if cur and cur not in labels:
        labels[cur] = cur
        all_ids.append(cur)

    try:
        idx = all_ids.index(cur) if cur in all_ids else 0
    except ValueError:
        idx = 0

    pick = st.selectbox(
        "Quelle étape de recette est active ?",
        options=all_ids,
        index=idx,
        format_func=lambda i: labels.get(i, i) if i else labels[""],
        key="recette_current_step_select",
    )
    if pick != (prog.get("current_step_id") or ""):
        prog["current_step_id"] = pick if pick else None
        ok, err = save_progress(prog)
        if ok:
            st.rerun(scope="fragment")
        else:
            st.error(f"Enregistrement impossible : {err}")

    st.divider()
    st.subheader("Cheminement")

    done_set = set(prog.get("completed_ids") or [])
    changed = False
    for phase_title, steps in RECETTE_PHASES:
        st.markdown(f"**{phase_title}**")
        for step in steps:
            was = step.id in done_set
            is_on = st.checkbox(step.label_with_note(), value=was, key=f"recette_chk_{step.id}")
            if is_on != was:
                changed = True
                if is_on:
                    done_set.add(step.id)
                else:
                    done_set.discard(step.id)
        st.markdown("")

    if changed:
        prog["completed_ids"] = sorted(done_set)
        ok, err = save_progress(prog)
        if ok:
            st.rerun(scope="fragment")
        else:
            st.error(
                f"La progression n'a pas pu être enregistrée sur le serveur : {err}. "
                "Les cases peuvent se réinitialiser au prochain chargement."
            )
            for k in list(st.session_state.keys()):
                if str(k).startswith("recette_chk_"):
                    del st.session_state[k]
            st.rerun(scope="fragment")

    st.divider()
    n_done = len(done_set)
    n_tot = len(_all_step_ids())
    st.progress(min(1.0, n_done / max(n_tot, 1)), text=f"Progression : {n_done} / {n_tot} étapes cochées")

    with st.expander("Réinitialiser la progression (local)", expanded=False):
        st.caption("Réinitialise uniquement le fichier JSON de suivi du chantier.")
        if st.button("Remettre à zéro les cases et l'étape active", type="secondary", key="recette_reset_progress"):
            ok, err = save_progress(_default_progress())
            if not ok:
                st.error(f"Réinitialisation impossible : {err}")
            else:
                st.session_state.pop("recette_current_step_select", None)
                for k in list(st.session_state.keys()):
                    if str(k).startswith("recette_chk_"):
                        del st.session_state[k]
                st.rerun(scope="fragment")


def render_admin_recette_continue() -> None:
    st.title("Recette continue — vigilance & tests automatisés")

    prog = load_progress()
    done = set(prog.get("completed_ids") or [])
    n_tot = len(_all_step_ids())
    n_done = len(done)
    cur = prog.get("current_step_id")

    if n_tot > 0 and n_done >= n_tot:
        st.success(
            "**Chantier recette continue : cheminement complet.** "
            f"Toutes les étapes sont cochées ({n_done} / {n_tot}). "
            "Le moteur de tests peut maintenant devenir un livrable technique autonome."
        )
    else:
        st.info(
            f"**Avancement checklist** : {n_done} / {n_tot} étapes terminées. "
            "Ce chantier prépare un protocole de self-diagnostic sans lancer de tests lourds au chargement de l'app."
        )

    if cur:
        st.caption(f"Étape sélectionnée comme active : `{cur}`.")
    else:
        st.caption("Aucune étape active pour le moment.")

    st.caption(
        "Progression locale : `data/continuous_reception_progress.json`. "
        "Les exécutions de tests réelles seront branchées dans un second temps, depuis un moteur léger dédié."
    )

    with st.expander("Document stratégique court", expanded=False):
        st.markdown(_STRATEGY_MD)

    st.divider()
    st.subheader("Cockpit cible")
    st.markdown(
        """
| Axe | État initial | Cible |
|---|---|---|
| Secrets / GCP | Diagnostic manuel dans `Test ressources` | Résultat structuré réutilisable |
| GSheet / GCS | Contrôles UI existants | Smoke test léger, déclenché à la demande |
| Sheets / AIP | Règles dispersées dans Sheets + prompts | Détection des doublons `Actif` et clés vitales manquantes |
| Persistance | `ADLG` disponible | Trace courte, puis `TST` si besoin d'historique |
| Heartbeat | Non présent | Déclencheur futur sans ralentir l'app |
        """.strip()
    )

    st.subheader("Ancrages techniques repérés")
    st.markdown(
        """
- **Diagnostics infra** : reprendre les primitives déjà utilisées par `ui/admin/test_resources.py` pour les secrets, GSheet et GCS.
- **Smoke IA** : conserver des appels courts et explicites, sans exécution automatique au chargement.
- **Sheets (append-only)** : contrôler les statuts via `sheet_row_status_is_live` pour respecter les formes historiques d'`Actif` / `Inactif`.
- **AIP** : comparer `PROMPT_TEMPLATE_KEYS` avec le résultat de `pick_effective_templates` afin de détecter une clé vitale sans gagnant.
- **Persistance** : utiliser `admin_changelog` / `ADLG` pour la trace synthétique ; réserver `TST` à une phase ultérieure si les scores doivent être historisés.
        """.strip()
    )

    st.divider()
    _recette_controls_fragment()
