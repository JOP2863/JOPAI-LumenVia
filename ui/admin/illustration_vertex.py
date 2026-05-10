"""Admin — Visuels liturgiques (étape 3) : grille Vertex + manifeste."""

from __future__ import annotations

import io
from datetime import date
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.illustration_thumbs import gcs_thumb_path_from_source_blob
from core.storage import blob_exists, download_bytes, upload_bytes
from core.vertex_gemini import VertexGeminiClient
from ui.components import loading_overlay


def _admin_target_has_illustration(*, gcs: object, bucket_name: str, target: dict) -> bool:
    return _admin_first_existing_blob_path(gcs=gcs, bucket_name=bucket_name, target=target) is not None


def _admin_first_existing_blob_path(
    *,
    gcs: object,
    bucket_name: str,
    target: dict,
    errors: list[str] | None = None,
) -> str | None:
    cand: list[str] = []
    p0 = str(target.get("gcs_path_primary") or "").strip()
    if p0:
        cand.append(p0)
    for a in target.get("alternates") or []:
        s = str(a or "").strip()
        if s:
            cand.append(s)
    for path in cand:
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=path):
                return path
        except Exception as ex:
            if errors is not None and len(errors) < 6:
                errors.append(f"{path} — {ex}")
            continue
    return None


def _admin_best_display_blob_path(*, gcs: object, bucket_name: str, target: dict) -> str | None:
    """Préfère la vignette ``Images/thumbs`` si elle existe, sinon le fichier illustration."""
    full = _admin_first_existing_blob_path(gcs=gcs, bucket_name=bucket_name, target=target)
    if not full:
        return None
    tp = gcs_thumb_path_from_source_blob(full)
    try:
        if blob_exists(gcs=gcs, bucket_name=bucket_name, path=tp):
            return tp
    except Exception:
        pass
    return full


def _admin_iso_week_label(date_str: str) -> str:
    try:
        d = date.fromisoformat(str(date_str).strip()[:10])
        return str(d.isocalendar()[1])
    except Exception:
        return "—"


def _admin_sort_targets_by_date(targets: list[dict]) -> list[dict]:
    return sorted(targets, key=lambda t: str(t.get("date") or ""))


