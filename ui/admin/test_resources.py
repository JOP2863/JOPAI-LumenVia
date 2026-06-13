"""Admin — Test ressources, diagnostic infra, voix TTS, prompts Paramètres_IA."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.config import gemini_api_key_status, load_config, resolve_gemini_api_key
from ui.streamlit_caches import (
    adm_sheets_fetch_cached,
    invalidate_adm_sheets_fetch_cache,
    load_voix_rules_cached,
    service_account_json_fingerprint,
)
from core.gcp_clients import build_gcs_client
from core.gemini_tts_catalog import gemini_tts_voice_names_ordered, load_gemini_tts_voice_catalog
from core.parametres_ia import pick_effective_templates
from gspread.exceptions import WorksheetNotFound

from core.sheets_db import (
    _resolve_table_name,
    audit_alias_tables,
    build_gspread_client,
    fetch_records,
    format_alias_audit_report,
    sheet_row_status_is_live,
)
from core.tts_pronunciation import tts_pronunciation_breakdown
from core.voix_audio import DEFAULT_GEMINI_TTS_VOICE, resolve_voice
from ui.components import loading_overlay

# Clés session pour replier les expanders après reconnexion admin (voir `collapse_admin_test_resources_expanders`).
_ADMIN_RES_EXPANDER_KEYS: tuple[str, ...] = (
    "adm_res_exp_default",
    "adm_res_exp_diag",
    "adm_res_exp_smoke",
    "adm_res_exp_voix",
    "adm_res_exp_tts_pron",
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
    "overlay_catechese_bridge": "Surcouche — passerelle catéchèse",
    "retry_hardened_prefix": "Surcouche — préfixe de relance (anti-hallucination renforcée)",
    "audio_style_default": "TTS — style oral par défaut (synthèse)",
    "audio_style_paques": "TTS — surcouche temps pascal (synthèse)",
    "audio_style_careme": "TTS — surcouche Carême (synthèse)",
    "audio_style_lectures": "TTS — style lectures du lectionnaire",
    "tts_pronunciation": "TTS — dictionnaire de prononciation (JSON, voix seulement)",
}

_AUDIO_PROMPT_KEYS: tuple[str, ...] = (
    "audio_style_default",
    "audio_style_paques",
    "audio_style_careme",
    "audio_style_lectures",
    "tts_pronunciation",
)


def _render_admin_ai_pipeline_matrix() -> None:
    """Tableau de référence : quel contenu passe par quelle IA."""
    st.markdown("#### Matrice IA — contenus du dimanche")
    st.markdown(
        """
| Élément | Généré par IA ? | Service | Modèles / remarque |
|---|---|---|---|
| **Synthèse (texte)** | Oui | **Vertex AI** (GCP) | `gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-pro-latest`… — prompt = lectures AELF + `Paramètres_IA` |
| **Audio de la synthèse** | Oui (TTS) | **Vertex TTS** → repli **API Gemini** | `gemini-2.5-flash-preview-tts`… — lit le texte de la synthèse (pas les consignes `audio_style_*`) |
| **Lectures (texte)** | Non | **API AELF** / cache **`readings_cache`** (RDC) | Textes officiels du lectionnaire — pas de réécriture LLM |
| **Audio des lectures** | Oui (TTS seul) | **Vertex TTS** → repli **API Gemini** | Même moteurs que la synthèse ; texte découpé par section liturgique (1re lecture, Psaume, Évangile…) |
| **PDF du dimanche** | Non (assemblage) | **ReportLab** (Python) | Mise en page : illustration + lectures + synthèse + liens audio |
| **Illustration (couverture)** | Oui *(à part)* | **Vertex** (image) | Générée sur la page admin illustrations, puis intégrée au PDF |

Seuls la **synthèse écrite**, les **deux audios (TTS)** et éventuellement **l’illustration** passent par une IA.
Les **lectures textuelles** viennent de l’**AELF** ; le **PDF** est un **montage** de contenus déjà produits.
"""
    )
    st.markdown("#### Stratégie audio (TTS) — Vertex ou API Gemini ?")
    st.markdown(
        """
**Recommandation LumenVia : Vertex en priorité, API Gemini en repli.**

