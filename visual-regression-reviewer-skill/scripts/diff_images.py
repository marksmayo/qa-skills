#!/usr/bin/env python3
"""Pair two screenshot sets and diff each pair: changed, anti-aliasing and flaky pixels, and regions (stdlib).

    python diff_images.py --baseline run/baseline --current run/current --out run/diff.json [--diffs run/diffs]
        [--threshold 0.1] [--min-region-pixels 20] [--ignore targets.json] [--playwright test-results/]

Pairing: PNGs with the same relative path in both sets (ignoring .takeN copies). A file in only one
set is `added` or `removed`; different dimensions are a `size_mismatch` (compared over the shared
area, and reported as a change of page size). With --playwright, pairs are the *-expected.png and
*-actual.png files of a test-results folder (Playwright's toHaveScreenshot failures).

Per pixel, the colour distance is pixelmatch's YIQ delta; a pixel changes when it exceeds
35215 x threshold^2. A changed pixel that pixelmatch's anti-aliasing test explains (in either
image) is counted as anti-aliasing, not as a change. When the current set has a .take2.png, pixels
that differ between the two current takes are flaky: excluded from the comparison and reported
with their own regions, which are candidates for dynamic content. Ignore regions (rectangles, or
element signatures resolved from the DOM files) are masked in both images.

Regions are connected components of 8x8-pixel cells holding changed or anti-aliasing pixels (so
gaps up to a cell apart join). A region under --min-region-pixels is counted but not grouped.
Every changed and anti-aliasing pixel is in exactly one region or the small-region count.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import png_io  # noqa: E402

CELL = 8


def yiq(r, g, b):
    return (r * 0.29889531 + g * 0.58662247 + b * 0.11448223,
            r * 0.59597799 - g * 0.27417610 - b * 0.32180189,
            r * 0.21147017 - g * 0.52261711 + b * 0.31114694)


def delta(p1, p2) -> float:
    if p1 == p2:
        return 0.0
    y1, i1, q1 = yiq(*p1)
    y2, i2, q2 = yiq(*p2)
    return 0.5053 * (y1 - y2) ** 2 + 0.299 * (i1 - i2) ** 2 + 0.1957 * (q1 - q2) ** 2


def bright(p) -> float:
    return p[0] * 0.29889531 + p[1] * 0.58662247 + p[2] * 0.11448223


def many_siblings(img: png_io.Image, x1: int, y1: int, w: int, h: int) -> bool:
    x0, x2, y0, y2 = max(x1 - 1, 0), min(x1 + 1, w - 1), max(y1 - 1, 0), min(y1 + 1, h - 1)
    zeroes = 1 if (x1 == x0 or x1 == x2 or y1 == y0 or y1 == y2) else 0
    p = img.px(x1, y1)
    for x in range(x0, x2 + 1):
        for y in range(y0, y2 + 1):
            if (x, y) != (x1, y1) and img.px(x, y) == p:
                zeroes += 1
                if zeroes > 2:
                    return True
    return False


def antialiased(img: png_io.Image, x1: int, y1: int, w: int, h: int, other: png_io.Image) -> bool:
    """pixelmatch's test: the pixel sits on an edge between a darker and a brighter neighbour that both
    have identical siblings in both images."""
    x0, x2, y0, y2 = max(x1 - 1, 0), min(x1 + 1, w - 1), max(y1 - 1, 0), min(y1 + 1, h - 1)
    zeroes = 1 if (x1 == x0 or x1 == x2 or y1 == y0 or y1 == y2) else 0
    mn = mx = 0.0
    mnxy = mxxy = None
    b0 = bright(img.px(x1, y1))
    for x in range(x0, x2 + 1):
        for y in range(y0, y2 + 1):
            if (x, y) == (x1, y1):
                continue
            d = b0 - bright(img.px(x, y))
            if d == 0:
                zeroes += 1
                if zeroes > 2:
                    return False
            elif d < mn:
                mn, mnxy = d, (x, y)
            elif d > mx:
                mx, mxxy = d, (x, y)
    if mn == 0 or mx == 0:
        return False
    return ((many_siblings(img, *mnxy, w, h) and many_siblings(other, *mnxy, w, h)) or
            (many_siblings(img, *mxxy, w, h) and many_siblings(other, *mxxy, w, h)))


def differing(a: png_io.Image, b: png_io.Image, w: int, h: int, limit: float, mask=None) -> dict[int, set[int]]:
    """{y: {x}} of pixels whose YIQ delta exceeds the limit, within the shared w x h."""
    out: dict[int, set[int]] = {}
    for y in range(h):
        ra, rb = a.rows[y][:3 * w], b.rows[y][:3 * w]
        if ra == rb:
            continue
        xs = set()
        for x in range(w):
            i = 3 * x
            if ra[i:i + 3] != rb[i:i + 3] and delta(tuple(ra[i:i + 3]), tuple(rb[i:i + 3])) > limit:
                if mask and x in mask.get(y, ()):
                    continue
                xs.add(x)
        if xs:
            out[y] = xs
    return out


def regions(pixels: dict[int, set[int]], aa: dict[int, set[int]], min_pixels: int) -> tuple[list[dict], int]:
    cells: dict[tuple[int, int], list[int]] = {}
    for src, idx in ((pixels, 0), (aa, 1)):
        for y, xs in src.items():
            for x in xs:
                c = cells.setdefault((x // CELL, y // CELL), [0, 0, x, y, x, y])
                c[idx] += 1
                c[2], c[3], c[4], c[5] = min(c[2], x), min(c[3], y), max(c[4], x), max(c[5], y)
    seen, out, small = set(), [], 0
    for start in sorted(cells):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        changed = sum(cells[c][0] for c in comp)
        aap = sum(cells[c][1] for c in comp)
        x0 = min(cells[c][2] for c in comp)
        y0 = min(cells[c][3] for c in comp)
        x1 = max(cells[c][4] for c in comp)
        y1 = max(cells[c][5] for c in comp)
        if changed + aap < min_pixels:
            small += changed + aap
            continue
        out.append({'bbox': [x0, y0, x1 - x0 + 1, y1 - y0 + 1], 'pixels': changed, 'aa_pixels': aap})
    out.sort(key=lambda r: (r['bbox'][1], r['bbox'][0]))
    return out, small


def mask_from(ignore: list, dom_files: list[Path], w: int, h: int) -> dict[int, set[int]]:
    rects = []
    for item in ignore:
        if 'rect' in item:
            rects.append(item['rect'])
        elif 'selector' in item:
            for f in dom_files:
                if f.is_file():
                    for e in json.loads(f.read_text(encoding='utf-8'))['elements']:
                        if e['sig'] == item['selector'] or e['sig'].endswith('>' + item['selector']):
                            rects.append(e['box'])
    out: dict[int, set[int]] = {}
    for x, y, rw, rh in rects:
        for yy in range(max(0, y), min(h, y + rh)):
            out.setdefault(yy, set()).update(range(max(0, x), min(w, x + rw)))
    return out


def count(d: dict[int, set[int]]) -> int:
    return sum(len(v) for v in d.values())


def pairs_from_sets(base: Path, cur: Path) -> tuple[list[tuple[str, Path, Path, Path | None]], list[str], list[str]]:
    def pngs(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob('*.png') if not re.search(r'\.take\d+\.png$', p.name)}
    b, c = pngs(base), pngs(cur)
    pairs = []
    for rel in sorted(set(b) & set(c)):
        t2 = c[rel].with_name(c[rel].name[:-4] + '.take2.png')
        pairs.append((rel, b[rel], c[rel], t2 if t2.exists() else None))
    return pairs, sorted(set(c) - set(b)), sorted(set(b) - set(c))


def pairs_from_playwright(folder: Path) -> list[tuple[str, Path, Path, None]]:
    out = []
    for exp in sorted(folder.rglob('*-expected.png')):
        act = exp.with_name(exp.name.replace('-expected.png', '-actual.png'))
        if act.exists():
            out.append((exp.relative_to(folder).as_posix().replace('-expected.png', '.png'), exp, act, None))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--current', type=Path)
    ap.add_argument('--playwright', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--diffs', type=Path)
    ap.add_argument('--threshold', type=float, default=0.1)
    ap.add_argument('--min-region-pixels', type=int, default=20)
    ap.add_argument('--ignore', type=Path, help='targets.json with "ignore_regions": {page: [{"selector"} | {"rect": [x, y, w, h]}]}')
    a = ap.parse_args()
    limit = 35215 * a.threshold ** 2
    if a.playwright:
        pairs, added, removed = pairs_from_playwright(a.playwright), [], []
        roots = {'playwright': a.playwright.as_posix()}
    else:
        pairs, added, removed = pairs_from_sets(a.baseline, a.current)
        roots = {'baseline': a.baseline.as_posix(), 'current': a.current.as_posix()}
    ignore_cfg = json.loads(a.ignore.read_text(encoding='utf-8')).get('ignore_regions', {}) if a.ignore else {}
    out_pairs = []
    for rel, bp, cp, t2p in pairs:
        bimg, cimg = png_io.read(bp), png_io.read(cp)
        w, h = min(bimg.width, cimg.width), min(bimg.height, cimg.height)
        page = Path(rel).stem
        dom = [bp.with_name(bp.name[:-4] + '.dom.json'), cp.with_name(cp.name[:-4] + '.dom.json')]
        ignore = mask_from(ignore_cfg.get(page.split('~')[0], []), dom, w, h)
        flaky: dict[int, set[int]] = {}
        if t2p:
            t2 = png_io.read(t2p)
            flaky = differing(cimg, t2, min(w, t2.width), min(h, t2.height), limit)
        mask = {y: set(ignore.get(y, set())) | flaky.get(y, set()) for y in set(ignore) | set(flaky)}
        raw = differing(bimg, cimg, w, h, limit, mask)
        changed: dict[int, set[int]] = {}
        aa: dict[int, set[int]] = {}
        for y, xs in raw.items():
            for x in xs:
                is_aa = antialiased(bimg, x, y, w, h, cimg) or antialiased(cimg, x, y, w, h, bimg)
                (aa if is_aa else changed).setdefault(y, set()).add(x)
        regs, small = regions(changed, aa, a.min_region_pixels)
        fregs, fsmall = regions(flaky, {}, a.min_region_pixels)
        for i, r in enumerate(regs, 1):
            r['id'] = f'R{i}'
        for i, r in enumerate(fregs, 1):
            r['id'] = f'F{i}'
        diff_file = None
        if a.diffs and (changed or aa or flaky):
            diff_file = a.diffs / (rel[:-4] + '.diff.png')
            png_io.write(diff_file, png_io.diff_image(png_io.crop(bimg, 0, 0, w, h), changed, aa, flaky))
        out_pairs.append({
            'rel': rel, 'page': page, 'viewport': Path(rel).parent.name, 'browser': Path(rel).parent.parent.name or None,
            'baseline': bp.as_posix(), 'current': cp.as_posix(), 'take2': t2p.as_posix() if t2p else None,
            'baseline_sha256': png_io.sha256_file(bp), 'current_sha256': png_io.sha256_file(cp),
            'size': {'baseline': [bimg.width, bimg.height], 'current': [cimg.width, cimg.height], 'compared': [w, h]},
            'size_mismatch': (bimg.width, bimg.height) != (cimg.width, cimg.height),
            'changed_pixels': count(changed), 'aa_pixels': count(aa), 'flaky_pixels': count(flaky),
            'ignored_pixels': count(ignore), 'small_region_pixels': small, 'regions': regs,
            'flaky_regions': fregs, 'flaky_small_pixels': fsmall, 'diff_image': diff_file.as_posix() if diff_file else None})
        print(f'{rel}: {count(changed)} changed, {count(aa)} anti-aliasing, {count(flaky)} flaky, {len(regs)} region(s)', file=sys.stderr)
    doc = {'sets': roots, 'threshold': a.threshold, 'min_region_pixels': a.min_region_pixels, 'pairs': out_pairs,
           'added': added, 'removed': removed}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
