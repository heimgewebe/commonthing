"""Build optional color bitmap fonts from the approved Garnschrift C glyph pixels.

Requires fonttools==4.61.1 at generation time only. The browser needs only the
checked-in local WOFF1. Never reconstruct or simulate yarn in JavaScript/CSS.
"""

from __future__ import annotations

from pathlib import Path

import fontTools
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0
from fontTools.ttLib.tables.sbixGlyph import Glyph
from fontTools.ttLib.tables.sbixStrike import Strike

from raster_from_sheet import GLYPHS, SHEET_SHA256, glyph, optical_overlap
from raster_from_sheet import read_png_rgb, slightly_thinner_edges, write_png_rgba

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parents[1] / "static/fonts"
UPM = 110
ASCENT = 105
DESCENT = -40
MAX_ASSET_BYTES = 1_200_000
# All sbix bitmaps have a shared vertical coordinate system. Keeping a
# constant originOffsetY avoids cross-engine interpretation differences for
# independently cropped, baseline-offset glyphs (notably on Apple/WebKit).
BITMAP_HEIGHT = ASCENT - DESCENT
CORE_ALPHA = 100

# Optical bottom edges in the original wool sheet are NOT aligned between
# punctuation rows and the letter rows. A period ends 17 source pixels below
# the letter baseline, an ampersand 13 and a hyphen almost on it. The values
# here are design-space ink-bottom targets relative to the letter baseline:
# full stops sit at the baseline, comma tails project slightly below, and
# dashes live near the x-height center. No rescaling or stroke simulation.
MARK_INK_BOTTOM = {
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


def adjusted_glyph_top(ch: str, asset: tuple[int, int, bytearray, int]) -> int:
    """Return the bitmap's y position in a common em-sized RGBA canvas."""
    width, height, rgba, dy = asset
    shift = 0
    if ch in MARK_INK_BOTTOM:
        core_rows = [
            i // width for i in range(width * height) if rgba[i * 4 + 3] >= CORE_ALPHA
        ]
        if not core_rows:
            raise ValueError(f"no dense wool core in glyph {ch!r}")
        shift = dy + max(core_rows) - MARK_INK_BOTTOM[ch]
    top = ASCENT + dy - shift
    if top < 0 or top + height > BITMAP_HEIGHT:
        raise ValueError(f"out-of-bounds bitmap {ch!r}: {top}, {height}")
    return top


def baseline_aligned_png(
    ch: str, asset: tuple[int, int, bytearray, int], *, text_variant: bool = False
) -> bytes:
    """Embed original wool pixels; contract fiber edges further for body text."""
    width, height, rgba, _ = asset
    top = adjusted_glyph_top(ch, asset)
    output = bytearray(width * BITMAP_HEIGHT * 4)
    pixels = slightly_thinner_edges(width, height, rgba)
    if text_variant:
        # Refine edge coverage for readable body text while preserving the
        # original three-ply fiber cores, RGB colors, and glyph geometry.
        # One additional gentle pass makes dense small labels visibly lighter.
        pixels = slightly_thinner_edges(width, height, pixels)
        pixels = slightly_thinner_edges(width, height, pixels)
        pixels = slightly_thinner_edges(width, height, pixels, strength=3)
    # The original i and k contain disconnected tiny fragments below their
    # non-descending stems. Prune only this measured 0.7%-1.2% alpha artifact;
    # keep actual letter cores, real descenders and the original RGB wool.
    for y in range(height):
        start = y * width * 4
        dest = (top + y) * width * 4
        if ch in ("i", "k") and asset[3] + y >= 10:
            for x in range(width):
                pixels[start + x * 4 + 3] = 0
        output[dest : dest + width * 4] = pixels[start : start + width * 4]
    return write_png_rgba(width, BITMAP_HEIGHT, output)


def generate(theme: str, *, text_variant: bool = False) -> bytes:
    if fontTools.__version__ != "4.61.1":
        raise RuntimeError("reproducibility requires fonttools==4.61.1")
    if theme not in ("dark", "light"):
        raise ValueError("invalid scheme")
    source = read_png_rgb()
    chars = sorted(GLYPHS)
    names = {ch: f"uni{ord(ch):04X}" for ch in chars}
    glyph_order = [".notdef", ".null", "space", *(names[ch] for ch in chars)]
    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({ord(ch): names[ch] for ch in chars} | {32: "space"})
    assets = {ch: glyph(ch, theme, source) for ch in chars}
    empty = {name: TTGlyphPen(None).glyph() for name in glyph_order}
    fb.setupGlyf(empty)
    fb.setupHorizontalMetrics(
        {
            ".notdef": (UPM // 2, 0),
            ".null": (0, 0),
            "space": (27, 0),
            **{names[ch]: (assets[ch][0], 0) for ch in chars},
        }
    )
    fb.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    family = "Garnschrift C Woll" if theme == "dark" else "Garnschrift C Petrol"
    if text_variant:
        family += " Text"
    fb.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "uniqueFontIdentifier": f"{family} 1.0",
            "fullName": f"{family} Regular",
            "psName": family.replace(" ", "") + "-Regular",
            "version": "Version 1.0",
        }
    )
    fb.setupOS2(
        sTypoAscender=ASCENT,
        sTypoDescender=DESCENT,
        sTypoLineGap=0,
        usWinAscent=ASCENT,
        usWinDescent=abs(DESCENT),
        sCapHeight=90,
        sxHeight=69,
        usWeightClass=400,
        fsSelection=0x40,
    )
    fb.setupPost(keepGlyphNames=True)
    # FontBuilder otherwise stamps the current wall clock into the head table.
    # Pin both OpenType timestamps to the SHA-approved source's revision date.
    fb.font["head"].created = 3876373264
    fb.font["head"].modified = 3876373264
    bitmap = newTable("sbix")
    bitmap.version = 1
    bitmap.flags = 1  # No synthetic outlines over the real wool PNGs.
    strike = Strike(ppem=UPM, resolution=72)
    strike.glyphs = {
        names[ch]: Glyph(
            glyphName=names[ch],
            originOffsetX=0,
            # Every glyph is padded to the same em box with a single bitmap
            # bottom origin. CoreGraphics/WebKit and FreeType now cannot
            # disagree about *relative* character placement.
            originOffsetY=DESCENT,
            graphicType="png ",
            imageData=baseline_aligned_png(ch, assets[ch], text_variant=text_variant),
        )
        for ch in chars
    }
    bitmap.strikes = {UPM: strike}
    fb.font["sbix"] = bitmap

    # Kerning must use the same *final* ink positions as the sbix bitmaps.
    # Raw source-sheet baselines would make pairs such as "O-" and "2." collide
    # after the Safari punctuation shift, even if isolated glyph tests pass.
    aligned_assets = {
        ch: (w, h, rgba, adjusted_glyph_top(ch, assets[ch]) - ASCENT)
        for ch, (w, h, rgba, _) in assets.items()
    }
    kerning = {
        (names[a], names[b]): -overlap
        for a in chars
        for b in chars
        if (overlap := optical_overlap(aligned_assets[a], aligned_assets[b])) > 0
    }
    kern = newTable("kern")
    kern.version = 0
    table = KernTable_format_0()
    table.version = 0
    table.coverage = 1  # Horizontal kerning
    table.kernTable = kerning
    kern.kernTables = [table]
    fb.font["kern"] = kern
    fb.font.flavor = "woff"  # zlib/WOFF1; avoids a build-time brotli dependency
    from io import BytesIO

    buf = BytesIO()
    fb.font.save(buf, reorderTables=True)
    payload = buf.getvalue()
    if not payload.startswith(b"wOFF") or len(payload) > MAX_ASSET_BYTES:
        raise ValueError(f"unexpected font output size: {len(payload)}")
    return payload


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        for text_variant in (False, True):
            data = generate(theme, text_variant=text_variant)
            name = f"garnschrift-c-{'text-' if text_variant else ''}{theme}.woff"
            path = OUTPUT / name
            path.write_bytes(data)
            print(f"{path.name}: {len(data)} bytes, source={SHEET_SHA256}")


if __name__ == "__main__":
    main()