| | **Vertex AI** (compte de service GCP) | **API Gemini** (`GEMINI_API_KEY`) |
|---|---|---|
| **Rôle** | Voie **production** — même projet GCP que Sheets / GCS / texte | **Repli** si Vertex TTS refuse ou quota temporaire |
| **Atouts** | Facturation GCP unifiée, pas de clé séparée, aligné avec la synthèse écrite | Fonctionne sans allowlist AUDIO Vertex ; pratique pour morceaux longs |
| **Limites** | Modèles TTS parfois soumis à une **allowlist** projet | Quotas API plus stricts (429) — morceaux limités et parallélisme réduit |

**Synthèse et lectures utilisent la même logique** : Vertex TTS d’abord, puis Gemini API fragmenté si allowlist / quota / 429.
La synthèse envoie souvent **un seul appel** Vertex (texte plus court) ; les lectures sont **découpées** par section liturgique car le texte AELF est plus long.
"""
    )


def _render_admin_default_behavior_summary() -> None:
    """Encart « si je ne touche à rien » : montre le pipeline effectif par défaut."""
    _render_admin_ai_pipeline_matrix()
    st.divider()
    st.markdown("#### Voix TTS par défaut (`Voix_Audio`)")
    st.markdown(
        """
**Texte de la synthèse** *(inchangé)* :  
socle `instructions_base_md` (Sheets) + surcouche selon les options cochées
(`overlay_takeaways` / `overlay_no_takeaways` / `overlay_catechese_bridge`) + secret sauce.

**Audio de la synthèse** :  
texte de la synthèse seul (les clés `audio_style_*` sont des **consignes admin**, non lues à voix haute),
lu par la voix résolue dans **`Voix_Audio`** :

| Couleur / Temps du dimanche | Voix retenue |
|---|---|
| Couleur **violet** (Avent / Carême) | **Sulafat** (douce) |
| Couleur **rouge** (Pentecôte, martyrs…) | **Sadachbia** (vibrante) |
| Temps **pascal** (sans couleur spéciale) | **Laomedeia** (tonique) |
| Temps **Carême** (sans couleur spéciale) | **Vindemiatrix** (douce) |
| Tout le reste | **Achird** (chaleureuse) |

**Audio des lectures AELF** *(option à cocher au moment de la génération)* :  
4 lectures AELF (sans lire `audio_style_lectures` à voix haute — consigne admin uniquement), voix **Charon** (lecteur).
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

            st.markdown("**AliasTables — tables métier**")
            alias_issues = audit_alias_tables(sh=sh)
            if not alias_issues:
                st.success("AliasTables OK — 23 tables logiques résolues vers leurs acronymes.")
            else:
                err_n = sum(1 for i in alias_issues if i.severity == "error")
                warn_n = len(alias_issues) - err_n
                if err_n:
                    st.error(
                        f"AliasTables : **{err_n} erreur(s)** et {warn_n} avertissement(s). "
                        "Corrigez le classeur ou lancez `python tools/audit_alias_tables.py`."
                    )
                else:
                    st.warning(f"AliasTables : {warn_n} avertissement(s) — vérifiez les doublons ou entrées manquantes.")
                st.code(format_alias_audit_report(alias_issues), language="text")
                st.caption(
                    "Le code applicatif utilise **uniquement les noms logiques** (`readings_cache`, `email_templates`, …) ; "
                    "AliasTables résout vers l’onglet acronyme (ex. RDC, ETPL). "
                    "**readings_cache** = cache lectures pour l’app ; **liturgy_fetches** (LITF) = journal des appels API AELF."
                )
        except Exception as e:
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg:
                st.error(
                    "Sheets KO — quota de lectures par minute dépassé (HTTP 429). "
                    "Attends **60 secondes** sans cliquer dans l'app, puis relance uniquement le diagnostic. "
                    "Évite d'ouvrir plusieurs pages admin en parallèle."
                )
            else:
                st.error(f"Sheets KO — {e}")

    st.divider()
    st.subheader("Dépendances runtime")
    st.caption("Vérifie que ce runtime Streamlit a bien les librairies nécessaires (PDF/Excel, etc.).")
    try:
        import openpyxl  # type: ignore

        st.success(f"openpyxl OK — version {getattr(openpyxl, '__version__', '?')} ({getattr(openpyxl, '__file__', '')})")
    except Exception as e:
        st.warning(f"openpyxl non importable dans CE runtime Streamlit : {e}")


