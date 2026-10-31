#!/usr/bin/env python3
"""Original, tileable 4px linen; run from any directory with standard Python."""

import math
from pathlib import Path
import random
import struct
import zlib

SIZE = 192
PITCH = 4
TAU = 2 * math.pi
TARGET = Path(__file__).resolve().parents[2] / "apps/web/static/textures"


def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def signals():
    grain = random.Random(20261031)
    for y in range(SIZE):
        for x in range(SIZE):
            sx = x + .15 * math.sin(TAU * y / 96 + .7) + .1 * math.sin(TAU * y / 48 + 2.5)
            sy = y + .12 * math.sin(TAU * x / 96 + 2) + .1 * math.sin(TAU * x / 64 + .3)
            ux, uy = (sx / PITCH) % 1 - .5, (sy / PITCH) % 1 - .5
            cx = max(0, 1 - (2 * ux / .96) ** 2) ** .6
            cy = max(0, 1 - (2 * uy / .96) ** 2) ** .6
            over = 1 if (math.floor(sx / PITCH) + math.floor(sy / PITCH)) % 2 else -1
            cross = over * cx * cy * (cx - cy)
            # Fine longitudinal fibres follow each thread, with tileable phases.
            fiber = (.025 * cx * math.sin(TAU * (y / 24 + x / 4))
                     + .025 * cy * math.sin(TAU * (x / 32 + y / 4) + .8))
            yield (.60 * cx + .52 * cy + .1 * cross
                   + .1 * (.5 - ux) * cx + .08 * (.5 - uy) * cy
                   + fiber + grain.uniform(-.018, .018))


def generate():
    field = list(signals())
    for mode, rgb, strength, cap in (
        ("light", (101, 88, 72), .085, .19),
        ("dark", (168, 187, 196), .075, .17),
    ):
        pixels = bytes(round(255 * min(cap, max(0, .014 + strength * s))) for s in field)
        raw = b"".join(b"\0" + pixels[y * SIZE:(y + 1) * SIZE] for y in range(SIZE))
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 3, 0, 0, 0))
               + chunk(b"PLTE", bytes(rgb) * 256)
               + chunk(b"tRNS", bytes(range(256)))
               + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
        assert len(png) < 55 * 1024
        (TARGET / f"porcelain-{mode}.png").write_bytes(png)


if __name__ == "__main__":
    generate()
