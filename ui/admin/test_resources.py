"""Admin — Test ressources, diagnostic infra, voix TTS, prompts Paramètres_IA."""

from __future__ import annotations

import time
from datetime import date
from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.gemini_tts_catalog import gemini_tts_voice_names_ordered, load_gemini_tts_voice_catalog
from core.parametres_ia import pick_effective_templates
from gspread.exceptions import WorksheetNotFound

from core.sheets_db import (
    _resolve_table_name,
    build_gspread_client,
    fetch_records,
    sheet_row_status_is_live,
)
from core.voix_audio import DEFAULT_GEMINI_TTS_VOICE, resolve_voice
from ui.components import loading_overlay

# Clés session pour replier les expanders après reconnexion admin (voir `collapse_admin_test_resources_expanders`).
_ADMIN_RES_EXPANDER_KEYS: tuple[str, ...] = (
    "adm_res_exp_default",
    "adm_res_exp_diag",
    "adm_res_exp_smoke",
    "adm_res_exp_voix",
    "adm_res_exp_audio",
    "adm_res_exp_texte",
)


def collapse_admin_test_resources_expanders() -> None:
    """Replie tous les expanders de la page Test ressources (ex. après changement du mot de passe admin / reconnexion)."""
    for k in _ADMIN_RES_EXPANDER_KEYS:
        st.session_state[k] = False


_PROMPT_TEMPLATE_LABELS: dict[str, str] = {
    "instructions_base_md": "Socle — consignes générales (structure du prompt)",
    "overlay_takeaways": "Surcouche — inclure « Le Psaume » + « À retenir »",
    "overlay_no_takeaways": "Surcouche — sans section « À retenir »",
    "overlay_catechese_bridge": "Surcouche — passerelle catéchèse (Stone Card)",
    "retry_hardened_prefix": "Surcouche — préfixe de relance (anti-hallucination renforcée)",
    "audio_style_default": "TTS — style oral par défaut (synthèse)",
    "audio_style_paques": "TTS — surcouche temps pascal (synthèse)",
    "audio_style_careme": "TTS — surcouche Carême (synthèse)",
    "audio_style_lectures": "TTS — style lectures du lectionnaire",
}

_AUDIO_PROMPT_KEYS: tuple[str, ...] = (
    "audio_style_default",
    "audio_style_paques",
    "audio_style_careme",
    "audio_style_lectures",
)


def _render_admin_default_behavior_summary() -> None:
    """Encart « si je ne touche à rien » : montre le pipeline effectif par défaut."""
    st.markdown(
        """
**Texte de la synthèse** *(inchangé)* :  
socle `instructions_base_md` (Sheets) + surcouche selon les options cochées
(`overlay_takeaways` / `overlay_no_takeaways` / `overlay_catechese_bridge`) + secret sauce.

**Audio de la synthèse** :  
préfixe `audio_style_default` (lu mais non visible) + texte de la synthèse, lu par la voix
résolue dans **`Voix_Audio`** :

| Couleur / Temps du dimanche | Voix retenue |
|---|---|
| Couleur **violet** (Avent / Carême) | **Sulafat** (douce) |
| Couleur **rouge** (Pentecôte, martyrs…) | **Sadachbia** (vibrante) |
| Temps **pascal** (sans couleur spéciale) | **Laomedeia** (tonique) |
| Temps **Carême** (sans couleur spéciale) | **Vindemiatrix** (douce) + surcouche `audio_style_careme` |
| Tout le reste | **Achird** (chaleureuse) |

**Audio des lectures AELF** *(option à cocher au moment de la génération)* :  
préfixe `audio_style_lectures` + 4 lectures, voix **Charon** (lecteur).
"""
    )


