#!/usr/bin/env python3
"""Merge normalised items into de-duplicated, component-level candidates and a draft SC matrix.

    python merge_findings.py items.json --run <run-dir> --out candidates.json

1. Instances: items about the same element (page + XPath) whose SCs overlap merge, across tools,
   viewports and states, keeping every source.
2. Candidates: instances with the same component signature and overlapping SCs merge across pages,
   so a defect in a shared header becomes one candidate listing every affected page.

Each candidate keeps its sources (tool, version, rule, ACT ids), outcome (`failed` if any source
failed, otherwise `cantTell`), kind, a suggested confidence (high when two independent sources
agree or the failing rule implements an ACT rule; medium for one heuristic source or a probe
alone) and the default failure pattern. The agent triages every candidate: finding, needs review,
advisory or dismissed with a checkable reason. `sc_draft` gives every SC at the target level a
draft EARL outcome; it never drafts `passed`.
"""
from __future__ import annotations
import argparse, json, sys
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
        })
    order = {'failed': 0, 'cantTell': 1}
    kind_order = {'wcag': 0, 'above_level': 1, 'outside_version': 2, 'best_practice': 3, 'experimental': 4, 'obsolete': 5}
    out.sort(key=lambda c: (kind_order.get(c['kind'], 9), order[c['outcome']], sc_key(c['sc'][0]) if c['sc'] else [99], c['component']))
    for n, c in enumerate(out, 1):
        c['id'] = f'C-{n:03d}'
    return [{'id': c.pop('id'), **c} for c in out]


def sc_key(s):
    return [int(x) for x in s.split('.')]


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
