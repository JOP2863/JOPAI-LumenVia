"""Admin temporaire — suivi chantier multi-langues (lectures API, pas de traduction maison)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from core.liturgy_sources_registry import LANG_PRIORITY

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROGRESS_PATH = _REPO_ROOT / "data" / "multilang_progress.json"
_UNIVERSALIS_LICENSE_PATH = _REPO_ROOT / "data" / "universalis_license_checklist.json"


@dataclass(frozen=True)
class MultilangStep:
    id: str
    label: str
    note: str = ""

    def label_with_note(self) -> str:
        if not (self.note or "").strip():
            return self.label
        return f"{self.label} — {self.note.strip()}"


MULTILANG_PHASES: tuple[tuple[str, tuple[MultilangStep, ...]], ...] = (
    (
        "Phase 0 — Règles produit",
        (
            MultilangStep(
                "rule_no_house_translation",
                "Interdiction de traduction maison des lectures",
                "uniquement API qui livrent les textes complets dans la langue",
            ),
            MultilangStep(
                "rule_full_mass_only",
                "Exclure évangile-seul / calendrier-seul (Romcal, Evangeli.net, etc.)",
            ),
            MultilangStep(
                "lang_priority_fr_de_en_es_it",
                f"Priorité langues figée : {' → '.join(LANG_PRIORITY)}",
            ),
            MultilangStep(
                "pref_langue_users",
                "Users : colonne pref_langue (ISO majuscules) + table LGP",
                "déjà en place",
            ),
        ),
    ),
    (
        "Phase 1 — Lab & validation API",
        (
            MultilangStep(
                "lab_page",
                "Page admin Lab lectures multi-langues",
                "semaine × sources × langues",
            ),
            MultilangStep(
                "validate_aelf_fr",
                "Confirmer AELF FR = production (messe complète)",
            ),
            MultilangStep(
                "spike_de",
                "Valider au moins une API DE (textes complets + licence)",
                "Evangelizo Reader lang=DE — ToS encore à checklist",
            ),
            MultilangStep(
                "spike_en",
                "Valider au moins une API EN (Universalis / USCCB / autre)",
                "Universalis + Evangelizo AM",
            ),
            MultilangStep(
                "spike_es",
                "Valider au moins une API ES messe complète",
                "Evangelizo Reader lang=SP (pas ES)",
            ),
            MultilangStep(
                "spike_it",
                "Valider au moins une API IT messe complète",
                "Evangelizo Reader lang=IT",
            ),
            MultilangStep(
                "license_checklist",
                "Checklist licence / ToS par source retenue (web, e-mail, TTS, PDF)",
                "Universalis fait ; Evangelizo ToS à faire",
            ),
        ),
    ),
    (
        "Phase 2 — Adapters & stockage",
        (
            MultilangStep(
                "adapter_contract",
                "Contrat commun LiturgyDayTexts (lecture1, psaume, lecture2?, évangile, infos)",
            ),
            MultilangStep(
                "adapter_first_non_fr",
                "Premier adapter non-FR branché (DE ou EN selon validation)",
            ),
            MultilangStep(
                "gcs_lang_paths",
                "Écriture génération admin vers Syntheses/{LANG}/, Audio/{LANG}/, …",
                "helpers content_locale_paths déjà prêts",
            ),
            MultilangStep(
                "readings_cache_lang",
                "Cache lectures AELF / multi-sources indexé par langue",
            ),
        ),
    ),
    (
        "Phase 3 — Produit utilisateur",
        (
            MultilangStep(
                "sunday_pref_langue",
                "Lumière du dimanche : servir les assets selon pref_langue",
            ),
            MultilangStep(
                "email_pref_langue",
                "E-mails / templates : langue du destinataire (pas FR forcé)",
            ),
            MultilangStep(
                "synth_tts_lang",
                "Synthèses + TTS générés dans la langue des lectures sources",
            ),
            MultilangStep(
                "fallback_fr",
                "Fallback explicite FR si langue non couverte (message utilisateur)",
            ),
            MultilangStep(
                "site_ui_i18n",
                "Interface du site (chrome UI) selon pref_langue — plus tard ; pour l’instant FR seul",
            ),
        ),
    ),
    (
        "Phase 4 — Clôture chantier temporaire",
        (
            MultilangStep(
                "registry_production_flags",
                "Passer les sources validées en status=production dans le registre",
            ),
            MultilangStep(
                "retire_temp_page",
                "Retirer ou archiver cette page de suivi une fois le chantier stabilisé",
            ),
            MultilangStep(
                "plan_consolide_sync",
                "Aligner le plan consolidé (statut Partiel → Livré par langue)",
            ),
        ),
    ),
)

_STRATEGY_MD = """
### Multi-langues LumenVia — règle d’or