def _render_admin_infra_diagnostic(*, cfg: object) -> None:
    """Identité projet + Bucket GCS + Google Sheet + dépendances runtime."""
    st.subheader("Identité / projet")
    sa = dict(cfg.gcp_service_account or {})
    sa_email = str(sa.get("client_email") or "").strip()
    sa_project_id = str(sa.get("project_id") or "").strip()
    sa_quota_project_id = str(sa.get("quota_project_id") or sa.get("project_id") or "").strip()
    diag = {
        "service_account": sa_email or "—",
        "project_id": sa_project_id or "—",
        "quota_project_id": sa_quota_project_id or "—",
        "gcs_bucket_name": str(cfg.gcs_bucket_name or "").strip() or "—",
        "gsheet_id_present": bool(str(cfg.gsheet_id or "").strip()),
    }
    st.code("\n".join([f"{k}: {v}" for k, v in diag.items()]))

    st.divider()
    st.subheader("Google Bucket")
    if not cfg.gcs_bucket_name:
        st.warning("gcs_bucket_name manquant.")
    else:
        with st.expander(f"Structure du bucket `gs://{cfg.gcs_bucket_name}`", expanded=False):
            try:
                gcs = build_gcs_client(cfg.gcp_service_account)
                gcs_project = str(getattr(gcs, "project", "") or "").strip()
                if gcs_project:
                    st.caption(f"Client Cloud — projet effectif : `{gcs_project}`")

                bucket_name = str(cfg.gcs_bucket_name).strip()
                bucket = gcs.bucket(bucket_name)

                # On liste un nombre raisonnable d'objets, puis on reconstruit une arborescence.
                # Objectif: diagnostic lisible (pas un inventaire exhaustif).
                names = [b.name for b in gcs.list_blobs(bucket, max_results=800)]
                if not names:
                    st.info("Aucun objet détecté (bucket vide ou droits insuffisants).")
                else:
                    def _add(tree: dict, parts: list[str]) -> None:
                        cur = tree
                        for p in parts:
                            if not p:
                                continue
                            cur = cur.setdefault(p, {})

                    tree: dict[str, dict] = {}
                    for n in names:
                        parts = [p for p in str(n).split("/") if p]
                        # On affiche uniquement la STRUCTURE de dossiers (pas les fichiers)
                        if len(parts) >= 2:
                            _add(tree, parts[: min(4, len(parts) - 1)])  # profondeur limitée, sans feuille fichier

                    def _render(cur: dict[str, dict], indent: int = 0, *, max_children: int = 40) -> list[str]:
                        out: list[str] = []
                        for i, k in enumerate(sorted(cur.keys())):
                            if i >= max_children:
                                out.append("  " * indent + "…")
                                break
                            # Ici on ne rend que des dossiers.
                            out.append("  " * indent + f"- {k}/")
                            out.extend(_render(cur[k], indent + 1, max_children=max_children))
                        return out

                    st.success(f"Cloud OK — bucket `{bucket_name}` ({len(names)} objet(s) échantillonnés)")
                    st.code("\n".join(_render(tree)), language="markdown")
                    st.caption("Affichage limité (profondeur/quantité) : c'est une vue de structure pour diagnostic.")
            except Exception as e:
                st.error(f"Cloud KO — {e}")

    st.divider()
    st.subheader("Google Sheet")
    if not cfg.gsheet_id:
        st.warning("gsheet_id manquant.")
    else:
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            sh = gs.open_by_key(cfg.gsheet_id)
            ws_titles = [w.title for w in sh.worksheets()]
            st.success(f"Sheets OK — {len(ws_titles)} onglet(s) accessibles.")
            ia_tab = _resolve_table_name(sh=sh, table="Paramètres_IA")
            if ia_tab in ws_titles:
                ws = sh.worksheet(ia_tab)
                header = ws.row_values(1)
                if "Description" in header:
                    st.success(
                        f"Table prompts IA OK — onglet `{ia_tab}` (logique `Paramètres_IA`) — colonne `Description` présente."
                    )
                else:
                    st.warning(
                        f"Table prompts IA (`{ia_tab}` / `Paramètres_IA`) : colonne `Description` absente (header à mettre à jour)."
                    )
            else:
                st.warning(
                    f"Onglet prompts IA introuvable (attendu `{ia_tab}` ou `Paramètres_IA` selon AliasTables). "
                    "Lance `python tools/init_sheets_db.py`."
                )
        except Exception as e:
            st.error(f"Sheets KO — {e}")

    st.divider()
    st.subheader("Dépendances runtime")
    st.caption("Vérifie que ce runtime Streamlit a bien les librairies nécessaires (PDF/Excel, etc.).")
    try:
        import openpyxl  # type: ignore

        st.success(f"openpyxl OK — version {getattr(openpyxl, '__version__', '?')} ({getattr(openpyxl, '__file__', '')})")
    except Exception as e:
        st.warning(f"openpyxl non importable dans CE runtime Streamlit : {e}")


