"""Admin — Visuels liturgiques (étape 3) : grille Vertex + manifeste."""

from __future__ import annotations

import io
from datetime import date
from hashlib import sha256
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
from core.sheets_db import append_immutable_row, build_gspread_client, fetch_records, sheet_row_status_is_live
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
    caption_after_upload: bool = False,
    caption_models: list[str] | None = None,
    zone_liturgy: str = "france",
) -> list[str]:
    lines: list[str] = []
    n = len(to_run)
    prog = st.progress(0.0)
    bucket = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
    cap_models = [x for x in (caption_models or []) if str(x).strip()]
    want_cap = bool(caption_after_upload and (not dry_run) and gsheet_id and cap_models)
    caption_gs = None
    ilus_rows: list[dict] = []
    if want_cap:
        try:
            caption_gs = build_gspread_client(cfg.gcp_service_account)
            ilus_rows = fetch_records(
                gspread_client=caption_gs,
                spreadsheet_id=gsheet_id,
                table="liturgy_illustrations",
                limit=0,
            )
        except Exception as ex:
            lines.append(
                f"KO — client Sheets / lecture ILUS : {ex} "
                "(légendes après upload ignorées pour ce lot)."
            )
            want_cap = False

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
                if want_cap and caption_gs is not None:
                    z = str(zone_liturgy or "").strip() or "france"
                    cap_ln = _admin_try_append_ilus_caption_single(
                        cfg=cfg,
                        gcs=gcs,
                        vx=vx,
                        gs=caption_gs,
                        ilus_rows=ilus_rows,
                        t=t,
                        path=dest,
                        zone_liturgy=z,
                        skip_existing=False,
                        caption_models=cap_models,
                    )
                    lines.append(cap_ln)
            except Exception as ex:
                lines.append(f"Upload KO {ds} — {ex}")

        prog.progress(min(1.0, (i + 1) / max(n, 1)))
        if pause_s > 0 and i < n - 1:
            time.sleep(float(pause_s))

    prog.progress(1.0)
    return lines


def _admin_finish_generation_log(lines: list[str], *, dry_run: bool, caption_ilus: bool = False) -> None:
    if not lines:
        return
    log_txt = "\n".join(lines)
    st.text_area("Journal du lot", value=log_txt, height=min(260, 80 + 18 * max(len(lines), 1)))
    if any(ln.startswith("OK ") for ln in lines):
        if caption_ilus:
            st.success(
                "Au moins une **description** a été enregistrée dans **ILUS**. "
                "Cherche les lignes **OK … ILUS** ci-dessus."
            )
        else:
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


def _guess_image_mime_from_gcs_path(path: str) -> str:
    p = (path or "").lower()
    if p.endswith(".webp"):
        return "image/webp"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    return "image/png"


def _ilus_stable_entity_id(date_str: str, zone: str) -> str:
    return sha256(f"lumen_via|ilus|{date_str}|{zone}".encode("utf-8")).hexdigest()[:24]


def _ilus_latest_live_has_description(rows: list[dict], *, date_str: str, zone: str) -> bool:
    d = str(date_str).strip()[:10]
    z = str(zone or "").strip()
    cand = [
        r
        for r in rows
        if str(r.get("date") or "").strip()[:10] == d
        and str(r.get("zone") or "").strip() == z
        and sheet_row_status_is_live(r.get("status"))
    ]
    if not cand:
        return False
    cand.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return bool(str((cand[0] or {}).get("description_illustration") or "").strip())


def _ilus_next_version(rows: list[dict], entity_id: str) -> int:
    ge = str(entity_id or "").strip()
    mx = 0
    for r in rows:
        if str(r.get("entity_id") or "").strip() != ge:
            continue
        try:
            mx = max(mx, int(str(r.get("version") or "0").strip()))
        except Exception:
            pass
    return mx + 1


