"""Versioned native-color wool-font asset contract (stdlib-only CI tests)."""

import hashlib
import struct
import unittest
import zlib

from raster_from_sheet import SHEET, SHEET_SHA256

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
ASSETS = {
    "dark": (
        "fe8979fe9f6cd5cd1cccbcc8d5a7ccf2cbe79e79ed575d82ec6c87bcfd5dff63",
        362748,
    ),
    "light": (
        "e16c9e1baae88bd56f46fbe97337991311e0a1cdcb7002a229deeb6613532f9e",
        399024,
    ),
}


TEXT_ASSETS = {
    "dark": ("cc5216bfcfa22537c4f0f7ede840af4558cdf02d216b1cf0050de10c1b48e42b", 430408),
    "light": ("dc0d5292b6ee71867b5dfb4c11d2b4c2ad259eb85a5f4238fff99057c3d8ea40", 465684),
}


def woff_tables(data: bytes) -> dict[bytes, bytes]:
    count = struct.unpack_from(">H", data, 12)[0]
    result = {}
    for index in range(count):
        tag, offset, compressed, original, _ = struct.unpack_from(
            ">4sIIII", data, 44 + index * 20
        )
        value = data[offset : offset + compressed]
        result[tag] = zlib.decompress(value) if compressed < original else value
    return result


