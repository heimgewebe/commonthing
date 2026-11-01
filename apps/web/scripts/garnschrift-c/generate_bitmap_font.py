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
from raster_from_sheet import read_png_rgb, write_png_rgba

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parents[1] / "static/fonts"
UPM = 110
ASCENT = 105
DESCENT = -40
MAX_ASSET_BYTES = 1_200_000


def generate(theme: str) -> bytes:
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
    fb.setupHorizontalMetrics({
        ".notdef": (UPM // 2, 0),
        ".null": (0, 0),
        "space": (27, 0),
        **{names[ch]: (assets[ch][0], 0) for ch in chars},
    })
    fb.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    family = "Garnschrift C Woll" if theme == "dark" else "Garnschrift C Petrol"
    fb.setupNameTable({
        "familyName": family,
        "styleName": "Regular",
        "uniqueFontIdentifier": f"{family} 1.0",
        "fullName": f"{family} Regular",
        "psName": family.replace(" ", "") + "-Regular",
        "version": "Version 1.0",
    })
    fb.setupOS2(
        sTypoAscender=ASCENT, sTypoDescender=DESCENT, sTypoLineGap=0,
        usWinAscent=ASCENT, usWinDescent=abs(DESCENT),
        sCapHeight=90, sxHeight=69, usWeightClass=400,
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
            glyphName=names[ch], originOffsetX=0,
            originOffsetY=assets[ch][3] + assets[ch][1],
            graphicType="png ",
            imageData=write_png_rgba(*assets[ch][:2], assets[ch][2]),
        )
        for ch in chars
    }
    bitmap.strikes = {UPM: strike}
    fb.font["sbix"] = bitmap

    # Pair-specific kerning follows the same fiber-core geometry as word PNGs.
    kerning = {
        (names[a], names[b]): -overlap
        for a in chars for b in chars
        if (overlap := optical_overlap(assets[a], assets[b])) > 0
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
        data = generate(theme)
        path = OUTPUT / f"garnschrift-c-{theme}.woff"
        path.write_bytes(data)
        print(f"{path.name}: {len(data)} bytes, source={SHEET_SHA256}")


if __name__ == "__main__":
    main()
