"""Page publique « À propos » (JOPAI LumenVia)."""

from __future__ import annotations

from html import escape as html_escape

import streamlit as st

_ABOUT_MARKDOWN = """
« *Ta Parole est une lampe sur mes pas, une lumière sur mon sentier.* »


JOPAI LumenVia est un compagnon spirituel conçu pour vous aider à franchir le seuil de la célébration avec un cœur ouvert et une intelligence éclairée.  
Trop souvent, nous arrivons à la messe sans avoir eu le temps de déposer le bruit du monde. Ce site est une pause, un chemin de lumière (**LumenVia**) pour vous préparer à recevoir la Parole de Dieu.

**Pourquoi utiliser LumenVia ?**

- **Comprendre l’essentiel** : avec l'aide de l'Intelligence Artificielle, nous mettons en perspective les lectures du dimanche pour vous en offrir la synthèse. Il ne s’agit pas d’inventer, mais de souligner le fil rouge qui relie les textes entre eux.
- **Se préparer en chemin** : que vous préfériez lire ou écouter, LumenVia génère pour vous un résumé écrit et un audio. Écoutez la synthèse dans les transports ou en marchant vers l'église pour laisser l’esprit de la fête infuser en vous.
- **Vivre le temps liturgique** : de l’or du Temps Ordinaire au violet du Carême, l’application s’habille aux couleurs de l’Église pour vous aider à habiter pleinement chaque saison de l’année.

**Comment parcourir ce chemin ?**

- **La Lumière du Dimanche** : découvrez les textes du jour et leur synthèse pour nourrir votre méditation.
- **Mon Aide-Mémoire** : créez vos propres mémos pour garder une trace de ce qui a touché votre cœur.
- **Nous rejoindre** : abonnez-vous pour recevoir chaque vendredi soir votre préparation dominicale directement par e-mail, ou par SMS.

Puisse cet outil vous aider à transformer chaque messe en une rencontre plus profonde et plus consciente avec le Christ.
""".strip()


def render_about() -> None:
    st.title("JOPAI LumenVia")
    try:
        st.image("Parole.jpg", use_container_width=True)
    except Exception:
        pass

    # Citation : centrée + couleur thème (autre que noir)
    try:
        quote, rest = _ABOUT_MARKDOWN.split("\n\n", 1)
    except Exception:
        quote, rest = _ABOUT_MARKDOWN, ""
    qtxt = quote.strip().strip("«").strip("»").strip()
    if qtxt:
        st.markdown(
            f"<div style='text-align:center;color:var(--liturgie-accent);font-style:italic;"
            f"font-size:1.02rem;line-height:1.55;margin:0.25rem auto 0.95rem;max-width:min(44rem,95vw);'>"
            f"« {html_escape(qtxt.strip('* ').strip())} »</div>",
            unsafe_allow_html=True,
        )
    if rest.strip():
        st.markdown(rest.strip())
    st.subheader("Référence")
    st.markdown(
        'Source liturgique : [AELF](https://api.aelf.org/) (Association Épiscopale Liturgique pour les pays Francophones).'
    )