def _render_gemini_api_key_diagnostics(*, cfg: object) -> None:
    """Vérifie que ce runtime Streamlit voit bien GEMINI_API_KEY (sans afficher la clé)."""
    st.subheader("Clé GEMINI_API_KEY (TTS repli)")
    status = gemini_api_key_status()
    cfg_key = str(getattr(cfg, "gemini_api_key", "") or "").strip()
    resolved = resolve_gemini_api_key()
    if status.get("detected"):
        st.success(
            f"Clé détectée — source : **{status.get('source')}**, "
            f"suffixe `…{status.get('suffix')}`."
        )
    else:
        st.error(
            "Clé **non détectée** par ce runtime Streamlit. "
            "Si elle est dans `.streamlit/secrets.toml` en local, vérifie aussi les **Secrets** "
            "de Streamlit Cloud (déploiement) puis **redémarre** l'app."
        )
    if cfg_key and resolved and cfg_key == resolved:
        st.caption("La clé est bien chargée dans `load_config()` (utilisée par la génération dominicale).")
    elif resolved and not cfg_key:
        st.warning(
            "Clé résolue via `resolve_gemini_api_key()` mais absente de `cfg.gemini_api_key` — "
            "recharge la page après redémarrage."
        )


def _md_table_cell(raw: object) -> str:
    s = str(raw if raw is not None else "").strip().replace("|", "\\|").replace("\n", " ")
    return s or "—"


@dataclass
class _TtsSmokeResult:
    test_id: str
    label: str
    intention: str
    production_hint: str
    status: str
    route: str
    duration_s: float | None
    bytes_n: int | None
    detail: str
    audio_bytes: bytes | None = None
    audio_mime: str | None = None
    is_production_path: bool = False


