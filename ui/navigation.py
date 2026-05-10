"""Navigation principale : menu public, tuiles admin, CSS des boutons sensibles."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from ui.reading_comfort import inject_reading_comfort_css, render_reading_comfort_expander


# Pages Administration (sans « Quitter administration » ni toggle) — même ordre que la grille bureau.
_ADMIN_PAGES: tuple[tuple[str, str, str], ...] = (
    ("step3", "Visuels\nliturgiques", "admin_step3"),
    ("thumbs", "Vignettes\nCloud", "admin_thumbs"),
    ("vision", "Texte\nimages", "admin_vision"),
    ("readings_cache", "Cache\nlectures", "admin_readings_cache"),
    ("accounts", "Comptes\ninscrits", "admin_accounts"),
    ("emailing", "Emailing", "admin_emailing"),
    ("feedback_ai", "Sondage\nsynthèse", "admin_feedback_insights"),
    ("scheduler", "Planificateur", "admin_scheduler"),
    ("res", "Test\nressources", "admin_resources"),
    ("cdc", "Cahier\ndes\ncharges", "admin_cdc"),
    ("plan", "Plan\nconsolidé", "admin_plan"),
    ("refactor", "Refactor\ncode", "admin_refactor"),
    ("recette", "Recette\ncontinue", "admin_recette_continue"),
    ("granularity", "Radar\ngranularité", "admin_granularity"),
    ("mobile_sim", "Simulateur\nmobile", "admin_mobile_sim"),
)

def _admin_pages_for_device() -> list[tuple[str, str, str]]:
    """Sur téléphone réel, le simulateur mobile n’a pas de sens → masqué."""
    pages = list(_ADMIN_PAGES)
    try:
        if _lumenvia_phone_like_user_agent():
            pages = [p for p in pages if p[0] != "mobile_sim"]
    except Exception:
        pass
    return pages

def _admin_do_logout_navigation() -> None:
    """Sortie administration : même effet depuis la grille bureau ou depuis le Menu mobile."""
    st.session_state.pop("admin_authenticated", None)
    st.session_state.pop("admin_phone_preview", None)
    st.session_state.route = "about"

def _lumenvia_phone_like_user_agent() -> bool:
    """Détection téléphone via User-Agent (``st.context.headers``), sans dépendre du viewport/CSS.

    Sur certains téléphones Streamlit/hosting, les media-queries voient encore une largeur « bureau » alors que les
    contrôles tactiles nécessitent le layout « Menu seul ».
    """
    try:
        hdrs = getattr(st.context, "headers", None)
        if hdrs is None:
            return False
        ua = str(hdrs.get("user-agent") or hdrs.get("User-Agent") or "").lower()
    except Exception:
        return False
    if not ua.strip():
        return False
    # iPad en mode bureau : UA type Mac sans « mobi » — on garde la grille large.
    if "ipad" in ua and "mobi" not in ua:
        return False
    if "iphone" in ua or "ipod" in ua:
        return True
    if "android" in ua and "tablet" not in ua:
        return True
    if "mobi" in ua:
        return True
    return False


def lumenvia_app_origin_url() -> str | None:
    """Origine HTTPS de l’app pour iframe simulateur (``PUBLIC_APP_URL`` ou URL courante Streamlit)."""
    try:
        s = st.secrets
        base = str(s.get("PUBLIC_APP_URL") or s.get("public_app_url") or "").strip().rstrip("/")
    except Exception:
        base = ""
    if base:
        return base
    try:
        u = str(getattr(st.context, "url", "") or "").strip()
        if not u:
            return None
        from urllib.parse import urlparse

        p = urlparse(u)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception:
        pass
    return None


def _use_compact_top_nav() -> bool:
    """Menu dépliant seul : iframe (`lumenvia_narrow_nav`) ou client téléphone détecté par UA."""
    if st.session_state.get("lumenvia_narrow_nav"):
        return True
    return _lumenvia_phone_like_user_agent()

def render_admin_navigation_in_popover() -> None:
    """Tuiles Administration dans le Menu dépliant (viewport étroit / iframe / téléphone UA)."""
    if not st.session_state.get("admin_authenticated"):
        return
    st.divider()
    st.caption("Administration")
    for slug, label, rte in _admin_pages_for_device():
        short = label.replace("\n", " ")
        if st.button(short, key=f"adm_p_{slug}", use_container_width=True, type="secondary"):
            st.session_state.route = rte
            st.rerun()
    if st.button("Quitter administration", key="adm_p_logout", use_container_width=True, type="secondary"):
        _admin_do_logout_navigation()
        st.rerun()

def _inject_admin_action_buttons_css() -> None:
    """
    Accentue deux actions sensibles (Déconnexion / Quitter administration) sans changer la grille.
    Cible plusieurs versions Streamlit : `id`/data contenant la clé du widget lorsqu’elle est exposée.
    """
    st.markdown(
        """
