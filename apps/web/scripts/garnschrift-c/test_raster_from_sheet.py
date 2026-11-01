"""Test the generated yarn image assets, their provenance and glyph coverage."""

import importlib.util
from pathlib import Path
import struct
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "raster_from_sheet", HERE / "raster_from_sheet.py"
)
raster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(raster)


class RasterYarnAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = raster.read_png_rgb()

    def test_source_and_glyph_inventory(self):
        self.assertEqual(self.source[:2], (1448, 1086))
        required = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            "ÄÖÜäöüß0123456789 .,:;!?-/+"
        )
        self.assertTrue(set(required).issubset(raster.GLYPHS | {" ": ()}))
        for char in ("Ä", "ö", "ß", "9", ".", "?", "-"):
            with self.subTest(char=char):
                glyph = raster.glyph(char, "dark", self.source)
                self.assertGreater(glyph[0], 0)
                self.assertGreater(glyph[1], 0)
        with self.assertRaises(ValueError):
            raster.render("🙂", "dark", self.source)

    def test_checked_in_words_equal_rebuild_and_are_small_transparent_pngs(self):
        for word in raster.WORDS:
            for theme in ("light", "dark"):
                with self.subTest(word=word, theme=theme):
                    expected = raster.render(word, theme, self.source)
                    stored = raster.OUTPUT / f"{word.lower()}-{theme}.png"
                    self.assertEqual(expected, stored.read_bytes())
                    self.assertLess(len(expected), 200_000)
                    self.assertTrue(expected.startswith(raster.PNG_MAGIC))
                    width, height, depth, color = struct.unpack_from(
                        ">IIBB", expected, 16
                    )
                    self.assertEqual(depth, 8)
                    self.assertEqual(color, 6)  # RGBA / transparent
                    self.assertEqual(height, 145)
                    self.assertEqual(width, 280 if word == "Login" else 647)

    def test_glyphs_are_tightly_set_without_added_tracking(self):
        self.assertEqual(raster.LETTER_GAP_PX, 0)
        for word in raster.WORDS:
            with self.subTest(word=word):
                glyph_widths = [raster.glyph(ch, "dark", self.source)[0] for ch in word]
                expected_width = 24 + sum(glyph_widths)
                png = raster.render(word, "dark", self.source)
                self.assertEqual(struct.unpack_from(">I", png, 16)[0], expected_width)
                self.assertLess(expected_width, 24 + sum(glyph_widths) + 9 * len(word))

    def test_arbitrary_german_word_renders_without_browser_or_external_assets(self):
        for theme in ("dark", "light"):
            png = raster.render("ÄÖÜäöüß Kollektivgüter 2026!", theme, self.source)
            self.assertTrue(png.startswith(raster.PNG_MAGIC))
            self.assertLess(len(png), 200_000)


if __name__ == "__main__":
    unittest.main()