def _admin_build_illus_caption_prompt_fr(
    target: dict,
    date_str: str,
    *,
    identity: object | None = None,
) -> str:
    tempo = str(target.get("temps_liturgique") or "").strip()
    col = str(target.get("couleur") or "").strip()
    kw = target.get("keywords") or []
    kw_txt = ", ".join(str(x) for x in kw[:14] if str(x).strip())
    lit_lines: list[str] = []
    if identity is not None:
        try:
            import app as ap

            jn = ap._jour_liturgique(identity)  # type: ignore[arg-type]
            ft = str(getattr(identity, "fete", None) or "").strip()
            per = str(getattr(identity, "periode", None) or "").strip()
            sem = str(getattr(identity, "semaine", None) or "").strip()
            an = str(getattr(identity, "annee", None) or "").strip()
            if jn:
                lit_lines.append(f"- Nom du jour (AELF) : {jn}")
            if ft:
                lit_lines.append(f"- Fête / solennité (AELF) : {ft}")
            if per:
                lit_lines.append(f"- Temps liturgique (AELF) : {ap._liturgy_display_label(per) or per}")
            if sem:
                lit_lines.append(f"- Semaine (AELF) : {sem}")
            if an:
                lit_lines.append(f"- Année liturgique (AELF) : {ap._cycle_year_display(an) or an}")
        except Exception:
            pass
    lit_block = ("\n" + "\n".join(lit_lines)) if lit_lines else ""
    return (
        "Tu es un assistant pour LumenVia (application catholique francophone).\n\n"
        f"On te montre l’illustration dominicale déjà produite pour le dimanche **{date_str}**.\n"
        "Contexte du manifeste (préparation dominicale, ambiance) :\n"
        f"- Temps liturgique (manifeste) : {tempo or '—'}\n"
        f"- Couleur liturgique (manifeste) : {col or '—'}\n"
        f"- Mots-clés (manifeste) : {kw_txt or '—'}"
        f"{lit_block}\n\n"
        "Ces indications servent à **situer** l’image dans la semaine du dimanche ; la description doit rester "
        "**fidèle au visible** dans l’image, sans inventer de détails non visibles.\n\n"
        "Tâche : rédige **uniquement en français**, **2 à 4 phrases courtes**, une description accessible pour un lecteur.\n\n"
        "Règles strictes :\n"
        "- Décris composition, lumière, couleurs dominantes, symboles visibles sans interpréter au-delà de ce que l’image montre.\n"
        "- **N’invente pas** de versets, titres de messes ou texte affiché (normalement aucune inscription lisible).\n"
        "- Ton sobre et contemplatif ; pas de jargon technique (« IA », « prompt », etc.).\n\n"
        "Réponds par la description seule, sans titre ni liste à puces."
    )


def _admin_try_append_ilus_caption_single(
    *,
    cfg: object,
    gcs: object,
    vx: VertexGeminiClient,
    gs: object,
    ilus_rows: list[dict],
    t: dict,
    path: str,
    zone_liturgy: str,
    skip_existing: bool,
    caption_models: list[str],
) -> str:
    """Une cible : télécharge l’image sur GCS, légende Vertex multimodal, append ILUS. Met à jour ``ilus_rows``."""
    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
    bucket_name = str(getattr(cfg, "gcs_bucket_name", "") or "").strip()
    if not gsheet_id:
        return "KO — gsheet_id manquant."
    ds = str(t.get("date") or "").strip()[:10]
    path = str(path or "").strip()
    if len(ds) < 10:
        return f"KO {ds or '?'} — date invalide."
    if not path:
        return f"KO {ds} — chemin GCS vide."
    if skip_existing and _ilus_latest_live_has_description(ilus_rows, date_str=ds, zone=zone_liturgy):
        return f"Skip {ds} — description ILUS déjà présente (Actif)."

    overlay = loading_overlay(f"Légende ILUS — {ds}…")
    try:
        try:
            img_bytes = download_bytes(gcs=gcs, bucket_name=bucket_name, path=path)
        except Exception as ex:
            return f"KO {ds} — téléchargement GCS : {ex}"
        if not img_bytes:
            return f"KO {ds} — fichier vide sur Cloud."
        mime = _guess_image_mime_from_gcs_path(path)
        ident_cap: object | None = None
        try:
            import app as ap

            ident_cap, _tx = ap.cached_aelf(ds, zone=zone_liturgy, _identity_schema=4)
        except Exception:
            ident_cap = None
        prompt = _admin_build_illus_caption_prompt_fr(t, ds, identity=ident_cap)
        models = caption_models or ["gemini-2.0-flash"]
        try:
            res = vx.generate_text_multimodal_auto(
                preferred_models=models,
                image_bytes=img_bytes,
                image_mime_type=mime,
                prompt=prompt,
                max_output_tokens=768,
                temperature=0.2,
            )
        except Exception as ex:
            return f"KO {ds} — Vertex : {ex}"
        desc = (res.text or "").strip()
        if not desc:
            return f"KO {ds} — réponse Vertex vide."

        eid = _ilus_stable_entity_id(ds, zone_liturgy)
        ver = _ilus_next_version(ilus_rows, eid)
        try:
            new_row = append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=gsheet_id,
                table="liturgy_illustrations",
                values_by_col={
                    "entity_id": eid,
                    "version": ver,
                    "date": ds,
                    "zone": zone_liturgy,
                    "gcs_path": path,
                    "description_illustration": desc,
                    "gen_entity_id": "",
                    "caption_source": "vertex",
                    "caption_model": str(res.model or ""),
                },
            )
            ilus_rows.append(dict(new_row))
            return f"OK {ds} — ILUS v{ver} ({res.model})"
        except Exception as ex:
            return f"KO {ds} — Sheets ILUS : {ex}"
    finally:
        overlay.empty()


