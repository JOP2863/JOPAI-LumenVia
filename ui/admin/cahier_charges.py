"""Admin — Cahier des charges (Markdown + journal Sheets)."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import streamlit as st

from core.config import load_config
from core.sheets_db import append_immutable_row, build_gspread_client, fetch_records
from ui.components import loading_overlay

_CDC_MARKDOWN_PATH = Path("data/cahier_des_charges.md")


def render_admin_cahier_charges() -> None:
    """Document Markdown versionné + journal Sheets."""
    st.title("Cahier des charges")
    st.markdown(
        """
**Document principal** : fichier Markdown dans le dépôt (`data/cahier_des_charges.md`), éditable ci-dessous puis sauvegardé sur le serveur qui exécute Streamlit.

**Journal des évolutions** : entrées dans la table Google Sheets `admin_changelog` (traçabilité des décisions sans effacer l’historique).
        """.strip()
    )

    _CDC_MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _CDC_MARKDOWN_PATH.is_file():
        _CDC_MARKDOWN_PATH.write_text(
            "# Cahier des charges — JOPAI LumenVia\n\n"
            "*Édite ce texte depuis l’administration, puis clique sur Enregistrer.*\n",
            encoding="utf-8",
        )
    cdc_body = _CDC_MARKDOWN_PATH.read_text(encoding="utf-8")
    edited = st.text_area(
        "Contenu (Markdown)",
        value=cdc_body,
        height=420,
        key="adm_cdc_editor",
    )
    if st.button("Enregistrer sur le disque", type="primary", key="adm_cdc_save"):
        _CDC_MARKDOWN_PATH.write_text(edited, encoding="utf-8")
        st.success(f"Sauvegardé : `{_CDC_MARKDOWN_PATH.as_posix()}` — pense à **commit** Git si tu veux versionner.")
        st.rerun()

    st.divider()
    st.subheader("Journal des évolutions (Sheets)")
    st.caption(
        "Ancien bloc « cahier des charges incrémental » déplacé ici : chaque ajout crée une nouvelle ligne dans `admin_changelog`."
    )

    cfg = load_config()
    title = st.text_input("Titre de l’entrée", key="adm_cdc_cl_title")
    detail = st.text_area("Détail", key="adm_cdc_cl_detail", height=160)
    if st.button("Ajouter une entrée au journal", type="primary", disabled=not (title and detail), key="adm_cdc_cl_add"):
        if not cfg.gcp_service_account or not cfg.gsheet_id:
            st.error("Configuration Google Sheets manquante (`gcp_service_account`, `gsheet_id`).")
        else:
            ov = loading_overlay("Enregistrement dans le journal (Google Sheets)…")
            gs = build_gspread_client(cfg.gcp_service_account)
            try:
                entry_id = sha256(f"adm|{title}|{detail}".encode("utf-8")).hexdigest()[:24]
                append_immutable_row(
                    gspread_client=gs,
                    spreadsheet_id=cfg.gsheet_id,
                    table="admin_changelog",
                    values_by_col={
                        "entity_id": entry_id,
                        "title": title.strip(),
                        "detail": detail.strip(),
                    },
                )
                st.success("Entrée ajoutée au journal.")
                st.rerun()
            finally:
                ov.empty()

    if cfg.gsheet_id and cfg.gcp_service_account:
        try:
            gs = build_gspread_client(cfg.gcp_service_account)
            cl = fetch_records(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="admin_changelog",
                limit=300,
            )
            cl_sorted = sorted(cl, key=lambda r: str(r.get("created_at", "")), reverse=True)
            st.markdown(f"**{len(cl_sorted)}** entrée(s) ; les 40 dernières :")
            for row in cl_sorted[:40]:
                t = str(row.get("title") or "—").strip()
                with st.expander(t[:100] + ("…" if len(t) > 100 else "")):
                    st.markdown(str(row.get("detail") or ""))
                    st.caption(f"`created_at` : {row.get('created_at', '—')}")
        except Exception as e:
            st.warning(f"Lecture du journal impossible : {e}")
    else:
        st.info("Configure `gsheet_id` pour afficher le journal Sheets ici.")

