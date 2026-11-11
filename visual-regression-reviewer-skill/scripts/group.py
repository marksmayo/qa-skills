#!/usr/bin/env python3
"""Group every pair's explained changes across pages, viewports and browsers; rate them; cut evidence crops.

    python group.py run/diff.json --out run/groups.json --crops run/crops --review run/review.md [--targets targets.json]

A group is one cause on the same element signatures (for example "div.logo moved" on six pages and
viewports). Changes without DOM data (region only) group per page and rounded box. Each group gets:
  * impact, confidence and review_order by RUBRIC.md;
  * instances: pair, pixels, anti-aliasing pixels, flaky pixels, boxes and the measured change;
  * crops of its first instance (baseline, current and diff, the change's boxes plus a 16 px margin),
    with SHA-256 hashes in crops/manifest.json.
Noise (dynamic_content, font_rendering) is grouped and listed like everything else.

The output also lists pairs with no changed pixels (`unchanged`), pairs or targets only in one set
(`added`, `removed`), targets that were never captured (`untested`, with --targets), and per pair the
pixel totals, so the validator can check every pixel is accounted for. review.md has an accept and
a reject box per group; nothing is accepted here.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attribute as at  # noqa: E402
import png_io  # noqa: E402

IMPACT_W = {'high': 3, 'medium': 2, 'low': 1}
CONF_W = {'high': 3, 'medium': 2, 'low': 1}
NOISE = ('dynamic_content', 'font_rendering')
MARGIN = 16


def impact_of(c: dict) -> str:
    if c['cause'] == 'missing_element':
        return 'high'
    if c['cause'] == 'layout_shift':
        m = c['measure']
        return 'high' if max(abs(m.get(k, 0)) for k in ('dx', 'dy', 'dw', 'dh')) > 8 else 'medium'
    if c['cause'] in NOISE:
        return 'low'
    return 'medium'


def confidence_of(c: dict) -> str:
    if c['dom'] == 'both' and c['cause'] != 'unexplained':
        return 'high'
    if c['cause'] == 'unexplained':
        return 'low'
    # region-only: a clear shape (dense pixels) is medium
    bb = c['boxes']['current'] or c['boxes']['baseline']
    area = max(1, bb[2] * bb[3])
    return 'medium' if (c['pixels'] + c['aa_pixels'] + c['flaky_pixels']) / area >= 0.05 else 'low'


def reach_of(pages: int) -> int:
    return 1 if pages <= 1 else 2 if pages <= 4 else 3


def family(sig: str) -> str:
    return re.sub(r':nth-of-type\(\d+\)$', '', sig)


def key_of(pair: dict, c: dict) -> tuple:
    if c['sigs'] and c['cause'] == 'dynamic_content':
        return (c['cause'], tuple(sorted({family(s) for s in c['sigs']})))   # a rotating set is one group
    if c['sigs']:
        return (c['cause'], tuple(c['sigs']))
    bb = c['boxes']['current'] or c['boxes']['baseline']
    return (c['cause'], pair['page'], tuple(round(v / 16) for v in bb))


def crop_box(c: dict, w: int, h: int) -> list[int]:
    rects = list(c['rects'])
    if c.get('pixel_bbox'):
        b = c['pixel_bbox']
        rects.append([b[0], b[1], b[2] - b[0] + 1, b[3] - b[1] + 1])
    x0 = max(0, min(r[0] for r in rects) - MARGIN)
    y0 = max(0, min(r[1] for r in rects) - MARGIN)
    x1 = min(w, max(r[0] + r[2] for r in rects) + MARGIN)
    y1 = min(h, max(r[1] + r[3] for r in rects) + MARGIN)
    return [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('diff', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--crops', type=Path, required=True)
    ap.add_argument('--review', type=Path, required=True)
    ap.add_argument('--targets', type=Path, help='targets.json, to list pages, viewports and browsers that were never captured')
    a = ap.parse_args()
    d = json.loads(a.diff.read_text(encoding='utf-8'))
    groups: dict[tuple, dict] = {}
    totals, unchanged = [], []
    first_images = {}
    for pair in d['pairs']:
        res = at.explain_pair(pair, d['threshold'], d['min_region_pixels'])
        totals.append({'pair': pair['rel'], 'changed_pixels': pair['changed_pixels'], 'aa_pixels': pair['aa_pixels'],
                       'flaky_pixels': pair['flaky_pixels'], 'ungrouped_pixels': res['ungrouped_pixels'],
                       'ungrouped_flaky_pixels': res['ungrouped_flaky_pixels'], 'size_mismatch': pair['size_mismatch'],
                       'size': pair['size']})
        if not res['changes'] and not pair['size_mismatch']:
            unchanged.append({'pair': pair['rel'], 'flaky_pixels': pair['flaky_pixels']})
        for c in res['changes']:
            k = key_of(pair, c)
            g = groups.setdefault(k, {'cause': c['cause'], 'element_sigs': list(k[1]) if c['sigs'] else [], 'instances': [], 'dom': c['dom']})
            g['instances'].append({'pair': pair['rel'], 'page': pair['page'], 'viewport': pair['viewport'], 'browser': pair['browser'],
                                   'pixels': c['pixels'], 'aa_pixels': c['aa_pixels'], 'flaky_pixels': c['flaky_pixels'],
                                   'element_sigs': c['sigs'], 'boxes': c['boxes'], 'measure': c['measure'], 'dynamic_reason': c.get('dynamic_reason'),
                                   'original_cause': c.get('original_cause')})
            if k not in first_images:
                first_images[k] = (res['images'], res['masks'], c)
    a.crops.mkdir(parents=True, exist_ok=True)
    manifest = {}
    out_groups = []
    for k, g in groups.items():
        first = g['instances'][0]
        c0 = first_images[k][2]
        c0 = dict(c0, measure=first['measure'])
        impact = impact_of(c0)
        conf = confidence_of(dict(c0, dom=g['dom']))
        pages = sorted({i['page'] for i in g['instances']})
        reach = reach_of(len(pages))
        out_groups.append({'key': k, 'cause': g['cause'], 'impact': impact, 'confidence': conf, 'reach': reach,
                           'review_score': IMPACT_W[impact] * reach * CONF_W[conf], 'noise': g['cause'] in NOISE,
                           'element_sigs': g['element_sigs'], 'dom': g['dom'], 'pages': pages,
                           'viewports': sorted({i['viewport'] for i in g['instances']}),
                           'browsers': sorted({i['browser'] for i in g['instances'] if i['browser']}),
                           'measure': first['measure'], 'dynamic_reason': first['dynamic_reason'],
                           'pixels': sum(i['pixels'] for i in g['instances']), 'aa_pixels': sum(i['aa_pixels'] for i in g['instances']),
                           'flaky_pixels': sum(i['flaky_pixels'] for i in g['instances']), 'instances': g['instances']})
    out_groups.sort(key=lambda g: (-g['review_score'], g['noise'], g['cause'], g['pages']))
    for i, g in enumerate(out_groups, 1):
        g['id'] = f'G{i}'
        (bimg, cimg), (changed, aa, flaky), c = first_images[g['key']]
        w, h = min(bimg.width, cimg.width), min(bimg.height, cimg.height)
        x, y, cw, ch = crop_box(c, w, h)
        diff = png_io.diff_image(png_io.crop(bimg, 0, 0, w, h), changed, aa, flaky)
        crop = {'pair': g['instances'][0]['pair'], 'box': [x, y, cw, ch]}
        for kind, img in (('baseline', bimg), ('current', cimg), ('diff', diff)):
            f = a.crops / f'{g["id"]}-{kind}.png'
            manifest[f.name] = png_io.write(f, png_io.crop(img, x, y, cw, ch))
            crop[kind] = f.name
        g['crops'] = [crop]
        del g['key']
    (a.crops / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    untested = []
    if a.targets:
        t = json.loads(a.targets.read_text(encoding='utf-8'))
        have = {p['rel'] for p in d['pairs']}
        for b in t.get('browsers') or ['chromium']:
            for vp in t['viewports']:
                for p in t['pages']:
                    for st in [None] + (p.get('states') or []):
                        rel = f'{b}/{vp}/{p["name"]}' + (f'~{st["name"]}' if st else '') + '.png'
                        if rel not in have and rel not in d['added'] and rel not in d['removed']:
                            untested.append({'pair': rel, 'reason': 'not captured in both sets'})
    doc = {'diff': a.diff.name, 'threshold': d['threshold'], 'min_region_pixels': d['min_region_pixels'], 'groups': out_groups,
           'unchanged': unchanged, 'added': d['added'], 'removed': d['removed'], 'untested': untested, 'totals': totals,
           'crops_manifest': 'crops/manifest.json'}
    a.out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    lines = ['# Visual review', '', 'Tick one box per group. Nothing is accepted until you tick it; apply_review.py copies current '
             'images over baselines only for pairs whose every non-noise group is accepted, and only when you run it.', '']
    for g in out_groups:
        where = f'{", ".join(g["pages"])} at {", ".join(g["viewports"])}'
        what = ', '.join(g['element_sigs']) or 'region'
        lines += [f'## {g["id"]} {g["cause"]} ({g["impact"]} impact, {g["confidence"]} confidence)', '',
                  f'{what} on {where}. Measure: {json.dumps(g["measure"])}. Crops: crops/{g["crops"][0]["baseline"]}, '
                  f'crops/{g["crops"][0]["current"]}, crops/{g["crops"][0]["diff"]}', '',
                  f'- [ ] accept {g["id"]}', f'- [ ] reject {g["id"]}', '']
    a.review.write_text('\n'.join(lines), encoding='utf-8')
    print(f'{len(out_groups)} group(s), {len(unchanged)} unchanged pair(s), {len(untested)} untested -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
