"""Découpage TTS lectures — pas de micro-morceau final."""

from __future__ import annotations

import unittest

from core.sunday_gemini_tts import (
    _BAD_DIT_COLON_TAIL,
    _LITURGY_READINGS_MAX_CHARS,
    _MIN_TTS_CHUNK_CHARS,
    _liturgy_section_tts_pieces,
    finalize_tts_chunks,
)


class TestTtsChunkFinalize(unittest.TestCase):
    def test_no_short_tail_after_long_evangelile_block(self) -> None:
        body = (
            "En ce temps-là, Jésus disait à ses disciples cette parabole. "
            "Celui qui avait reçu deux talents s'approcha aussi et dit : "
            "Seigneur, tu m'as confié deux talents. "
            "Celui qui avait reçu un seul talent s'approcha aussi et dit : "
            "Seigneur, je savais que tu es un homme dur."
        )
        body = body + " " + ("Parole du Seigneur. " * 400)
        pieces = _liturgy_section_tts_pieces(
            "Évangile",
            body,
            max_chars=_LITURGY_READINGS_MAX_CHARS,
            pref_langue="FR",
        )
        self.assertGreaterEqual(len(pieces), 1)
        for p in pieces:
            self.assertFalse(
                _BAD_DIT_COLON_TAIL.search(p.strip()),
                f"césure après « dit : » : {p[-80:]!r}",
            )
            if len(p) < _MIN_TTS_CHUNK_CHARS:
                self.assertGreaterEqual(
                    len(p),
                    40,
                    f"micro-morceau TTS ({len(p)} car.) : {p[:80]!r}…",
                )

    def test_merge_after_dit_colon(self) -> None:
        chunks = ["Avant. Celui qui avait reçu un seul talent s'approcha aussi et dit :", "Seigneur, je savais."]
        out = finalize_tts_chunks(chunks, max_chars=80)
        self.assertEqual(len(out), 1)
        self.assertIn("Seigneur, je savais", out[0])

    def test_finalize_merges_overflow_tail(self) -> None:
        a = "x" * 1380
        b = "y" * 60
        out = finalize_tts_chunks([a, b], max_chars=1400)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(len(out[0]), _MIN_TTS_CHUNK_CHARS)


if __name__ == "__main__":
    unittest.main()