def _run_tts_smoke_battery(*, cfg: object, gemini_key: str | None, voice_name: str) -> list[_TtsSmokeResult]:
    """Exécute tous les smoke tests TTS et retourne un résultat structuré par ligne."""
    from core.gemini_tts_api import GeminiTtsApiClient
    from core.sunday_gemini_tts import tts_readings_audio_bytes
    from core.vertex_gemini import VertexGeminiClient

    results: list[_TtsSmokeResult] = []
    sample_readings = (
        "Première lecture.\n\n"
        "Lecture du livre des Nombres.\n\n"
        "Psaume.\n\n"
        "Heureux l'homme qui met sa confiance dans le Seigneur.\n\n"
        "Deuxième lecture.\n\n"
        "Lecture de la lettre aux Romains.\n\n"
        "Évangile selon saint Matthieu.\n\n"
        "Jésus dit à ses disciples."
    )
    sample_short = "Test audio LumenVia. Un, deux, trois."

    # 1 — Vertex TTS (souvent refusé allowlist)
    t0 = time.perf_counter()
    try:
        vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
        res = vx.generate_audio_auto(
            preferred_models=["gemini-2.5-flash-tts", "gemini-2.5-flash-preview-tts"],
            text=sample_short,
            voice_name=voice_name,
        )
        dt = time.perf_counter() - t0
        from core.sunday_gemini_tts import clear_vertex_tts_allowlist_blocked

        clear_vertex_tts_allowlist_blocked()
        results.append(
            _TtsSmokeResult(
                test_id="vertex_tts",
                label="Vertex TTS",
                intention="Vérifier que le **compte GCP** peut produire de l'audio (allowlist Vertex AUDIO).",
                production_hint="Voie **prioritaire** synthèse + lectures",
                status="OK",
                route="vertex_tts",
                duration_s=dt,
                bytes_n=len(res.audio_bytes or b""),
                detail=getattr(res, "model", "vertex"),
                audio_bytes=res.audio_bytes,
                audio_mime=res.mime_type,
            )
        )
    except Exception as ex:
        dt = time.perf_counter() - t0
        msg = str(ex)
        allowlist = "allowlisted" in msg.lower() or "audio output" in msg.lower()
        results.append(
            _TtsSmokeResult(
                test_id="vertex_tts",
                label="Vertex TTS",
                intention="Vérifier que le **compte GCP** peut produire de l'audio (allowlist Vertex AUDIO).",
                production_hint="Voie **prioritaire** synthèse + lectures",
                status="KO",
                route="vertex_tts (refusé)",
                duration_s=dt,
                bytes_n=None,
                detail=("Allowlist attendu — repli Gemini requis. " if allowlist else "") + msg[:180],
            )
        )

    # 2 — Gemini API TTS court (modèle GA = repli synthèse)
    if not gemini_key:
        results.append(
            _TtsSmokeResult(
                test_id="gemini_short",
                label="Gemini API TTS (court)",
                intention="Vérifier la **clé GEMINI_API_KEY** en repli (modèle API `preview-tts`, ≠ Vertex).",
                production_hint="**Repli** si Vertex refuse ou quota",
                status="Ignoré",
                route="—",
                duration_s=None,
                bytes_n=None,
                detail="GEMINI_API_KEY absente dans ce runtime.",
            )
        )
    else:
        t0 = time.perf_counter()
        last_err = ""
        audio_b: bytes | None = None
        mime = ""
        model_used = ""
        for model in ("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"):
            try:
                cli = GeminiTtsApiClient(api_key=str(gemini_key))
                res = cli.generate_audio(model=model, text=sample_short, voice_name=voice_name)
                audio_b = res.audio_bytes
                mime = res.mime_type or "audio/wav"
                model_used = model
                break
            except Exception as ex:
                last_err = str(ex)
        dt = time.perf_counter() - t0
        if audio_b:
            results.append(
                _TtsSmokeResult(
                    test_id="gemini_short",
                    label="Gemini API TTS (court)",
                    intention="Vérifier la **clé GEMINI_API_KEY** en repli (modèle API `preview-tts`, ≠ Vertex).",
                    production_hint="**Repli** si Vertex refuse ou quota",
                    status="OK",
                    route=f"gemini_api ({model_used})",
                    duration_s=dt,
                    bytes_n=len(audio_b),
                    detail="Modèle API : `gemini-2.5-flash-preview-tts` (le GA `flash-tts` est réservé à Vertex).",
                    audio_bytes=audio_b,
                    audio_mime=mime,
                )
            )
        else:
            results.append(
                _TtsSmokeResult(
                    test_id="gemini_short",
                    label="Gemini API TTS (court)",
                    intention="Vérifier la **clé GEMINI_API_KEY** en repli (modèle API `preview-tts`, ≠ Vertex).",
                    production_hint="**Repli** si Vertex refuse ou quota",
                    status="KO",
                    route="gemini_api",
                    duration_s=dt,
                    bytes_n=None,
                    detail=last_err[:220],
                )
            )

    # 3 — Lectures : chemin production (Vertex → repli Gemini)
    if not gemini_key:
        results.append(
            _TtsSmokeResult(
                test_id="readings_production",
                label="Audio lectures (production)",
                intention="Simuler **exactement** le code « Compléter les manquants » (4 lectures AELF lues à voix haute).",
                production_hint="**Oui** — fichier `AudioLectures/…`",
                status="Ignoré",
                route="—",
                duration_s=None,
                bytes_n=None,
                detail="GEMINI_API_KEY requise si Vertex TTS refuse l'audio.",
                is_production_path=True,
            )
        )
    else:
        t0 = time.perf_counter()
        route_used = "vertex_tts → gemini_api"
        try:
            vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
            audio_b, mime, _ext = tts_readings_audio_bytes(
                cfg=cfg,
                text=sample_readings,
                voice_name=voice_name,
                vertex_client=vx,
                gemini_api_key=str(gemini_key),
            )
            dt = time.perf_counter() - t0
            from core.sunday_gemini_tts import last_tts_route

            route_used = last_tts_route() or "vertex_tts"
            results.append(
                _TtsSmokeResult(
                    test_id="readings_production",
                    label="Audio lectures (production)",
                    intention="Simuler **exactement** le code « Compléter les manquants » (4 lectures AELF lues à voix haute).",
                    production_hint="**Oui** — fichier `AudioLectures/…`",
                    status="OK",
                    route=route_used,
                    duration_s=dt,
                    bytes_n=len(audio_b),
                    detail="Découpage liturgique + voix sélectionnée.",
                    audio_bytes=audio_b,
                    audio_mime=mime,
                    is_production_path=True,
                )
            )
        except Exception as ex:
            dt = time.perf_counter() - t0
            results.append(
                _TtsSmokeResult(
                    test_id="readings_production",
                    label="Audio lectures (production)",
                    intention="Simuler **exactement** le code « Compléter les manquants » (4 lectures AELF lues à voix haute).",
                    production_hint="**Oui** — fichier `AudioLectures/…`",
                    status="KO",
                    route=route_used,
                    duration_s=dt,
                    bytes_n=None,
                    detail=str(ex)[:220],
                    is_production_path=True,
                )
            )

    # 4 — Synthèse audio : simulation repli (Vertex refuse → Gemini morceau)
    if not gemini_key:
        results.append(
            _TtsSmokeResult(
                test_id="synthesis_production",
                label="Audio synthèse (repli)",
                intention="Simuler le TTS de la **synthèse dominicale** (bouton « Tout régénérer »).",
                production_hint="**Oui** — fichier `Audio/…`",
                status="Ignoré",
                route="—",
                duration_s=None,
                bytes_n=None,
                detail="GEMINI_API_KEY absente.",
                is_production_path=True,
            )
        )
    else:
        t0 = time.perf_counter()
        route_used = "vertex_tts"
        try:
            vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
            from core.sunday_gemini_tts import last_tts_route, tts_spoken_audio_bytes

            audio_b, mime, _ext = tts_spoken_audio_bytes(
                cfg=cfg,
                text=sample_short,
                voice_name=voice_name,
                vertex_client=vx,
                gemini_api_key=str(gemini_key),
            )
            dt = time.perf_counter() - t0
            route_used = last_tts_route() or "vertex_tts"
            results.append(
                _TtsSmokeResult(
                    test_id="synthesis_production",
                    label="Audio synthèse (production)",
                    intention="Simuler le TTS de la **synthèse dominicale** (bouton « Tout régénérer »).",
                    production_hint="**Oui** — fichier `Audio/…`",
                    status="OK",
                    route=route_used,
                    duration_s=dt,
                    bytes_n=len(audio_b),
                    detail="Même code que « Tout régénérer » (TTS morcelé Vertex → repli Gemini).",
                    audio_bytes=audio_b,
                    audio_mime=mime,
                    is_production_path=True,
                )
            )
        except Exception as ex:
            dt = time.perf_counter() - t0
            results.append(
                _TtsSmokeResult(
                    test_id="synthesis_production",
                    label="Audio synthèse (production)",
                    intention="Simuler le TTS de la **synthèse dominicale** (bouton « Tout régénérer »).",
                    production_hint="**Oui** — fichier `Audio/…`",
                    status="KO",
                    route=route_used,
                    duration_s=dt,
                    bytes_n=None,
                    detail=str(ex)[:220],
                    is_production_path=True,
                )
            )

    return results


