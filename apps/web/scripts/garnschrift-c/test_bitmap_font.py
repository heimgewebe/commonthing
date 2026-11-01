"""Versioned native-color wool-font asset contract (stdlib-only CI tests)."""

import hashlib
import struct
import unittest
import zlib

from raster_from_sheet import SHEET, SHEET_SHA256

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
ASSETS = {
    "dark": ("9e6a99a1dba8ed165fd69f4e3006eb1eca47d96f760d8d70a1f284c738ec869e", 415720),
    "light": ("f1fa4c02f50b731f8bd681ad6a16aea48cbbc60354267f5d5f55d278bef4154f", 442136),
}


class BitmapFontAssets(unittest.TestCase):
    def test_approved_source_unchanged(self):
        self.assertEqual(hashlib.sha256(SHEET.read_bytes()).hexdigest(), SHEET_SHA256)

    def test_fonts_are_versioned_valid_woff_with_real_bitmap_tables(self):
        for theme, (expected_hash, expected_size) in ASSETS.items():
            with self.subTest(theme=theme):
                data = (FONT_DIR / f"garnschrift-c-{theme}.woff").read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected_hash)
                self.assertEqual(len(data), expected_size)
                signature, flavor, length, count = struct.unpack_from(">4s4sIH", data)
                self.assertEqual(signature, b"wOFF")
                self.assertEqual(flavor, bytes((0, 1, 0, 0)))
                self.assertEqual(length, len(data))
                directory = {}
                for index in range(count):
                    tag, offset, compressed, original, checksum = struct.unpack_from(
                        ">4sIIII", data, 44 + index * 20
                    )
                    raw = data[offset:offset + compressed]
                    self.assertEqual(len(raw), compressed)
                    table = zlib.decompress(raw) if compressed < original else raw
                    self.assertEqual(len(table), original)
                    directory[tag] = table
                self.assertIn(b"sbix", directory)
                self.assertIn(b"kern", directory)
                self.assertIn(b"cmap", directory)
                self.assertIn(b"glyf", directory)
                self.assertEqual(struct.unpack_from(">H", directory[b"sbix"])[0], 1)
                self.assertGreater(struct.unpack_from(">I", directory[b"sbix"], 4)[0], 0)
                self.assertGreater(len(directory[b"kern"]), 100)

                # Bitmap bottom offset must align real descenders; sbix
                # uses upward-positive values (opposite raster coordinates).
                from raster_from_sheet import GLYPHS, glyph, read_png_rgb

                strike = directory[b"sbix"]
                strike_start = struct.unpack_from(">I", strike, 8)[0]
                ppem, resolution = struct.unpack_from(">HH", strike, strike_start)
                self.assertEqual(ppem, 110)
                self.assertEqual(resolution, 72)
                approved = read_png_rgb()
                for char in ("g", "p", "L", "E", "."):
                    with self.subTest(glyph=char):
                        gid = 3 + sorted(GLYPHS).index(char)
                        start_offset = struct.unpack_from(
                            ">I", strike, strike_start + 4 + gid * 4
                        )[0]
                        end_offset = struct.unpack_from(
                            ">I", strike, strike_start + 4 + (gid + 1) * 4
                        )[0]
                        self.assertGreater(end_offset, start_offset)
                        offset = strike_start + start_offset
                        origin_x, origin_y = struct.unpack_from(">hh", strike, offset)
                        self.assertEqual(origin_x, 0)
                        self.assertEqual(strike[offset + 4:offset + 8], b"png ")
                        width, height, pixels, dy = glyph(char, theme, approved)
                        self.assertEqual(origin_y, -(dy + height))


if __name__ == "__main__":
    unittest.main()