<style>
/* Déconnexion — ton pétrole (charte footer) */
div[class*="st-key-nav_feedback_beside_logout"] button,
div[class*="nav_feedback_beside_logout"] button,
div[id*="nav_feedback_beside_logout"] button,
div[data-anchor-streamlit*="nav_feedback_beside_logout"] button {
  background-color: #ffffff !important;
  color: #0b2745 !important;
  border: 1px solid #D4AF37 !important;
  justify-content: center !important;
  min-height: 64px !important;
}
div[class*="st-key-nav_feedback_beside_logout"] button p,
div[class*="nav_feedback_beside_logout"] button p,
div[id*="nav_feedback_beside_logout"] button p,
div[data-anchor-streamlit*="nav_feedback_beside_logout"] button p {
  text-align: center !important;
  white-space: normal !important;
  overflow: visible !important;
  word-break: keep-all !important;
  line-height: 1.18 !important;
  width: 100% !important;
}
div[class*="st-key-auth_logout_nav"] button,
div[class*="auth_logout_nav"] button,
div[id*="auth_logout_nav"] button,
div[data-anchor-streamlit*="auth_logout_nav"] button {
  background-color: #145a72 !important;
  color: #ffffff !important;
  border-color: #0f4456 !important;
  justify-content: center !important;
  text-align: center !important;
  min-height: 64px !important;
}
div[class*="st-key-auth_logout_nav"] button p,
div[class*="auth_logout_nav"] button p,
div[id*="auth_logout_nav"] button p,
div[data-anchor-streamlit*="auth_logout_nav"] button p {
  color: #ffffff !important;
  text-align: center !important;
  white-space: normal !important;
  overflow: visible !important;
  word-break: keep-all !important;
  overflow-wrap: normal !important;
  hyphens: none !important;
  width: 100% !important;
  line-height: 1.2 !important;
}
div[class*="st-key-auth_logout_nav"] button span,
div[class*="auth_logout_nav"] button span,
div[id*="auth_logout_nav"] button span,
div[data-anchor-streamlit*="auth_logout_nav"] button span {
  color: #ffffff !important;
}
div[class*="st-key-auth_logout_nav"] button:hover,
div[class*="auth_logout_nav"] button:hover,
div[id*="auth_logout_nav"] button:hover,
div[data-anchor-streamlit*="auth_logout_nav"] button:hover {
  filter: brightness(1.06);
}

/* Quitter administration — doré/ocre */
div[class*="st-key-adm_nav_logout"] button,
div[class*="adm_nav_logout"] button,
div[id*="adm_nav_logout"] button,
div[data-anchor-streamlit*="adm_nav_logout"] button {
  background-color: #8b6914 !important;
  color: #ffffff !important;
  border-color: #654d0f !important;
}
div[class*="st-key-adm_nav_logout"] button p,
div[class*="adm_nav_logout"] button p,
div[id*="adm_nav_logout"] button p,
div[data-anchor-streamlit*="adm_nav_logout"] button p {
  color: #ffffff !important;
}
div[class*="st-key-adm_nav_logout"] button:hover,
div[class*="adm_nav_logout"] button:hover,
div[id*="adm_nav_logout"] button:hover,
div[data-anchor-streamlit*="adm_nav_logout"] button:hover {
  filter: brightness(1.06);
}