def _render_tts_smoke_results_table(results: list[_TtsSmokeResult]) -> None:
    st.caption(
        "**Intention** = ce que ce test vérifie sur la page Ressources. "
        "**En prod si OK** = ce qui se passe réellement lors de la génération d'un dimanche lorsque le test réussit."
    )
    lines = [
        "| Test | Intention (Ressources) | En prod si OK | Statut | Canal effectif | Durée | Taille | Détail |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for r in results:
        dur = f"{r.duration_s:.2f} s" if r.duration_s is not None else "—"
        size = f"{r.bytes_n:,} o".replace(",", " ") if r.bytes_n is not None else "—"
        stat = f"**{r.status}**"
        if r.is_production_path and r.status == "OK":
            stat = f"**{r.status}** ✓"
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_table_cell(r.label),
                    _md_table_cell(r.intention),
                    _md_table_cell(r.production_hint),
                    stat,
                    _md_table_cell(r.route),
                    _md_table_cell(dur),
                    _md_table_cell(size),
                    _md_table_cell(r.detail[:100]),
                ]
            )
            + " |"
        )
    st.markdown("\n".join(lines))


def _production_tts_verdict(*, results: list[_TtsSmokeResult], gemini_key: str | None) -> None:
    prod = [r for r in results if r.is_production_path]
    ok = [r for r in prod if r.status == "OK"]
    if len(ok) == len(prod) and prod:
        routes = ", ".join({r.route for r in ok})
        st.success(
            "Génération dominicale : les **deux chemins production** (synthèse + lectures) sont OK. "
            f"Canaux effectifs observés : **{routes}**."
        )
    elif not gemini_key:
        st.error(
            "Génération dominicale : **GEMINI_API_KEY** absente — l'audio échouera si Vertex TTS "
            "n'est pas allowlisté sur le projet GCP."
        )
    else:
        ko = [r.label for r in prod if r.status != "OK"]
        st.warning(
            "Génération dominicale : certains chemins production ont échoué — "
            + ", ".join(ko)
            + ". Corrige avant de regénérer un dimanche."
        )
    vertex = next((r for r in results if r.test_id == "vertex_tts"), None)
    gem = next((r for r in results if r.test_id == "gemini_short"), None)
    if vertex and vertex.status == "OK":
        st.info(
            "Vertex TTS est **opérationnel** sur ce projet (`gemini-2.5-flash-tts` via GCP). "
            "La génération dominicale utilisera **Vertex en priorité** — meilleure qualité que l'API Gemini seule."
        )
        if gem and gem.status == "KO" and "404" in (gem.detail or ""):
            st.caption(
                "Le test « Gemini API TTS (court) » en KO est **normal** : le modèle `gemini-2.5-flash-tts` "
                "n'existe que sur Vertex. Le repli Gemini API utilise `gemini-2.5-flash-preview-tts` si Vertex refuse."
            )
    elif vertex and vertex.status == "KO" and gemini_key:
        if gem and gem.status == "OK":
            st.info(
                "Vertex TTS refuse l'audio (allowlist) — l'enregistrement passera par **Gemini API TTS** "
                "(`gemini-2.5-flash-preview-tts`)."
            )


