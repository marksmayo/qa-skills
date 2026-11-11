#!/usr/bin/env python3
"""Normalise check and probe results to WCAG SCs, merge them per component, and merge crashes by root cause.

    python merge_findings.py --run run --out run/candidates.json

Accessibility candidates (M###): every failed or advisory result is mapped through data/rule-map.json to its
SC and failure pattern, then merged per component: the same check on the same resource id (or class and
label, when there is no id) across screens is one candidate with one instance per screen. Crash
candidates (K##): crashes and ANRs from crashes.json merged by signature (exception and top app frame, or ANR
reason and top app frame), keeping every occurrence with its steps.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = json.loads((HERE.parent / 'data' / 'rule-map.json').read_text(encoding='utf-8'))['checks']


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    crashes = json.loads((a.run / 'crashes.json').read_text(encoding='utf-8'))
    groups: dict = {}
    for r in run['results']:
        rule = RULES.get(r['check'])
        if not rule or r['status'] not in ('failed', 'advisory'):
            continue
        el = r['element']
        comp = el['resource_id'] or f'{el["class"]}:{el["label"]}'
        g = groups.setdefault((r['check'], comp), {'check': r['check'], 'sc': rule['sc'], 'pattern': rule['pattern'], 'kind': rule['kind'],
                                                   'probe': rule['probe'], 'component': comp, 'instances': []})
        g['instances'].append({'screen': r['screen'], 'element': el, 'screenshot': r['screenshot'], 'sha256': r.get('sha256'), 'measured': r['measured']})
    cands = []
    for i, g in enumerate(sorted(groups.values(), key=lambda g: (g['sc'], g['check'], g['component'])), 1):
        g['screens'] = sorted({x['screen'] for x in g['instances']})
        g['sources'] = [{'tool': 'mobile-checks', 'rule_id': g['check']}]
        cands.append({'id': f'M{i:03d}', **g})
    review = {'id': f'M{len(cands) + 1:03d}', 'check': 'heading.not-exposed', 'sc': '1.3.1', 'pattern': None, 'kind': 'review', 'probe': 'a11y_tree',
              'component': 'all screens', 'instances': [], 'screens': [s['id'] for s in run['sample'] if s['reason'] != 'unreachable'],
              'sources': [{'tool': 'mobile-checks', 'rule_id': 'heading.not-exposed'}]}
    cands.append(review)
    by_sig: dict = {}
    for c in crashes:
        sig = (c['kind'], c.get('exception') or c.get('reason'), c.get('top_app_frame'))
        by_sig.setdefault(sig, []).append(c)
    kc = []
    for i, (sig, cs) in enumerate(by_sig.items(), 1):
        kc.append({'id': f'K{i:02d}', 'kind': sig[0], 'signature': {'what': sig[1], 'top_app_frame': sig[2]},
                   'crash_ids': [c['id'] for c in cs], 'screens': sorted({c['screen'] for c in cs}),
                   'events': sorted({json.dumps(c['steps'][-1], sort_keys=True) for c in cs})})
    out = {'run': a.run.as_posix(), 'candidates': cands, 'crash_candidates': kc}
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(cands)} accessibility candidate(s), {len(kc)} crash candidate(s) -> {a.out}')
    for c in cands:
        print(f'  {c["id"]} {c["sc"]:6} {c["check"]:26} {c["kind"]:8} {c["component"]} {c["screens"]}')
    for c in kc:
        print(f'  {c["id"]} {c["kind"]} {c["signature"]["what"]} {c["crash_ids"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
