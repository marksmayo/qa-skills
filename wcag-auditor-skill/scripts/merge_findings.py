#!/usr/bin/env python3
"""Merge normalised items into de-duplicated, component-level candidates and a draft SC matrix.

    python merge_findings.py items.json --run <run-dir> --out candidates.json

1. Instances: items about the same element (page + XPath) whose SCs overlap merge, across tools,
   viewports and states, keeping every source.
2. Candidates: instances with the same component signature and overlapping SCs merge across pages,
   so a defect in a shared header becomes one candidate listing every affected page.
3. Contrast "needs review" candidates get a `contrast_estimate` from the background the runner
   recorded, and those with the same cause and background collapse into one grouped candidate
   (`group`); any estimated below the threshold stay separate (see group_contrast()).

Each candidate keeps its sources (tool, version, rule, ACT ids), outcome (`failed` if any source
failed, otherwise `cantTell`), kind, a suggested confidence (high when two independent sources
agree or the failing rule implements an ACT rule; medium for one heuristic source or a probe
alone) and the default failure pattern. The agent triages every candidate: finding, needs review,
advisory or dismissed with a checkable reason. `sc_draft` gives every SC at the target level a
draft EARL outcome; it never drafts `passed`.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
LEVEL_ORDER = {'A': 0, 'AA': 1, 'AAA': 2}
VERSION_ORDER = {'2.0': 0, '2.1': 1, '2.2': 2}
SEVERITY_ORDER = ['minor', 'moderate', 'serious', 'critical']


def scs_overlap(a, b):
    return bool(set(a) & set(b)) or (not a and not b)


def default_pattern(scs: dict, sc_list, items):
    for i in items:
        if i.get('pattern'):
            return i['pattern']
    best = None
    for s in sc_list:
        for p in scs[s]['failure_patterns']:
            if best is None or SEVERITY_ORDER.index(p['default_severity']) > SEVERITY_ORDER.index(best[1]):
                best = (p['id'], p['default_severity'])
    return best[0] if best else None


def related_xpath(a: str | None, b: str | None) -> bool:
    """Same element, or one is the parent or grandparent of the other (an img inside its link)."""
    if not a or not b:
        return a == b
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short + '/') and long_[len(short) + 1:].count('/') <= 1


def merge(items: list[dict], scs: dict) -> list[dict]:
    # 1. instances per element (tools report an unnamed image link on the img or on the a)
    instances: list[dict] = []
    by_page: dict[tuple, list[dict]] = {}
    for it in items:
        bucket = by_page.setdefault((it['page'], it['kind']), [])
        for inst in bucket:
            same = related_xpath(inst['xpath'], it['xpath']) if (inst['xpath'] and it['xpath']) else \
                (inst['xpath'] is None and it['xpath'] is None and inst['sig'] == it['sig'])
            if same and scs_overlap(inst['sc'], it['sc']):
                inst['items'].append(it)
                inst['sc'] = sorted(set(inst['sc']) | set(it['sc']), key=sc_key)
                break
        else:
            inst = {'page': it['page'], 'xpath': it['xpath'], 'sig': it['sig'], 'kind': it['kind'], 'sc': list(it['sc']), 'items': [it]}
            bucket.append(inst)
            instances.append(inst)
    # 2. candidates per component signature
    cands: list[dict] = []
    by_sig: dict[tuple, list[dict]] = {}
    for inst in instances:
        key = (inst['sig'], inst['kind'])
        bucket = by_sig.setdefault(key, [])
        for c in bucket:
            if scs_overlap(c['sc'], inst['sc']):
                c['instances'].append(inst)
                c['sc'] = sorted(set(c['sc']) | set(inst['sc']), key=sc_key)
                break
        else:
            c = {'sig': inst['sig'], 'kind': inst['kind'], 'sc': list(inst['sc']), 'instances': [inst]}
            bucket.append(c)
            cands.append(c)
    cands = group_contrast(cands)
    out = []
    for c in cands:
        its = [i for inst in c['instances'] for i in inst['items']]
        failed = [i for i in its if i['outcome'] == 'failed']
        outcome = 'failed' if failed else 'cantTell'
        tools = sorted({i['tool'] if i['tool'] != 'probe' else f"probe:{i['rule_id'].split('.')[0]}" for i in (failed or its)})
        act = sorted({a for i in failed for a in i['act_rules']})
        if outcome == 'failed' and (len(tools) >= 2 or act):
            conf = 'high'
        else:
            conf = 'medium'
        sources = {}
        for i in its:
            k = (i['tool'], i['rule_id'])
            s = sources.setdefault(k, {'tool': i['tool'], 'tool_version': i['tool_version'], 'rule_id': i['rule_id'],
                                       'act_rules': i['act_rules'], 'outcomes': set(), 'count': 0, 'message': i['message']})
            s['outcomes'].add(i['outcome']); s['count'] += 1
        pages = sorted({inst['page'] for inst in c['instances']})
        inst_out = []
        for inst in c['instances']:
            first = inst['items'][0]
            inst_out.append({'page': inst['page'], 'url': first['url'], 'xpath': inst['xpath'],
                             'selector': next((i['selector'] for i in inst['items'] if i['selector']), None),
                             'html': next((i['html'] for i in inst['items'] if i['html']), None),
                             'viewports': sorted({i['viewport'] for i in inst['items']}),
                             'states': sorted({i['state'] for i in inst['items']}),
                             'screenshot': first['screenshot'],
                             'items': [i['id'] for i in inst['items']]})
        pattern = default_pattern(scs, c['sc'], failed or its) if c['kind'] == 'wcag' else None
        out.append({
            'sc': c['sc'], 'kind': c['kind'], 'outcome': outcome, 'component': c['sig'],
            'suggested_confidence': conf, 'agreeing_sources': tools if outcome == 'failed' else [],
            'evidence_type': 'probe' if all(i['tool'] == 'probe' for i in its) else 'tool',
            'pattern': pattern,
            'default_severity': next((p['default_severity'] for s in c['sc'] for p in scs[s]['failure_patterns'] if p['id'] == pattern), None),
            'non_interference': any(scs[s]['non_interference'] for s in c['sc']),
            'pages': pages, 'instance_count': len(inst_out), 'instances': inst_out,
            'sources': [{**v, 'outcomes': sorted(v['outcomes'])} for v in sources.values()],
            'related_sc': sorted({r for i in its for r in i['related_sc']}, key=sc_key),
            **({'contrast_estimate': c['contrast_estimate']} if c.get('contrast_estimate') else {}),
            **({'group': c['group']} if c.get('group') else {}),
        })
    order = {'failed': 0, 'cantTell': 1}
    kind_order = {'wcag': 0, 'above_level': 1, 'outside_version': 2, 'best_practice': 3, 'experimental': 4, 'obsolete': 5}
    out.sort(key=lambda c: (kind_order.get(c['kind'], 9), order[c['outcome']], sc_key(c['sc'][0]) if c['sc'] else [99], c['component']))
    for n, c in enumerate(out, 1):
        c['id'] = f'C-{n:03d}'
    return [{'id': c.pop('id'), **c} for c in out]


def sc_key(s):
    return [int(x) for x in s.split('.')]


# --- contrast "needs review" ------------------------------------------------------------------
# A faint page-level gradient makes axe and HTML_CodeSniffer ask for a manual contrast check on
# nearly every piece of text, burying the real failures. run_audit.py records what lies behind each
# such element (the background image, its colour stops, translucent overlays, the text colour), so
# here each instance gets an estimated worst-case ratio, and candidates with the same cause and the
# same background collapse into one grouped candidate. Instances estimated below the threshold
# are never grouped; they stay separate candidates so they aren't buried.

AXE_CAUSE = {'bgGradient': 'background gradient', 'bgImage': 'background image', 'pseudoContent': 'pseudo-element background',
             'bgOverlap': 'overlapping elements', 'elmPartiallyObscured': 'overlapping elements',
             'elmPartiallyObscuring': 'overlapping elements', 'outsideViewport': 'outside the viewport',
             'nonBmp': 'non-text content', 'shortTextContent': 'non-text content', 'equalRatio': 'same text and background colour'}
HTMLCS_CAUSE = {'Alpha': 'transparent colour', 'BgImage': 'background image', 'Abs': 'absolute positioning'}


def is_contrast(it) -> bool:
    return it['outcome'] == 'cantTell' and it['kind'] == 'wcag' and (
        it['rule_id'].startswith('color-contrast') or it['rule_id'] == 'text_contrast_sufficient' or bool(re.search(r'1_4_[36]\.', it['rule_id'])))


def contrast_cause(it) -> str:
    d = it.get('data') or {}
    ctx = d.get('contrast') or {}
    if ctx.get('visually_hidden'):
        return 'visually hidden text'
    if ctx.get('bg_image_on') and not ctx.get('opaque_first'):
        return 'background gradient' if 'gradient(' in (ctx.get('bg_image') or '') else 'background image'
    key = (d.get('axe_check') or {}).get('messageKey')
    if key in AXE_CAUSE:
        return AXE_CAUSE[key]
    if it['tool'] == 'htmlcs':
        return HTMLCS_CAUSE.get(it['rule_id'].rsplit('.', 1)[-1], it['rule_id'].rsplit('.', 1)[-1])
    msg = re.sub(r'^Fix any of the following:\s*', '', it.get('message') or '').strip()
    return re.sub(r'[\d.#]+', '', msg)[:80] or it['rule_id']


def rgba(s):
    v = [float(x) for x in re.findall(r'[\d.]+', s or '')]
    return (v + [1.0])[:4] if len(v) >= 3 else None


def over(top, base):
    a = top[3]
    return [top[k] * a + base[k] * (1 - a) for k in range(3)] + [1.0]


def lum(c):
    f = lambda x: x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0] / 255) + 0.7152 * f(c[1] / 255) + 0.0722 * f(c[2] / 255)


def estimate(ctx) -> dict | None:
    """Worst-case contrast of the text against what is behind it, from the recorded colours.

    A gradient interpolates channel by channel between its stops, and relative luminance rises
    with every channel, so along each segment luminance moves monotonically: the background's
    luminance range is spanned by its stops, and the lowest ratio is at the stop nearest the
    text's luminance (or 1:1 if the text's luminance falls inside the range). Translucent layers
    are composited over each stop, which keeps the interpolation linear. Images (url()) can't be
    estimated."""
    if not ctx or ctx.get('visually_hidden'):
        return None
    fg, base = rgba(ctx.get('fg')), rgba(ctx.get('bg_color'))
    if not fg or fg[3] < 1 or not base:
        return None
    image = ctx.get('bg_image') or ''
    if ctx.get('opaque_first') or not ctx.get('bg_image_on'):
        stops, basis = [base], 'opaque background'
    elif 'url(' in image or 'gradient(' not in image:
        return None
    else:
        stops = [over(c, base) for c in (rgba(m) for m in re.findall(r'rgba?\([^)]*\)', image)) if c]
        if not stops:
            return None
        basis = 'gradient colour stops'
    layers = [rgba(o) for o in reversed(ctx.get('overlays') or [])]
    shown = []
    for s in stops:
        for o in layers:
            if o:
                s = over(o, s)
        shown.append(lum(s))
    lf, lo, hi = lum(fg), min(shown), max(shown)
    worst = 1.0 if lo <= lf <= hi else (lo + 0.05) / (lf + 0.05) if lf < lo else (lf + 0.05) / (hi + 0.05)
    px, wt = ctx.get('font_px') or 16, ctx.get('font_weight') or 400
    need = 3.0 if px >= 24 or (px >= 18.66 and wt >= 700) else 4.5
    return {'min_ratio': round(worst, 2), 'required': need, 'below': worst < need, 'basis': basis}


def group_contrast(cands: list[dict], min_group: int = 2) -> list[dict]:
    keyed: dict[tuple, list[dict]] = {}
    for c in cands:
        its = [i for inst in c['instances'] for i in inst['items']]
        if c['kind'] != 'wcag' or not its or not all(is_contrast(i) for i in its):
            continue
        ests = [e for e in (estimate((i.get('data') or {}).get('contrast')) for i in its) if e]
        if ests:
            c['contrast_estimate'] = min(ests, key=lambda e: e['min_ratio'] - e['required'])
        if any(e['below'] for e in ests):
            continue  # possibly a real failure: keep it as its own candidate
        if not all((i.get('data') or {}).get('contrast') for i in its):
            continue  # no record of the background (older runs): grouping would be a guess
        causes = tuple(sorted({contrast_cause(i) for i in its}))
        bgs = tuple(sorted({((i['data']['contrast'].get('bg_image_on') or {}).get('sig') or '', i['data']['contrast'].get('bg_image') or '')
                            for i in its if not i['data']['contrast'].get('visually_hidden')}))
        keyed.setdefault((causes, bgs), []).append(c)
    grouped_ids = set()
    out = []
    for (causes, bgs), members in keyed.items():
        if len(members) < min_group:
            continue
        insts = [inst for m in members for inst in m['instances']]
        ests = [m['contrast_estimate'] for m in members if m.get('contrast_estimate')]
        fgs = sorted({i['data']['contrast'].get('fg') for inst in insts for i in inst['items']})
        on = ', '.join(sorted({b[0] for b in bgs if b[0]})) or 'none'
        g = {'sig': f"contrast needs review: {' + '.join(causes)} (background on {on})", 'kind': 'wcag',
             'sc': sorted({s for m in members for s in m['sc']}, key=sc_key), 'instances': insts,
             'contrast_estimate': min(ests, key=lambda e: e['min_ratio'] - e['required']) if ests else None,
             'group': {'cause': list(causes), 'backgrounds': [{'on': b[0] or None, 'image': b[1] or None} for b in bgs],
                       'candidate_count': len(members), 'instance_count': len(insts), 'text_colours': fgs[:10],
                       'estimated': len(ests), 'members': [{'component': m['sig'], 'pages': sorted({i['page'] for i in m['instances']}),
                                                            'instance_count': len(m['instances'])} for m in members],
                       'sample_selectors': [s for s in dict.fromkeys(next((i['selector'] for i in inst['items'] if i['selector']), None) for inst in insts) if s][:8]}}
        grouped_ids |= {id(m) for m in members}
        out.append(g)
    return [c for c in cands if id(c) not in grouped_ids] + out


def draft_matrix(cands, scs, version, level, run, evidence):
    probes_ran = {}
    for pg in run.get('pages', []):
        for vp in (pg.get('viewports') or {}).values():
            for k, v in (vp.get('probes') or {}).items():
                probes_ran.setdefault(k, set()).add(v)
    media_none = 'ran' in probes_ran.get('media', set()) and not any(
        c for c in cands if any(s.startswith('1.2.') for s in c['sc']))
    rows = []
    for s in sorted(scs.values(), key=lambda x: sc_key(x['sc'])):
        if VERSION_ORDER[s['added_in']] > VERSION_ORDER[version] or LEVEL_ORDER[s['level']] > LEVEL_ORDER[level]:
            continue
        ids_f = [c['id'] for c in cands if c['kind'] == 'wcag' and s['sc'] in c['sc'] and c['outcome'] == 'failed']
        ids_c = [c['id'] for c in cands if c['kind'] == 'wcag' and s['sc'] in c['sc'] and c['outcome'] == 'cantTell']
        if s.get('obsolete_in') and VERSION_ORDER[version] >= VERSION_ORDER[s['obsolete_in']]:
            outcome, note = 'inapplicable', f"Obsolete in WCAG {s['obsolete_in']}"
        elif ids_f:
            outcome, note = 'failed', 'Draft: failing candidates found; confirm in triage'
        elif ids_c:
            outcome, note = 'cantTell', 'Draft: only needs-review candidates'
        elif s['sc'].startswith('1.2.') and media_none:
            outcome, note = 'inapplicable', 'Draft: no video, audio or embedded player in the sampled pages; confirm'
        else:
            outcome, note = 'untested', 'Draft: no automated failure; tool passes never establish a pass'
        rows.append({'sc': s['sc'], 'title': s['title'], 'level': s['level'], 'automatable': s['automatable'],
                     'draft_outcome': outcome, 'failed_candidates': ids_f, 'cant_tell_candidates': ids_c,
                     'evidence': evidence.get(s['sc'], {}), 'note': note})
    return rows


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('items')
    ap.add_argument('--run', required=True, help='run directory (for run.json)')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    d = json.loads(Path(args.items).read_text(encoding='utf-8'))
    run = json.loads((Path(args.run) / 'run.json').read_text(encoding='utf-8'))
    scs = {s['sc']: s for s in json.loads((DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}
    cands = merge(d['items'], scs)
    matrix = draft_matrix(cands, scs, d['wcag_version'], d['level'], run, d['sc_evidence'])
    out = {'tool': 'merge_findings.py', 'wcag_version': d['wcag_version'], 'level': d['level'], 'item_count': len(d['items']),
           'candidates': cands, 'sc_draft': matrix}
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding='utf-8')
    summary = {}
    for c in cands:
        summary[f"{c['kind']}/{c['outcome']}"] = summary.get(f"{c['kind']}/{c['outcome']}", 0) + 1
    print(json.dumps({'items': len(d['items']), 'candidates': len(cands), 'by_kind': summary,
                      'sc_draft': {o: sum(1 for r in matrix if r['draft_outcome'] == o) for o in ('failed', 'cantTell', 'inapplicable', 'untested')}}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