def _render_admin_ia_smoke_tests(*, cfg: object) -> None:
    """Batterie TTS : tous les tests d'un coup + tableau de synthèse."""
    _render_gemini_api_key_diagnostics(cfg=cfg)
    st.divider()
    st.markdown("#### Batterie TTS — synthèse des canaux")
    st.caption(
        "Lance **tous** les tests d'un coup. Le tableau indique quel canal est réellement utilisé "
        "(Vertex GCP ou repli **Gemini API**). Les lignes marquées **✓ prod.** reproduisent la génération dominicale."
    )
    gemini_key = resolve_gemini_api_key() or getattr(cfg, "gemini_api_key", None)
    _tnames = gemini_tts_voice_names_ordered()
    _tmap, _ = load_gemini_tts_voice_catalog()
    try:
        _tix = _tnames.index("Charon")
    except ValueError:
        try:
            _tix = _tnames.index("Achird")
        except ValueError:
            _tix = 0
    test_voice_pick = st.selectbox(
        "Voix pour les tests TTS",
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

    col_run, col_clr = st.columns([3, 1])
    with col_run:
        run_all = st.button(
            "Lancer toute la batterie TTS",
            key="adm_tts_battery_run",
            type="primary",
        )
    with col_clr:
        if st.button("Effacer les résultats", key="adm_tts_battery_clear"):
            st.session_state.pop("adm_tts_battery_results", None)
            st.rerun()

    if run_all:
        ov = loading_overlay("Batterie TTS en cours (Vertex + Gemini + production)…")
        try:
            st.session_state["adm_tts_battery_results"] = _run_tts_smoke_battery(
                cfg=cfg,
                gemini_key=str(gemini_key) if gemini_key else None,
                voice_name=test_voice_use,
            )
            st.session_state["adm_tts_battery_voice"] = test_voice_use
        finally:
            ov.empty()

    results: list[_TtsSmokeResult] = st.session_state.get("adm_tts_battery_results") or []
    if results:
        if st.session_state.get("adm_tts_battery_voice") != test_voice_use:
            st.caption(
                f"Résultats ci-dessous pour la voix **{st.session_state.get('adm_tts_battery_voice')}** "
                f"(voix sélectionnée actuellement : **{test_voice_use}** — relance la batterie pour comparer)."
            )
        _render_tts_smoke_results_table(results)
        _production_tts_verdict(results=results, gemini_key=str(gemini_key) if gemini_key else None)

        with st.expander("Écouter les extraits audio réussis", expanded=False):
            for r in results:
                if r.status != "OK" or not r.audio_bytes:
                    continue
                st.markdown(f"**{r.label}** — `{r.route}`")
                st.audio(r.audio_bytes, format=r.audio_mime or "audio/wav")
    else:
        st.info("Clique sur **Lancer toute la batterie TTS** pour afficher le tableau de résultats.")

    st.divider()
    st.markdown("#### Vertex AI — texte (hors TTS)")
    st.caption("Vérifie le canal **rédaction** de la synthèse écrite (distinct de l'audio).")
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
            st.success(f"VertexAI texte OK — {res.model} en {dt:.2f}s")
            st.code((res.text or "").strip()[:400] or "—")
        except Exception as e:
            st.error(f"VertexAI texte KO — {e}")
        finally:
            ov.empty()


def _render_admin_tts_pronunciation_viewer(*, cfg: object) -> None:
    """Liste fusionnée du dictionnaire TTS (fichier dépôt + surcharges Sheets)."""
    import app as ap

    st.caption(
        "Appliqué automatiquement à **tout** TTS (lectures AELF + synthèse) via `spoken_text_for_tts`. "
        "N'affecte ni le PDF ni le texte affiché à l'écran."
    )
    try:
        ap._load_prompt_templates_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=ap._service_account_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
        )
    except Exception:
        pass

    bd = tts_pronunciation_breakdown()
    file_rules = dict(bd.get("file") or {})
    sheet_rules = dict(bd.get("sheet") or {})
    merged = dict(bd.get("merged") or {})
    json_path = str(bd.get("json_path") or "data/tts_pronunciation_fr.json")

    c1, c2, c3 = st.columns(3)
    c1.metric("Entrées (fichier dépôt)", len(file_rules))
    c2.metric("Surcharges Sheets", len(sheet_rules))
    c3.metric("Effectif au TTS", len(merged))

    if not sheet_rules:
        st.info(
            "Aucune ligne **Actif** pour la clé `tts_pronunciation` dans `Paramètres_IA` — "
            "seul le fichier dépôt est utilisé pour l'instant. "
            "Pour éditer sans redéployer : section **Audio — consignes de style** → "
            "activer **Créer un nouveau prompt** → clé `tts_pronunciation`, "
            "ou lancer `python .\\tools\\init_sheets_db.py` pour créer la ligne initiale.",
            icon="ℹ️",
        )

    if merged:
        lines = [f"{k}\t{v}" for k, v in sorted(merged.items(), key=lambda kv: kv[0].lower())]
        st.markdown("**Dictionnaire effectif** (mot affiché → forme lue par la voix)")
        st.code("\n".join(lines), language=None)
    else:
        st.warning("Dictionnaire vide — ajoutez des entrées dans le fichier dépôt ou dans Sheets.")

    with st.expander("Détail des sources", expanded=False):
        st.markdown(f"**Fichier dépôt** : `{json_path}`")
        st.code(json.dumps(file_rules, ensure_ascii=False, indent=2) or "{}", language="json")
        st.markdown("**Surcharges `Paramètres_IA` → clé `tts_pronunciation`** (prioritaires sur le fichier)")
        st.code(json.dumps(sheet_rules, ensure_ascii=False, indent=2) or "{}", language="json")

    if str(getattr(cfg, "gsheet_id", "") or "").strip() and getattr(cfg, "gcp_service_account", None):
        st.caption(
            "Régénération depuis `readings_cache` (colonnes fête + 4 lectures) : "
            "met à jour le fichier dépôt et peut publier une nouvelle version Sheets."
        )
        pub = st.checkbox(
            "Publier aussi dans `Paramètres_IA` (nouvelle ligne Actif)",
            value=True,
            key="adm_tts_pron_publish",
        )
        force = st.checkbox(
            "Forcer une nouvelle version même si une ligne Actif existe",
            value=False,
            key="adm_tts_pron_force",
        )
        if st.button("Régénérer le dictionnaire depuis readings_cache", key="adm_tts_pron_rebuild"):
            import app as ap

            ov = loading_overlay("Analyse readings_cache et construction du dictionnaire…")
            try:
                from core.sheets_db import build_gspread_client, fetch_records, sheet_row_status_is_live
                from core.tts_pronunciation import clear_tts_pronunciation_file_cache
                from core.tts_pronunciation_lexicon import (
                    build_pronunciation_dict_from_readings_rows,
                    pronunciation_dict_to_json_text,
                )
                from pathlib import Path as _Path

                gs_loc = build_gspread_client(cfg.gcp_service_account)
                rc = fetch_records(
                    gspread_client=gs_loc,
                    spreadsheet_id=str(cfg.gsheet_id).strip(),
                    table="readings_cache",
                    limit=0,
                )
                live = [
                    r
                    for r in rc
                    if sheet_row_status_is_live(r.get("status"))
                    and not str(r.get("error") or "").strip()
                ]
                rules = build_pronunciation_dict_from_readings_rows(live, include_manual_always=True)
                body = pronunciation_dict_to_json_text(rules)
                out_path = _Path("data/tts_pronunciation_fr.json")
                out_path.write_text(body, encoding="utf-8")
                clear_tts_pronunciation_file_cache()
                msg = f"{len(rules)} entrée(s) — fichier dépôt mis à jour ({len(live)} lignes RDC analysées)."
                if pub:
                    from tools.seed_tts_pronunciation_from_readings_cache import (
                        _append_parametres_ia_tts_pronunciation,
                        _parametres_ia_has_active_tts_pronunciation,
                    )

                    if _parametres_ia_has_active_tts_pronunciation(
                        gc=gs_loc, gsheet_id=str(cfg.gsheet_id).strip()
                    ) and not force:
                        st.warning(
                            msg + " Sheets inchangé (ligne Actif déjà présente — cochez « Forcer »)."
                        )
                    else:
                        _append_parametres_ia_tts_pronunciation(
                            gc=gs_loc, gsheet_id=str(cfg.gsheet_id).strip(), body=body
                        )
                        ap._load_prompt_templates_cached.clear()
                        st.success(msg + " Nouvelle ligne `tts_pronunciation` publiée dans Sheets.")
                else:
                    st.success(msg)
                st.rerun()
            except Exception as ex:
                st.error(f"Échec régénération : {ex}")
            finally:
                ov.empty()