div[class*="st-key-adm_p_logout"] button,
div[class*="adm_p_logout"] button,
div[id*="adm_p_logout"] button,
div[data-anchor-streamlit*="adm_p_logout"] button {
  background-color: #8b6914 !important;
  color: #ffffff !important;
  border-color: #654d0f !important;
}
div[class*="st-key-adm_p_logout"] button p,
div[class*="adm_p_logout"] button p,
div[id*="adm_p_logout"] button p,
div[data-anchor-streamlit*="adm_p_logout"] button p {
  color: #ffffff !important;
}
div[class*="st-key-adm_p_logout"] button:hover,
div[class*="adm_p_logout"] button:hover,
div[id*="adm_p_logout"] button:hover,
div[data-anchor-streamlit*="adm_p_logout"] button:hover {
  filter: brightness(1.06);
}
</style>

        """,
        unsafe_allow_html=True,
    )


def _inject_admin_active_tile_css() -> None:
    """Même état visuel « tuile active » que le menu principal (fond doré léger + bordure)."""
    if not st.session_state.get("admin_authenticated"):
        return
    cur = str(st.session_state.get("route") or "").strip()
    for slug, _label, rte in _admin_pages_for_device():
        if cur == rte:
            st.markdown(
                f"""
<style>
/* Grille bureau (`adm_nav_*`) et Menu dépliant (`adm_p_*`) */
div[class*="st-key-adm_nav_{slug}"] button[kind="secondary"],
div[class*="st-key-adm_p_{slug}"] button[kind="secondary"] {{
  background: rgba(212, 175, 55, 0.16) !important;
  border-color: rgba(212, 175, 55, 0.65) !important;
}}
div[class*="st-key-adm_nav_{slug}"] button[kind="secondary"]:focus-visible,
div[class*="st-key-adm_p_{slug}"] button[kind="secondary"]:focus-visible {{
  outline: 2px solid rgba(212, 175, 55, 0.75) !important;
  outline-offset: 2px !important;
}}
</style>
""",
                unsafe_allow_html=True,
            )
            return


def admin_nav_bar() -> None:
    """Menu complémentaire réservé à la session administrateur (après connexion).

    Masqué en session **iframe simulateur** (`lumenvia_narrow_nav`) : l’admin y est uniquement sous Menu.
    Sur grand écran : `lv_admin_desktop_shell`. Sinon entrées sous Menu (CSS compact ou UA / iframe).
    """
    if not st.session_state.get("admin_authenticated"):
        return
    if _use_compact_top_nav():
        return
    pages = _admin_pages_for_device()
    with st.container(key="lv_admin_desktop_shell"):
        st.markdown("---")
        st.caption("Administration")
        # Rend toutes les tuiles admin sans dépendre d’index fixes
        tiles = list(pages)
        cols_per_row = 4
        for start in range(0, len(tiles), cols_per_row):
            row = tiles[start : start + cols_per_row]
            rcols = st.columns(cols_per_row, gap="small")
            for i, (slug, label, rte) in enumerate(row):
                with rcols[i]:
                    if st.button(label, key=f"adm_nav_{slug}", use_container_width=True, type="secondary"):
                        st.session_state.route = rte
                        st.rerun()
            # Complète la ligne avec “Quitter administration” si c’est la dernière ligne et qu’il reste de la place
            if start + cols_per_row >= len(tiles) and len(row) < cols_per_row:
                with rcols[len(row)]:
                    if st.button(
                        "Quitter\nadministration",
                        key="adm_nav_logout",
                        use_container_width=True,
                        type="secondary",
                    ):
                        _admin_do_logout_navigation()
                        st.rerun()
        # Si la dernière ligne était pleine, ajoute un bouton de sortie sur une ligne dédiée
        if len(tiles) % cols_per_row == 0:
            r = st.columns(cols_per_row, gap="small")
            with r[cols_per_row - 1]:
                if st.button(
                    "Quitter\nadministration",
                    key="adm_nav_logout",
                    use_container_width=True,
                    type="secondary",
                ):
                    _admin_do_logout_navigation()
                    st.rerun()

def top_nav() -> str:
    if "route" not in st.session_state:
        st.session_state.route = "about"

    _lv_tc = str(st.session_state.get("lv_text_comfort") or "").strip().lower()
    if _lv_tc and _lv_tc not in ("standard", "large", "xlarge"):
        st.session_state.lv_text_comfort = "standard"

    inject_reading_comfort_css()

    logo_path = Path("assets/branding/logo_mark.svg")
    uid = str(st.session_state.get("auth_user_entity_id") or "").strip()
    email = str(st.session_state.get("auth_email_lc") or "").strip()
    is_admin = bool(st.session_state.get("admin_authenticated"))
    compact_nav = _use_compact_top_nav()

    if logo_path.is_file():
        _, mid, _ = st.columns([1, 1, 1])
        with mid:
            st.image(str(logo_path), width=56)

    render_reading_comfort_expander()

    nbsp = "\u00A0"
    labels = [
        # Évite le markdown dans les labels (peut décaler le rendu). "𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮" = LumenVia en gras unicode.
        ("about", f"𝗟𝘂𝗺𝗲𝗻𝗩𝗶𝗮{nbsp}:\nc'est quoi?"),
        ("sunday", "La lumière\ndu dimanche"),
        ("memo", f"Mon\nAide‑Mémoire"),
        ("join", "S'inscrire à la Newsletter"),
        ("account", "Mon Compte"),
    ]

    def _nav_popover_body() -> None:
        for route, label in labels:
            short = label.replace("\n", " ")
            if st.button(short, key=f"nav_m_{route}", use_container_width=True, type="secondary"):
                st.session_state.route = route
                st.rerun()
        if st.button("Donner\nVotre avis", key="nav_m_feedback", use_container_width=True, type="secondary"):
            st.session_state.route = "feedback"
            st.rerun()
        if is_admin:
            render_admin_navigation_in_popover()

    # Nouvelle instance du popover par route pour refermer après navigation (BaseWeb peut garder le volet ouvert).
    _menu_pop_key = f"lv_menu_pop_{str(st.session_state.route)}"

    if compact_nav:
        # Téléphone / iframe étroit : un seul “Menu”
        with st.popover("Menu", use_container_width=True, key=_menu_pop_key):
            _nav_popover_body()
    else:
        # Tuile active : on colore l'entrée correspondant à la route courante.
        try:
            cur = str(st.session_state.get("route") or "").strip().lower()
        except Exception:
            cur = ""
        # Styles d'état actif (tuile courante) + bouton “Donner votre avis”
        active_tile_css = ""
        if cur in {r for r, _ in labels}:
            active_tile_css = f"""
