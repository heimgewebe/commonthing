"""Versioned native-color wool-font asset contract (stdlib-only CI tests)."""

import hashlib
import struct
import unittest
import zlib

from raster_from_sheet import SHEET, SHEET_SHA256

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
ASSETS = {
    "dark": ("1a564a5737a627154cefa514ea91c6fb07442e7811d9d36278a8c340bb7e9ecb", 464556),
    "light": ("ad6d6fa413bb5d9bc3b21b7043a580e12338e26c9d55a64ad91e8af09aeb081e", 490260),
}


def decode_generator_png(png: bytes) -> tuple[int, int, bytes]:
    """Decode the no-filter RGBA PNG format emitted by raster_from_sheet."""
    if not png.startswith(bytes((137, 80, 78, 71, 13, 10, 26, 10))):
        raise AssertionError("sbix glyph is not a PNG")
    width, height = struct.unpack_from(">II", png, 16)
    pos = 8
    compressed = bytearray()
    while pos < len(png):
        size = struct.unpack_from(">I", png, pos)[0]
        kind = png[pos + 4:pos + 8]
        chunk = png[pos + 8:pos + 8 + size]
        if kind == b"IDAT":
            compressed.extend(chunk)
        pos += 12 + size
        if kind == b"IEND":
            break
    stride = 4 * width
    rows = zlib.decompress(compressed)
    if len(rows) != (stride + 1) * height:
        raise AssertionError("unexpected wool-glyph scanline length")
    if any(rows[i * (stride + 1)] != 0 for i in range(height)):
        raise AssertionError("unexpected wool-glyph PNG filter")
    return width, height, b"".join(
        rows[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        for y in range(height)
    )


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
                        original = pixels
                        png = strike[offset + 8:strike_start + end_offset]
                        result_width, result_height, refined = decode_generator_png(png)
                        self.assertEqual((result_width, result_height), (width, height))
                        # Exact wool light/shadow RGB and the original glyph box.
                        for channel in range(3):
                            self.assertEqual(
                                original[channel::4], refined[channel::4]
                            )
                        original_alpha = original[3::4]
                        slender_alpha = refined[3::4]
                        self.assertTrue(all(
                            0 <= new <= old
                            and (old == 0) == (new == 0)
                            for old, new in zip(original_alpha, slender_alpha)
                        ))
                        self.assertLess(sum(slender_alpha), sum(original_alpha))
                        self.assertGreater(
                            sum(slender_alpha), 0.82 * sum(original_alpha)
                        )


if __name__ == "__main__":
    unittest.main()
