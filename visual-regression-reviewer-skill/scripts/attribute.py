#!/usr/bin/env python3
"""Explain one pair's differences: element changes from the two DOM snapshots, and every pixel assigned once.

Used by group.py; also runnable on one pair for inspection:

    python attribute.py run/diff.json --pair chromium/1280x800/home.png

Element changes (signatures from capture.py; text is not part of a signature):
  missing_element / new_element   the signature is visible in only one snapshot (outermost only)
  layout_shift                    the same signature's box moved or resized; children that moved with
                                  their parent are dropped, and outermost elements sharing one delta
                                  are one change
  text_change / image_change / colour_change
                                  same signature, different own text / image source / colour or background
  dynamic_content                 any of the above when at least half its changed pixels lie within 8 px of a
                                  flaky region (it differed between the two current takes), or its text looks
                                  like a time, date or counter
Pixels: each changed and anti-aliasing pixel goes to the change owning the smallest box (baseline or
current) containing it. Pixels no change explains are grouped by region: `font_rendering` when anti-aliasing
pixels are most of the region, otherwise `unexplained`; regions under the minimum size are counted as
ungrouped. Flaky pixels are assigned the same way to dynamic_content changes, or counted per flaky
region. Without DOM files (external screenshots) only the region rules apply.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import diff_images as di  # noqa: E402
import png_io  # noqa: E402

DYNAMIC_TEXT = re.compile(r'\b\d{1,2}:\d{2}(:\d{2})?\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b')
STYLE_KEYS = ('color', 'background')


def load_dom(png: Path) -> dict | None:
    p = png.with_name(png.name[:-4] + '.dom.json')
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def parent(path: str) -> str:
    return path.rsplit('>', 1)[0] if '>' in path else ''


def inside(path: str, outer: str) -> bool:
    return path.startswith(outer + '>')


def overlap(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def element_changes(bdom: dict, cdom: dict, flaky_boxes: list) -> list[dict]:
    bmap, cmap = {}, {}
    for e in bdom['elements']:
        bmap.setdefault(e['sig'], e)
    for e in cdom['elements']:
        cmap.setdefault(e['sig'], e)
    sigs = list(dict.fromkeys([e['sig'] for e in bdom['elements']] + [e['sig'] for e in cdom['elements']]))
    raw = []
    for s in sigs:
        b, c = bmap.get(s), cmap.get(s)
        path = (b or c).get('path', s)
        if b and not c:
            raw.append({'cause': 'missing_element', 'sigs': [s], 'path': path, 'boxes': {'baseline': b['box'], 'current': None},
                        'measure': {'text_before': b['text'] or None}})
        elif c and not b:
            raw.append({'cause': 'new_element', 'sigs': [s], 'path': path, 'boxes': {'baseline': None, 'current': c['box']},
                        'measure': {'text_after': c['text'] or None}})
        else:
            boxes = {'baseline': b['box'], 'current': c['box']}
            if b['text'] != c['text']:
                raw.append({'cause': 'text_change', 'sigs': [s], 'path': path, 'boxes': boxes,
                            'measure': {'text_before': b['text'], 'text_after': c['text']}})
            elif (b.get('image') or None) != (c.get('image') or None):
                raw.append({'cause': 'image_change', 'sigs': [s], 'path': path, 'boxes': boxes,
                            'measure': {'image_before': b.get('image'), 'image_after': c.get('image')}})
            elif any(b.get(k) != c.get(k) for k in STYLE_KEYS):
                k = next(k for k in STYLE_KEYS if b.get(k) != c.get(k))
                raw.append({'cause': 'colour_change', 'sigs': [s], 'path': path, 'boxes': boxes,
                            'measure': {'property': k, 'colour_before': b.get(k), 'colour_after': c.get(k)}})
            d = [c['box'][i] - b['box'][i] for i in range(4)]
            if any(d):
                raw.append({'cause': 'layout_shift', 'sigs': [s], 'path': path, 'boxes': boxes,
                            'measure': {'dx': d[0], 'dy': d[1], 'dw': d[2], 'dh': d[3]}})
    # outermost missing/new only
    gone = {r['path'] for r in raw if r['cause'] in ('missing_element', 'new_element')}
    out = []
    for r in raw:
        s = r['path']
        if any(inside(s, g) for g in gone if g != s):
            continue
        if r['cause'] == 'layout_shift':
            m = r['measure']
            par = next((x for x in raw if x['cause'] == 'layout_shift' and x['path'] == parent(s)), None)
            if par and (par['measure']['dx'], par['measure']['dy']) == (m['dx'], m['dy']) and not (m['dw'] or m['dh']):
                continue
        out.append(r)
    # one change per shared shift delta among siblings-or-unrelated outermost elements
    merged, shifts = [], {}
    for r in out:
        if r['cause'] == 'layout_shift' and not (r['measure']['dw'] or r['measure']['dh']):
            key = (r['measure']['dx'], r['measure']['dy'])
            if key in shifts:
                shifts[key]['sigs'].append(r['sigs'][0])
                shifts[key]['extra_boxes'].append(r['boxes'])
                continue
            r = dict(r, extra_boxes=[])
            shifts[key] = r
        merged.append(r)
    for r in merged:
        r.setdefault('extra_boxes', [])
        rects = [bx for bx in (r['boxes']['baseline'], r['boxes']['current']) if bx] + \
                [bx for eb in r['extra_boxes'] for bx in (eb['baseline'], eb['current']) if bx]
        r['rects'] = rects
    return merged


def classify_dynamic(changes: list[dict]) -> None:
    """A change is dynamic content when most of its own pixels lie near flaky regions (it changed between the two
    current takes), or its text looks like a time, date or counter."""
    for r in changes:
        own = r.get('pixels', 0) + r.get('aa_pixels', 0)
        texts = ' '.join(str(v) for k, v in r['measure'].items() if k.startswith('text') and v)
        reason = None
        if own and r.get('near_flaky', 0) / own >= 0.5:
            reason = 'most of its changed pixels are where the two current takes differ'
        elif texts and DYNAMIC_TEXT.search(texts):
            reason = 'its text looks like a time, date or counter'
        if reason and r['cause'] != 'dynamic_content':
            r['dynamic_reason'], r['original_cause'], r['cause'] = reason, r['cause'], 'dynamic_content'
    # a rotating set (carousel, ticker): a missing or new element whose same-class sibling is dynamic
    family = lambda sig: re.sub(r':nth-of-type\(\d+\)$', '', sig)  # noqa: E731
    dynamic_families = {family(s) for r in changes if r['cause'] == 'dynamic_content' for s in r['sigs'] if ':nth-of-type(' in s}
    for r in changes:
        if r['cause'] in ('missing_element', 'new_element') and any(family(s) in dynamic_families for s in r['sigs']):
            r['dynamic_reason'], r['original_cause'], r['cause'] = \
                'a sibling of the same kind changes between the two current takes (a rotating set)', r['cause'], 'dynamic_content'


def assign(pixels: dict[int, set[int]], changes: list[dict], key: str, flaky_boxes: list | None = None) -> dict[int, set[int]]:
    """Give each pixel to the change owning the smallest rectangle that contains it; return what is left."""
    left: dict[int, set[int]] = {}
    rects = sorted(((r[2] * r[3], i, r) for i, c in enumerate(changes) for r in c['rects']), key=lambda t: (t[0], t[1]))
    near = [[b[0] - 8, b[1] - 8, b[2] + 16, b[3] + 16] for b in flaky_boxes or []]
    for y, xs in pixels.items():
        for x in xs:
            for _, i, r in rects:
                if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                    c = changes[i]
                    c[key] = c.get(key, 0) + 1
                    if any(b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3] for b in near):
                        c['near_flaky'] = c.get('near_flaky', 0) + 1
                    b = c.setdefault('pixel_bbox', [x, y, x, y])
                    b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)
                    break
            else:
                left.setdefault(y, set()).add(x)
    return left


def explain_pair(pair: dict, threshold: float, min_pixels: int) -> dict:
    bp, cp = Path(pair['baseline']), Path(pair['current'])
    bimg, cimg = png_io.read(bp), png_io.read(cp)
    w, h = pair['size']['compared']
    limit = 35215 * threshold ** 2
    flaky: dict[int, set[int]] = {}
    if pair['take2']:
        t2 = png_io.read(Path(pair['take2']))
        flaky = di.differing(cimg, t2, min(w, t2.width), min(h, t2.height), limit)
    ign_rows = {}
    raw = di.differing(bimg, cimg, w, h, limit, {y: flaky.get(y, set()) for y in flaky})
    changed, aa = {}, {}
    for y, xs in raw.items():
        for x in xs:
            is_aa = di.antialiased(bimg, x, y, w, h, cimg) or di.antialiased(cimg, x, y, w, h, bimg)
            (aa if is_aa else changed).setdefault(y, set()).add(x)
    # honour the ignored pixels exactly as diff_images did: pixels it dropped are dropped here too
    total = di.count(changed) + di.count(aa)
    if total != pair['changed_pixels'] + pair['aa_pixels'] and not pair['ignored_pixels']:
        raise RuntimeError(f'{pair["rel"]}: pixel counts differ from diff.json; rerun diff_images.py')
    bdom, cdom = load_dom(bp), load_dom(cp)
    flaky_boxes = [r['bbox'] for r in pair['flaky_regions']]
    changes = element_changes(bdom, cdom, flaky_boxes) if bdom and cdom else []
    left_c = assign(changed, changes, 'pixels', flaky_boxes)
    left_a = assign(aa, changes, 'aa_pixels', flaky_boxes)
    classify_dynamic(changes)
    left_f = assign(flaky, [c for c in changes if c['cause'] == 'dynamic_content'], 'flaky_pixels')
    changes = [c for c in changes if c.get('pixels', 0) + c.get('aa_pixels', 0) + c.get('flaky_pixels', 0) > 0]
    regs, small = di.regions(left_c, left_a, min_pixels)
    for r in regs:
        aa_share = r['aa_pixels'] / max(1, r['pixels'] + r['aa_pixels'])
        changes.append({'cause': 'font_rendering' if aa_share >= 0.6 else 'unexplained', 'sigs': [], 'boxes': {'baseline': r['bbox'], 'current': r['bbox']},
                        'rects': [r['bbox']], 'extra_boxes': [], 'measure': {'aa_share': round(aa_share, 2)}, 'pixels': r['pixels'],
                        'aa_pixels': r['aa_pixels'], 'region_only': True,
                        'pixel_bbox': [r['bbox'][0], r['bbox'][1], r['bbox'][0] + r['bbox'][2] - 1, r['bbox'][1] + r['bbox'][3] - 1]})
    fregs, fsmall = di.regions(left_f, {}, min_pixels)
    for r in fregs:
        changes.append({'cause': 'dynamic_content', 'sigs': [], 'boxes': {'baseline': None, 'current': r['bbox']}, 'rects': [r['bbox']],
                        'extra_boxes': [], 'measure': {}, 'flaky_pixels': r['pixels'], 'region_only': True,
                        'dynamic_reason': 'differs between the two current takes',
                        'pixel_bbox': [r['bbox'][0], r['bbox'][1], r['bbox'][0] + r['bbox'][2] - 1, r['bbox'][1] + r['bbox'][3] - 1]})
    for c in changes:
        for k in ('pixels', 'aa_pixels', 'flaky_pixels'):
            c.setdefault(k, 0)
        c['dom'] = 'both' if (bdom and cdom and not c.get('region_only')) else 'none'
    return {'rel': pair['rel'], 'changes': changes, 'ungrouped_pixels': small, 'ungrouped_flaky_pixels': fsmall,
            'images': (bimg, cimg), 'masks': (changed, aa, flaky)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('diff', type=Path)
    ap.add_argument('--pair', required=True)
    a = ap.parse_args()
    d = json.loads(a.diff.read_text(encoding='utf-8'))
    pair = next(p for p in d['pairs'] if p['rel'] == a.pair)
    res = explain_pair(pair, d['threshold'], d['min_region_pixels'])
    for c in res['changes']:
        print(c['cause'], c['sigs'], c['measure'], c['pixels'], c['aa_pixels'], c['flaky_pixels'], c.get('dynamic_reason', ''))
    print('ungrouped', res['ungrouped_pixels'], res['ungrouped_flaky_pixels'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
