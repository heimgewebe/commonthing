"""Render actual yarn glyphs from the approved PNG contact sheet.

Uses only Python's standard library. Produces transparent local PNG word images,
not an installable WOFF/OpenType font; never runs in the browser.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import zlib

HERE = Path(__file__).resolve().parent
SHEET = HERE.parents[3] / "docs/assets/garnschrift-c-contact-sheet.png"
OUTPUT = HERE.parents[1] / "static/garnschrift-c"
SHEET_SHA256 = "eb09901816a4fc04a001cd2a7c2e51f12ca6b04e68a892727087cd1bcd76e2c2"
WORDS = ("Login", "Einstellungen")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Exact source-sheet pixel bounds (inclusive x, exclusive y); shared baseline
# per row ensures ascenders, descenders and umlauts align in new words.
ROWS = (
    ("ABCDEFGHIJKLMNOPQR", 100, 205, 197, (
        (67, 134), (157, 207), (230, 284), (309, 362), (387, 432),
        (456, 500), (520, 577), (601, 654), (681, 696), (718, 761),
        (786, 838), (862, 906), (926, 1001), (1022, 1085), (1104, 1165),
        (1187, 1236), (1253, 1315), (1335, 1387),
    )),
    ("STUVWXYZÄÖÜ", 206, 298, 289, (
        (66, 113), (136, 193), (215, 268), (289, 352), (373, 463),
        (485, 547), (571, 632), (656, 707), (731, 799), (818, 878),
        (901, 955),
    )),
    ("abcdefghijklmnopqrst", 312, 421, 402, (
        (65, 123), (148, 196), (219, 263), (285, 336), (360, 404),
        (429, 473), (493, 540), (567, 618), (646, 660), (686, 723),
        (755, 798), (824, 839), (864, 930), (953, 999), (1020, 1071),
        (1094, 1141), (1161, 1209), (1234, 1270), (1289, 1326),
        (1345, 1385),
    )),
    ("uvwxyzäöüß", 417, 506, 484, (
        (68, 114), (141, 193), (218, 291), (316, 363), (390, 435),
        (461, 531), (536, 590), (619, 669), (700, 744), (779, 829),
    )),
    ("0123456789", 518, 615, 608, (
        (65, 122), (147, 177), (213, 261), (287, 334), (358, 413),
        (437, 483), (509, 559), (582, 632), (657, 709), (734, 784),
    )),
)
GLYPHS = {
    ch: (max(0, x1 - 5), y1, x2 + 6, y2, baseline)
    for chars, y1, y2, baseline, boxes in ROWS
    for ch, (x1, x2) in zip(chars, boxes, strict=True)
}
# A small punctuation subset from the sheet; other symbols stay in ordinary
# semantic HTML text rather than silently substituting a different yarn shape.
for char, (x1, x2) in {
    ".": (67, 78), ",": (121, 134), ";": (177, 190),
    ":": (232, 244), "!": (288, 300), "?": (338, 373),
    "-": (1016, 1047), "–": (1078, 1121), "—": (1150, 1208),
    "/": (1292, 1325),
}.items():
    GLYPHS[char] = (x1 - 5, 620, x2 + 6, 714, 690)
for char, (x1, x2) in {
    "+": (66, 107), "=": (147, 184), "*": (223, 252),
    "&": (288, 334), "@": (369, 424), "#": (456, 500),
    "%": (535, 594), "€": (631, 679), "$": (722, 759),
    "§": (804, 836),
}.items():
    GLYPHS[char] = (x1 - 5, 711, x2 + 6, 793, 779)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + kind + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def read_png_rgb(path: Path = SHEET) -> tuple[int, int, bytes]:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != SHEET_SHA256:
        raise ValueError("source sheet SHA-256 differs from the approved image")
    if not payload.startswith(PNG_MAGIC):
        raise ValueError("not a PNG")
    pos = len(PNG_MAGIC)
    compressed = bytearray()
    header = None
    while pos < len(payload):
        size = struct.unpack_from(">I", payload, pos)[0]
        kind = payload[pos + 4:pos + 8]
        data = payload[pos + 8:pos + 8 + size]
        checksum = struct.unpack_from(">I", payload, pos + 8 + size)[0]
        if zlib.crc32(kind + data) != checksum:
            raise ValueError("bad PNG chunk checksum")
        pos += size + 12
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    if header != (1448, 1086, 8, 2, 0, 0, 0):
        raise ValueError("source must be 1448x1086 8-bit noninterlaced RGB")
    width, height = header[:2]
    data = zlib.decompress(compressed)
    stride = width * 3
    if len(data) != (stride + 1) * height:
        raise ValueError("unexpected PNG scanline count")
    output = bytearray(stride * height)
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        mode = data[start]
        row = bytearray(data[start + 1:start + stride + 1])
        if mode not in range(5):
            raise ValueError("unsupported PNG row filter")
        for i in range(stride):
            left = row[i - 3] if i >= 3 else 0
            above = previous[i]
            upper_left = previous[i - 3] if i >= 3 else 0
            if mode == 1:
                prediction = left
            elif mode == 2:
                prediction = above
            elif mode == 3:
                prediction = (left + above) // 2
            elif mode == 4:
                trial = left + above - upper_left
                a, b, c = (abs(trial - v) for v in
                           (left, above, upper_left))
                prediction = (left if a <= b and a <= c
                              else above if b <= c else upper_left)
            else:
                prediction = 0
            row[i] = (row[i] + prediction) & 255
        output[y * stride:(y + 1) * stride] = row
        previous = row
    return width, height, bytes(output)


def write_png_rgba(width: int, height: int, rgba: bytes) -> bytes:
    if len(rgba) != width * height * 4:
        raise ValueError("wrong RGBA pixel count")
    stride = width * 4
    rows = b"".join(
        b"\0" + rgba[y * stride:(y + 1) * stride]
        for y in range(height)
    )
    return (
        PNG_MAGIC + _chunk(b"IHDR", struct.pack(">IIBBBBB",
        width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(rows, 9))
        + _chunk(b"IEND", b"")
    )


def _byte(value: float) -> int:
    return max(0, min(255, round(value)))


def glyph(
    ch: str, theme: str, image: tuple[int, int, bytes]
) -> tuple[int, int, bytearray, int]:
    if ch not in GLYPHS:
        raise ValueError(f"no raster glyph for {ch!r}")
    source_width, _, rgb = image
    x1, y1, x2, y2, baseline = GLYPHS[ch]
    width, height = x2 - x1, y2 - y1
    data = bytearray(width * height * 4)
    xmin, ymin, xmax, ymax = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            origin = ((y1 + y) * source_width + x1 + x) * 3
            r, g, b = rgb[origin:origin + 3]
            # Source teal has red-blue difference near -40; yarn is warm.
            alpha = _byte(max(0, min(1, (r - b + 40) * 1.2 / 68)) * 255)
            if alpha < 13:
                alpha = 0
            dest = (y * width + x) * 4
            if not alpha:
                continue
            fraction = alpha / 255
            if theme == "dark":
                divisor = max(.15, fraction)
                red = _byte((r - (1 - fraction) * 14) / divisor)
                green = _byte((g - (1 - fraction) * 47) / divisor)
                blue = _byte((b - (1 - fraction) * 58) / divisor)
            else:
                luminance = (30 * r + 60 * g + 10 * b) / 100
                factor = max(-1, min(1, (luminance - 160) / 80))
                red, green, blue = (
                    _byte(40 + 28 * factor),
                    _byte(100 + 40 * factor),
                    _byte(88 + 35 * factor),
                )
            data[dest:dest + 4] = bytes((red, green, blue, alpha))
            xmin, ymin = min(xmin, x), min(ymin, y)
            xmax, ymax = max(xmax, x), max(ymax, y)
    if xmax < 0:
        raise ValueError(f"empty raster glyph {ch!r}")
    left, top = max(0, xmin - 1), max(0, ymin - 1)
    right, bottom = min(width, xmax + 2), min(height, ymax + 2)
    cropped_width, cropped_height = right - left, bottom - top
    cropped = bytearray(cropped_width * cropped_height * 4)
    for y in range(cropped_height):
        start = ((y + top) * width + left) * 4
        cropped[y * cropped_width * 4:(y + 1) * cropped_width * 4] = (
            data[start:start + cropped_width * 4]
        )
    return cropped_width, cropped_height, cropped, y1 + top - baseline


def render(word: str, theme: str, image: tuple[int, int, bytes]) -> bytes:
    if theme not in ("dark", "light"):
        raise ValueError("invalid color scheme")
    parts = [
        (27, 0, bytearray(), 0) if char == " " else glyph(char, theme, image)
        for char in word
    ]
    width = 24 + sum(part[0] + 9 for part in parts)
    height = 145
    output = bytearray(width * height * 4)
    x = 12
    for w, h, source, dy in parts:
        top = 105 + dy
        if h and (top < 0 or top + h > height):
            raise ValueError("glyph outside target canvas")
        for y in range(h):
            start = ((top + y) * width + x) * 4
            output[start:start + 4 * w] = source[y * 4 * w:(y + 1) * 4 * w]
        x += w + 9
    return write_png_rgba(width, height, output)


def main() -> None:
    image = read_png_rgb()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for word in WORDS:
        for theme in ("dark", "light"):
            data = render(word, theme, image)
            if len(data) > 200_000:
                raise ValueError(f"rendered PNG exceeds size budget: {word}")
            file = OUTPUT / f"{word.lower()}-{theme}.png"
            file.write_bytes(data)
            print(f"{file.name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
