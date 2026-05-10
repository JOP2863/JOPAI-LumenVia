"""Page publique « Mon Aide-Mémoire » (mémos utilisateur, Sheets + GCS, export PDF)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from hashlib import sha256

import streamlit as st

from core.config import load_config
from core.gcp_clients import build_gcs_client
from core.pdf_graine_parole_mensuel import build_graine_parole_monthly_pdf_bytes, strip_light_markdown_to_plain
from core.sheets_db import append_immutable_row, build_gspread_client, fetch_records
from core.storage import download_bytes, upload_text
from ui.components import loading_overlay


def next_sunday(d: date) -> date:
    # Sunday = 6 (Mon=0)
    days_ahead = (6 - d.weekday()) % 7
    return d + timedelta(days=days_ahead or 7)


def _random_takeaway_line(synthesis_text: str) -> str | None:
    t = synthesis_text or ""
    low = t.lower()
    idx = low.find("à retenir")
    if idx == -1:
        idx = low.find("a retenir")
    chunk = t[idx:] if idx != -1 else t
    bullets: list[str] = []
    for line in chunk.splitlines():
        s = line.strip()
        if len(s) < 4:
            continue
        if s.startswith(("- ", "• ", "* ", "– ")):
            bullets.append(s[2:].strip())
        else:
            for prefix in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."):
                if s.startswith(prefix):
                    bullets.append(s[len(prefix) :].strip())
                    break
    bullets = [b for b in bullets if len(b) > 8]
    if not bullets:
        return None
    return random.choice(bullets)


def _french_month_year(d: date) -> str:
    mois = (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    )
    return f"{mois[d.month - 1].capitalize()} {d.year}"


def _fmt_created_fr(created_at: str) -> str:
    from datetime import datetime

    s = (created_at or "").strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        mois = (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        )
        return f"{dt.day} {mois[dt.month - 1]} {dt.year}"
    except Exception:
        return s[:10] if len(s) >= 10 else s


def _extract_liturgical_week_num(semaine: str | None) -> str | None:
    import re

    if not semaine:
        return None
    m = re.match(r"\s*(\d+)", semaine.strip())
    return m.group(1) if m else None


def _memo_option_label(m: dict, ident: object | None) -> str:
    import app as _app

    title = str(m.get("title") or "(sans titre)")
    if len(title) > 50:
        title = title[:47] + "…"
    created = _fmt_created_fr(str(m.get("created_at") or ""))
    if ident is not None:
        wn = _extract_liturgical_week_num(getattr(ident, "semaine", None))
        temps = (getattr(ident, "periode", None) or "").strip() or "—"
        semaine_txt = (getattr(ident, "semaine", None) or "").strip()
        if wn:
            head = f"Semaine {wn} · {temps}"
        elif semaine_txt:
            head = _app._liturgy_display_label(semaine_txt)
        else:
            head = temps
        return f"{head} · {title} · noté le {created}"
    ds = str(m.get("date") or "?")
    return f"{ds} · {title} · noté le {created}"


def render_memo() -> None:
    st.markdown(
        """
