"""Admin — Vignettes Cloud et montage annuel."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.illustration_text_audit import existing_illustration_blob_path
from core.illustration_thumbs import (
    THUMB_GCS_PREFIX,
    gcs_thumb_path_from_source_blob,
    generate_thumb_from_source_and_upload,
    thumb_blob_exists,
)
from core.storage import blob_exists, download_bytes, upload_bytes
from ui.admin.illustration_vertex import _admin_sort_targets_by_date
from ui.components import loading_overlay


def render_admin_thumbs() -> None:
    st.title("Génération des vignettes")
    st.caption(
        "Cette page permet d'identifier les images qui nécessitent d'avoir leur équivalent en vignette "
        "pour optimiser les performances du site. Ces vignettes sont ensuite utilisées pour les illustrations "
        "qui ne nécessitent pas les images en taille pleine."
    )
    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}`.")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return
    render_admin_thumbs_panel(data=data)


def render_admin_thumbs_panel(*, data: dict) -> None:
    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name`.")
        return

    gcs = build_gcs_client(cfg.gcp_service_account)
    bucket_name = str(cfg.gcs_bucket_name).strip()
    sorted_targets = _admin_sort_targets_by_date(list(data.get("targets") or []))
    if not sorted_targets:
        st.warning("Aucune cible dans le manifeste.")
        return

    n_src = 0
    n_thumb = 0
    missing_sources: list[str] = []
    for t in sorted_targets:
        src = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
        if not src:
            continue
        n_src += 1
        if thumb_blob_exists(gcs=gcs, bucket_name=bucket_name, source_blob_path=src):
            n_thumb += 1
        else:
            missing_sources.append(src)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Images pleines sur Cloud", n_src)
    with c2:
        st.metric("Vignettes présentes", n_thumb)
    with c3:
        st.metric("Vignettes manquantes", len(missing_sources))

    mx = st.slider("Taille max. du côté (pixels)", min_value=280, max_value=720, value=420, step=20, key="adm_thumb_max")

    st.divider()
    st.subheader("Montage annuel (52 vignettes)")
    years = sorted({str(t.get("date") or "")[:4] for t in sorted_targets if str(t.get("date") or "")[:4].isdigit()})
    year = st.selectbox("Année", options=years or ["2026"], index=0, key="adm_thumb_montage_year")
    montage_path = f"{THUMB_GCS_PREFIX}/montage_{year}.png"
    montage_pastel_path = f"{THUMB_GCS_PREFIX}/montage_{year}_pastel.png"
    montage_preview_path = f"{THUMB_GCS_PREFIX}/montage_{year}_preview.webp"
    st.caption(f"Sortie : `gs://{bucket_name}/{montage_path}` et version pastel pour le dos du PDF.")
    # Perf : ne pas retélécharger le montage à chaque rerun (ex: checkbox).
    cache_key = f"_adm_montage_cache_{year}"
    cache = dict(st.session_state.get(cache_key) or {})
    montage_exists = bool(cache.get("exists")) if "exists" in cache else False

    # Rafraîchir l'état (existence) à la demande seulement.
    if st.button("Rafraîchir l’état du montage", key=f"adm_montage_refresh_{year}"):
        overlay = loading_overlay("Vérification du montage sur Cloud…")
        try:
            montage_exists = blob_exists(gcs=gcs, bucket_name=bucket_name, path=montage_path)
            cache = {"exists": montage_exists}
            st.session_state[cache_key] = cache
        finally:
            overlay.empty()

    # Si jamais pas encore vérifié, on fait une vérif légère (sans download).
    if "exists" not in cache:
        try:
            montage_exists = blob_exists(gcs=gcs, bucket_name=bucket_name, path=montage_path)
            st.session_state[cache_key] = {"exists": montage_exists}
        except Exception:
            montage_exists = False
            st.session_state[cache_key] = {"exists": False}

    if montage_exists:
        st.info("Un montage existe déjà sur Cloud pour cette année.")
        with st.expander("Afficher le montage existant", expanded=False):
            if st.button("Charger l’aperçu (Cloud)", key=f"adm_montage_load_{year}"):
                overlay = loading_overlay("Téléchargement de l’aperçu…")
                try:
                    # On charge une vignette (WebP) beaucoup plus légère que le PNG complet.
                    montage_b = b""
                    try:
                        montage_b = download_bytes(gcs=gcs, bucket_name=bucket_name, path=montage_preview_path)
                    except Exception:
                        montage_b = b""
                    if not montage_b:
                        # Fallback si la vignette n'existe pas encore.
                        montage_b = download_bytes(gcs=gcs, bucket_name=bucket_name, path=montage_path)
                    cache2 = dict(st.session_state.get(cache_key) or {})
                    cache2["bytes"] = montage_b
                    st.session_state[cache_key] = cache2
                finally:
                    overlay.empty()
            montage_b = (st.session_state.get(cache_key) or {}).get("bytes")
            if montage_b:
                st.image(montage_b, caption=f"Montage {year} (depuis Cloud)")

    force_regen_montage = st.checkbox(
        "Régénérer le montage même s’il existe déjà",
        value=False,
        key="adm_thumb_montage_force",
    )

    if st.button(
        "Générer le montage (PNG) et l’enregistrer sur Cloud",
        type="primary",
        disabled=bool(montage_exists and not force_regen_montage),
        key="adm_thumb_montage_btn",
    ):
        overlay = loading_overlay(f"Montage des vignettes {year}…")
        try:
            # Liste des thumbs dans l’ordre des dimanches
            year_targets = [t for t in sorted_targets if str(t.get("date") or "").startswith(str(year))]
            thumb_paths: list[str] = []
            for t in year_targets:
                src = existing_illustration_blob_path(gcs=gcs, bucket_name=bucket_name, target=t)
                if not src:
                    continue
                thumb_paths.append(gcs_thumb_path_from_source_blob(src))

            # Download en parallèle
            from core.illustration_thumbs import build_thumbnail_webp, build_thumbs_montage_png, pastelize_png

            thumbs_bytes: list[tuple[str, bytes]] = []
            with ThreadPoolExecutor(max_workers=16) as ex:
                futs = {ex.submit(download_bytes, gcs=gcs, bucket_name=bucket_name, path=p): p for p in thumb_paths}
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        b = fut.result()
                        if b:
                            thumbs_bytes.append((p, b))
                    except Exception:
                        continue

            # Re-trier selon l’ordre initial (car as_completed)
            idx = {p: i for i, p in enumerate(thumb_paths)}
            thumbs_bytes.sort(key=lambda x: idx.get(x[0], 10**9))

            # Montage portrait (A4) : 52 vignettes → 4 colonnes × 13 lignes.
            montage_png = build_thumbs_montage_png(
                thumbs_bytes,
                cols=4,
                rows=13,
                cell=200,
                pad=10,
                title_cell_text=f"Le Chemin de l'Année\n{year}",
            )
            montage_pastel_png = pastelize_png(montage_png, alpha=0.55)
            montage_preview_webp = build_thumbnail_webp(montage_png, max_side=1200, quality=80)
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_path,
                data=montage_png,
                content_type="image/png",
            )
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_pastel_path,
                data=montage_pastel_png,
                content_type="image/png",
            )
            upload_bytes(
                gcs=gcs,
                bucket_name=bucket_name,
                path=montage_preview_path,
                data=montage_preview_webp,
                content_type="image/webp",
            )
            st.success("Montage enregistré.")
            st.image(montage_png, caption=f"Montage {year} (aperçu)")
            # Met à jour le cache : existe désormais.
            st.session_state[cache_key] = {"exists": True, "bytes": montage_preview_webp}
        finally:
            overlay.empty()

    if not missing_sources:
        st.success("Toutes les vignettes sont déjà générées pour les illustrations présentes sur le bucket.")
    else:
        n_missing = len(missing_sources)
        st.info(
            f"**{n_missing}** vignette(s) manquante(s) sur **{n_src}** image(s) présentes sur Cloud — "
            "tu peux les générer avec le bouton ci-dessous."
        )
        if st.button(
            "Générer les vignettes manquantes",
            type="primary",
            key="adm_thumb_gen_missing",
        ):
            overlay = loading_overlay("Génération des vignettes sur Cloud…")
            prog = st.progress(0.0)
            ok = 0
            err_n = 0
            ntot = len(missing_sources)
            try:

                def _job(src: str) -> None:
                    generate_thumb_from_source_and_upload(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        source_blob_path=src,
                        download_bytes_fn=download_bytes,
                        upload_bytes_fn=upload_bytes,
                        max_side=int(mx),
                    )

                with ThreadPoolExecutor(max_workers=12) as ex:
                    fut_map = {ex.submit(_job, src): src for src in missing_sources}
                    for i, fut in enumerate(as_completed(fut_map)):
                        try:
                            fut.result()
                            ok += 1
                        except Exception:
                            err_n += 1
                        prog.progress(min(1.0, (i + 1) / max(ntot, 1)))
                prog.progress(1.0)
                if ok:
                    st.success(f"{ok} vignette(s) enregistrée(s) sous `{THUMB_GCS_PREFIX}/`.")
                if err_n:
                    st.warning(f"{err_n} erreur(s) — vérifie les logs ou relance.")
            except Exception as ex:
                st.exception(ex)
            finally:
                overlay.empty()
            st.rerun()

