"""Admin — Atelier audio (ambiance intro / outro / bed pour les TTS)."""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import streamlit as st

from core.audio_ambiance import (
    CIBLES,
    LICENCES,
    LICENCES_SAFE,
    LANGUES,
    ROLES,
    ambiance_gcs_path,
    list_active_clips,
    wav_duration_seconds,
)
from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.sheets_db import (
    SHEETS_ROW_STATUS_INACTIVE,
    append_immutable_row,
    build_gspread_client,
    invalidate_fetch_records_cache,
    open_spreadsheet,
    sheet_row_status_is_live,
    utc_now_iso,
)
from core.storage import download_bytes, upload_bytes
from ui.components import loading_overlay
from ui.streamlit_caches import (
    adm_sheets_fetch_cached,
    invalidate_adm_sheets_fetch_cache,
    service_account_json_fingerprint,
)


def render_admin_audio_atelier() -> None:
    st.title("Atelier audio")
    st.caption(
        "Bibliothèque d’ambiance (WAV) pour habiller les audios de **lectures** et de **synthèse** : "
        "intro → voix (± bed très bas) → outro. Table Sheets `audio_ambiance` (AAMB) · GCS `Audio/ambiance/`."
    )

    with st.expander("Comment ça marche — upload, choix, retrait", expanded=True):
        st.markdown(
            """
**1. Uploader un clip**  
Formulaire ci-dessous → fichier **WAV PCM 16-bit** + métadonnées (rôle, cible, langue, licence).  
Le fichier part dans **GCS** (`Audio/ambiance/{id}.wav`) et une ligne **AAMB** est ajoutée (append-only).

**2. Comment le mix choisit un clip**  
À chaque génération TTS (lectures ou synthèse) :
1. seuls les clips **Actifs** sont candidats ;
2. filtre **cible** (`lectures` / `synthese` / `both`) + **langue** (`ALL` ou langue du dimanche) ;
3. si un clip est marqué **Prioritaire** pour ce rôle, il est pris en premier ;
4. sinon choix **déterministe** parmi les candidats (même dimanche → même clip).

**3. Choisir ce qui plaît**  
- Gardez **actifs** uniquement les clips que vous aimez.  
- Bouton **Mettre en priorité** : force ce clip pour son rôle (et retire la priorité des autres du même rôle + même cible).  
- Plusieurs clips actifs sans priorité → rotation stable via empreinte date/langue.

**4. Retirer un clip qui ne plaît pas**  
Bouton **Retirer de la bibliothèque** → statut **Inactif** (plus jamais mixé).  
Le fichier GCS reste (traçabilité) ; on n’efface pas physiquement en V1.

**Licences**  
Utiliser **CC0**, **domaine public** ou **CC-BY** (attribution obligatoire dans le champ).  
Éviter « autre » sans preuve écrite. Banques typiques : Freesound (filtre CC0), Pixabay Music, Musopen, CPDL (partitions / enregistrements sous licence libre).
            """.strip()
        )

    st.info(
        "V1 : fichiers **WAV PCM 16-bit** uniquement (mono ou stéréo). "
        "Licences recommandées : **CC0** / **domaine public** / **CC-BY** (attribution obligatoire)."
    )

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
        st.error("Configure `gcp_service_account`, `gsheet_id` et `gcs_bucket_name` dans les secrets.")
        return

    sa_json = service_account_json_fingerprint(cfg.gcp_service_account)
    try:
        rows = adm_sheets_fetch_cached(cfg.gsheet_id, "audio_ambiance", 0, sa_json)
    except Exception as ex:
        st.error(
            f"Lecture AAMB impossible : {ex}. "
            "Crée la table via `python tools/init_sheets_db.py` si besoin "
            "(ajoute aussi la colonne `preferred` si l’onglet existait déjà)."
        )
        return
    active = list_active_clips(rows)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Clips actifs", len(active))
    with c2:
        st.metric(
            "Par rôle",
            " · ".join(f"{role}:{sum(1 for c in active if c.role == role)}" for role in ROLES),
        )
    with c3:
        st.metric("Prioritaires", sum(1 for c in active if c.preferred))

    st.subheader("Ajouter un clip")
    with st.form("aamb_upload_form", clear_on_submit=True):
        title = st.text_input("Titre", max_chars=120)
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            role = st.selectbox(
                "Rôle",
                options=list(ROLES),
                format_func=lambda r: {
                    "intro": "Intro (début)",
                    "outro": "Outro (fin)",
                    "bed": "Bed (fond bas)",
                }.get(r, r),
            )
        with col_b:
            cible = st.selectbox(
                "Cible",
                options=list(CIBLES),
                format_func=lambda c: {
                    "lectures": "Lectures",
                    "synthese": "Synthèse",
                    "both": "Les deux",
                }.get(c, c),
            )
        with col_c:
            langue = st.selectbox("Langue", options=list(LANGUES))
        col_d, col_e = st.columns(2)
        with col_d:
            licence = st.selectbox("Licence", options=list(LICENCES))
        with col_e:
            attribution = st.text_input("Attribution (obligatoire si CC-BY)", max_chars=200)
        notes = st.text_input("Notes (optionnel)", max_chars=240)
        as_preferred = st.checkbox(
            "Mettre en priorité dès l’upload (ce rôle + cette cible)",
            value=False,
        )
        up = st.file_uploader("Fichier WAV", type=["wav"], accept_multiple_files=False)
        submitted = st.form_submit_button("Enregistrer le clip", type="primary")

    if submitted:
        if not up or not title.strip():
            st.error("Titre et fichier WAV requis.")
        elif licence == "CC-BY" and not (attribution or "").strip():
            st.error("CC-BY : renseignez l’attribution (auteur / source).")
        elif licence == "autre":
            st.error(
                "Licence « autre » non acceptée en V1 sans preuve écrite. "
                "Choisissez CC0, domaine public ou CC-BY."
            )
        else:
            data = up.getvalue()
            if data[:4] != b"RIFF":
                st.error("Fichier non reconnu comme WAV (en-tête RIFF manquant).")
            else:
                ov = loading_overlay("Enregistrement du clip ambiance…")
                try:
                    try:
                        duration_s = round(wav_duration_seconds(data), 2)
                    except Exception as ex:
                        st.error(f"WAV illisible : {ex}")
                        raise
                    entity_id = sha256(
                        f"aamb|{title.strip()}|{role}|{utc_now_iso()}|{uuid4()}".encode("utf-8")
                    ).hexdigest()[:24]
                    path = ambiance_gcs_path(entity_id=entity_id, ext="wav")
                    gcs = build_gcs_client(cfg.gcp_service_account)
                    gs = build_gspread_client(cfg.gcp_service_account)
                    upload_bytes(
                        gcs=gcs,
                        bucket_name=cfg.gcs_bucket_name,
                        path=path,
                        data=data,
                        content_type="audio/wav",
                    )
                    append_immutable_row(
                        gspread_client=gs,
                        spreadsheet_id=cfg.gsheet_id,
                        table="audio_ambiance",
                        values_by_col={
                            "entity_id": entity_id,
                            "title": title.strip(),
                            "role": role,
                            "cible": cible,
                            "langue": langue,
                            "licence": licence,
                            "attribution": (attribution or "").strip(),
                            "gcs_path": path,
                            "duration_s": str(duration_s),
                            "preferred": "oui" if as_preferred else "",
                            "notes": (notes or "").strip(),
                        },
                    )
                    if as_preferred:
                        try:
                            _clear_preferred_siblings(
                                gs=gs,
                                spreadsheet_id=cfg.gsheet_id,
                                keep_entity_id=entity_id,
                                role=role,
                                cible=cible,
                                rows=rows,
                            )
                        except Exception:
                            pass
                    invalidate_fetch_records_cache(
                        spreadsheet_id=cfg.gsheet_id, table="audio_ambiance"
                    )
                    invalidate_adm_sheets_fetch_cache()
                    st.success(f"Clip enregistré · `{path}` ({duration_s}s).")
                    st.rerun()
                except Exception as ex:
                    if "WAV illisible" not in str(ex):
                        st.error(f"Échec enregistrement : {ex}")
                finally:
                    ov.empty()

    st.subheader("Bibliothèque active")
    if not active:
        st.caption("Aucun clip actif — uploadez intro / outro / bed pour activer le mix TTS.")
        return

    unsafe = [c for c in active if c.licence and c.licence not in LICENCES_SAFE]
    if unsafe:
        st.warning(
            "Clips actifs hors licences sûres (CC0 / domaine public / CC-BY) : "
            + ", ".join(c.title for c in unsafe)
            + ". Retirez-les ou remplacez-les."
        )

    gcs = build_gcs_client(cfg.gcp_service_account)
    for clip in sorted(
        active, key=lambda c: (0 if c.preferred else 1, c.role, c.langue, c.title.lower())
    ):
        star = "⭐ " if clip.preferred else ""
        with st.expander(
            f"{star}{clip.role.upper()} · {clip.title} · {clip.langue} · {clip.cible}",
            expanded=False,
        ):
            st.markdown(
                f"- **Licence** : {clip.licence or '—'}  \n"
                f"- **Attribution** : {clip.attribution or '—'}  \n"
                f"- **Prioritaire** : {'oui' if clip.preferred else 'non'}  \n"
                f"- **GCS** : `{clip.gcs_path}`  \n"
                f"- **Durée** : {clip.duration_s or '—'} s  \n"
                f"- **entity_id** : `{clip.entity_id}`"
            )
            try:
                raw = download_bytes(
                    gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=clip.gcs_path
                )
                st.audio(raw, format="audio/wav")
            except Exception as ex:
                st.warning(f"Préécoute impossible : {ex}")
            b1, b2 = st.columns(2)
            with b1:
                if st.button(
                    "Mettre en priorité",
                    key=f"aamb_pref_{clip.entity_id}",
                    help="Ce clip sera choisi en premier pour ce rôle + cette cible.",
                ):
                    try:
                        gs = build_gspread_client(cfg.gcp_service_account)
                        _set_preferred_clip(
                            gs=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            entity_id=clip.entity_id,
                            role=clip.role,
                            cible=clip.cible,
                            rows=rows,
                        )
                        invalidate_fetch_records_cache(
                            spreadsheet_id=cfg.gsheet_id, table="audio_ambiance"
                        )
                        invalidate_adm_sheets_fetch_cache()
                        st.success("Clip prioritaire.")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"Priorité impossible : {ex}")
            with b2:
                if st.button(
                    "Retirer de la bibliothèque",
                    key=f"aamb_off_{clip.entity_id}",
                    help="Passe le clip en Inactif — il ne sera plus mixé (fichier GCS conservé).",
                ):
                    try:
                        gs = build_gspread_client(cfg.gcp_service_account)
                        _deactivate_clip_row(
                            gs=gs,
                            spreadsheet_id=cfg.gsheet_id,
                            entity_id=clip.entity_id,
                            rows=rows,
                        )
                        invalidate_fetch_records_cache(
                            spreadsheet_id=cfg.gsheet_id, table="audio_ambiance"
                        )
                        invalidate_adm_sheets_fetch_cache()
                        st.success("Clip retiré (Inactif).")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"Retrait impossible : {ex}")