div[class*="st-key-nav_w_{cur}"] button[kind="secondary"] {{
  background: rgba(212, 175, 55, 0.16) !important;
  border-color: rgba(212, 175, 55, 0.65) !important;
}}
            """.strip()
        # “Donner votre avis” (hors tuiles) : actif si route feedback
        feedback_bg = "rgba(212, 175, 55, 0.16)" if cur == "feedback" else "white"
        feedback_border = "rgba(212, 175, 55, 0.65)" if cur == "feedback" else "var(--liturgie-gold)"
        st.markdown(
            f"""
<style>
{active_tile_css}
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] {{
  background: {feedback_bg} !important;
  border-color: {feedback_border} !important;
}}
/* Force le retour à la ligne “Donner / Votre avis” */
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] p,
div[class*="st-key-nav_feedback_beside_logout"] button[kind="secondary"] span {{
  white-space: pre-line !important;
  color: var(--liturgie-text) !important;
}}
</style>
            """.strip(),
            unsafe_allow_html=True,
        )
        # Version web : pas de tuile “Menu”, uniquement les entrées directes sur une seule ligne.
        # Les libellés peuvent avoir au plus 2 lignes via '\n' (CSS: white-space: pre-line).
        with st.container(key="lv_nav_web_one_row"):
            cols = st.columns([1, 1, 1, 1, 1], gap="small")
            for i, (route, label) in enumerate(labels):
                with cols[i]:
                    if st.button(label, key=f"nav_w_{route}", use_container_width=True, type="secondary"):
                        st.session_state.route = route
                        st.rerun()

    if uid:
        b1, b2, b3 = st.columns([3.35, 1.45, 1.95], gap="small")
        with b1:
            st.caption(f"🟢 Connecté · {email or 'session active'}")
        with b2:
            if st.button(
                "Donner\nVotre avis",
                key="nav_feedback_beside_logout",
                type="secondary",
                use_container_width=True,
            ):
                st.session_state.route = "feedback"
                st.rerun()
        with b3:
            if st.button("Déconnexion", key="auth_logout_nav", use_container_width=True):
                for k in ("auth_user_entity_id", "auth_email_lc"):
                    if k in st.session_state:
                        del st.session_state[k]
                st.session_state.pop("admin_authenticated", None)
                st.session_state.pop("admin_phone_preview", None)
                st.rerun()

    if is_admin:
        _inject_admin_active_tile_css()
    admin_nav_bar()

    # Styles des boutons “Déconnexion” / “Quitter administration” (couleurs distinctes si le DOM expose la clé).
    if uid or st.session_state.get("admin_authenticated"):
        _inject_admin_action_buttons_css()

    return st.session_state.route
