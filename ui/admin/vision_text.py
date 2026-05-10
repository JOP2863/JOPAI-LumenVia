"""Admin — Détection de texte dans les illustrations (Vision API)."""

from __future__ import annotations

import csv
import json
from datetime import date
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client, build_vision_image_annotator_client
from core.illustration_text_audit import (
    all_errors_are_vision_service_disabled,
    audit_targets_for_text,
    extract_console_url_from_error,
    filter_rows_with_text,
    shorten_audit_error_message,
)
from core.illustration_thumbs import (
    extract_gcp_project_id_from_error,
    gcs_thumb_path_from_source_blob,
    generate_thumb_from_source_and_upload,
    vision_console_activation_url,
)
from core.storage import download_bytes, upload_bytes
from core.sheets_db import (
    BASE_COLUMNS,
    TableSpec,
    append_immutable_row,
    append_immutable_rows_bulk,
    build_gspread_client,
    ensure_table,
    fetch_records,
    sheet_row_status_is_live,
    utc_now_iso,
    with_concat,
)
from core.vertex_gemini import VertexGeminiClient
from ui.admin.illustration_vertex import _admin_sort_targets_by_date
from ui.components import loading_overlay


def render_admin_vision_text() -> None:
    st.title("Admin — Détection de texte (Vision)")

    manifest_path = Path("data/manifests/illustration_pipeline.json")
    if not manifest_path.is_file():
        st.error(f"Manifest introuvable : `{manifest_path}`.")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"Lecture JSON impossible : {e}")
        return

    cfg = load_config()
    if not cfg.gcp_service_account:
        st.error("Configure `gcp_service_account` dans `.streamlit/secrets.toml`.")
        return
    if not str(cfg.gcs_bucket_name or "").strip():
        st.error("Configure `gcs_bucket_name`.")
        return

    targets_all = list(data.get("targets") or [])
    if not targets_all:
        st.warning("Aucune cible dans le manifeste.")
        return

    ov_load = loading_overlay("Chargement des cibles Vision…")
    try:
        gcs = build_gcs_client(cfg.gcp_service_account)
        bucket_name = str(cfg.gcs_bucket_name).strip()
        sorted_targets = _admin_sort_targets_by_date(targets_all)
    finally:
        ov_load.empty()

    # Filtre année (par défaut : année courante).
    years = sorted(
        {str(t.get("date") or "")[:4] for t in sorted_targets if str(t.get("date") or "")[:4].isdigit()}
    )
    y_now = str(date.today().year)
    y_default = y_now if y_now in years else (years[-1] if years else y_now)
    year = st.selectbox("Année", options=years or [y_default], index=(years.index(y_default) if y_default in years else 0))
    targets_year = [t for t in sorted_targets if str(t.get("date") or "").startswith(str(year))]
    if not targets_year:
        st.warning("Aucune cible pour cette année dans le manifeste.")
        return

    # Mode “échantillon” : 60 premières entrées de l’année (utile pour tester vite sans UI de pagination).
    per_page = 60
    slice_start = 0

    st.divider()
    st.subheader("Détection de texte dans les images")
    st.write(
        "Cette page va lancer une détection des textes dans les images générées par l'intelligence artificielle. "
        "Elle va détecter s'il y a des anomalies dans les orthographes et identifier un fichier d'exception à régénérer."
    )

    # Valeurs par défaut validées (UX perf) : pas de sélecteurs.
    ta_min = 2
    ta_workers = 8

    # Calcule le nombre d’images concernées (cibles de l’année qui existent sur le Cloud).
    cache_key = f"_adm_vision_existing_set_{year}"
    set_existing: set[str] | None = st.session_state.get(cache_key)
    if set_existing is None:
        try:
            # Listing par préfixe : beaucoup plus rapide qu'un blob_exists() par cible.
            pref = f"Images/illustrations/{year}/"
            bucket = gcs.bucket(bucket_name)
            set_existing = {b.name for b in gcs.list_blobs(bucket, prefix=pref)}
        except Exception:
            set_existing = set()
        st.session_state[cache_key] = set_existing

    def _targets_with_existing_blob(targets: list[dict]) -> list[dict]:
        out: list[dict] = []
        for t in targets:
            cand: list[str] = []
            p0 = str(t.get("gcs_path_primary") or "").strip()
            if p0:
                cand.append(p0)
            for a in t.get("alternates") or []:
                s = str(a or "").strip()
                if s:
                    cand.append(s)
            if any((c in (set_existing or set())) for c in cand):
                out.append(t)
        return out

    eligible = _targets_with_existing_blob(targets_year)
    st.metric("Images concernées (sur Cloud)", len(eligible))
    st.caption(
        "Méthode : Vision détecte des fragments qui *ressemblent* à du texte, puis on compare les mots à un dictionnaire FR. "
        "Les exceptions sont des mots inconnus / sans signification (ex. suites de lettres) ou manifestement mal orthographiés."
    )

    # Traitement par lots si le volume est important.
    batch_size = 120
    st.caption(f"Traitement par lots : {batch_size} images maximum par lancement.")
    _audit_key = "adm_text_audit_last_rows"
    # Queue persistante par année : permet d'enchaîner les lots sans UI complexe.
    q_key = f"_adm_text_audit_queue_{year}"
    done_key = f"_adm_text_audit_done_{year}"
    init_key = f"_adm_text_audit_inited_{year}"
    if q_key not in st.session_state:
        st.session_state[q_key] = list(eligible)
        st.session_state[done_key] = 0
        st.session_state[init_key] = True
        # Nouvelle analyse (année) : on repart de zéro pour éviter l'accumulation et les incohérences de compteurs.
        st.session_state[_audit_key] = []

    remaining = len(st.session_state.get(q_key) or [])
    done_n = int(st.session_state.get(done_key) or 0)
    if remaining > 0:
        st.info(f"Lot prêt : {min(batch_size, remaining)} image(s) à analyser (restant : {remaining} / total : {done_n + remaining}).")
    else:
        if done_n:
            st.success(f"Analyse terminée pour {year} ({done_n} image(s)). Relance une analyse pour recalculer si besoin.")
            if st.button("Relancer l’analyse (recalculer depuis zéro)", key="adm_text_audit_reset"):
                # Réinitialise la file et les résultats pour cette année.
                st.session_state[q_key] = list(eligible)
                st.session_state[done_key] = 0
                st.session_state[_audit_key] = []
                # Force un rerun immédiat pour réactiver le bouton "lot suivant".
                st.rerun()

    if st.button(
        "Lancer l’analyse (lot suivant)",
        key="adm_text_audit_run",
        type="primary",
        disabled=(len(eligible) == 0 or remaining == 0),
    ):
        overlay = loading_overlay("Analyse Vision des illustrations sur Cloud…")
        try:
            queue: list[dict] = list(st.session_state.get(q_key) or [])
            scan_targets = queue[:batch_size]
            vc = build_vision_image_annotator_client(cfg.gcp_service_account)
            rows_new = audit_targets_for_text(
                gcs=gcs,
                bucket_name=bucket_name,
                targets=scan_targets,
                vision_client=vc,
                max_workers=int(ta_workers),
                min_chars=int(ta_min),
            )
            prev = list(st.session_state.get(_audit_key) or [])
            st.session_state[_audit_key] = [*prev, *rows_new]
            # Avance la queue
            st.session_state[q_key] = queue[len(scan_targets) :]
            st.session_state[done_key] = int(st.session_state.get(done_key) or 0) + len(scan_targets)
        except Exception as ex:
            st.exception(ex)
        finally:
            overlay.empty()

    rows = list(st.session_state.get(_audit_key) or [])
    if rows:
        # Whitelist : permet de confirmer qu'une image est "bonne" même si Vision détecte du bruit.
        whitelist: set[str] = set()
        if cfg.gsheet_id and cfg.gcp_service_account:
            wl_key = f"_adm_vision_whitelist_{year}"
            if wl_key not in st.session_state:
                try:
                    gs_wl = build_gspread_client(cfg.gcp_service_account)
                    wl_rows = fetch_records(
                        gspread_client=gs_wl,
                        spreadsheet_id=cfg.gsheet_id,
                        table="vision_text_whitelist",
                        limit=2000,
                    )
                    whitelist = {
                        str(r.get("gcs_path") or "").strip()
                        for r in wl_rows
                        if str(r.get("gcs_path") or "").strip().startswith(f"Images/illustrations/{year}/")
                        and sheet_row_status_is_live(r.get("status"))
                    }
                except Exception:
                    whitelist = set()
                st.session_state[wl_key] = whitelist
            else:
                whitelist = set(st.session_state.get(wl_key) or set())

        flagged = [r for r in filter_rows_with_text(rows) if str(r.get("gcs_path") or "").strip() not in whitelist]
        errs = [r for r in rows if r.get("error")]
        scanned_unique = len({str(r.get("gcs_path") or "").strip() for r in rows if str(r.get("gcs_path") or "").strip()})
        st.metric("Images analysées (Vision)", scanned_unique)

        if errs:
            if all_errors_are_vision_service_disabled(rows) and len(errs) >= max(1, scanned_unique):
                ex0 = str(errs[0].get("error") or "")
                sa_project_id = str(cfg.gcp_service_account.get("project_id") or "").strip()
                sa_quota_project_id = str(
                    cfg.gcp_service_account.get("quota_project_id") or cfg.gcp_service_account.get("project_id") or ""
                ).strip()
                pid_from_err = extract_gcp_project_id_from_error(ex0)
                act_url = extract_console_url_from_error(ex0) or vision_console_activation_url(
                    pid_from_err or sa_quota_project_id or sa_project_id
                )
                st.error(
                    "L’API **Google Cloud Vision** n’est pas activée pour ce projet GCP "
                    "(ou la propagation des droits est encore en cours — attends quelques minutes après activation)."
                )
                if sa_project_id or sa_quota_project_id or pid_from_err:
                    st.info(
                        "Projet ciblé par la config / credentials : "
                        f"`project_id={sa_project_id or '—'}` · "
                        f"`quota_project_id={sa_quota_project_id or '—'}` · "
                        f"`projet détecté dans l’erreur={pid_from_err or '—'}`"
                    )
                st.markdown(f"[Ouvrir la console Google Cloud — activer Cloud Vision API]({act_url})")
                pid_for_links = (pid_from_err or sa_quota_project_id or sa_project_id or "").strip()
                if pid_for_links:
                    billing_url = f"https://console.cloud.google.com/billing?project={pid_for_links}"
                    st.markdown(
                        f"[Vérifier la facturation du projet (souvent la cause si l’API semble « activée »)]({billing_url})"
                    )
            elif all_errors_are_vision_service_disabled(rows):
                st.warning(
                    "Certaines images n’ont pas pu être analysées par Vision (403 service disabled) "
                    "mais d’autres ont réussi. Si l’API vient d’être activée, attends la propagation puis relance."
                )
            else:
                st.warning(f"{len(errs)} erreur(s) Vision ou téléchargement — voir le détail ci-dessous.")

        if flagged:
            st.error(
                f"{len(flagged)} image(s) avec texte détecté (≥ {int(ta_min)} caractères) — candidats au post-traitement."
            )
            buf = StringIO()
            w = csv.DictWriter(
                buf,
                fieldnames=["date", "gcs_path", "gs_uri", "detected_text"],
                extrasaction="ignore",
            )
            w.writeheader()
            for r in flagged:
                w.writerow(
                    {
                        "date": r["date"],
                        "gcs_path": r["gcs_path"],
                        "gs_uri": f"gs://{bucket_name}/{r['gcs_path']}",
                        "detected_text": r.get("detected_text") or "",
                    }
                )
            if not bool(st.session_state.get("_adm_text_audit_hide_csv")):
                st.download_button(
                    "Télécharger la liste (CSV)",
                    data=buf.getvalue().encode("utf-8-sig"),
                    file_name="lumenvia_images_avec_texte.csv",
                    mime="text/csv; charset=utf-8",
                    key="adm_text_audit_csv",
                )
            try:
                from openpyxl import Workbook

                wb = Workbook()
                ws = wb.active
                ws.title = "images_avec_texte"
                ws.append(["date", "gcs_path", "gs_uri", "detected_text"])
                for r in flagged:
                    ws.append(
                        [
                            r.get("date"),
                            r.get("gcs_path"),
                            f"gs://{bucket_name}/{r.get('gcs_path')}",
                            (r.get("detected_text") or ""),
                        ]
                    )
                xbuf = BytesIO()
                wb.save(xbuf)
                st.download_button(
                    "Télécharger la liste (Excel)",
                    data=xbuf.getvalue(),
                    file_name="lumenvia_images_avec_texte.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="adm_text_audit_xlsx",
                )
                st.session_state["_adm_text_audit_hide_csv"] = True
            except Exception as ex:
                st.warning(
                    "Export Excel indisponible (dépendance manquante). Installe `openpyxl` puis relance l’app. "
                    f"Détail: {ex}"
                )

            st.divider()
            st.subheader("Corrections (remplacer → régénérer → écraser sur Cloud)")
            st.caption(
                "Flux économe : on journalise l’audit et les corrections dans Google Sheets (historique), "
                "puis on régénère l’image via Vertex en prenant l’image actuelle comme référence."
            )

            can_sheets = bool(cfg.gsheet_id and cfg.gcp_service_account)
            if not can_sheets:
                st.info("Configure `gsheet_id` (Sheets) pour activer le journal audit/corrections.")

            run_id = sha256(f"vision_audit|{utc_now_iso()}|{bucket_name}".encode("utf-8")).hexdigest()[:12]

            if can_sheets and st.button("Enregistrer cet audit dans Google Sheets", key="adm_vision_audit_save_sheets"):
                ov = loading_overlay("Enregistrement audit Vision dans Sheets…")
                try:
                    from core.sheets_db import TableSpec, ensure_table

                    gs = build_gspread_client(cfg.gcp_service_account)
                    ensure_table(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table=TableSpec(
                            name="vision_text_audit",
                            columns=with_concat(
                                [
                                    *BASE_COLUMNS,
                                    "run_id",
                                    "date",
                                    "gcs_path",
                                    "min_chars",
                                    "detected_text",
                                    "detected_text_chars",
                                    "detected_text_alpha_chars",
                                    "has_meaningful_text",
                                    "error",
                                ]
                            ),
                        ),
                    )

                    # Économise le quota : on journalise par défaut uniquement les exceptions (texte détecté ou erreur).
                    to_save = [r for r in rows if str(r.get("detected_text") or "").strip() or str(r.get("error") or "").strip()]
                    payload: list[dict] = []
                    for r in to_save:
                        dt = str(r.get("detected_text") or "")
                        dt_norm = " ".join(dt.split()).strip()
                        alpha_n = sum(1 for ch in dt_norm if ch.isalpha())
                        ent = sha256(
                            f"audit|{run_id}|{r.get('date')}|{r.get('gcs_path')}|{sha256(dt_norm.encode('utf-8')).hexdigest()}".encode(
                                "utf-8"
                            )
                        ).hexdigest()[:24]
                        payload.append(
                            {
                                "entity_id": ent,
                                "run_id": run_id,
                                "date": r.get("date"),
                                "gcs_path": r.get("gcs_path"),
                                "min_chars": int(ta_min),
                                "detected_text": dt_norm,
                                "detected_text_chars": len(dt_norm),
                                "detected_text_alpha_chars": int(alpha_n),
                                "has_meaningful_text": "true" if bool(r.get("has_text")) else "false",
                                "error": str(r.get("error") or ""),
                            }
                        )
                    saved = append_immutable_rows_bulk(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="vision_text_audit",
                        values_by_col_list=payload,
                        chunk_size=120,
                    )
                    st.success(f"Audit enregistré ({saved} ligne(s) — exceptions uniquement). run_id={run_id}")
                finally:
                    ov.empty()

            flagged_sorted = sorted(flagged, key=lambda r: str(r.get("date") or ""))
            options = [
                f"{r.get('date')} — {str(r.get('gcs_path') or '').split('/')[-1]}".strip()
                for r in flagged_sorted
            ]

            def _sync_vision_pick() -> None:
                sel = str(st.session_state.get("adm_vision_pick_flagged") or "")
                ii = options.index(sel) if sel in options else 0
                pp = flagged_sorted[ii] if flagged_sorted else {}
                txt = str(pp.get("detected_text") or "").strip()
                st.session_state["adm_vision_detected_preview"] = txt[:1200]
                st.session_state["adm_vision_replace_from"] = (txt[:120] if txt else "")
                st.session_state["adm_vision_replace_to"] = ""

            # Post-correction (st.rerun) : la sélection peut changer car la liste "flagged" change,
            # sans déclencher on_change. On force donc la resync si la sélection effective diffère.
            cur = str(st.session_state.get("adm_vision_pick_flagged") or "")
            if options and cur not in options:
                st.session_state["adm_vision_pick_flagged"] = options[0]
                cur = options[0]
            last = str(st.session_state.get("_adm_vision_pick_last") or "")
            if options and cur and cur != last:
                _sync_vision_pick()
                st.session_state["_adm_vision_pick_last"] = cur

            pick = st.selectbox(
                "Image à corriger",
                options=options,
                index=0,
                key="adm_vision_pick_flagged",
                on_change=_sync_vision_pick,
            )
            idx = options.index(pick) if pick in options else 0
            picked = flagged_sorted[idx] if flagged_sorted else {}
            picked_text = str(picked.get("detected_text") or "").strip()
            picked_date = str(picked.get("date") or "").strip()
            picked_path = str(picked.get("gcs_path") or "").strip()

            st.write(f"Chemin : `gs://{bucket_name}/{picked_path}`")
            # Aperçu image (utile pour confirmer qu'il n'y a pas de texte humain).
            try:
                if picked_path:
                    img_prev = download_bytes(gcs=gcs, bucket_name=bucket_name, path=picked_path)
                    if img_prev:
                        st.image(img_prev, caption="Aperçu de l’image (Cloud)", use_container_width=True)
            except Exception:
                pass

            # Bouton "confirmer OK" : ajoute à la whitelist (persistante) pour ne plus remonter.
            if can_sheets and picked_path:
                if st.button("Confirmer : image OK (whitelist)", key="adm_vision_whitelist_add"):
                    ovw = loading_overlay("Ajout à la whitelist (Sheets)…")
                    try:
                        from core.sheets_db import TableSpec, ensure_table

                        gs_w = build_gspread_client(cfg.gcp_service_account)
                        ensure_table(
                            gspread_client=gs_w,
                            spreadsheet_id=cfg.gsheet_id,
                            table=TableSpec(
                                name="vision_text_whitelist",
                                columns=with_concat([*BASE_COLUMNS, "date", "gcs_path", "reason"]),
                            ),
                        )
                        ent = sha256(f"wl|{picked_date}|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                        append_immutable_row(
                            gspread_client=gs_w,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_whitelist",
                            values_by_col={
                                "entity_id": ent,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "reason": "confirmé OK (pas de texte humain)",
                            },
                        )
                        # Met à jour cache whitelist et retire de la liste courante.
                        wl_key2 = f"_adm_vision_whitelist_{year}"
                        cur_wl = set(st.session_state.get(wl_key2) or set())
                        cur_wl.add(picked_path)
                        st.session_state[wl_key2] = cur_wl
                        try:
                            prev_rows = list(st.session_state.get(_audit_key) or [])
                            for rr in prev_rows:
                                if str(rr.get("gcs_path") or "").strip() == picked_path:
                                    rr["has_text"] = False
                                    rr["detected_text"] = ""
                            st.session_state[_audit_key] = prev_rows
                        except Exception:
                            pass
                        st.success("Ajouté à la whitelist : l’image ne remontera plus aux prochaines analyses.")
                        st.rerun()
                    finally:
                        ovw.empty()
            if "adm_vision_detected_preview" not in st.session_state:
                st.session_state["adm_vision_detected_preview"] = picked_text[:1200]
            if "adm_vision_replace_from" not in st.session_state:
                st.session_state["adm_vision_replace_from"] = (picked_text[:120] if picked_text else "")
            if "adm_vision_replace_to" not in st.session_state:
                st.session_state["adm_vision_replace_to"] = ""

            if st.session_state.get("adm_vision_detected_preview"):
                st.text_area(
                    "Texte détecté (extrait)",
                    value=str(st.session_state.get("adm_vision_detected_preview") or ""),
                    height=140,
                    key="adm_vision_detected_preview",
                )

            cfa, cfb = st.columns(2)
            with cfa:
                replace_from = st.text_input("Remplacer (from)", key="adm_vision_replace_from")
            with cfb:
                replace_to = st.text_input("Par (to) — vide = suppression", key="adm_vision_replace_to")

            if st.button(
                "Soumettre la correction + régénérer + écraser (illustration + vignette)",
                type="primary",
                disabled=not bool(picked_path),
                key="adm_vision_do_correction",
            ):
                overlay = loading_overlay("Correction en cours (Vertex → Cloud)…")
                try:
                    corr_entity = sha256(f"corr|{picked_date}|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24]
                    gs = build_gspread_client(cfg.gcp_service_account) if can_sheets else None
                    if gs and cfg.gsheet_id:
                        from core.sheets_db import TableSpec, ensure_table

                        ensure_table(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table=TableSpec(
                                name="vision_text_corrections",
                                columns=with_concat(
                                    [
                                        *BASE_COLUMNS,
                                        "audit_entity_id",
                                        "run_id",
                                        "date",
                                        "gcs_path",
                                        "replace_from",
                                        "replace_to",
                                        "status_detail",
                                        "vertex_model",
                                        "result_mime",
                                        "result_gcs_path",
                                        "thumb_gcs_path",
                                        "error",
                                    ]
                                ),
                            ),
                        )
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_corrections",
                            values_by_col={
                                "entity_id": corr_entity,
                                "audit_entity_id": "",
                                "run_id": run_id,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "replace_from": replace_from.strip(),
                                "replace_to": replace_to.strip(),
                                "status_detail": "requested",
                            },
                        )

                    src_bytes = download_bytes(gcs=gcs, bucket_name=bucket_name, path=picked_path)
                    vx = VertexGeminiClient(service_account_info=cfg.gcp_service_account)
                    rep_from = (replace_from or "").strip()
                    rep_to = (replace_to or "").strip()
                    rep_to_disp = rep_to if rep_to else "(remove)"
                    prompt_edit = (
                        "You are editing the provided reference image.\n"
                        "Task: replace the exact visible text substring delimited by:\n"
                        f"FROM: {rep_from!r}\n"
                        f"TO: {rep_to_disp!r}\n\n"
                        "Constraints:\n"
                        "- Keep the same illustration style, framing, composition, colors.\n"
                        "- Do NOT add any new text anywhere.\n"
                        "- If TO is (remove), remove the text completely.\n"
                        "- Do not introduce any other glyphs, letters, numbers, or watermarks.\n"
                        "- Return only the edited image.\n"
                    )
                    img_res = vx.generate_image_auto(
                        preferred_models=["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
                        prompt=prompt_edit,
                        aspect_ratio="4:3",
                        reference_image_bytes=src_bytes,
                        reference_image_mime_type="image/png",
                    )

                    ct = img_res.mime_type if (img_res.mime_type or "").startswith("image/") else "image/png"
                    upload_bytes(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        path=picked_path,
                        data=img_res.image_bytes,
                        content_type=ct,
                    )
                    thumb_path = generate_thumb_from_source_and_upload(
                        gcs=gcs,
                        bucket_name=bucket_name,
                        source_blob_path=picked_path,
                        download_bytes_fn=download_bytes,
                        upload_bytes_fn=upload_bytes,
                        max_side=420,
                    )

                    if gs and cfg.gsheet_id:
                        append_immutable_row(
                            gspread_client=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            table="vision_text_corrections",
                            values_by_col={
                                "entity_id": corr_entity,
                                "audit_entity_id": "",
                                "run_id": run_id,
                                "date": picked_date,
                                "gcs_path": picked_path,
                                "replace_from": rep_from,
                                "replace_to": rep_to,
                                "status_detail": "done",
                                "vertex_model": img_res.model,
                                "result_mime": ct,
                                "result_gcs_path": picked_path,
                                "thumb_gcs_path": thumb_path,
                                "error": "",
                            },
                        )
                    try:
                        prev_rows = list(st.session_state.get(_audit_key) or [])
                        for rr in prev_rows:
                            if str(rr.get("gcs_path") or "").strip() == picked_path:
                                rr["has_text"] = False
                                rr["detected_text"] = ""
                        st.session_state[_audit_key] = prev_rows
                    except Exception:
                        pass
                    st.success("Correction appliquée (illustration + vignette écrasées).")
                    # Force la resync au prochain rerun (la liste et la sélection vont changer).
                    for k in (
                        "_adm_vision_pick_last",
                        "adm_vision_detected_preview",
                        "adm_vision_replace_from",
                        "adm_vision_replace_to",
                    ):
                        if k in st.session_state:
                            del st.session_state[k]
                except Exception as ex:
                    try:
                        if can_sheets and cfg.gsheet_id and cfg.gcp_service_account:
                            gs2 = build_gspread_client(cfg.gcp_service_account)
                            append_immutable_row(
                                gspread_client=gs2,
                                spreadsheet_id=cfg.gsheet_id,
                                table="vision_text_corrections",
                                values_by_col={
                                    "entity_id": sha256(f"corr_err|{picked_path}|{utc_now_iso()}".encode("utf-8")).hexdigest()[:24],
                                    "run_id": run_id,
                                    "date": picked_date,
                                    "gcs_path": picked_path,
                                    "replace_from": (replace_from or "").strip(),
                                    "replace_to": (replace_to or "").strip(),
                                    "status_detail": "error",
                                    "error": str(ex),
                                },
                            )
                    except Exception:
                        pass
                    st.exception(ex)
                finally:
                    overlay.empty()
                st.rerun()
        else:
            if scanned_unique == 0:
                st.info("Aucun fichier sur Cloud dans la portée choisie.")
            elif errs and len(errs) >= scanned_unique and scanned_unique > 0:
                st.warning(
                    "Aucune analyse réussie : tous les appels Vision ont échoué. "
                    "Corrige la configuration (API activée, facturation, droits du compte de service) puis réessaie."
                )
            else:
                st.success("Aucune image avec texte détecté selon ces réglages.")

        if errs:
            show_raw = st.checkbox("Afficher les erreurs brutes (debug)", value=False, key="adm_text_audit_show_raw")
            with st.expander("Détail des erreurs", expanded=True):
                if show_raw:
                    err_tbl = [
                        {
                            "date": r.get("date"),
                            "chemin": r.get("gcs_path"),
                            "erreur": str(r.get("error") or ""),
                        }
                        for r in errs
                    ]
                else:
                    err_tbl = [
                        {
                            "date": r.get("date"),
                            "chemin": r.get("gcs_path"),
                            "erreur": shorten_audit_error_message(str(r.get("error") or "")),
                        }
                        for r in errs
                    ]
                st.write(f"{len(err_tbl)} erreur(s).")