def _aamb_worksheet(gs: object, spreadsheet_id: str):
    from core.sheets_db import _resolve_table_name  # noqa: PLC2701

    sh = open_spreadsheet(gs, spreadsheet_id, use_cache=True)
    tab = _resolve_table_name(sh=sh, table="audio_ambiance")
    ws = sh.worksheet(tab)
    header = [str(h or "").strip() for h in ws.row_values(1)]
    return ws, header


def _sheet_row_for_entity(*, ws, header: list[str], entity_id: str, rows: list[dict]) -> int:
    want = str(entity_id or "").strip()
    candidates = [
        r
        for r in rows
        if str(r.get("entity_id") or "").strip() == want and sheet_row_status_is_live(r.get("status"))
    ]
    if not candidates:
        raise RuntimeError("Ligne active introuvable.")
    best = sorted(candidates, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
    row_id = str(best.get("row_id") or "").strip()
    if not row_id:
        raise RuntimeError("row_id manquant.")
    if "row_id" not in header:
        raise RuntimeError("Colonne row_id absente.")
    id_col = header.index("row_id") + 1
    col_vals = ws.col_values(id_col)
    for i, v in enumerate(col_vals):
        if str(v or "").strip() == row_id:
            return i + 1
    raise RuntimeError("Ligne introuvable dans l’onglet.")


def _deactivate_clip_row(
    *,
    gs: object,
    spreadsheet_id: str,
    entity_id: str,
    rows: list[dict],
) -> None:
    """Passe le statut de la dernière ligne Actif de l’entité à Inactif (1 écriture)."""
    ws, header = _aamb_worksheet(gs, spreadsheet_id)
    if "status" not in header:
        raise RuntimeError("Colonne status absente.")
    sheet_row = _sheet_row_for_entity(ws=ws, header=header, entity_id=entity_id, rows=rows)
    ws.update_cell(sheet_row, header.index("status") + 1, SHEETS_ROW_STATUS_INACTIVE)


def _set_preferred_clip(
    *,
    gs: object,
    spreadsheet_id: str,
    entity_id: str,
    role: str,
    cible: str,
    rows: list[dict],
) -> None:
    """Marque un clip prioritaire ; retire la priorité des autres même rôle+cible."""
    ws, header = _aamb_worksheet(gs, spreadsheet_id)
    if "preferred" not in header:
        raise RuntimeError(
            "Colonne `preferred` absente — ajoutez-la à l’onglet AAMB "
            "(ou relancez `python tools/init_sheets_db.py` / migration d’en-tête)."
        )
    pref_col = header.index("preferred") + 1
    want_role = str(role or "").strip().lower()
    want_cible = str(cible or "").strip().lower()
    want_eid = str(entity_id or "").strip()

    siblings = [
        r
        for r in rows
        if sheet_row_status_is_live(r.get("status"))
        and str(r.get("role") or "").strip().lower() == want_role
        and str(r.get("cible") or "").strip().lower() == want_cible
    ]
    for r in siblings:
        eid = str(r.get("entity_id") or "").strip()
        try:
            sheet_row = _sheet_row_for_entity(ws=ws, header=header, entity_id=eid, rows=rows)
        except Exception:
            continue
        ws.update_cell(sheet_row, pref_col, "oui" if eid == want_eid else "")


def _clear_preferred_siblings(
    *,
    gs: object,
    spreadsheet_id: str,
    keep_entity_id: str,
    role: str,
    cible: str,
    rows: list[dict],
) -> None:
    """Après upload déjà marqué preferred=oui : retire la priorité des frères."""
    ws, header = _aamb_worksheet(gs, spreadsheet_id)
    if "preferred" not in header:
        return
    pref_col = header.index("preferred") + 1
    want_role = str(role or "").strip().lower()
    want_cible = str(cible or "").strip().lower()
    keep = str(keep_entity_id or "").strip()
    for r in rows:
        if not sheet_row_status_is_live(r.get("status")):
            continue
        if str(r.get("role") or "").strip().lower() != want_role:
            continue
        if str(r.get("cible") or "").strip().lower() != want_cible:
            continue
        eid = str(r.get("entity_id") or "").strip()
        if eid == keep:
            continue
        try:
            sheet_row = _sheet_row_for_entity(ws=ws, header=header, entity_id=eid, rows=rows)
        except Exception:
            continue
        ws.update_cell(sheet_row, pref_col, "")