<style>
/*
  Mémo : marge basse par défaut (bouton « Enregistrer le mémo » / expander).
  Quand le textarea « Ton mémo » est actif, le padding renforcé est dans set_page_style (:has(textarea:focus), 20vh).
*/
@media (max-width: 1024px) {
  section[data-testid="stMain"] .block-container {
    padding-bottom: max(20rem, calc(env(safe-area-inset-bottom, 0px) + 14rem)) !important;
  }
}
</style>
        """,
        unsafe_allow_html=True,
    )
    st.title("Mon Aide-Mémoire")
    st.write("Espace réservé aux utilisateurs connectés.")

    cfg = load_config()
    if not cfg.gcp_service_account or not cfg.gsheet_id or not cfg.gcs_bucket_name:
        st.warning("Configuration incomplète (service account / gsheet_id / bucket).")
        return

    gs = build_gspread_client(cfg.gcp_service_account)
    gcs = build_gcs_client(cfg.gcp_service_account)

    if "auth_user_entity_id" not in st.session_state:
        st.session_state.auth_user_entity_id = ""
    if "auth_email_lc" not in st.session_state:
        st.session_state.auth_email_lc = ""

    user_entity_id = str(st.session_state.auth_user_entity_id or "").strip()
    if not user_entity_id:
        st.warning("Pour accéder à **Mon Aide‑Mémoire**, il faut être connecté.", icon="🔒")
        if st.button("Aller à Mon compte", type="primary", key="memo_go_account"):
            st.session_state.route = "account"
            st.rerun()
        return

    import app as _app

    zone = "france"

    try:
        memos = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="memos", limit=500)
    except Exception:
        memos = []
    my_memos = [m for m in memos if str(m.get("user_entity_id", "")).strip() == user_entity_id]
    my_memos_sorted = sorted(my_memos, key=lambda r: str(r.get("created_at", "")), reverse=True)

    with st.expander("Mes mémos existants", expanded=bool(my_memos_sorted)):
        if not my_memos_sorted:
            st.write("Aucun mémo pour le moment.")
        else:
            slice_memos = my_memos_sorted[:30]
            dates_u = sorted({str(m.get("date") or "").strip() for m in slice_memos if str(m.get("date") or "").strip()})
            id_by_date: dict[str, object | None] = {}
            for ds in dates_u:
                try:
                    ident_i, _ = _app.cached_aelf(ds, zone, _identity_schema=4)
                    id_by_date[ds] = ident_i
                except Exception:
                    id_by_date[ds] = None
            options = [_memo_option_label(m, id_by_date.get(str(m.get("date") or "").strip())) for m in slice_memos]
            idx = st.selectbox("Ouvrir un mémo", options=list(range(len(options))), format_func=lambda i: options[i])
            chosen = my_memos_sorted[idx]
            path = str(chosen.get("memo_gcs_path") or "").strip()
            if path:
                try:
                    content = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=path).decode(
                        "utf-8", errors="replace"
                    )
                except Exception as e:
                    content = f"[Erreur lecture Cloud] {e}"
                st.text_area("Contenu", value=content, height=260)
                st.caption(f"Cloud: `{path}`")

    st.divider()
    st.subheader("Créer un nouveau mémo")

    chosen_date = st.date_input("Date (dimanche)", value=next_sunday(date.today()), key="memo_date")
    date_str = chosen_date.isoformat()

    default_title = f"Mémo — {date_str}"
    title = st.text_input("Titre", value=default_title, key="memo_title").strip()

    if "memo_prefill_requested" not in st.session_state:
        st.session_state.memo_prefill_requested = False
    if "memo_inspire_requested" not in st.session_state:
        st.session_state.memo_inspire_requested = False

    b_prefill, b_inspire = st.columns(2, gap="small")
    with b_prefill:
        if st.button("Pré-remplir avec la dernière synthèse du jour", type="secondary"):
            st.session_state.memo_prefill_requested = True
            st.session_state.memo_prefill_date = date_str
            st.rerun()
    with b_inspire:
        if st.button("S'inspirer de la synthèse (un point « À retenir »)", type="secondary"):
            st.session_state.memo_inspire_requested = True
            st.session_state.memo_inspire_date = date_str
            st.rerun()

    if st.session_state.get("memo_prefill_requested") and st.session_state.get("memo_prefill_date") == date_str:
        ov = loading_overlay("LumenVia charge la dernière synthèse…")
        try:
            gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=500)
            gens_day = [
                g
                for g in gens
                if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
            ]
            gens_day_sorted = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)
            if gens_day_sorted:
                p = str(gens_day_sorted[0].get("text_gcs_path") or "").strip()
                if p:
                    body_txt = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=p).decode(
                        "utf-8", errors="replace"
                    )
                    st.session_state["memo_body"] = body_txt
                    st.success("OK — synthèse chargée dans le mémo.")
            else:
                st.info("Aucune synthèse trouvée pour cette date.")
        except Exception as e:
            st.error(f"Impossible de pré-remplir: {e}")
        finally:
            ov.empty()
            st.session_state.memo_prefill_requested = False

    if st.session_state.get("memo_inspire_requested") and st.session_state.get("memo_inspire_date") == date_str:
        ov = loading_overlay("LumenVia extrait un point à retenir…")
        try:
            gens = fetch_records(gspread_client=gs, spreadsheet_id=cfg.gsheet_id, table="generations", limit=500)
            gens_day = [
                g
                for g in gens
                if str(g.get("date", "")).strip() == date_str and str(g.get("zone", "")).strip() == zone
            ]
            gens_day_sorted = sorted(gens_day, key=lambda r: str(r.get("created_at", "")), reverse=True)
            if gens_day_sorted:
                p = str(gens_day_sorted[0].get("text_gcs_path") or "").strip()
                if p:
                    syn = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=p).decode(
                        "utf-8", errors="replace"
                    )
                    pick = _random_takeaway_line(syn)
                    if pick:
                        st.session_state["memo_body"] = pick
                        st.success("Un point « À retenir » a été inséré dans ton mémo.")
                    else:
                        st.info(
                            "Aucune liste « À retenir » détectée dans cette synthèse. "
                            "Génère une synthèse avec l’option « À retenir », ou utilise le pré-remplissage complet."
                        )
            else:
                st.info("Aucune synthèse trouvée pour cette date.")
        except Exception as e:
            st.error(f"Impossible de charger la synthèse : {e}")
        finally:
            ov.empty()
            st.session_state.memo_inspire_requested = False

    body = st.text_area("Ton mémo", height=220, key="memo_body").strip()
    resolution = st.text_input(
        "Ma résolution (cette semaine)",
        max_chars=140,
        key="memo_resolution",
        placeholder="Une action concrète pour les jours qui viennent…",
    ).strip()

    if st.button("Enregistrer le mémo", type="primary", disabled=not (title and body)):
        ov = loading_overlay("LumenVia enregistre ton mémo…")
        try:
            memo_id = sha256(
                f"memo|{user_entity_id}|{date_str}|{title}|{body}|{resolution}".encode("utf-8")
            ).hexdigest()[:24]
            memo_path = f"Memos/{user_entity_id}/{date_str}/{memo_id}.md"
            md_body = body.rstrip()
            if resolution:
                md_body += "\n\n---\n\n**Ma résolution :** " + resolution
            upload_text(
                gcs=gcs,
                bucket_name=cfg.gcs_bucket_name,
                path=memo_path,
                text=md_body,
                content_type="text/markdown; charset=utf-8",
            )
            append_immutable_row(
                gspread_client=gs,
                spreadsheet_id=cfg.gsheet_id,
                table="memos",
                values_by_col={
                    "entity_id": memo_id,
                    "user_entity_id": user_entity_id,
                    "date": date_str,
                    "zone": zone,
                    "title": title,
                    "resolution": resolution,
                    "memo_gcs_path": memo_path,
                    "gen_entity_id": "",
                },
            )
            st.success("OK — mémo enregistré.")
        finally:
            ov.empty()

    st.divider()
    st.subheader("Export PDF — Graine de Parole")
    st.caption(
        "Source des mémos : lignes **memos** (Sheets) + fichier Markdown sur Cloud ; les **résolutions** viennent du champ "
        "« Ma résolution » pour chaque ligne du mois."
    )
    today = date.today()
    default_month = today.replace(day=1)
    ref_pdf = st.date_input(
        "Mois à exporter (n’importe quel jour dans ce mois)",
        value=default_month,
        key="memo_pdf_month_pick",
    )
    ym_key = ref_pdf.strftime("%Y-%m")
    month_memos_pdf = sorted(
        [m for m in my_memos_sorted if str(m.get("date") or "").strip().startswith(ym_key)],
        key=lambda r: str(r.get("date") or ""),
    )
    st.caption(f"**{len(month_memos_pdf)}** mémo(s) trouvé(s) pour **{_french_month_year(ref_pdf)}**.")

    if st.button("Préparer le PDF du mois", type="secondary", key="memo_pdf_build_btn"):
        ov = loading_overlay("LumenVia compose le PDF mensuel…")
        try:
            items: list[dict] = []
            resolutions_pdf: list[tuple[str, str]] = []
            for m in month_memos_pdf:
                ds = str(m.get("date") or "").strip()[:10]
                title_pdf = str(m.get("title") or "Mémo").strip()
                res = str(m.get("resolution") or "").strip()
                if res:
                    resolutions_pdf.append((ds, res))
                body_raw = ""
                mp = str(m.get("memo_gcs_path") or "").strip()
                if mp:
                    try:
                        body_raw = download_bytes(gcs=gcs, bucket_name=cfg.gcs_bucket_name, path=mp).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception as ex:
                        body_raw = f"[Erreur lecture Cloud] {ex}"
                items.append(
                    {
                        "title": title_pdf,
                        "date_str": ds,
                        "body_plain": strip_light_markdown_to_plain(body_raw),
                    }
                )
            pdf_bytes = build_graine_parole_monthly_pdf_bytes(
                month_label_fr=_french_month_year(ref_pdf),
                items=items,
                resolutions=resolutions_pdf,
            )
            st.session_state[f"memo_pdf_blob_{ym_key}"] = pdf_bytes
        except Exception as ex:
            st.exception(ex)
        finally:
            ov.empty()

    pdf_blob = st.session_state.get(f"memo_pdf_blob_{ym_key}")
    if pdf_blob:
        st.download_button(
            label=f"Télécharger le PDF ({ym_key})",
            data=pdf_blob,
            file_name=f"lumenvia_graine_parole_{ym_key}.pdf",
            mime="application/pdf",
            key=f"memo_pdf_dl_{ym_key}",
        )
