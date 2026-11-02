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

    def test_adjacent_row_fibers_do_not_leak_into_z_or_a_umlaut(self):
        # On the source sheet, the row above overlaps the original z and ä
        # bounding boxes. This created a floating wool dash after every z.
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                for char, expected_geometry, dense_pixels in (
                    ("z", (49, 54, -50), 1865),
                    ("ä", (61, 70, -62), 2437),
                ):
                    with self.subTest(char=char):
                        width, height, rgba, dy = raster.glyph(char, theme, self.source)
                        self.assertEqual((width, height, dy), expected_geometry)
                        self.assertEqual(
                            sum(alpha >= 70 for alpha in rgba[3::4]), dense_pixels
                        )
                        if char == "z":
                            # The actual lowercase z starts at its x-height;
                            # the former extra stroke occupied the rows above.
                            self.assertFalse(any(a >= 70 for a in rgba[3 : width * 4 : 4]))

    def test_cast_shadows_are_removed_without_erasing_real_yarn(self):
        # Shadows on the photographed teal sheet are not fibers. The old
        # 13-alpha mask made grey halos around every glyph on the cream UI.
        self.assertEqual(raster.yarn_alpha(0), 0)
        self.assertEqual(raster.yarn_alpha(80), 0)
        self.assertEqual(raster.yarn_alpha(raster.CAST_SHADOW_ALPHA_CUTOFF), 0)
        self.assertGreater(raster.yarn_alpha(140), 0)
        self.assertLess(raster.yarn_alpha(140), 140)
        self.assertEqual(raster.yarn_alpha(raster.FIBER_FULL_ALPHA), 160)
        self.assertEqual(raster.yarn_alpha(255), 255)
        for theme in ("dark", "light"):
            for ch in ("E", "g", "i", "z"):
                with self.subTest(theme=theme, char=ch):
                    _, _, data, _ = raster.glyph(ch, theme, self.source)
                    alpha = data[3::4]
                    self.assertGreater(sum(a >= 200 for a in alpha), 600)
                    self.assertTrue(any(0 < a < 160 for a in alpha))
                    # Background cast shadows must never survive opaque.
                    self.assertLess(
                        sum(0 < a < 70 for a in alpha),
                        sum(a >= 200 for a in alpha) // 4,
                    )

    def test_real_descenders_are_not_cropped_and_no_underlined_i_remains(self):
        # Rows in the original sheet overlap; these are exact visually
        # inspected g/p/q/y tails and non-descender i/j/k baselines.
        expected = {
            "g": ((54, 76, -50), 24),
            "p": ((54, 75, -52), 21),
            "q": ((57, 78, -52), 24),
            "y": ((52, 75, -51), 22),
            "i": ((20, 75, -72), 1),
            "j": ((44, 89, -74), 12),
            "k": ((50, 78, -74), 2),
        }
        for theme in ("dark", "light"):
            for ch, (metrics, core_bottom) in expected.items():
                with self.subTest(theme=theme, char=ch):
                    width, height, data, dy = raster.glyph(ch, theme, self.source)
                    self.assertEqual((width, height, dy), metrics)
                    core_rows = [
                        pos // width
                        for pos, alpha in enumerate(data[3::4])
                        if alpha >= 100
                    ]
                    self.assertTrue(core_rows)
                    self.assertEqual(dy + max(core_rows), core_bottom)
                    # A cut-off g used to end on a full-ink bottom row.
                    self.assertFalse(
                        any(a >= 100 for a in data[(height - 1) * width * 4 + 3::4])
                    )
                    if ch in ("i", "k"):
                        self.assertLessEqual(dy + max(core_rows), 2)

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
                    self.assertEqual(width, 254 if word == "Login" else 584)

    def test_optical_kerning_follows_real_yarn_core_shapes(self):
        # These pairwise values originate from the approved contact sheet.
        expected_pairs = {
            "Login": [4, 0, 0, 1],
            "Einstellungen": [0, 1, 0, 1, 3, 0, 1, 2, 1, 2, 1, 1],
        }
        for word, expected in expected_pairs.items():
            with self.subTest(word=word):
                parts = [raster.glyph(ch, "dark", self.source) for ch in word]
                overlaps = [
                    raster.optical_overlap(a, b)
                    for a, b in zip(parts, parts[1:])
                ]
                self.assertEqual(overlaps, expected)
                self.assertGreater(len(set(overlaps)), 1)
                for overlap, (left, right) in zip(
                    overlaps, zip(parts, parts[1:])
                ):
                    self.assertGreaterEqual(overlap, 0)
                    self.assertLessEqual(overlap, raster.MAX_OPTICAL_OVERLAP_PX)
                    self.assertLessEqual(overlap, min(left[0], right[0]) // 3)
                png = raster.render(word, "dark", self.source)
                expected_width = 24 + sum(part[0] for part in parts) - sum(overlaps)
                self.assertEqual(struct.unpack_from(">I", png, 16)[0], expected_width)

    def test_kerning_preserves_spaces_and_theme_geometry(self):
        left = raster.glyph("n", "dark", self.source)
        right = raster.glyph("s", "dark", self.source)
        space = (27, 0, bytearray(), 0)
        self.assertEqual(raster.optical_overlap(left, space), 0)
        self.assertEqual(raster.optical_overlap(space, right), 0)
        for word in raster.WORDS:
            dark = raster.render(word, "dark", self.source)
            light = raster.render(word, "light", self.source)
            self.assertEqual(dark[16:24], light[16:24])

    def test_wool_white_palette_preserves_fiber_detail_and_contours(self):
        self.assertEqual(raster.wool_white_color(75), (198, 195, 186))
        self.assertEqual(raster.wool_white_color(175), (240, 238, 231))
        self.assertEqual(raster.wool_white_color(225), (252, 250, 245))
        for ch in ("E", "n", "L", "g"):
            with self.subTest(ch=ch):
                dark = raster.glyph(ch, "dark", self.source)
                light = raster.glyph(ch, "light", self.source)
                self.assertEqual((dark[0], dark[1], dark[3]),
                                 (light[0], light[1], light[3]))
                self.assertEqual(dark[2][3::4], light[2][3::4])
                pixels = dark[2]
                self.assertTrue(any(0 < a < 255 for a in pixels[3::4]))
                opaque = [tuple(pixels[i:i + 3])
                          for i in range(0, len(pixels), 4)
                          if pixels[i + 3] >= 200]
                self.assertGreater(len(set(opaque)), 35)
                self.assertTrue(all(198 <= r <= 252 and 195 <= g <= 250
                                    and 186 <= b <= 245 and
                                    r >= g >= b and r - g <= 3 and g - b <= 9
                                    for r, g, b in opaque))
                tones = sorted(sum(rgb) / 3 for rgb in opaque)
                self.assertGreater(tones[len(tones) * 95 // 100]
                                   - tones[len(tones) * 5 // 100], 25)

    def test_arbitrary_german_word_renders_without_browser_or_external_assets(self):
        for theme in ("dark", "light"):
            png = raster.render("ÄÖÜäöüß Kollektivgüter 2026!", theme, self.source)
            self.assertTrue(png.startswith(raster.PNG_MAGIC))
            self.assertLess(len(png), 200_000)


if __name__ == "__main__":
    unittest.main()