def _render_admin_ia_smoke_tests(*, cfg: object) -> None:
    """Smoke tests : Gemini TTS court + Vertex texte court (avec sélecteur de voix)."""
    st.caption(
        "VertexAI est la voie principale (via compte de service). "
        "La Gemini API (clé `GEMINI_API_KEY`) sert de **fallback** pour la TTS si Vertex refuse l'AUDIO (allowlist) "
        "ou en cas de quota/erreur transitoire."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        if not cfg.gemini_api_key:
            st.info("Gemini API : non configurée (`GEMINI_API_KEY` manquante).")
        else:
            _tnames = gemini_tts_voice_names_ordered()
            _tmap, _ = load_gemini_tts_voice_catalog()
            try:
                _tix = _tnames.index("Achird")
            except ValueError:
                _tix = 0
            test_voice_pick = st.selectbox(
                "Voix pour le test TTS",
                options=_tnames + ["__custom__"],
                index=_tix,
                format_func=lambda x: (_tmap.get(x) if x != "__custom__" else "Autre — saisie libre"),
                key="adm_smoke_tts_voice",
            )
            test_voice_custom = ""
            if test_voice_pick == "__custom__":
                test_voice_custom = st.text_input("Nom de voix Gemini", key="adm_smoke_tts_voice_custom").strip()
            test_voice_use = (
                test_voice_custom if test_voice_pick == "__custom__" else str(test_voice_pick or "").strip()
            ) or DEFAULT_GEMINI_TTS_VOICE

            if st.button("Tester Gemini TTS (court)", key="adm_test_gemini_tts"):
                ov = loading_overlay("Test Gemini TTS…")
                try:
                    from core.gemini_tts_api import GeminiTtsApiClient

                    t0 = time.perf_counter()
                    cli = GeminiTtsApiClient(api_key=cfg.gemini_api_key)
                    res = cli.generate_audio(
                        model="gemini-2.5-flash-preview-tts",
                        text="Test audio LumenVia. Un, deux, trois.",
                        voice_name=test_voice_use,
                    )
                    dt = time.perf_counter() - t0
                    st.success(f"Gemini TTS OK — {len(res.audio_bytes)} octets en {dt:.2f}s ({res.mime_type})")
                    st.audio(res.audio_bytes, format=res.mime_type or "audio/wav")
                except Exception as e:
                    st.error(f"Gemini TTS KO — {e}")
                finally:
                    ov.empty()
    with col_b:
        if st.button("Tester VertexAI (texte court)", key="adm_test_vertex_text"):
            ov = loading_overlay("Test VertexAI (texte)…")
            try:
                from core.vertex_gemini import VertexGeminiClient

                t0 = time.perf_counter()
                vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
                res = vx.generate_text_auto(
                    preferred_models=["gemini-2.0-flash", "gemini-2.5-flash"],
                    prompt="Réponds uniquement par « OK ».",
                    max_output_tokens=32,
                )
                dt = time.perf_counter() - t0
                st.success(f"VertexAI OK — {res.model} en {dt:.2f}s")
                st.code((res.text or "").strip()[:400] or "—")
            except Exception as e:
                st.error(f"VertexAI KO — {e}")
            finally:
                ov.empty()


def _render_admin_voix_audio_section(*, cfg: object, gs: object) -> None:
    """Affichage et édition des règles `Voix_Audio` (VOIX) — sans `st.dataframe`."""
    st.caption(
        "Règles append-only : **Cible** (`synthese`, `lectures`, `*`), **Couleur**, **Temps liturgique** (`pascal`, `careme`, … ou `*`). "
        "La règle la plus **spécifique** l'emporte. Catalogue des voix : `data/gemini_tts_voices.json` (à jour avec la doc Google)."
    )
    try:
        voix_all = fetch_records(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="Voix_Audio",
            limit=0,
        )
    except Exception as ex_v:
        voix_all = []
        st.warning(f"Lecture `Voix_Audio` impossible : {ex_v}")

    voix_actifs = [r for r in voix_all if sheet_row_status_is_live(r.get("Statut"))]
    voix_actifs.sort(key=lambda r: str(r.get("Date_Effet") or ""), reverse=True)

    if voix_actifs:
        def _md_cell(v: object) -> str:
            s = str(v if v is not None else "").strip().replace("|", "\\|").replace("\n", " ")
            return s or "—"

        md_lines = [
            "| Cible | Couleur | Temps liturgique | Voix | Description | Version | Date d'effet |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in voix_actifs[:80]:
            md_lines.append(
                "| "
                + " | ".join(
                    [
                        _md_cell(r.get("Cible")),
                        _md_cell(r.get("Couleur")),
                        _md_cell(r.get("Temps_Liturgique")),
                        f"**{_md_cell(r.get('Voix'))}**",
                        _md_cell(r.get("Description")),
                        _md_cell(r.get("Version")),
                        _md_cell(r.get("Date_Effet")),
                    ]
                )
                + " |"
            )
        st.markdown("\n".join(md_lines))
    else:
        st.info(
            "Aucune règle **Actif** dans `Voix_Audio`. Lance `python .\\tools\\init_sheets_db.py` "
            "ou ajoute une règle ci‑dessous."
        )

    cat_map, cat_readme = load_gemini_tts_voice_catalog()

    with st.expander("Tester la résolution voix (simulation)", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            t_cible = st.selectbox(
                "Cible",
                options=["synthese", "lectures"],
                key="adm_voix_test_cible",
            )
        with tc2:
            t_couleur = st.selectbox(
                "Couleur (du dimanche)",
                options=["", "vert", "blanc", "rouge", "violet", "rose", "noir"],
                format_func=lambda x: x or "(non précisée)",
                key="adm_voix_test_couleur",
                help="Couleur liturgique renvoyée par AELF pour le dimanche concerné.",
            )
        with tc3:
            t_temps = st.selectbox(
                "Temps liturgique",
                options=["", "pascal", "careme", "avent", "noel", "ordinaire", "pentecote", "saint"],
                format_func=lambda x: x or "(non précisé)",
                key="adm_voix_test_temps",
            )
        t_date = st.date_input("Date d'effet (simulée)", value=date.today(), key="adm_voix_test_date")
        res_test = resolve_voice(
            voix_actifs,
            cible=t_cible,
            couleur=(t_couleur or None),
            periode=(t_temps or None),
            today=t_date,
        )
        voice_lab = cat_map.get(res_test["voice"], res_test["voice"])
        if res_test.get("fallback"):
            st.warning(
                f"Aucune règle ne correspond → fallback **{res_test['voice']}** *(en dur dans le code)*."
            )
        else:
            rule_id = (res_test.get("rule") or {}).get("#ID") or ""
            st.success(
                f"Voix retenue : **{voice_lab}** — règle `#ID {rule_id}` (score {res_test.get('score')})."
            )
            st.caption(
                "Spécificité = +1 Cible · +2 Couleur · +2 Temps. Tie-break = Version desc, puis Date_Effet desc."
            )

    with st.expander("Ajouter une règle de voix", expanded=False):
        voice_names = gemini_tts_voice_names_ordered()
        try:
            _def_voice_ix = voice_names.index("Achird")
        except ValueError:
            _def_voice_ix = 0
        sel_voice = st.selectbox(
            "Voix (catalogue)",
            options=voice_names + ["__custom__"],
            index=_def_voice_ix,
            format_func=lambda x: (cat_map.get(x) if x != "__custom__" else "Autre — saisie libre"),
            key="adm_voix_pick",
        )
        custom_voice = ""
        if sel_voice == "__custom__":
            custom_voice = st.text_input("Nom technique de la voix Gemini", value="", key="adm_voix_custom").strip()
        voix_eff = custom_voice if sel_voice == "__custom__" else sel_voice

        vc1, vc2, vc3 = st.columns(3)
        with vc1:
            v_cible = st.selectbox("Cible", options=["*", "synthese", "lectures"], key="adm_voix_cible")
        with vc2:
            v_couleur = st.selectbox(
                "Couleur (du dimanche)",
                options=["*", "vert", "blanc", "rouge", "violet", "rose", "noir"],
                key="adm_voix_couleur",
                help="Couleur liturgique. `*` = la règle s'applique quelle que soit la couleur.",
            )
        with vc3:
            v_temps = st.selectbox(
                "Temps liturgique",
                options=[
                    "*",
                    "pascal",
                    "careme",
                    "avent",
                    "noel",
                    "ordinaire",
                    "pentecote",
                    "saint",
                ],
                key="adm_voix_temps",
            )
        v_desc = st.text_input("Description (interne)", value="", key="adm_voix_desc")
        v_date = st.date_input("Date d'effet", value=date.today(), key="adm_voix_de")
        if cat_readme:
            st.caption(cat_readme[:280] + ("…" if len(cat_readme) > 280 else ""))

        if st.button("Ajouter la règle (append-only)", key="adm_voix_save"):
            if not voix_eff.strip():
                st.error("Choisis une voix.")
            else:
                ov = loading_overlay("Enregistrement règle Voix_Audio…")
                try:
                    sh = gs.open_by_key(cfg.gsheet_id)
                    import core.sheets_db as _sdb

                    ws_v = sh.worksheet(_sdb._resolve_table_name(sh=sh, table="Voix_Audio"))  # noqa: SLF001
                    hdr = ws_v.row_values(1)
                    if not hdr:
                        raise RuntimeError("Voix_Audio : header vide.")
                    max_ver = 0
                    for r in voix_all:
                        try:
                            max_ver = max(max_ver, int(str(r.get("Version") or "0").strip()))
                        except Exception:
                            pass
                    next_ver = str(max_ver + 1)
                    de = str(v_date)
                    rid = sha256(
                        f"voix|adm|{next_ver}|{voix_eff}|{v_cible}|{v_couleur}|{v_temps}|{de}".encode("utf-8")
                    ).hexdigest()[:18]
                    statut = "Actif"
                    parts_concat = [rid, statut, next_ver, de, v_cible, v_couleur, v_temps, voix_eff.strip(), v_desc.strip()]
                    concat = " | ".join(p.strip() for p in parts_concat if str(p).strip())
                    row_map = {
                        "#ID": rid,
                        "Statut": statut,
                        "Version": next_ver,
                        "Date_Effet": de,
                        "Cible": v_cible,
                        "Couleur": v_couleur,
                        "Temps_Liturgique": v_temps,
                        "Voix": voix_eff.strip(),
                        "Description": v_desc.strip(),
                        "Concaténation": concat,
                    }
                    ws_v.append_rows([[row_map.get(c, "") for c in hdr]], value_input_option="RAW")
                    st.success("Règle Voix_Audio enregistrée.")
                    try:
                        import app as ap

                        ap._load_voix_rules_cached.clear()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    st.rerun()
                finally:
                    ov.empty()


def _render_admin_prompts_editor_section(
    *,
    cfg: object,
    gs: object,
    rows: list[dict],
    audio_only: bool,
    section_key_suffix: str,
) -> None:
    """Éditeur de prompts MARPA (Sheets `Paramètres_IA`).

    `audio_only=True` : restreint aux clés `audio_style_*` (Levier B).
    `audio_only=False` : restreint aux clés *non* audio (socle + surcouches texte).
    """
    if not (cfg.gsheet_id and cfg.gcp_service_account):
        st.info("Configure `gsheet_id` + `gcp_service_account` pour gérer les templates IA ici.")
        return

    if not audio_only:
        try:
            ss = str(st.secrets.get("IA_SECRET_SAUCE_MD") or "").strip()
        except Exception:
            ss = ""
        if ss:
            st.success(f"Secret sauce : configurée ({len(ss)} caractères).")
        else:
            st.warning("Secret sauce : absente (`IA_SECRET_SAUCE_MD` non défini).")

        with st.expander("Fallback local minimal (dépôt public) — `data/instructions_ia.md`", expanded=False):
            instr_path = Path("data/instructions_ia.md")
            if not instr_path.is_file():
                st.warning(f"Fichier introuvable : `{instr_path.as_posix()}`.")
            else:
                st.code(instr_path.read_text(encoding="utf-8").strip()[:2000] or "", language="markdown")
                st.caption("Ce fallback doit rester minimal dans le dépôt public (il ne doit pas contenir la matière du prompt).")

    latest = pick_effective_templates(rows, allowed_keys=None)

    def _is_audio_key(k: str) -> bool:
        return k in _AUDIO_PROMPT_KEYS

    def _wanted(k: str) -> bool:
        return _is_audio_key(k) if audio_only else (not _is_audio_key(k))

    existing_keys = sorted(
        [k for k, v in latest.items() if (v.content_md or "").strip() and _wanted(k)]
    )

    def _norm0(v: object) -> str:
        return str(v or "").strip()

    desc_by_key: dict[str, str] = {}
    for k, eff in latest.items():
        if not _wanted(k):
            continue
        chosen: str = ""
        eff_id = _norm0(getattr(eff, "id", ""))
        eff_ver = int(getattr(eff, "version", 0) or 0)
        for r in rows:
            if _norm0(r.get("Clé_Prompt")) != k:
                continue
            rid = _norm0(r.get("#ID") or r.get("ID") or r.get("id"))
            if eff_id and rid and rid == eff_id:
                chosen = _norm0(r.get("Description"))
                break
        if not chosen:
            for r in rows:
                if _norm0(r.get("Clé_Prompt")) != k:
                    continue
                try:
                    rv = int(_norm0(r.get("Version") or 0) or 0)
                except Exception:
                    rv = 0
                if rv != eff_ver:
                    continue
                chosen = _norm0(r.get("Description"))
                if chosen:
                    break
        if chosen:
            desc_by_key[k] = chosen

    def _fmt_key(k: str) -> str:
        d = (desc_by_key.get(k) or _PROMPT_TEMPLATE_LABELS.get(k) or "").strip()
        return f"{k} — {d}" if d else k

    suffix = section_key_suffix
    create_new = st.toggle(
        "Créer un nouveau prompt", value=False, key=f"adm_tpl_create_new_{suffix}"
    )
    if create_new:
        picked = st.text_input(
            "Clé_Prompt (identifiant technique stable)",
            value="",
            key=f"adm_tpl_new_key_{suffix}",
            help=(
                "Exemples (audio) : `audio_style_default`, `audio_style_paques`, … — "
                "Exemples (texte) : `instructions_base_md`, `overlay_takeaways`, `retry_hardened_prefix`. "
                "Minuscules + underscores, pas d'espaces."
            ),
        ).strip()
        current = ""
        current_desc = ""
    else:
        if not existing_keys:
            st.warning(
                "Aucun prompt Actif trouvé pour cette section. "
                "Active « Créer un nouveau prompt » ou lance `python .\\tools\\init_sheets_db.py`."
            )
            picked = ""
            current = ""
            current_desc = ""
        else:
            default_key = (
                "audio_style_default" if audio_only else "instructions_base_md"
            )
            default_ix = (
                existing_keys.index(default_key) if default_key in existing_keys else 0
            )
            picked = st.selectbox(
                "Choisir un prompt existant (Actif)",
                options=existing_keys,
                index=default_ix,
                key=f"adm_tpl_key_{suffix}",
                format_func=_fmt_key,
            )
            current = (latest.get(picked).content_md if picked in latest else "").strip()
            current_desc = (desc_by_key.get(picked) or _PROMPT_TEMPLATE_LABELS.get(picked) or "").strip()

    edited_desc = st.text_input(
        "Description (affichage dans la liste)",
        value=current_desc,
        key=f"adm_tpl_desc_{suffix}__{picked or '__none__'}",
        help="Optionnel. Sert uniquement à rendre la liste plus claire (tu peux mettre un nom métier).",
    )
    edited = st.text_area(
        "Contenu (Markdown)",
        value=current,
        height=260,
        key=f"adm_tpl_editor_{suffix}__{picked or '__none__'}",
        help="Append-only : enregistre une nouvelle version (Version + 1).",
    )
    notes = st.text_input("Notes (optionnel)", key=f"adm_tpl_notes_{suffix}", value="")
    active = st.checkbox(
        "Activer ce prompt",
        value=True,
        key=f"adm_tpl_active_{suffix}",
        help="Si coché, l'ancien Actif de la même Clé_Prompt sera automatiquement désactivé.",
    )
    date_effet = st.date_input("Date d'effet", value=date.today(), key=f"adm_tpl_date_effet_{suffix}")

    disabled_save = (not bool(edited.strip())) or (not bool(picked.strip()))
    if st.button(
        "Enregistrer (nouvelle version dans Sheets)",
        type="primary",
        disabled=disabled_save,
        key=f"adm_tpl_save_{suffix}",
    ):
        ov = loading_overlay("Enregistrement du template IA (Sheets)…")
        try:
            body = edited.strip()
            sh = gs.open_by_key(cfg.gsheet_id)
            ws_name = _resolve_table_name(sh=sh, table="Paramètres_IA")
            try:
                ws = sh.worksheet(ws_name)
            except WorksheetNotFound:
                st.error(
                    f"Onglet introuvable : `{ws_name}` (table logique `Paramètres_IA`, ex. acronyme `AIP`). "
                    "Vérifie `AliasTables` ou lance `python tools/init_sheets_db.py`."
                )
                return
            header = ws.row_values(1)
            if not header:
                raise RuntimeError("Onglet `Paramètres_IA` non initialisé (header vide). Lance init_sheets_db.")
            if "Description" not in header:
                raise RuntimeError("Colonne `Description` manquante dans `Paramètres_IA`. Relance init_sheets_db.py ou mets à jour le header.")

            def _norm(s: object) -> str:
                return str(s or "").strip()

            key_norm = _norm(picked)
            max_ver = 0
            for r in rows:
                if _norm(r.get("Clé_Prompt")) != key_norm:
                    continue
                try:
                    max_ver = max(max_ver, int(_norm(r.get("Version") or 0) or 0))
                except Exception:
                    pass
            next_ver = int(max_ver + 1)
            de = str(date_effet)

            if active:
                try:
                    records = ws.get_all_records()
                except Exception:
                    records = []

                def _make_concat(*, row_id: str, key: str, version: str, statut: str, date_effet: str) -> str:
                    return " | ".join([_norm(row_id), _norm(key), _norm(version), _norm(statut), _norm(date_effet)])

                try:
                    col_statut = header.index("Statut") + 1
                    col_concat = header.index("Concaténation") + 1
                except Exception:
                    col_statut = 0
                    col_concat = 0

                if col_statut and col_concat:
                    for i, r in enumerate(records):
                        if _norm(r.get("Clé_Prompt")) != key_norm:
                            continue
                        if not sheet_row_status_is_live(r.get("Statut")):
                            continue
                        row_num = i + 2
                        ws.update_cell(row_num, col_statut, "Inactif")
                        row_id = _norm(r.get("#ID") or r.get("ID") or r.get("id"))
                        ver_str = _norm(r.get("Version"))
                        de_str = _norm(r.get("Date_Effet")) or de
                        ws.update_cell(
                            row_num,
                            col_concat,
                            _make_concat(row_id=row_id, key=key_norm, version=ver_str, statut="Inactif", date_effet=de_str),
                        )

            row_id = sha256(f"ia|{key_norm}|{next_ver}|{body}".encode("utf-8")).hexdigest()[:18]
            statut = "Actif" if active else "Inactif"
            concat = " | ".join([row_id, key_norm, str(next_ver), statut, de])
            row_map = {
                "#ID": row_id,
                "Clé_Prompt": key_norm,
                "Description": str(edited_desc or "").strip(),
                "Version": str(next_ver),
                "Statut": statut,
                "Date_Effet": de,
                "Contenu_Markdown": body,
                "Concaténation": concat,
            }
            ws.append_rows([[row_map.get(c, "") for c in header]], value_input_option="RAW")

            st.success("Paramètre IA enregistré.")
            try:
                import app as ap

                ap._load_prompt_templates_cached.clear()  # type: ignore[attr-defined]
            except Exception:
                pass
            st.rerun()
        finally:
            ov.empty()


def render_admin_test_resources() -> None:
    st.title("Admin — Réglages & diagnostic")
    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("`gcp_service_account` manquant dans `secrets.toml`.")
        return

    st.caption(
        "Cette page rassemble les réglages liés à la génération des contenus du dimanche. "
        "Tout est replié par défaut — déplie uniquement ce dont tu as besoin."
    )

    with st.expander(
        "Comportement par défaut « si je ne touche à rien »",
        expanded=True,
        key="adm_res_exp_default",
    ):
        _render_admin_default_behavior_summary()

    with st.expander(
        "Diagnostic infrastructure (Cloud / Sheets / dépendances)",
        expanded=False,
        key="adm_res_exp_diag",
    ):
        _render_admin_infra_diagnostic(cfg=cfg)

    with st.expander(
        "Tests rapides IA (smoke TTS / texte)",
        expanded=False,
        key="adm_res_exp_smoke",
    ):
        _render_admin_ia_smoke_tests(cfg=cfg)

    if not (cfg.gsheet_id and cfg.gcp_service_account):
        st.info("Configure `gsheet_id` + `gcp_service_account` pour gérer voix et prompts.")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    try:
        rows = fetch_records(
            gspread_client=gs,
            spreadsheet_id=cfg.gsheet_id,
            table="Paramètres_IA",
            limit=5000,
        )
    except Exception as e:
        rows = []
        st.warning(f"Lecture `Paramètres_IA` impossible : {e}")

    with st.expander(
        "Audio — voix TTS (table `Voix_Audio`)",
        expanded=False,
        key="adm_res_exp_voix",
    ):
        _render_admin_voix_audio_section(cfg=cfg, gs=gs)

    with st.expander(
        "Audio — styles de lecture (prompts `audio_style_*`)",
        expanded=False,
        key="adm_res_exp_audio",
    ):
        st.caption(
            "Préfixes ajoutés au texte envoyé au TTS Gemini (pas au modèle texte). "
            "Append-only : chaque enregistrement crée une nouvelle version."
        )
        _render_admin_prompts_editor_section(
            cfg=cfg, gs=gs, rows=rows, audio_only=True, section_key_suffix="audio"
        )

    with st.expander(
        "Texte — socle + surcouches de la synthèse écrite",
        expanded=False,
        key="adm_res_exp_texte",
    ):
        st.caption(
            "Socle anti-hallucination + surcouches utilisées par la génération **écrite** "
            "(`instructions_base_md`, `overlay_takeaways`, `overlay_no_takeaways`, "
            "`overlay_catechese_bridge`, `retry_hardened_prefix`)."
        )
        _render_admin_prompts_editor_section(
            cfg=cfg, gs=gs, rows=rows, audio_only=False, section_key_suffix="texte"
        )