**Pas de traduction maison** des lectures bibliques / lectionnaire.  
Chaque langue repose sur une **API (ou feed officiel) qui renvoie déjà les textes complets** de la messe du jour dans cette langue.

#### Accepté
- Lecture 1 + psaume + (lecture 2 si dimanche/fête) + évangile **en texte intégral**
- Informations du jour (titre, couleur) si fournies par la même source

#### Refusé
- Traduction IA / humaine à partir de l’AELF
- Sources « évangile du jour » seules
- Calendriers / références seules (Romcal, calapi, …) comme source de textes

#### Priorité langues
""" + " → ".join(LANG_PRIORITY) + """

#### Itérations
0. Lab admin (cette page + page Lab)  
1. Spikes HTTP / licences  
2. Adapters + GCS `{LANG}/`  
3. UI / e-mail / TTS selon `pref_langue`  
4. Promotion `production` + retrait page temporaire
"""


def _all_step_ids() -> list[str]:
    out: list[str] = []
    for _, steps in MULTILANG_PHASES:
        for s in steps:
            out.append(s.id)
    return out


def _default_progress() -> dict:
    # Pré-cocher ce qui est déjà livré côté socle
    return {
        "completed_ids": [
            "rule_no_house_translation",
            "rule_full_mass_only",
            "lang_priority_fr_de_en_es_it",
            "pref_langue_users",
            "lab_page",
            "gcs_lang_paths",
            "validate_aelf_fr",
            "spike_en",
            "adapter_contract",
            "adapter_first_non_fr",
            "license_checklist",
        ],
        "current_step_id": "sunday_pref_langue",
        "notes": (
            "Licence Universalis traitée (checklist JSON) : prod e-mail/TTS/PDF bloqués "
            "sans accord écrit. Prochaine étape : affichage EN conditionnel ou contact éditeur."
        ),
    }


def load_progress() -> dict:
    try:
        if _PROGRESS_PATH.is_file():
            data = json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("completed_ids", [])
                data.setdefault("current_step_id", "")
                data.setdefault("notes", "")
                return data
    except Exception:
        pass
    return _default_progress()


def save_progress(prog: dict) -> tuple[bool, str]:
    try:
        _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROGRESS_PATH.write_text(
            json.dumps(prog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True, ""
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


@st.fragment
def _multilang_controls_fragment() -> None:
    prog = load_progress()
    ids = _all_step_ids()
    cur = str(prog.get("current_step_id") or "")
    options = ["(aucune)"] + ids
    try:
        idx = options.index(cur) if cur in options else 0
    except Exception:
        idx = 0
    sel = st.selectbox(
        "Étape active (suivi)",
        options=options,
        index=idx,
        key="multilang_current_step_select",
    )
    new_cur = "" if sel == "(aucune)" else sel
    if new_cur != cur:
        prog["current_step_id"] = new_cur
        ok, err = save_progress(prog)
        if ok:
            st.rerun(scope="fragment")
        else:
            st.error(f"Enregistrement impossible : {err}")

    notes = st.text_area(
        "Notes de chantier (locales)",
        value=str(prog.get("notes") or ""),
        height=100,
        key="multilang_notes_area",
    )
    if st.button("Enregistrer les notes", type="secondary", key="multilang_save_notes"):
        prog["notes"] = notes
        ok, err = save_progress(prog)
        if ok:
            st.success("Notes enregistrées.")
        else:
            st.error(err)

    st.divider()
    st.subheader("Cheminement")
    done_set = set(prog.get("completed_ids") or [])
    changed = False
    for phase_title, steps in MULTILANG_PHASES:
        st.markdown(f"**{phase_title}**")
        for step in steps:
            was = step.id in done_set
            is_on = st.checkbox(step.label_with_note(), value=was, key=f"multilang_chk_{step.id}")
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
            st.error(f"Progression non enregistrée : {err}")
            for k in list(st.session_state.keys()):
                if str(k).startswith("multilang_chk_"):
                    del st.session_state[k]
            st.rerun(scope="fragment")

    n_done = len(done_set)
    n_tot = len(ids)
    st.progress(min(1.0, n_done / max(n_tot, 1)), text=f"Progression : {n_done} / {n_tot}")

    with st.expander("Réinitialiser (local)", expanded=False):
        if st.button("Remettre la checklist aux valeurs par défaut", key="multilang_reset"):
            ok, err = save_progress(_default_progress())
            if not ok:
                st.error(err)
            else:
                for k in list(st.session_state.keys()):
                    if str(k).startswith("multilang_chk_") or str(k).startswith("multilang_"):
                        del st.session_state[k]
                st.rerun(scope="fragment")


def load_universalis_license() -> dict:
    try:
        if _UNIVERSALIS_LICENSE_PATH.is_file():
            data = json.loads(_UNIVERSALIS_LICENSE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _render_universalis_license_panel() -> None:
    data = load_universalis_license()
    if not data:
        st.warning("Checklist licence Universalis introuvable (`data/universalis_license_checklist.json`).")
        return

    st.subheader("Licence Universalis — checklist")
    gate = "fermée" if not data.get("production_gate_open") else "ouverte"
    st.caption(
        f"Revue {data.get('reviewed_at') or '—'} · gate production **{gate}** · "
        f"{data.get('summary_fr') or ''}"
    )

    rows_md = ["| Canal / item | Verdict | Notes |", "|---|---|---|"]
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        rows_md.append(
            f"| {it.get('label', it.get('id', ''))} | `{it.get('verdict', '')}` | "
            f"{(it.get('notes') or '').replace('|', '/')} |"
        )
    st.markdown("\n".join(rows_md))

    policy = data.get("lumenvia_policy") or {}
    until = policy.get("until_written_permission") or []
    if until:
        st.markdown("**Tant qu’aucun accord écrit :**")
        for line in until:
            st.markdown(f"- {line}")

    with st.expander("Brouillon de contact éditeur", expanded=False):
        st.text_input(
            "Sujet",
            value=str(policy.get("contact_draft_subject") or ""),
            key="univ_lic_subj",
            disabled=True,
        )
        st.text_area(
            "Corps (à envoyer via le formulaire Contact Universalis)",
            value=str(policy.get("contact_draft_body_fr") or ""),
            height=220,
            key="univ_lic_body",
        )
        st.caption("Références : " + " · ".join(str(u) for u in (data.get("sources_consulted") or [])))

    st.info(
        "Checklist **cochée** (analyse faite). Gate production **fermée** jusqu’à réponse éditeur — "
        "adapter Lab/admin OK ; e-mail / TTS / PDF EN Universalis interdits pour l’instant."
    )


def render_admin_multilang_roadmap() -> None:
    st.title("Multi-langues — suivi (page temporaire)")
    st.caption(
        "Chantier : lectures via API natives par langue · "
        f"priorité {' → '.join(LANG_PRIORITY)} · "
        "fichier `data/multilang_progress.json`."
    )

    prog = load_progress()
    done = set(prog.get("completed_ids") or [])
    n_tot = len(_all_step_ids())
    n_done = len(done)

    if n_tot > 0 and n_done >= n_tot:
        st.success(f"**Chantier multi-langues : checklist complète** ({n_done}/{n_tot}).")
    else:
        st.info(f"**Avancement** : {n_done} / {n_tot} · lab = tuile « Lab lectures ».")

    with st.expander("Règles & stratégie", expanded=False):
        st.markdown(_STRATEGY_MD)

    st.markdown(
        """
| Livré / en cours | Cible |
|---|---|
| Facade + dimanche `pref_langue` (AELF FR / Evangelizo) | Synthèse IA hors FR (prompts) |
| E-mail : URLs + titre par `pref_langue` destinataire | Templates ETPL multilingues |
| Admin génération : sélecteur langue + GCS `{LANG}/` | TTS annonces localisées |
| Checklist Universalis (secours) | Attente réponse (non bloquant) |
| Evangelizo Lab 7/7 | ToS Evangelizo canaux larges |
        """.strip()
    )

    st.divider()
    _render_universalis_license_panel()
    st.divider()
    _multilang_controls_fragment()
