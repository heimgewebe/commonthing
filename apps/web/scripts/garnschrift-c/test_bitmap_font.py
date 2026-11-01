"""Versioned native-color wool-font asset contract (stdlib-only CI tests)."""

import hashlib
import struct
import unittest
import zlib

from raster_from_sheet import SHEET, SHEET_SHA256

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
ASSETS = {
    "dark": ("f9557768fcdae1218d998449edbbad41a6973828ae39980ac832fe60003eba62", 415724),
    "light": ("d4d0036348e0ef59df81d19351eb96ef6bcf9cbb08e6c5bfe5e6297968ecc5e8", 442140),
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


if __name__ == "__main__":
    unittest.main()
