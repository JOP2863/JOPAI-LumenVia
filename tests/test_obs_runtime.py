"""Sonde contrat ``jopai-obs-1`` — canaux 0 €, ISGC hors boot, pas d’API HTTP."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.gemini_usage import (
    estimer_cout_usd,
    enregistrer_usage_depuis_reponse,
    tokens_from_usage_metadata,
)
from core.obs_http_usage import noter_hit, resume_hits
from core.obs_isgc_cache import lire_snapshot_integrite
from core.obs_runtime import (
    APPLICATION,
    SCHEMA_OBS,
    construire_canaux,
    construire_contrat_obs,
    ecrire_battement,
)


def _gemini_vide() -> dict:
    return {
        "appels": 0,
        "totalTokens": 0,
        "coutEur": 0.0,
        "moisCourant": {"cle": "2026-08", "appels": 0, "totalTokens": 0, "coutEur": 0.0},
        "budget": {"budgetMensuelEur": 50.0},
        "alerte": {"niveau": "ok", "pctBudget": 0},
    }


class TestGeminiUsageMetadata(unittest.TestCase):
    def test_extrait_usage_metadata_camel(self) -> None:
        raw = {"usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 8, "totalTokenCount": 20}}
        self.assertEqual(tokens_from_usage_metadata(raw), (12, 8, 20))

    def test_extrait_usage_metadata_snake(self) -> None:
        raw = {"usage_metadata": {"prompt_token_count": 3, "candidates_token_count": 7}}
        self.assertEqual(tokens_from_usage_metadata(raw), (3, 7, 10))

    def test_journalise_depuis_reponse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "gemini_usage.json"
            data_dir = Path(tmp)
            with (
                patch("core.gemini_usage._DATA_DIR", data_dir),
                patch("core.gemini_usage._FICHIER_USAGE", usage_path),
            ):
                enregistrer_usage_depuis_reponse(
                    modele="gemini-2.5-flash",
                    usage="texte",
                    raw={"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50}},
                    meta={"provider": "vertex"},
                )
                self.assertTrue(usage_path.is_file())
                data = json.loads(usage_path.read_text(encoding="utf-8"))
                self.assertEqual(len(data["entrees"]), 1)
                self.assertEqual(data["entrees"][0]["totalTokens"], 150)
                self.assertEqual(data["entrees"][0]["meta"]["provider"], "vertex")

    def test_estime_cout_positif(self) -> None:
        self.assertGreater(estimer_cout_usd(modele="gemini-2.5-flash", prompt_tokens=1_000_000, candidates_tokens=0), 0)


class TestHitsGratuits(unittest.TestCase):
    def test_compteur_mensuel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "http_usage.json"
            with (
                patch("core.obs_http_usage._DATA_DIR", Path(tmp)),
                patch("core.obs_http_usage._FICHIER_USAGE", path),
            ):
                noter_hit("aelf")
                noter_hit("aelf")
                noter_hit("evangelizo")
                rec = resume_hits("aelf")
                self.assertEqual(rec["canal"], "aelf")
                self.assertEqual(rec["tarif"], "gratuit")
                self.assertEqual(rec["coutEur"], 0)
                self.assertEqual(rec["unite"], "hits")
                self.assertEqual(rec["appels"], 2)
                self.assertEqual(rec["presence"], "actif")
                ev = resume_hits("evangelizo")
                self.assertEqual(ev["appels"], 1)
                uni = resume_hits("universalis")
                self.assertEqual(uni["appels"], 0)
                self.assertEqual(uni["presence"], "declare")
                self.assertEqual(uni["coutEur"], 0)


class TestContratObs(unittest.TestCase):
    def test_canaux_gemini_plus_trois_gratuits_sans_gcs_ni_tenant(self) -> None:
        hits = {
            "aelf": {
                "canal": "aelf",
                "presence": "actif",
                "tarif": "gratuit",
                "mois": "2026-08",
                "appels": 3,
                "unite": "hits",
                "quantite": 3,
                "coutEur": 0,
                "budgetMensuelEur": None,
                "pctBudget": None,
                "alerte": "ok",
            },
            "evangelizo": {
                "canal": "evangelizo",
                "presence": "declare",
                "tarif": "gratuit",
                "mois": "2026-08",
                "appels": 0,
                "unite": "hits",
                "quantite": 0,
                "coutEur": 0,
                "budgetMensuelEur": None,
                "pctBudget": None,
                "alerte": "ok",
            },
            "universalis": {
                "canal": "universalis",
                "presence": "declare",
                "tarif": "gratuit",
                "mois": "2026-08",
                "appels": 0,
                "unite": "hits",
                "quantite": 0,
                "coutEur": 0,
                "budgetMensuelEur": None,
                "pctBudget": None,
                "alerte": "ok",
            },
        }

        def _hits(canal: str, *, mois: str | None = None) -> dict:
            return dict(hits[canal])

        with (
            patch("core.obs_runtime.resume_usage_gemini", return_value=_gemini_vide()),
            patch("core.obs_runtime.resume_hits", side_effect=_hits),
            patch("core.obs_runtime.lire_snapshot_integrite", return_value={"statut": "absent"}),
        ):
            c = construire_contrat_obs(version="test")
        self.assertEqual(c["schema"], SCHEMA_OBS)
        self.assertEqual(c["vitalite"]["version"], "test")
        self.assertIn("uptimeSec", c["vitalite"])
        self.assertIsNone(c["connectivite"]["postgres"])
        self.assertEqual(c["connectivite"]["stockage"], "gcs")
        rt = c["finops"]["runtime"]
        ids = [x["canal"] for x in rt["canaux"]]
        self.assertEqual(ids, ["gemini", "aelf", "evangelizo", "universalis"])
        self.assertNotIn("gcs", ids)
        self.assertEqual(rt["canal"], "gemini")
        self.assertEqual(rt["coutEur"], 0.0)
        for nom in ("aelf", "evangelizo", "universalis"):
            ligne = next(x for x in rt["canaux"] if x["canal"] == nom)
            self.assertEqual(ligne["tarif"], "gratuit")
            self.assertEqual(ligne["coutEur"], 0)
            self.assertNotIn("tenants", ligne)
        self.assertNotIn("tenants", rt)
        self.assertNotIn("health_url", c)
        self.assertEqual(c["finops"]["atelier"]["statut"], "not_implemented")

    def test_construire_canaux_toujours_quatre(self) -> None:
        with (
            patch("core.obs_runtime.resume_usage_gemini", return_value=_gemini_vide()),
            patch(
                "core.obs_runtime.resume_hits",
                side_effect=lambda canal, **_k: {
                    "canal": canal,
                    "presence": "declare",
                    "tarif": "gratuit",
                    "mois": "2026-08",
                    "appels": 0,
                    "unite": "hits",
                    "quantite": 0,
                    "coutEur": 0,
                    "budgetMensuelEur": None,
                    "pctBudget": None,
                    "alerte": "ok",
                },
            ),
        ):
            canaux = construire_canaux(gemini=_gemini_vide())
        self.assertEqual([c["canal"] for c in canaux], ["gemini", "aelf", "evangelizo", "universalis"])
        self.assertEqual(canaux[0]["tarif"], "payant")
        for c in canaux[1:]:
            self.assertEqual(c["coutEur"], 0)
            self.assertNotIn("tenants", c)

    def test_integrite_cache_sans_scan(self) -> None:
        payload = {
            "statut": "ok",
            "isgc": {
                "lettre": "B",
                "score": 72,
                "label": "Surveillance légère",
                "piliers": {
                    "granularite": 80,
                    "hypertrophie": 70,
                    "orchestration": 90,
                    "latence": 60,
                    "documentation": 95,
                },
            },
            "depot": {"tailleMo": 4.2, "stack": "python_streamlit", "loc": 1000},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "isgc_telemetry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("core.obs_isgc_cache.isgc_cache_path", return_value=path):
                integ = lire_snapshot_integrite()
        self.assertEqual(integ["isgc"]["lettre"], "B")
        self.assertEqual(integ["depot"]["tailleMo"], 4.2)
        self.assertEqual(integ["isgc"]["piliers"]["documentation"], 95)
        self.assertIn("ageCacheSec", integ["isgc"])

    def test_heartbeat_ne_importe_pas_le_scan(self) -> None:
        sys.modules.pop("core.obs_isgc_scan", None)
        cache = {
            "statut": "ok",
            "isgc": {
                "lettre": "C",
                "score": 60,
                "piliers": {
                    "granularite": 1,
                    "hypertrophie": 2,
                    "orchestration": 3,
                    "latence": 4,
                    "documentation": 5,
                },
            },
            "depot": {"tailleMo": 1.5, "loc": 12},
        }
        with (
            patch("core.obs_runtime.resume_usage_gemini", return_value=_gemini_vide()),
            patch(
                "core.obs_runtime.resume_hits",
                side_effect=lambda canal, **_k: {
                    "canal": canal,
                    "presence": "declare",
                    "tarif": "gratuit",
                    "mois": "2026-08",
                    "appels": 0,
                    "unite": "hits",
                    "quantite": 0,
                    "coutEur": 0,
                    "budgetMensuelEur": None,
                    "pctBudget": None,
                    "alerte": "ok",
                },
            ),
            patch("core.obs_runtime.lire_snapshot_integrite", return_value=cache) as lect,
        ):
            contrat = construire_contrat_obs()
        lect.assert_called_once()
        self.assertNotIn("core.obs_isgc_scan", sys.modules)
        self.assertEqual(contrat["integrite"]["isgc"]["lettre"], "C")
        self.assertEqual(contrat["integrite"]["depot"]["tailleMo"], 1.5)
        self.assertNotIn("tenants", json.dumps(contrat))

    def test_ecrire_battement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "nexus_heartbeat.json"
            with (
                patch("core.obs_runtime._FICHIER_BATTEMENT", dest),
                patch("core.obs_runtime.resume_usage_gemini", return_value=_gemini_vide()),
                patch(
                    "core.obs_runtime.resume_hits",
                    side_effect=lambda canal, **_k: {
                        "canal": canal,
                        "presence": "declare",
                        "tarif": "gratuit",
                        "mois": "2026-08",
                        "appels": 0,
                        "unite": "hits",
                        "quantite": 0,
                        "coutEur": 0,
                        "budgetMensuelEur": None,
                        "pctBudget": None,
                        "alerte": "ok",
                    },
                ),
                patch("core.obs_runtime.lire_snapshot_integrite", return_value={"statut": "absent"}),
            ):
                path = ecrire_battement()
                self.assertEqual(path, dest)
                payload = json.loads(dest.read_text(encoding="utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["application"], APPLICATION)
                self.assertEqual(payload["contrat"]["schema"], SCHEMA_OBS)
                ids = [x["canal"] for x in payload["contrat"]["finops"]["runtime"]["canaux"]]
                self.assertEqual(ids, ["gemini", "aelf", "evangelizo", "universalis"])
                dump = json.dumps(payload)
                self.assertNotIn("private_key", dump)
                self.assertNotIn("health_url", dump)
                self.assertNotIn("tenants", dump)


class TestIsgcScanFroid(unittest.TestCase):
    def test_taille_mo_hors_venv_git_et_piliers_fr(self) -> None:
        from core.obs_isgc_scan import mesurer_isgc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n" * 20, encoding="utf-8")
            (root / "core").mkdir()
            (root / "core" / "foo.py").write_text("x = 1\n" * 40, encoding="utf-8")
            (root / "ui" / "pages").mkdir(parents=True)
            (root / "ui" / "pages" / "bar.py").write_text("y = 2\n" * 30, encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "cdc.md").write_text("# cdc\n" * 10, encoding="utf-8")
            (root / "requirements.txt").write_text("streamlit\n", encoding="utf-8")
            (root / ".venv" / "lib").mkdir(parents=True)
            (root / ".venv" / "lib" / "fat.bin").write_bytes(b"x" * (2 * 1024 * 1024))
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "pack").write_bytes(b"y" * (2 * 1024 * 1024))
            dest = root / "data" / "isgc_telemetry.json"
            out = mesurer_isgc(root=root, dest=dest)
        isgc = out["isgc"]
        depot = out["depot"]
        self.assertIn(isgc["lettre"], list("ABCDE"))
        self.assertIsInstance(isgc["score"], int)
        for cle in ("granularite", "hypertrophie", "orchestration", "latence", "documentation"):
            self.assertIn(cle, isgc["piliers"])
        self.assertLess(float(depot["tailleMo"]), 1.0)
        self.assertEqual(depot["stack"], "python_streamlit")
        self.assertEqual(depot["deps"], "requirements.txt")
        self.assertGreater(depot["cdcLignes"], 0)


if __name__ == "__main__":
    unittest.main()