def _admin_execute_illus_caption_writes(
    *,
    cfg: object,
    gcs: object,
    vx: VertexGeminiClient,
    gs: object,
    targets_with_paths: list[tuple[dict, str]],
    zone_liturgy: str,
    skip_existing: bool,
    pause_s: float,
    caption_models: list[str],
) -> tuple[list[str], list[dict]]:
    """Écrit dans ILUS ; enrichit la liste locale pour les versions suivantes."""
    lines: list[str] = []
    gsheet_id = str(getattr(cfg, "gsheet_id", "") or "").strip()
    if not gsheet_id:
        return (["KO — gsheet_id manquant dans les secrets."], [])
    ilus_rows: list[dict] = fetch_records(
        gspread_client=gs,
        spreadsheet_id=gsheet_id,
        table="liturgy_illustrations",
        limit=0,
    )
    n = len(targets_with_paths)
    prog = st.progress(0.0)
    for i, (t, path) in enumerate(targets_with_paths):
        line = _admin_try_append_ilus_caption_single(
            cfg=cfg,
            gcs=gcs,
            vx=vx,
            gs=gs,
            ilus_rows=ilus_rows,
            t=t,
            path=path,
            zone_liturgy=zone_liturgy,
            skip_existing=skip_existing,
            caption_models=caption_models,
        )
        lines.append(line)

        prog.progress(min(1.0, (i + 1) / max(n, 1)))
        if pause_s > 0 and i < n - 1:
            time.sleep(float(pause_s))

    prog.progress(1.0)
    return lines, ilus_rows


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

    gsheet_ok = bool(str(cfg.gsheet_id or "").strip())
    if gsheet_ok:
        cz, cm = st.columns(2)
        with cz:
            zone_ilus = st.selectbox(
                "Zone liturgique (ILUS)",
                options=["france"],
                index=0,
                key="adm_ilus_zone",
            )
        with cm:
            cap_models_line = st.text_input(
                "Modèles Vertex **texte + vision** (légendes ILUS, ordre, virgules)",
                value="gemini-2.0-flash,gemini-2.5-flash,gemini-2.5-pro",
                key="adm_ilus_models",
                help="Utilisés après chaque upload (si coché) et dans la section « Descriptions ILUS ».",
            )
        caption_models = [x.strip() for x in cap_models_line.split(",") if x.strip()] or ["gemini-2.0-flash"]
        caption_after_upload = st.checkbox(
            "Après chaque **upload réussi** : générer la **légende ILUS** (Vertex) et l’écrire dans Sheets",
            value=True,
            key="adm_img_caption_after",
        )
    else:
        zone_ilus = "france"
        caption_models = ["gemini-2.0-flash"]
        caption_after_upload = False
        st.caption("Sans `gsheet_id` dans les secrets, les légendes ILUS (y compris après upload) ne sont pas disponibles.")

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
                caption_after_upload=bool(caption_after_upload),
                caption_models=caption_models,
                zone_liturgy=str(zone_ilus).strip() or "france",
            )
            _admin_finish_generation_log(
                lines,
                dry_run=dry_run,
                caption_ilus=any(" — ILUS v" in ln for ln in lines),
            )
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
                caption_after_upload=bool(caption_after_upload),
                caption_models=caption_models,
                zone_liturgy=str(zone_ilus).strip() or "france",
            )
            _admin_finish_generation_log(
                lines,
                dry_run=dry_run,
                caption_ilus=any(" — ILUS v" in ln for ln in lines),
            )
            if not dry_run and any(ln.startswith("OK ") for ln in lines):
                _admin_cached_manifest_cloud_presence.clear()

    st.divider()
    st.subheader("Descriptions ILUS (Vertex)")
    st.caption(
        "Pour les **fichiers déjà sur Cloud** (sans régénérer les pixels) : envoi de l’image à Gemini (multimodal), "
        "puis **append** dans la table **`liturgy_illustrations` / ILUS** (MARPA). "
        "Même **zone** et **modèles texte + vision** que ceux au-dessus de la grille (y compris pour la légende juste après upload)."
    )
    if not str(cfg.gsheet_id or "").strip():
        st.warning("Configure `gsheet_id` dans les secrets pour écrire dans ILUS.")
    else:
        pause_cap = st.number_input(
            "Pause entre deux légendes (secondes)",
            min_value=0,
            max_value=120,
            value=1,
            step=1,
            key="adm_ilus_pause",
        )
        skip_ilus_existing = st.checkbox(
            "Ignorer les dimanches qui ont déjà une description ILUS **Actif**",
            value=True,
            key="adm_ilus_skip",
        )
        cap_run_page = st.button(
            "Générer les descriptions — **page grille courante** (avec fichier Cloud)",
            key="adm_ilus_run_page",
        )
        with st.expander("Lot complet du manifeste", expanded=False):
            st.caption(
                "Traite **tous** les dimanches du manifeste pour lesquels un fichier existe sur le bucket — "
                "plusieurs dizaines d’appels Vertex possibles."
            )
            cap_confirm_all = st.checkbox(
                "Je confirme le lot complet sur tout le manifeste",
                value=False,
                key="adm_ilus_confirm_all",
            )
            cap_run_all = st.button(
                "Générer les descriptions — **tout le manifeste**",
                key="adm_ilus_run_all",
                type="primary",
            )

        targets_page_caps: list[tuple[dict, str]] = []
        for gi in range(slice_start, min(slice_start + per_page, n_targets)):
            if not has_map[gi]:
                continue
            fp = first_paths[gi]
            if fp:
                targets_page_caps.append((sorted_targets[gi], fp))

        targets_all_caps: list[tuple[dict, str]] = [
            (sorted_targets[i], first_paths[i])
            for i in range(n_targets)
            if has_map[i] and first_paths[i]
        ]

        if cap_run_page:
            if not targets_page_caps:
                st.info("Aucune illustration Cloud sur cette page de grille.")
            else:
                gs_ilus = build_gspread_client(cfg.gcp_service_account)
                lines_ilus, _ = _admin_execute_illus_caption_writes(
                    cfg=cfg,
                    gcs=gcs,
                    vx=vx,
                    gs=gs_ilus,
                    targets_with_paths=targets_page_caps,
                    zone_liturgy=str(zone_ilus).strip() or "france",
                    skip_existing=bool(skip_ilus_existing),
                    pause_s=float(pause_cap),
                    caption_models=caption_models or ["gemini-2.0-flash"],
                )
                _admin_finish_generation_log(lines_ilus, dry_run=False, caption_ilus=True)

        if cap_run_all:
            if not cap_confirm_all:
                st.error("Coche la confirmation pour lancer le lot complet.")
            elif not targets_all_caps:
                st.info("Aucune illustration présente sur Cloud pour ce manifeste.")
            else:
                gs_ilus = build_gspread_client(cfg.gcp_service_account)
                lines_ilus, _ = _admin_execute_illus_caption_writes(
                    cfg=cfg,
                    gcs=gcs,
                    vx=vx,
                    gs=gs_ilus,
                    targets_with_paths=targets_all_caps,
                    zone_liturgy=str(zone_ilus).strip() or "france",
                    skip_existing=bool(skip_ilus_existing),
                    pause_s=float(pause_cap),
                    caption_models=caption_models or ["gemini-2.0-flash"],
                )
                _admin_finish_generation_log(lines_ilus, dry_run=False, caption_ilus=True)



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