def _admin_targets_presence_compact(
    targets_sorted: list[dict],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    sig: list[tuple[str, str, tuple[str, ...]]] = []
    for t in targets_sorted:
        ds = str(t.get("date") or "").strip()[:10]
        p0 = str(t.get("gcs_path_primary") or "").strip()
        alts = tuple(
            sorted(
                {
                    str(a or "").strip()
                    for a in (t.get("alternates") or [])
                    if str(a or "").strip()
                }
            )
        )
        sig.append((ds, p0, alts))
    return tuple(sig)


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def _admin_cached_manifest_cloud_presence(
    bucket_name: str,
    account_fp: str,
    manifest_mtime_ns: int,
    manifest_size: int,
    compact: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[tuple[bool, ...], tuple[str | None, ...], tuple[str, ...]]:
    """
    Probe GCS blob existence for every manifest target (heavy). Cached ~5 min keyed by manifest size/mtime + bucket + SA fingerprint.
    Exécution séquentielle pour éviter les problèmes de concurrence sur le client Storage.
    """
    if not compact:
        return (), (), ()

    errs: list[str] = []
    cfg_inner = load_config()
    gcs_inner = build_gcs_client(cfg_inner.gcp_service_account)
    has_list: list[bool] = []
    path_list: list[str | None] = []
    for row in compact:
        ds, p0, alts = row
        target_dict = {"date": ds, "gcs_path_primary": p0, "alternates": list(alts)}
        pth = _admin_first_existing_blob_path(
            gcs=gcs_inner,
            bucket_name=bucket_name,
            target=target_dict,
            errors=errs if len(errs) < 8 else None,
        )
        has_list.append(pth is not None)
        path_list.append(pth)
    return tuple(has_list), tuple(path_list), tuple(errs[:8])


def _admin_execute_image_generations(
    *,
    cfg: object,
    gcs: object,
    vx: VertexGeminiClient,
    to_run: list[dict],
    aspect: str,
    pause_s: float,
    dry_run: bool,
    preferred_models: list[str],
    skip_existing: bool,
) -> list[str]:
    lines: list[str] = []
    n = len(to_run)
    prog = st.progress(0.0)
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    for i, t in enumerate(to_run):
        ds = str(t.get("date") or "")
        if skip_existing and _admin_target_has_illustration(gcs=gcs, bucket_name=bucket, target=t):
            lines.append(f"Skip {ds} — fichier déjà présent.")
            prog.progress(min(1.0, (i + 1) / max(n, 1)))
            continue

        prompt = str(t.get("prompt_midjourney_style") or "").strip()
        if not prompt:
            tempo = str(t.get("temps_liturgique") or "").strip()
            col = str(t.get("couleur") or "").strip()
            prompt = (
                "Minimalist Catholic liturgical illustration, woodcut-inspired line art, "
                f"gold accent #D4AF37 on cream, serene, wordless symbolic scene; "
                f"season mood (no labels): {tempo or 'Sunday'}; palette mood: {col or 'gold'}."
            )
        prompt_final = _augment_vertex_illustration_prompt(prompt)

        overlay = loading_overlay(f"Illustration du dimanche {ds}…")
        try:
            try:
                img_res = vx.generate_image_auto(
                    preferred_models=preferred_models,
                    prompt=prompt_final,
                    aspect_ratio=aspect,
                )
            except Exception as ex:
                lines.append(f"KO {ds} — {ex}")
                prog.progress(min(1.0, (i + 1) / max(n, 1)))
                continue
        finally:
            overlay.empty()

        dest = _admin_pick_gcs_path_for_upload(t, img_res.mime_type)
        ct = img_res.mime_type if (img_res.mime_type or "").startswith("image/") else "image/png"

        if dry_run:
            st.image(io.BytesIO(img_res.image_bytes), caption=f"{ds} — {img_res.model}")
            lines.append(f"Dry-run OK {ds} — modèle {img_res.model}")
        else:
            try:
                upload_bytes(
                    gcs=gcs,
                    bucket_name=bucket,
                    path=dest,
                    data=img_res.image_bytes,
                    content_type=ct,
                )
                lines.append(f"OK {ds} → `gs://{bucket}/{dest}` ({img_res.model})")
            except Exception as ex:
                lines.append(f"Upload KO {ds} — {ex}")

        prog.progress(min(1.0, (i + 1) / max(n, 1)))
        if pause_s > 0 and i < n - 1:
            time.sleep(float(pause_s))

    prog.progress(1.0)
    return lines


def _admin_finish_generation_log(lines: list[str], *, dry_run: bool) -> None:
    if not lines:
        return
    log_txt = "\n".join(lines)
    st.text_area("Journal du lot", value=log_txt, height=min(260, 80 + 18 * max(len(lines), 1)))
    if any(ln.startswith("OK ") for ln in lines):
        st.success(
            "Au moins une image est enregistrée sur le bucket. Cherche les lignes **OK … → `gs://`** ci-dessus."
        )
    elif dry_run and lines:
        st.warning("Mode **aperçu seulement** : aucun fichier n’a été envoyé sur Cloud.")


def _augment_vertex_illustration_prompt(base: str) -> str:
    """Consigne stricte anti-texte (les modèles orthographient très mal les mots dans l’image)."""
    prefix = (
        "CRITICAL ZERO-TEXT RULE — The image must contain NO glyphs at all: "
        "no letters, Latin or French words, evangelist names, liturgical titles, numbers, captions, "
        "subtitles, banners, speech bubbles, scrolls with writing, open books with visible lines, "
        "mock typography, watermarks, or logos. "
        "If any word appears it will be misspelled — therefore paint NO words and NO readable characters in any language. "
        "Show mood and theme only through wordless symbolism: figures without labels, landscape, abstract shapes, "
        "crosses, bread/grapes as icons without text. "
        "Any comma-separated theme hints below are for mood only — do not spell them as labels or titles in the picture.\n\n"
    )
    suffix = (
        "\n\nFINAL CHECK: output must be purely visual with zero readable text anywhere in the frame."
    )
    return f"{prefix}{(base or '').strip()}{suffix}"


def _admin_pick_gcs_path_for_upload(target: dict, mime_type: str) -> str:
    """Choisit un chemin manifeste cohérent (PNG/JPG préféré selon le MIME renvoyé par Vertex)."""
    m = (mime_type or "").lower()
    alts = list(target.get("alternates") or [])
    if "png" in m:
        for a in alts:
            if str(a).lower().endswith(".png"):
                return str(a).strip()
    if "jpeg" in m or "jpg" in m:
        for a in alts:
            if str(a).lower().endswith((".jpg", ".jpeg")):
                return str(a).strip()
    ds = str(target.get("date") or "").strip()
    y = ds[:4] if len(ds) >= 4 else "2026"
    return f"Images/illustrations/{y}/{ds}.png"


def render_admin_illustration_gen_panel(*, data: dict, manifest_path: Path) -> None:
    st.subheader("Génération Vertex AI → bucket Cloud")
    st.info(
        "**Stockage Cloud** : pour que l’image soit **envoyée sur le bucket**, laisse la case "
        "« Aperçu seulement… » **décochée**. Si elle est cochée, tu vois l’image à l’écran mais "
        "**rien n’est enregistré** dans Google Cloud Storage."
    )

    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name` dans les secrets.")
        return

    targets_all = list(data.get("targets") or [])
    if not targets_all:
        st.warning("Aucune cible dans le manifeste.")
        return

    bucket_name = str(cfg.gcs_bucket_name).strip()
    sorted_targets = _admin_sort_targets_by_date(targets_all)
    try:
        mstat = manifest_path.stat()
        m_mtime_ns = int(getattr(mstat, "st_mtime_ns", int(mstat.st_mtime * 1e9)))
        m_sz = int(mstat.st_size)
    except Exception:
        m_mtime_ns, m_sz = 0, 0
    compact_presence = _admin_targets_presence_compact(sorted_targets)
    import app as ap
    sa_fp = ap._service_account_fingerprint(cfg.gcp_service_account)

    c_cache, _ = st.columns([1, 3])
    with c_cache:
        if st.button("Invalider le cache Cloud (rafraîchir la grille)", key="adm_img_cache_clear"):
            _admin_cached_manifest_cloud_presence.clear()
            st.rerun()

    # Présence sur le bucket : résultat mis en cache (TTL) pour accélérer les navigations suivantes.
    ov_load = loading_overlay("Vérification de la présence des fichiers sur Cloud…")
    try:
        gcs = build_gcs_client(cfg.gcp_service_account)
        has_tpl, paths_tpl, err_tpl = _admin_cached_manifest_cloud_presence(
            bucket_name,
            sa_fp,
            m_mtime_ns,
            m_sz,
            compact_presence,
        )
        err_samples = list(err_tpl)
        first_paths = list(paths_tpl)
        has_map = list(has_tpl)
    finally:
        ov_load.empty()
    n_missing = sum(1 for h in has_map if not h)

    COLS, ROWS = 10, 6
    per_page = COLS * ROWS
    n_targets = len(sorted_targets)
    n_pages = max(1, (n_targets + per_page - 1) // per_page)

    # Cocher / décocher en masse : doit s'exécuter AVANT les st.checkbox (adm_sel_*), sinon Streamlit bloque.
    _pg_bulk = int(st.session_state.get("adm_grid_page", 0))
    _pg_bulk = max(0, min(_pg_bulk, n_pages - 1))
    _slice_bulk = _pg_bulk * per_page
    if st.session_state.pop("_adm_bulk_check_page", False):
        for gi in range(_slice_bulk, min(_slice_bulk + per_page, n_targets)):
            if not has_map[gi]:
                st.session_state[f"adm_sel_{gi}"] = True
    if st.session_state.pop("_adm_bulk_uncheck_page", False):
        for gi in range(_slice_bulk, min(_slice_bulk + per_page, n_targets)):
            k = f"adm_sel_{gi}"
            if k in st.session_state:
                st.session_state[k] = False

    c1, c2 = st.columns(2)
    with c1:
        aspect = st.selectbox("Ratio d’image", ["4:3", "3:4", "1:1", "16:9"], index=0, key="adm_img_aspect")
    with c2:
        pause_s = st.number_input(
            "Tempo après chaque image avant la suivante",
            min_value=0,
            max_value=180,
            value=2,
            step=1,
            key="adm_img_pause",
        )

    models_line = st.text_input(
        "Modèles Vertex à essayer (ordre, séparés par des virgules)",
        value="gemini-2.5-flash-image,gemini-3-pro-image-preview",
        key="adm_img_models",
    )
    preferred_models = [x.strip() for x in models_line.split(",") if x.strip()]

    dry_run = st.checkbox(
        "Aperçu seulement — ne pas envoyer sur Cloud (aucun fichier dans le bucket)",
        value=False,
        key="adm_img_dry",
    )

    # --- Grille 10 × 6 : semaine ISO, vignette ou sélection si manquant ---
    st.divider()
    st.subheader("Calendrier des illustrations")
    st.caption(
        f"**{n_missing}** dimanche(s) sans fichier sur Cloud sur **{len(sorted_targets)}** — "
        f"manifeste `{manifest_path.as_posix()}`. Semaine = **numéro ISO** (semaine civile du dimanche)."
    )
    if err_samples:
        st.error(
            "Accès Cloud en erreur : l’app n’arrive pas à vérifier l’existence des objets "
            "(souvent bucket incorrect, projet/credentials incorrects, ou droits IAM insuffisants)."
        )
        with st.expander("Exemples d’erreurs (vérification d’existence sur Cloud)"):
            st.code("\n".join(err_samples[:6]))

    page_ix = st.number_input(
        "Page grille (60 cases)",
        min_value=0,
        max_value=max(0, n_pages - 1),
        value=0,
        step=1,
        key="adm_grid_page",
    )
    slice_start = int(page_ix) * per_page

    thumb_bytes: dict[int, bytes] = {}
    to_fetch: list[tuple[int, str]] = []
    for gi in range(slice_start, min(slice_start + per_page, n_targets)):
        if not has_map[gi]:
            continue
        full = first_paths[gi]
        if not full:
            continue
        # Préférer la vignette si présente.
        bp = full
        tp = gcs_thumb_path_from_source_blob(full)
        try:
            if blob_exists(gcs=gcs, bucket_name=bucket_name, path=tp):
                bp = tp
        except Exception as ex:
            if len(err_samples) < 6:
                err_samples.append(f"{tp} — {ex}")
        to_fetch.append((gi, bp))

    if to_fetch:
        with ThreadPoolExecutor(max_workers=12) as ex:
            fut_to_gi: dict = {}
            for gi, bp in to_fetch:
                fut = ex.submit(
                    partial(download_bytes, gcs=gcs, bucket_name=bucket_name, path=bp)
                )
                fut_to_gi[fut] = gi
            for fut in as_completed(fut_to_gi):
                gi = fut_to_gi[fut]
                try:
                    b = fut.result()
                    if b:
                        thumb_bytes[gi] = b
                except Exception:
                    pass

    for row in range(ROWS):
        cols = st.columns(COLS)
        for col_i in range(COLS):
            gi = slice_start + row * COLS + col_i
            with cols[col_i]:
                if gi >= n_targets:
                    continue
                t = sorted_targets[gi]
                ds = str(t.get("date") or "")[:10]
                sw = _admin_iso_week_label(ds)
                st.markdown(
                    f"<div style='font-size:0.72rem;color:#342E29;text-align:center;"
                    f"font-weight:600;margin-bottom:2px;'>S{sw}<br/><span style='font-weight:400'>{ds}</span></div>",
                    unsafe_allow_html=True,
                )
                if has_map[gi]:
                    tb = thumb_bytes.get(gi)
                    if tb:
                        st.image(io.BytesIO(tb), use_container_width=True)
                    else:
                        st.caption("✓ Cloud")
                else:
                    st.checkbox(
                        "Manquant",
                        key=f"adm_sel_{gi}",
                        value=False,
                        label_visibility="visible",
                    )

    ga1, ga2, ga3, ga4 = st.columns(4)
    with ga1:
        if st.button("Cocher manquantes (page)", key="adm_grid_chk_page"):
            st.session_state["_adm_bulk_check_page"] = True
            st.rerun()
    with ga2:
        if st.button("Décocher (page)", key="adm_grid_unchk_page"):
            st.session_state["_adm_bulk_uncheck_page"] = True
            st.rerun()
    with ga3:
        run_missing_page = st.button(
            "Générer toutes les manquantes de la page",
            key="adm_grid_run_page_missing",
        )
    with ga4:
        run_selected = st.button(
            "Générer les cases cochées",
            type="primary",
            key="adm_grid_run_selected",
        )

    vx = VertexGeminiClient(
        service_account_info=cfg.gcp_service_account,
        locations=["global", "europe-west1", "us-central1"],
    )

    if run_missing_page:
        to_gen = [
            sorted_targets[gi]
            for gi in range(slice_start, min(slice_start + per_page, n_targets))
            if not has_map[gi]
        ]
        if not to_gen:
            st.info("Aucun dimanche sans fichier sur cette page.")
        else:
            lines = _admin_execute_image_generations(
                cfg=cfg,
                gcs=gcs,
                vx=vx,
                to_run=to_gen,
                aspect=aspect,
                pause_s=float(pause_s),
                dry_run=dry_run,
                preferred_models=preferred_models,
                skip_existing=False,
            )
            _admin_finish_generation_log(lines, dry_run=dry_run)
            if not dry_run and any(ln.startswith("OK ") for ln in lines):
                _admin_cached_manifest_cloud_presence.clear()

    if run_selected:
        to_gen = [
            sorted_targets[gi]
            for gi in range(n_targets)
            if st.session_state.get(f"adm_sel_{gi}", False) and not has_map[gi]
        ]
        if not to_gen:
            st.warning("Coche au moins un dimanche encore sans fichier (ou utilise « manquantes de la page »).")
        else:
            lines = _admin_execute_image_generations(
                cfg=cfg,
                gcs=gcs,
                vx=vx,
                to_run=to_gen,
                aspect=aspect,
                pause_s=float(pause_s),
                dry_run=dry_run,
                preferred_models=preferred_models,
                skip_existing=False,
            )
            _admin_finish_generation_log(lines, dry_run=dry_run)
            if not dry_run and any(ln.startswith("OK ") for ln in lines):
                _admin_cached_manifest_cloud_presence.clear()



def render_admin_step3() -> None:
    st.title("Admin — Génération des visuels liturgiques")
    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}` (relatif à la racine du projet).")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return

    targets = data.get("targets") or []
    year_hint = ""
    if targets:
        ds0 = str(targets[0].get("date") or "")
        if len(ds0) >= 4:
            year_hint = ds0[:4]

    st.markdown(
        f"""
### À quoi servent ces illustrations

- **Une image par dimanche** listée dans le manifeste : elle correspond à **la semaine liturgique** centrée sur ce dimanche.
- **Dans l’app** : sur « La Lumière du Dimanche », l’image affichée est celle du **dimanche choisi** par l’utilisateur (fichier présent dans le Cloud au chemin du manifeste).
- **Communication** : la même illustration peut illustrer le **SMS**, l’**e-mail** ou la **newsletter** de la semaine pour laquelle tu fixes ce dimanche comme référence.

**Autres usages possibles** : visuel pour **réseaux sociaux** ou **Open Graph** du lien du jour ; **PDF** ou fascicule mensuel ; **diaporama** ou fond d’écran en paroisse ; **carte de partage** (PWA / lien) ; **miniature** dans un récap hebdomadaire ; **kit presse** ou **affiche** locale pour une grande solennité.

### Fréquence de production

Le manifeste est construit **pour une année civile** (script étape 2 avec `--year`). Une fois **toutes** les images générées et déposées sur le Cloud pour cette année, **tu n’as pas besoin d’y revenir** tant que tu restes sur cette même année — sauf **retouche ponctuelle**, **changement de charte**, ou passage à **l’année suivante** (nouveau manifeste + nouvelles images).

{f"**Année couverte par ce fichier** : **{year_hint}** ({len(targets)} dimanches)." if year_hint else f"**Dimanches dans ce manifeste** : {len(targets)}."}
        """.strip()
    )

    render_admin_illustration_gen_panel(data=data, manifest_path=manifest_path)

