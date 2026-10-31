"""Deterministic, stdlib-only three-ply yarn from approved design 02 centre lines."""
import bisect
import json
import math
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parents[1] / 'static' / 'garnschrift-c'
GLYPHS = json.loads((HERE / 'paths.json').read_text())
PALETTES = {'dark': ('#b59973', '#cfb48e', '#ead2ad'),
            'light': ('#174841', '#256259', '#3e7c70')}


def sample(path):
    """Flatten absolute M/L/C/Q/Z, then resample by arc length (1.6 units)."""
    tokens = re.findall(r'[MLCQZ]|-?\d+(?:\.\d+)?', path)
    points = []
    i = 0
    current = start = (0., 0.)
    while i < len(tokens):
        command = tokens[i]
        i += 1
        count = {'M': 2, 'L': 2, 'C': 6, 'Q': 4, 'Z': 0}[command]
        v = list(map(float, tokens[i:i + count]))
        i += count
        if command == 'M':
            current = start = tuple(v)
            points.append(current)
            continue
        end = start if command == 'Z' else tuple(v[-2:])
        controls = [current] + list(zip(v[::2], v[1::2]))
        steps = 64 if command in ('C', 'Q') else max(1, math.ceil(math.dist(current, end)))
        for step in range(1, steps + 1):
            t = step / steps
            if command in ('C', 'Q'):
                work = controls[:]
                while len(work) > 1:
                    work = [tuple(a * (1-t) + b*t for a, b in zip(p, q))
                            for p, q in zip(work, work[1:])]
                points.append(work[0])
            else:
                points.append(tuple(a*(1-t)+b*t for a, b in zip(current, end)))
        current = end
    distances = [0.]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + math.dist(a, b))
    result = []
    for n in range(max(1, math.ceil(distances[-1] / 1.6)) + 1):
        s = min(n * 1.6, distances[-1])
        j = min(max(1, bisect.bisect_left(distances, s)), len(points)-1)
        fraction = (s-distances[j-1]) / (distances[j]-distances[j-1] or 1)
        p = tuple(a*(1-fraction)+b*fraction for a, b in zip(points[j-1], points[j]))
        dx, dy = (points[j][k]-points[j-1][k] for k in (0, 1))
        length = math.hypot(dx, dy) or 1
        result.append((p[0], p[1], -dy/length, dx/length, s))
    return result


def line(points):
    return 'M' + ' '.join(f'{x:.1f},{y:.1f}' for x, y in points)


def render(word, theme):
    colors = PALETTES[theme]
    paths = []
    offset = 10
    for char in word:
        advance, strokes = GLYPHS[char]
        for path in strokes:
            paths.append([(x+offset, y, nx, ny, s) for x, y, nx, ny, s in sample(path)])
        offset += advance
    width, height = offset + 8, 170 if 'g' in word else 132
    body = []
    for stroke_index, points in enumerate(paths):
        # Split each helix at its front/back crossings. Paint all rear arcs
        # before all front arcs, retaining round caps and three distinct plies.
        arcs = []
        for ply in range(3):
            run = []
            previous_front = None
            for x, y, nx, ny, s in points:
                phase = s * math.tau / 21 + ply * math.tau / 3 + stroke_index * .7
                front = math.cos(phase) >= 0
                displacement = 2.7 * math.sin(phase) + .35 * math.sin(s*.19+stroke_index)
                point = (x+nx*displacement, y+ny*displacement)
                if previous_front is not None and front != previous_front:
                    run.append(point)
                    arcs.append((previous_front, run))
                    run = [point]
                run.append(point)
                previous_front = front
            arcs.append((previous_front, run))
        for front, coords in sorted(arcs, key=lambda arc: arc[0]):
            if len(coords) < 2:
                continue
            d = line(coords)
            body.append(f'<path d="{d}" stroke="{colors[0]}" stroke-width="3.9"/>')
            body.append(f'<path d="{d}" stroke="{colors[2 if front else 1]}" stroke-width="2.3"/>')
        # Sparse, deterministic flyaway fibres attached to the material.
        for j in range(3, len(points), 11):
            x, y, nx, ny, s = points[j]
            side = 1 if j % 2 else -1
            a = (x+nx*3*side, y+ny*3*side)
            b = (x+nx*6*side+ny*2, y+ny*6*side-nx*2)
            body.append(f'<path d="{line([a,b])}" stroke="{colors[1]}" stroke-width=".45" opacity=".55"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            '<g fill="none" stroke-linecap="round" stroke-linejoin="round">' + ''.join(body) + '</g></svg>\n')


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for word in ('Login', 'Einstellungen'):
        for theme in PALETTES:
            content = render(word, theme)
            if len(content.encode()) >= 200_000:
                raise ValueError('SVG exceeds the 200 KB asset budget')
            target = OUTPUT / f'{word.lower()}-{theme}.svg'
            target.write_text(content)
            print(f'{target.name}: {target.stat().st_size:,} bytes')


if __name__ == '__main__':
    main()
