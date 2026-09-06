"""Frise identitaire — fichier local, sous le titre, pas sur la vitrine."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from core.frise_identitaire import chemin_frise_identitaire, nom_et_baseline


class TestFriseIdentitaire(unittest.TestCase):
    def test_chemin_local_si_present(self) -> None:
        p = chemin_frise_identitaire()
        self.assertIsNotNone(p)
        assert p is not None
        self.assertTrue(p.is_file())
        self.assertGreater(p.stat().st_size, 0)
        self.assertEqual(p.name, "lumen_via.mp4")
        self.assertIn("JOPAI-LumenVia", str(p.resolve()))

    def test_rien_si_absent(self) -> None:
        with patch("core.frise_identitaire._REPO_ROOT", Path("/tmp/lv-frise-absente")):
            self.assertIsNone(chemin_frise_identitaire())

    def test_nom_et_baseline_carte_locale(self) -> None:
        nom, baseline = nom_et_baseline()
        self.assertEqual(nom, "JOPAI-LumenVia")
        self.assertTrue(baseline)
        self.assertNotIn("http", baseline.lower())
        self.assertNotIn("nexus", baseline.lower())

    def test_login_sous_le_titre_pas_vitrine(self) -> None:
        login = Path(__file__).resolve().parents[1] / "ui" / "admin" / "login.py"
        about = Path(__file__).resolve().parents[1] / "ui" / "pages" / "about.py"
        src_login = login.read_text(encoding="utf-8")
        src_about = about.read_text(encoding="utf-8")
        self.assertIn("render_titre_puis_frise", src_login)
        self.assertIn("nom_et_baseline", src_login)
        self.assertNotIn("afficher_frise", src_about)
        self.assertNotIn("st.video", src_about)
        self.assertNotIn("lumen_via.mp4", src_about)

    def test_apptest_titre_puis_video_muette(self) -> None:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_string(
            "\n".join(
                [
                    "from core.frise_identitaire import nom_et_baseline",
                    "from ui.frise_banniere import render_titre_puis_frise",
                    "import streamlit as st",
                    "nom, baseline = nom_et_baseline()",
                    "render_titre_puis_frise(titre=nom, baseline=baseline)",
                    "st.subheader('Connexion administration')",
                ]
            ),
            default_timeout=20,
        )
        at.run()
        self.assertFalse(bool(at.exception))
        self.assertEqual(at.title[0].value, "JOPAI-LumenVia")
        self.assertIn("compagnon", (at.caption[0].value or "").lower())
        types = [getattr(n, "type", "") for n in at]
        self.assertIn("title", types)
        self.assertIn("caption", types)
        self.assertIn("video", types)
        self.assertLess(types.index("title"), types.index("caption"))
        self.assertLess(types.index("caption"), types.index("video"))
        self.assertLess(types.index("video"), types.index("subheader"))
        video = next(n for n in at if getattr(n, "type", "") == "video")
        proto = video.proto
        self.assertTrue(proto.loop)
        self.assertTrue(proto.autoplay)
        self.assertTrue(proto.muted)

    def test_pas_de_genese_ni_rotation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in ("core/frise_identitaire.py", "ui/frise_banniere.py", "ui/admin/login.py"):
            txt = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("genese", txt.lower())
            self.assertNotIn("random", txt.lower())
            self.assertNotIn("JOPAI-NEXUS", txt)


if __name__ == "__main__":
    unittest.main()
