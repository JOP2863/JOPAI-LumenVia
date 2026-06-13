"""Page admin : stratégie de refactor + checklist persistante (fichier JSON)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

_PROGRESS_PATH = Path(__file__).resolve().parent.parent / "data" / "refactor_migration_progress.json"
_STRATEGY_MD = Path(__file__).resolve().parent.parent / "docs" / "admin" / "refactor_migration_strategy.md"
_CLOSURE_AUDIT_E_MD = Path(__file__).resolve().parent.parent / "docs" / "admin" / "refactor_closure_audit_e.md"


@dataclass(frozen=True)
class RefactorStep:
    """app_py_lines_hint : ordre de grandeur des lignes retirées de app.py pour l’étape (suivi manuel / mesure locale)."""

    id: str
    label: str
    app_py_lines_hint: str = ""

    def label_with_lines(self) -> str:
        if not (self.app_py_lines_hint or "").strip():
            return self.label
        return f"{self.label} — ≈{self.app_py_lines_hint.strip()} lignes sorties de app.py"


REFACTOR_PHASES: tuple[tuple[str, tuple[RefactorStep, ...]], ...] = (
    (
        "Phase 0 — Cadrage",
        (
            RefactorStep("doc_strategy", "Stratégie documentée + page admin checklist (ce chantier)", "0"),
            RefactorStep(
                "plan_before_extract",
                "Process : avant chaque extraction, marquer la tâche « en cours » dans l’admin",
                "0",
            ),
        ),
    ),
    (
        "Phase A — Shell UI",
        (
            RefactorStep("ui_styles", "Extraire styles globaux / injections (viewport, thème) → module ui/", "730"),
            RefactorStep("ui_components", "Extraire loading_overlay → ui/components.py", "18"),
            RefactorStep("ui_navigation", "Extraire top_nav + barre admin → ui/navigation.py", "368"),
        ),
    ),
    (
        "Phase B — Pages publiques (thin)",
        (
            RefactorStep("page_about", "ui/pages — À propos", "52"),
            RefactorStep("page_feedback_join", "ui/pages — Feedback, inscription, compte, reset MDP", "1080"),
            RefactorStep("page_memo", "ui/pages — Mémo", "~308"),
            RefactorStep("page_sunday", "ui/pages — Dimanche (+ facades core si besoin)", "~1590"),
            RefactorStep(
                "qa_sunday_admin_flows",
                "QA admin — Dimanche : « Compléter les manquants » + « Tout régénérer » (Vertex, audios, Sheets/GCS ; réseau)",
                "0",
            ),
        ),
    ),
    (
        "Phase C — Admin (1 tuile ≈ 1 fichier)",
        (
            RefactorStep("adm_plan", "ui/admin/plan_consolide.py — Plan consolidé", "~261"),
            RefactorStep("adm_cdc", "ui/admin/cahier_charges.py — Cahier des charges", "~83"),
            RefactorStep(
                "adm_mobile_sim",
                "ui/admin/mobile_simulator.py — Simulateur mobile (`lumenvia_app_origin_url` dans ui/navigation)",
                "~110",
            ),
            RefactorStep(
                "adm_test_resources",
                "ui/admin/test_resources.py — Test ressources & diagnostic (prompts, voix, smoke IA)",
                "~707",
            ),
            RefactorStep(
                "adm_liturgy",
                "ui/admin — illustration_vertex.py, thumbs.py, vision_text.py, readings_cache.py (Step3, vignettes, Vision, cache AELF)",
                "~1606",
            ),
            RefactorStep("adm_users_comms", "ui/admin — Comptes, emailing, sondage, planificateur", "~2840"),
        ),
    ),
    (
        "Phase D — Core par domaine",
        (
            RefactorStep(
                "granularity_gauss_audit",
                "Vigilance de granularité (Index gaussien) : core/system_audit.py (scan LOC + stats) + ui/admin/granularity_audit.py (histogramme Gauss, alerting hors-nuage) + entrée nav admin — Constitution JOPAI V16.10",
                "0",
            ),
            RefactorStep("core_split", "Scinder ou regrouper core/* par domaine métier (sans casser l'immuabilité Sheets)", ""),
            RefactorStep("final_shell", "app.py réduit au shell (routage + imports) ; validation finale", ""),
        ),
    ),
    (
        "Phase E — Clôture chantier (auto-audit)",
        (
            RefactorStep(
                "closure_audit_self_review",
                "Réponses écrites aux critères d’audit final (Thin Page, immuabilité Sheets, Zéro-trace, FinOps Dimanche, navigation cognitive) — voir stratégie §6",
                "0",
            ),
        ),
    ),
)


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
    """Écrit le JSON sur disque. Sur hébergement read-only (ex. certains clouds), retourne (False, message)."""
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
    out: list[str] = []
    for _, steps in REFACTOR_PHASES:
        for s in steps:
            out.append(s.id)
    return out


@st.fragment
def _refactor_controls_fragment() -> None:
    """Checklist + sélecteur : fragment seul se ré-exécute au clic (pas tout app.py / pas relecture du MD stratégie)."""
    st.subheader("Tâche en cours (navigation cognitive)")
    prog = load_progress()
    all_ids = [""] + _all_step_ids()
    labels: dict[str, str] = {"": "— Aucune —"}
    for _, steps in REFACTOR_PHASES:
        for s in steps:
            labels[s.id] = s.label_with_lines()

    cur = prog.get("current_step_id") or ""
    if cur and cur not in labels:
        labels[cur] = cur
        all_ids.append(cur)

    try:
        idx = all_ids.index(cur) if cur in all_ids else 0
    except ValueError:
        idx = 0
    pick = st.selectbox(
        "Quelle étape est active avant la prochaine extraction / merge ?",
        options=all_ids,
        index=idx,
        format_func=lambda i: labels.get(i, i) if i else labels[""],
        key="refactor_current_step_select",
    )
    if pick != (prog.get("current_step_id") or ""):
        prog["current_step_id"] = pick if pick else None
        ok, err = save_progress(prog)
        if ok:
            st.rerun(scope="fragment")
        else:
            st.error(f"Enregistrement impossible : {err}")

    st.divider()
    st.subheader("Checklist")

    done_set = set(prog.get("completed_ids") or [])
    changed = False
    for phase_title, steps in REFACTOR_PHASES:
        st.markdown(f"**{phase_title}**")
        for step in steps:
            was = step.id in done_set
            is_on = st.checkbox(step.label_with_lines(), value=was, key=f"refactor_chk_{step.id}")
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
                f"La checklist n’a pas pu être enregistrée sur le serveur : {err}. "
                "Les cases peuvent se réinitialiser au prochain chargement. "
                "Testez en local ou vérifiez que `data/` est inscriptible."
            )
            for k in list(st.session_state.keys()):
                if str(k).startswith("refactor_chk_"):
                    del st.session_state[k]
            st.rerun(scope="fragment")

    st.divider()
    n_done = len(done_set)
    n_tot = len(_all_step_ids())
    st.progress(min(1.0, n_done / max(n_tot, 1)), text=f"Progression : {n_done} / {n_tot} étapes cochées")

    with st.expander("Réinitialiser la progression (local)", expanded=False):
        st.caption("Réinitialise le fichier JSON — usage ponctuel seulement.")
        if st.button("Remettre à zéro les cases et la tâche en cours", type="secondary"):
            ok, err = save_progress(_default_progress())
            if not ok:
                st.error(f"Réinitialisation impossible : {err}")
            else:
                st.session_state.pop("refactor_current_step_select", None)
                for k in list(st.session_state.keys()):
                    if str(k).startswith("refactor_chk_"):
                        del st.session_state[k]
                st.rerun(scope="fragment")


def render_admin_refactor_migration() -> None:
    st.title("Refactor codebase — stratégie & suivi")

    prog0 = load_progress()
    done0 = set(prog0.get("completed_ids") or [])
    n_tot0 = len(_all_step_ids())
    n_done0 = len(done0)
    cur0 = prog0.get("current_step_id")

    if n_tot0 > 0 and n_done0 >= n_tot0:
        st.success(
            "**Chantier refactor : clôturé.** Toutes les étapes de la checklist sont cochées "
            f"({n_done0} / {n_tot0}). Le fichier `data/refactor_migration_progress.json` en est la trace ; "
            "`app.py` joue le rôle de shell, le métier est dans `core/` et `ui/`. "
            "L’audit Phase E est dans `docs/admin/refactor_closure_audit_e.md`."
        )
    else:
        st.info(
            f"**Avancement checklist** : {n_done0} / {n_tot0} étapes terminées. "
            "Chaque case correspond à une livraison du chantier (extraire une zone, valider un QA, etc.). "
            "Le sélecteur « Tâche en cours » sert uniquement à **signaler** sur quoi vous travaillez avant le prochain merge ; "
            "il ne bloque rien dans l’application."
        )

    if cur0:
        st.caption(f"Tâche sélectionnée comme « en cours » : `{cur0}`.")
    else:
        st.caption(
            "Aucune tâche « en cours » (valeur normale une fois le chantier terminé, ou entre deux sessions)."
        )

    st.caption(
        "Les réglages sont versionnés dans `data/refactor_migration_progress.json`. "
        "Si le serveur ne peut pas écrire ce fichier (permissions, image sans volume persistant), une erreur s’affiche à la sauvegarde : "
        "utiliser un clone local ou corriger le déploiement. "
        "Les mentions **≈N lignes** sur les étapes sont un ordre de grandeur au moment du merge."
    )

    if _STRATEGY_MD.is_file():
        with st.expander("📄 Document stratégique (Markdown)", expanded=False):
            st.markdown(_STRATEGY_MD.read_text(encoding="utf-8"))
    else:
        st.warning("Fichier stratégie introuvable : `docs/admin/refactor_migration_strategy.md`")

    if _CLOSURE_AUDIT_E_MD.is_file():
        with st.expander("✅ Phase E — Auto-audit de clôture (réponses §6)", expanded=False):
            st.caption("Livrable versionné : `docs/admin/refactor_closure_audit_e.md`")
            st.markdown(_CLOSURE_AUDIT_E_MD.read_text(encoding="utf-8"))
    else:
        st.info(
            "Phase E : rédiger les réponses §6 dans `docs/admin/refactor_closure_audit_e.md` "
            "(voir stratégie `refactor_migration_strategy.md`)."
        )

    st.divider()
    _refactor_controls_fragment()
