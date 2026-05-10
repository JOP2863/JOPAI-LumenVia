"""Admin — Simulateur vision mobile."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st
import streamlit.components.v1 as components

from ui.navigation import _lumenvia_phone_like_user_agent, lumenvia_app_origin_url


def render_admin_mobile_simulator() -> None:
    """Panneau recette : prévisualisation iframe + paramètres du cadre appliqué à toute la session."""
    if _lumenvia_phone_like_user_agent():
        st.info("Simulateur mobile masqué sur téléphone : utilise l’app directement sur cet écran.")
        return
    st.title("Simulateur vision mobile")
    st.markdown(
        """
Recette depuis un **ordinateur** : même session Streamlit que l’écran suivant après navigation.

**Deux modes :**
1. **Cadre sur l’app** : même onglet, réduit la zone principale façon téléphone (`max-width`), utile pour *Menu*,
   *La Lumière du Dimanche*, *Mon Aide‑Mémoire* (dont expander « Mes mémos ») et les saisies.
2. **iframe** ci‑dessous : **nouvelle connexion Streamlit** (session distincte).

Le **clavier virtuel** réel du téléphone n’est pas reproductible ici ; pour un faux clavier utilise
**Chrome / Edge → F12 → mode appareil** en complément si besoin.
        """.strip()
    )
    st.toggle(
        "Cadre téléphone sur l’app (session)",
        key="admin_phone_preview",
        help="Réduit la zone principale comme sur un téléphone (largeur ci‑dessous). À activer ici puis naviguer dans l’admin ou les pages métier.",
    )

    if "admin_mobile_preview_width" not in st.session_state:
        st.session_state["admin_mobile_preview_width"] = 390

    p1, p2, p3, p4 = st.columns(4)
    if p1.button("320 px · petit tel.", key="adm_mob_preset_320"):
        st.session_state["admin_mobile_preview_width"] = 320
        st.rerun()
    if p2.button("360 px · classique", key="adm_mob_preset_360"):
        st.session_state["admin_mobile_preview_width"] = 360
        st.rerun()
    if p3.button("390 px · iPhone", key="adm_mob_preset_390"):
        st.session_state["admin_mobile_preview_width"] = 390
        st.rerun()
    if p4.button("428 px · large", key="adm_mob_preset_428"):
        st.session_state["admin_mobile_preview_width"] = 428
        st.rerun()

    w = st.slider(
        "Largeur du cadre (px)",
        min_value=280,
        max_value=560,
        value=int(st.session_state.get("admin_mobile_preview_width", 390)),
        step=2,
        help="Largeur du cadre quand « Cadre téléphone sur l’app » est activé, et pour l’iframe ci‑dessous.",
    )
    st.session_state["admin_mobile_preview_width"] = int(w)
    st.caption(
        "Active d’abord **Cadre téléphone sur l’app** ci‑dessus, puis navigue ; ou utilise un bouton "
        "**+ cadre** pour l’allumer automatiquement."
    )

    st.subheader("Ouvrir une page métier avec le cadre")
    oc1, oc2, oc3 = st.columns(3)
    with oc1:
        if st.button("La Lumière du Dimanche + cadre", key="adm_mob_go_sunday", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "sunday"
            st.rerun()
    with oc2:
        if st.button("Mon Aide‑Mémoire + cadre", key="adm_mob_go_memo", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "memo"
            st.rerun()
    with oc3:
        if st.button("À propos + cadre", key="adm_mob_go_about", use_container_width=True):
            st.session_state["_lumenvia_enable_phone_preview"] = True
            st.session_state["admin_mobile_preview_width"] = int(
                st.session_state.get("admin_mobile_preview_width", 390)
            )
            st.session_state.route = "about"
            st.rerun()

    st.divider()
    st.subheader("Aperçu iframe (session distincte)")
    origin = lumenvia_app_origin_url()
    if not origin:
        st.info(
            "Pour charger l’iframe, définis `PUBLIC_APP_URL` (ou `public_app_url`) dans les secrets avec l’URL publique "
            "de déploiement (ex. ton app Streamlit Cloud). Sinon Streamlit doit exposer `st.context.url` sur ton hébergement."
        )
    else:
        base_src = origin.rstrip("/") + "/"
        sep = "&" if "?" in base_src else "?"
        src = html_escape(base_src + sep + "lumenvia_narrow_nav=1", quote=True)
        iw = max(280, min(560, int(st.session_state.get("admin_mobile_preview_width", 390) or 390)))
        iframe_html = f"""
<div style="display:flex;justify-content:center;background:linear-gradient(165deg,#3a3a42,#121214);padding:1rem;border-radius:12px;">
  <iframe
    src="{src}"
    title="LumenVia — aperçu mobile"
    style="width:{iw}px;height:760px;border:12px solid #0d0d0f;border-radius:36px;box-sizing:border-box;background:#fdfbf7;"
    loading="lazy"
    referrerpolicy="strict-origin-when-cross-origin"
  ></iframe>
</div>
"""
        components.html(iframe_html, height=840, scrolling=True)
        # (supprimé) texte d’explication sous l’iframe