def decode_generator_png(png: bytes) -> tuple[int, int, bytes]:
    """Decode the no-filter RGBA PNG format emitted by raster_from_sheet."""
    if not png.startswith(bytes((137, 80, 78, 71, 13, 10, 26, 10))):
        raise AssertionError("sbix glyph is not a PNG")
    width, height = struct.unpack_from(">II", png, 16)
    pos = 8
    compressed = bytearray()
    while pos < len(png):
        size = struct.unpack_from(">I", png, pos)[0]
        kind = png[pos + 4 : pos + 8]
        chunk = png[pos + 8 : pos + 8 + size]
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
    return (
        width,
        height,
        b"".join(
            rows[y * (stride + 1) + 1 : (y + 1) * (stride + 1)] for y in range(height)
        ),
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
                    raw = data[offset : offset + compressed]
                    self.assertEqual(len(raw), compressed)
                    table = zlib.decompress(raw) if compressed < original else raw
                    self.assertEqual(len(table), original)
                    directory[tag] = table
                self.assertIn(b"sbix", directory)
                self.assertIn(b"kern", directory)
                self.assertIn(b"cmap", directory)
                self.assertIn(b"glyf", directory)
                self.assertEqual(struct.unpack_from(">H", directory[b"sbix"])[0], 1)
                self.assertGreater(
                    struct.unpack_from(">I", directory[b"sbix"], 4)[0], 0
                )
                self.assertGreater(len(directory[b"kern"]), 100)

                # Every glyph has the same sbix image origin; no
                # browser-dependent per-character bitmap origin remains.
                from raster_from_sheet import GLYPHS, glyph, read_png_rgb

                strike = directory[b"sbix"]
                strike_start = struct.unpack_from(">I", strike, 8)[0]
                ppem, resolution = struct.unpack_from(">HH", strike, strike_start)
                self.assertEqual(ppem, 110)
                self.assertEqual(resolution, 72)
                approved = read_png_rgb()
                punctuation_bottom = {
                    ".": 2,
                    ",": 10,
                    ";": 10,
                    ":": 2,
                    "!": 2,
                    "?": 2,
                    "-": -20,
                    "–": -20,
                    "—": -20,
                    "/": 4,
                    "&": 2,
                    "@": 2,
                    "#": 2,
                    "%": 2,
                    "€": 2,
                    "$": 2,
                    "§": 2,
                    "+": 0,
                    "*": -22,
                }
                for gid, char in enumerate(sorted(GLYPHS), start=3):
                    with self.subTest(glyph=char):
                        beginning = struct.unpack_from(
                            ">I", strike, strike_start + 4 + gid * 4
                        )[0]
                        ending = struct.unpack_from(
                            ">I", strike, strike_start + 4 + (gid + 1) * 4
                        )[0]
                        self.assertGreater(ending, beginning)
                        offset = strike_start + beginning
                        origin_x, origin_y = struct.unpack_from(">hh", strike, offset)
                        self.assertEqual((origin_x, origin_y), (0, -40))
                        self.assertEqual(strike[offset + 4 : offset + 8], b"png ")
                        width, height, source, dy = glyph(char, theme, approved)
                        png = strike[offset + 8 : strike_start + ending]
                        output_width, output_height, bitmap = decode_generator_png(png)
                        self.assertEqual((output_width, output_height), (width, 145))
                        source_core = [
                            i // width
                            for i in range(width * height)
                            if source[4 * i + 3] >= 100
                        ]
                        self.assertTrue(source_core)
                        original_bottom = dy + max(source_core)
                        target = punctuation_bottom.get(char, original_bottom)
                        top = 105 + dy - (original_bottom - target)
                        self.assertGreaterEqual(top, 0)
                        self.assertLessEqual(top + height, 145)
                        self.assertFalse(any(bitmap[: top * width * 4]))
                        self.assertFalse(any(bitmap[(top + height) * width * 4 :]))
                        crop = bitmap[top * width * 4 : (top + height) * width * 4]
                        for channel in range(3):
                            self.assertEqual(crop[channel::4], source[channel::4])
                        source_alpha = source[3::4]
                        out_alpha = crop[3::4]
                        self.assertTrue(
                            all(
                                0 <= value <= original
                                for original, value in zip(source_alpha, out_alpha)
                            )
                        )
                        self.assertGreater(sum(out_alpha), 0.79 * sum(source_alpha))
                        self.assertEqual(top + max(source_core) - 105, target)
                        # Real terminal dots and symbols sit on their
                        # intentionally designed common baseline after thinning.
                        if char in punctuation_bottom:
                            visible_core = [
                                y // width
                                for y in range(145 * width)
                                if bitmap[y * 4 + 3] >= 100
                            ]
                            self.assertTrue(visible_core)
                            actual_bottom = max(visible_core) - 105
                            self.assertLessEqual(abs(actual_bottom - target), 2)
                        # The corrected source crops stop before foreign
                        # next-row pixels. No underlined stray may survive
                        # below the intact non-descending i/k stems.
                        if char in ("i", "k"):
                            stray_alpha = sum(
                                source_alpha[max(0, 10 - dy) * width :]
                            )
                            self.assertEqual(stray_alpha, 0)
                            for row in range(115, 145):
                                self.assertFalse(
                                    any(
                                        bitmap[
                                            (row * width * 4 + 3) : (
                                                (row + 1) * width * 4
                                            ) : 4
                                        ]
                                    )
                                )


    def test_dense_wool_kerning_pairs_do_not_collide(self):
        """The font's kern table must use the final vertically aligned ink."""
        from raster_from_sheet import GLYPHS

        alphabet = sorted(GLYPHS)
        punct = set(".,;:!?-–—/&@#%€$§+*")
        for theme in ASSETS:
            with self.subTest(theme=theme):
                payload = (FONT_DIR / f"garnschrift-c-{theme}.woff").read_bytes()
                count = struct.unpack_from(">H", payload, 12)[0]
                tables = {}
                for index in range(count):
                    tag, offset, compressed, original, _ = struct.unpack_from(
                        ">4sIIII", payload, 44 + index * 20
                    )
                    value = payload[offset : offset + compressed]
                    tables[tag] = zlib.decompress(value) if compressed < original else value
                strike = tables[b"sbix"]
                strike_start = struct.unpack_from(">I", strike, 8)[0]
                rows = {}
                for index, char in enumerate(alphabet, start=3):
                    begin = struct.unpack_from(
                        ">I", strike, strike_start + 4 + index * 4
                    )[0]
                    end = struct.unpack_from(
                        ">I", strike, strike_start + 4 + (index + 1) * 4
                    )[0]
                    bitmap_offset = strike_start + begin
                    width, height, pixels = decode_generator_png(
                        strike[bitmap_offset + 8 : strike_start + end]
                    )
                    self.assertEqual(height, 145)
                    bounds = []
                    for y in range(height):
                        xs = [
                            x for x in range(width)
                            if pixels[(y * width + x) * 4 + 3] >= 100
                        ]
                        bounds.append((xs[0], xs[-1]) if xs else None)
                    rows[index] = (width, bounds)

                kern = tables[b"kern"]
                self.assertEqual(struct.unpack_from(">HH", kern), (0, 1))
                self.assertGreater(struct.unpack_from(">H", kern, 10)[0], 0)
                n_pairs = struct.unpack_from(">H", kern, 10)[0]
                pairs = {
                    (left, right): adjust
                    for i in range(n_pairs)
                    for left, right, adjust in [
                        struct.unpack_from(">HHh", kern, 18 + i * 6)
                    ]
                }
                for (left, right), adjust in pairs.items():
                    if left < 3 or right < 3:
                        continue
                    if alphabet[left - 3] not in punct and alphabet[right - 3] not in punct:
                        continue
                    lw, left_rows = rows[left]
                    _, right_rows = rows[right]
                    gaps = [
                        lw + rb[0] - lb[1] - 1 + adjust
                        for lb, rb in zip(left_rows, right_rows)
                        if lb is not None and rb is not None
                    ]
                    if gaps:
                        self.assertGreaterEqual(
                            min(gaps), 2,
                            f"{theme} {alphabet[left - 3]}{alphabet[right - 3]} "
                            f"kerning {adjust} gives a core gap {min(gaps)}",
                        )
                for left, right in (("O", "-"), ("2", ".")):
                    lid, rid = 3 + alphabet.index(left), 3 + alphabet.index(right)
                    lw, left_rows = rows[lid]
                    _, right_rows = rows[rid]
                    shift = pairs.get((lid, rid), 0)
                    gaps = [
                        lw + rb[0] - lb[1] - 1 + shift
                        for lb, rb in zip(left_rows, right_rows)
                        if lb is not None and rb is not None
                    ]
                    self.assertTrue(gaps)
                    self.assertGreaterEqual(
                        min(gaps), 2, f"{theme} {left}{right} overlaps"
                    )

    def test_text_font_is_thinner_with_identical_metrics_and_real_wool(self):
        """A separate body font loses only alpha coverage, not ink or geometry."""
        from raster_from_sheet import GLYPHS

        for theme, (expected_hash, expected_size) in TEXT_ASSETS.items():
            with self.subTest(theme=theme):
                path = FONT_DIR / f"garnschrift-c-text-{theme}.woff"
                text = path.read_bytes()
                regular = (FONT_DIR / f"garnschrift-c-{theme}.woff").read_bytes()
                self.assertEqual(hashlib.sha256(text).hexdigest(), expected_hash)
                self.assertEqual(len(text), expected_size)
                self.assertEqual(text[:4], b"wOFF")
                body_tables = woff_tables(text)
                heading_tables = woff_tables(regular)
                for table in (b"cmap", b"hmtx", b"hhea", b"kern"):
                    self.assertEqual(body_tables[table], heading_tables[table])
                # Different pixel treatment must not change shared sbix origin.
                body_strike = body_tables[b"sbix"]
                heading_strike = heading_tables[b"sbix"]
                body_start = struct.unpack_from(">I", body_strike, 8)[0]
                heading_start = struct.unpack_from(">I", heading_strike, 8)[0]
                original_coverage = lighter_coverage = 0
                for index, char in enumerate(sorted(GLYPHS), start=3):
                    bitmaps = []
                    for strike, start in ((heading_strike, heading_start), (body_strike, body_start)):
                        begin = struct.unpack_from(">I", strike, start + 4 + index * 4)[0]
                        end = struct.unpack_from(">I", strike, start + 4 + (index + 1) * 4)[0]
                        offset = start + begin
                        self.assertEqual(struct.unpack_from(">hh", strike, offset), (0, -40))
                        bitmaps.append(decode_generator_png(strike[offset + 8 : start + end]))
                    width, height, old = bitmaps[0]
                    self.assertEqual((width, height), bitmaps[1][:2])
                    thin = bitmaps[1][2]
                    # The original yarn fiber color remains pixel-identical.
                    for channel in range(3):
                        self.assertEqual(old[channel::4], thin[channel::4])
                    old_alpha, thin_alpha = old[3::4], thin[3::4]
                    self.assertTrue(all(b <= a for a, b in zip(old_alpha, thin_alpha)))
                    self.assertTrue(any(b >= 100 for b in thin_alpha), char)
                    original_coverage += sum(old_alpha)
                    lighter_coverage += sum(thin_alpha)
                # The additional edge pass makes text visibly finer while
                # preserving dense glyph cores even for punctuation and i.
                self.assertLess(lighter_coverage, original_coverage * 0.82)
                self.assertGreater(lighter_coverage, original_coverage * 0.76)

    def test_css_selects_body_font_without_changing_headings_or_forced_colors(self):
        css = (FONT_DIR.parent / "wool-experiment.css").read_text()
        for theme, title, body in (
            ("dark", "Garnschrift C Woll", "Garnschrift C Woll Text"),
            ("light", "Garnschrift C Petrol", "Garnschrift C Petrol Text"),
        ):
            self.assertIn(f"garnschrift-c-text-{theme}.woff", css)
            self.assertIn(f'font-family: "{body}", system-ui, sans-serif !important;', css)
            self.assertIn(f'font-family: "{title}", system-ui, sans-serif !important;', css)
        self.assertIn("@media (forced-colors: none)", css)
        self.assertIn(":is(h1, h2, h3, h4, h5, h6, strong, b, [role=\"heading\"])", css)
        self.assertIn("ui-monospace, SFMono-Regular, Menlo, monospace", css)


if __name__ == "__main__":
    unittest.main()