def _render_admin_voix_audio_section(*, cfg: object, gs: object) -> None:
    """Affichage et édition des règles `Voix_Audio` (VOIX) — sans `st.dataframe`."""
    st.caption(
        "Règles append-only : **Cible** (`synthese`, `lectures`, `*`), **Couleur**, **Temps liturgique** (`pascal`, `careme`, … ou `*`). "
        "La règle la plus **spécifique** l'emporte. Catalogue des voix : `data/gemini_tts_voices.json` (à jour avec la doc Google)."
    )
    try:
        voix_all = load_voix_rules_cached(
            gsheet_id=str(getattr(cfg, "gsheet_id", "") or "").strip(),
            service_account_fingerprint=service_account_json_fingerprint(
                getattr(cfg, "gcp_service_account", {}) or {}
            ),
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
    if audio_only and "tts_pronunciation" not in existing_keys:
        existing_keys = sorted(existing_keys + ["tts_pronunciation"])

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
            if not current and picked == "tts_pronunciation":
                try:
                    file_rules = dict(tts_pronunciation_breakdown().get("file") or {})
                    current = json.dumps(file_rules, ensure_ascii=False, indent=2)
                except Exception:
                    current = ""
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
        st.caption(
            "Pour préserver le quota Google Sheets (~300 lectures/min), le diagnostic complet "
            "ne se lance **que sur action** (pas à chaque rerun Streamlit)."
        )
        if st.button("Lancer / actualiser le diagnostic", key="adm_sheets_diag_run_btn"):
            st.session_state["adm_sheets_diag_run"] = True
        if st.session_state.get("adm_sheets_diag_run"):
            _render_admin_infra_diagnostic(cfg=cfg)
        else:
            st.info("Clique sur le bouton ci-dessus pour tester Cloud et Sheets.")

    with st.expander(
        "Tests rapides IA (smoke TTS / texte)",
        expanded=False,
        key="adm_res_exp_smoke",
    ):
        _render_admin_ia_smoke_tests(cfg=cfg)

    if not (cfg.gsheet_id and cfg.gcp_service_account):
        st.info("Configure `gsheet_id` + `gcp_service_account` pour gérer voix et prompts.")
        return

    sa_json = service_account_json_fingerprint(cfg.gcp_service_account)
    try:
        rows = adm_sheets_fetch_cached(cfg.gsheet_id, "Paramètres_IA", 5000, sa_json)
    except Exception as e:
        rows = []
        msg = str(e)
        if "429" in msg or "Quota exceeded" in msg:
            st.warning(
                "Lecture `Paramètres_IA` reportée — quota Sheets saturé. "
                "Attends une minute puis recharge la page."
            )
        else:
            st.warning(f"Lecture `Paramètres_IA` impossible : {e}")

    gs = build_gspread_client(cfg.gcp_service_account)

    with st.expander(
        "Dictionnaire TTS (prononciation)",
        expanded=True,
        key="adm_res_exp_tts_pron",
    ):
        _render_admin_tts_pronunciation_viewer(cfg=cfg)

    with st.expander(
        "Audio — voix TTS (table `Voix_Audio`)",
        expanded=False,
        key="adm_res_exp_voix",
    ):
        _render_admin_voix_audio_section(cfg=cfg, gs=gs)

    with st.expander(
        "Audio — consignes de style (prompts `audio_style_*`, documentation)",
        expanded=False,
        key="adm_res_exp_audio",
    ):
        st.caption(
            "Consignes `audio_style_*` : documentation du style (non lues au TTS). "
            "Pour **modifier le dictionnaire de prononciation**, utilisez la section "
            "**Dictionnaire TTS (prononciation)** ci-dessus (lecture) ou créez la clé "
            "`tts_pronunciation` ici (édition Sheets). Append-only : chaque enregistrement "
            "crée une nouvelle version."
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
