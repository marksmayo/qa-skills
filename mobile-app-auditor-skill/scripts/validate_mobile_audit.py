#!/usr/bin/env python3
"""Validate a mobile audit against its contract and its run, and recompute every rating and count.

    python validate_mobile_audit.py audit.json --run run [--write-summary]

The WCAG auditor's rules, adapted to screens: EARL outcomes per target SC (failed only with findings, passed
never on tool evidence alone, cantTell with a needs-review item, untested in the manual test plan,
inapplicable with a basis; EN 301 549 clause 11, with 2.4.1, 2.4.5, 3.2.3 and 3.2.4 not applicable to
non-web software); severity from the failure pattern, one level up on a critical path, Critical for
non-interference SCs; priority = severity weight x reach (screens) x leverage with the same bands, floors and
caps; agent judgment never high confidence; the verdict only does_not_conform or not_established, and no
text claims conformance.
Mobile rules: every instance names the screen, the element (id or class, bounds, label) and a screenshot in
the run that matches its SHA-256; checks equal run.json; an iOS tool never "ran" off macOS; every changed
device setting was restored; every candidate (accessibility and crash) is triaged once; a crash has steps,
a screenshot, and a log excerpt from one process only; a reproduced crash has a replay record that
reproduced it; a crash from random input has a seed; crash scores, bands and the not-reproduced cap (P1)
are recomputed.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / 'data'
SEVERITY = ['minor', 'moderate', 'serious', 'critical']
SEVERITY_W = {'critical': 4, 'serious': 3, 'moderate': 2, 'minor': 1}
LEVEL_ORDER = {'A': 0, 'AA': 1, 'AAA': 2}
VERSION_ORDER = {'2.0': 0, '2.1': 1, '2.2': 2}
BANNED = re.compile(r'(?i)(?<!not )(?<!n\'t )\b(conforms|compliant|conformant|fully accessible|is accessible|meets wcag|passes wcag|crash[- ]free|stable app)\b')
RULES = json.loads((DATA / 'rule-map.json').read_text(encoding='utf-8'))
NA_SOFTWARE = set(RULES['not_applicable_software']['scs'])
EN = json.loads((DATA / 'en301549-map.json').read_text(encoding='utf-8'))['map']
BLAST = {'B1': 3, 'B2': 2}
LIKELIHOOD = {'normal_use': 4, 'unusual_sequence': 3, 'state_change': 2, 'random_only': 1}
RECOVER = {'crash_loop': 3, 'restart': 2, 'retry': 1}


def scs() -> dict:
    return {s['sc']: s for s in json.loads((DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}


def target(sc_map: dict, version: str, level: str) -> list[str]:
    out = [s for s, m in sc_map.items() if VERSION_ORDER[m['added_in']] <= VERSION_ORDER[version] and LEVEL_ORDER[m['level']] <= LEVEL_ORDER[level]
           and not m.get('obsolete_in')]
    return sorted(out, key=lambda s: [int(x) for x in s.split('.')])


def expected_severity(f: dict) -> str:
    sev = f['base_severity']
    if f.get('critical_path'):
        sev = SEVERITY[min(SEVERITY.index(sev) + 1, 3)]
    if f['non_interference']:
        sev = 'critical'
    return sev


def priority(f: dict, n_screens: int) -> dict:
    n = len(set(f['screens']))
    reach = 3 if n >= 2 and n >= n_screens / 2 else 2 if n >= 2 else 1
    lev = 2 if len(f['instances']) >= 2 else 1
    score = SEVERITY_W[f['severity']] * reach * lev
    b = 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'
    if f['severity'] == 'critical' and b in ('P2', 'P3'):
        b = 'P1'
    if f['severity'] == 'serious' and b == 'P3':
        b = 'P2'
    if f.get('blocks_critical_path'):
        b = 'P0'
    if f['evidence_type'] == 'agent_judgment' and b == 'P0':
        b = 'P1'
    return {'weight': SEVERITY_W[f['severity']], 'reach': reach, 'leverage': lev, 'score': score, 'band': b}


def crash_priority(c: dict) -> dict:
    score = BLAST[c['tier']] * c['reach'] * LIKELIHOOD[c['likelihood']] * RECOVER[c['recoverability']]
    b = 'P0' if score >= 48 else 'P1' if score >= 24 else 'P2' if score >= 8 else 'P3'
    if c['confidence'] != 'reproduced' and b == 'P0':
        b = 'P1'
    return {'score': score, 'band': b}


def summary(d: dict, tools: dict) -> dict:
    counts = {k: sum(1 for f in d['findings'] if f['severity'] == k) for k in ('critical', 'serious', 'moderate', 'minor')}
    cov = {'total': len(d['sc_results']), **{k: sum(1 for r in d['sc_results'] if r['outcome'] == k)
                                            for k in ('failed', 'passed', 'cantTell', 'inapplicable', 'untested')}}
    checks = d['metadata']['checks']
    comp = round(sum(1 for c in checks if c['status'] == 'ran') / len(checks), 2) if checks else 0.0
    engines_ran = all(t['status'] == 'ran' for t in d['metadata']['tools'] if t['name'] in ('mobile-checks', 'accessibility-test-framework'))
    conf = 'high' if comp >= 0.9 and engines_ran and d['assisted_review']['scs_reviewed'] else 'medium' if comp >= 0.6 else 'low'
    return {'severity_counts': counts, 'sc_coverage': cov, 'evidence_completeness': comp, 'overall_confidence': conf,
            'blocked_critical_paths': sorted({f['critical_path'] for f in d['findings'] if f.get('blocks_critical_path') and f.get('critical_path')}),
            'crash_counts': {p: sum(1 for c in d['crashes'] if c['priority']['band'] == p) for p in ('P0', 'P1', 'P2', 'P3')},
            'conformance_verdict': 'does_not_conform' if cov['failed'] else 'not_established'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('audit', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.audit.read_text(encoding='utf-8'))
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    graph = json.loads((a.run / 'screen_graph.json').read_text(encoding='utf-8'))
    crashes = {c['id']: c for c in json.loads((a.run / 'crashes.json').read_text(encoding='utf-8'))}
    replays = {r['crash']: r for r in json.loads((a.run / 'replays.json').read_text(encoding='utf-8'))['replays']} if (a.run / 'replays.json').exists() else {}
    cands_file = json.loads((a.run / 'candidates.json').read_text(encoding='utf-8'))
    cands = {c['id']: c for c in cands_file['candidates'] + cands_file['crash_candidates']}
    tools_file = json.loads((a.run / 'tools.json').read_text(encoding='utf-8'))
    E: list[str] = []
    e = E.append
    for k in ('schema_version', 'skill', 'scope', 'metadata', 'headline', 'sc_results', 'findings', 'advisories', 'needs_review', 'triage',
              'crashes', 'screen_graph', 'manual_test_plan', 'assisted_review', 'limitations'):
        if k not in d:
            e(f'missing {k}')
    if E:
        print('\n'.join(E)); return 1
    M = scs()
    sc_scope = d['scope']
    tset = target(M, sc_scope['wcag_version'], sc_scope['level'])
    sampled = [s['id'] for s in run['sample'] if s['reason'] != 'unreachable']

    # metadata -------------------------------------------------------------------
    host = tools_file['host']['os']
    for t in d['metadata']['tools']:
        if t['name'] == 'xcode-accessibility-audit' and t['status'] == 'ran' and host != 'Darwin':
            e(f'{t["name"]} ran on {host}: iOS needs macOS')
        if t['status'] != 'ran' and not t.get('reason'):
            e(f'tool {t["name"]} is {t["status"]} without a reason')
    if d['metadata']['tools'] != tools_file['tools']:
        e('metadata.tools differ from tools.json')
    if d['metadata']['checks'] != run['checks']:
        e('metadata.checks differ from run.json')
    if sc_scope['platforms'].get('ios') == 'ran' and host != 'Darwin':
        e('scope says iOS ran on a non-macOS host')
    for src in (run['settings_changed'], graph['settings_changed']) + ((json.loads((a.run / 'replays.json').read_text())['settings_changed'],) if replays else ()):
        for c in src:
            if not c.get('restored'):
                e(f'device setting {c.get("ns", "")} {c.get("key", c["kind"])} was changed and not restored')
    if d['metadata'].get('settings_changed') is None:
        e('metadata.settings_changed missing')
    if sorted(s['id'] for s in d['scope']['sample']) != sorted(s['id'] for s in run['sample']):
        e('scope.sample differs from run.json')

    # sc results -----------------------------------------------------------------
    seen = [r['sc'] for r in d['sc_results']]
    if sorted(seen, key=lambda s: [int(x) for x in s.split('.')]) != tset:
        e(f'sc_results must hold each target SC once ({len(tset)} for WCAG {sc_scope["wcag_version"]} {sc_scope["level"]}); got {len(seen)}')
    fids = {f['id']: f for f in d['findings']}
    nrids = {n['id']: n for n in d['needs_review']}
    manual = {m['sc'] for m in d['manual_test_plan']}
    for r in d['sc_results']:
        w = f'sc {r["sc"]}'
        m = M.get(r['sc'])
        if not m:
            e(f'{w}: unknown SC'); continue
        want_clause = None if r['sc'] in NA_SOFTWARE else ('11.' + EN[r['sc']][2:] if r['sc'] in EN else None)
        if r.get('en301549_clause') != want_clause:
            e(f'{w}: en301549_clause {r.get("en301549_clause")!r} should be {want_clause!r} (clause 11)')
        own = [f for f in d['findings'] if f['sc'] == r['sc']]
        if r['outcome'] == 'failed':
            if not own or sorted(r['finding_ids']) != sorted(f['id'] for f in own):
                e(f'{w}: failed must list exactly its findings')
        elif own:
            e(f'{w}: has findings {[f["id"] for f in own]} but outcome {r["outcome"]}')
        if r['outcome'] == 'passed' and (r['basis'] == 'tool' or r['confidence'] == 'high' and r['basis'] == 'agent_judgment'):
            e(f'{w}: a pass needs probe or human evidence, never tool alone, and agent judgment is never high confidence')
        if r['outcome'] == 'cantTell' and not (r.get('needs_review_ids') and all(i in nrids for i in r['needs_review_ids'])):
            e(f'{w}: cantTell needs needs_review items')
        if r['outcome'] == 'untested' and r['sc'] not in manual:
            e(f'{w}: untested must be in the manual test plan')
        if r['outcome'] == 'inapplicable' and not r.get('evidence_note'):
            e(f'{w}: inapplicable needs an evidence note')
        if r['sc'] in NA_SOFTWARE and r['outcome'] != 'inapplicable':
            e(f'{w}: EN 301 549 clause 11 treats this as not applicable to non-web software')

    # findings -------------------------------------------------------------------
    pats = {p['id']: p for s in M.values() for p in s['failure_patterns']}
    for f in d['findings']:
        w = f['id']
        if f['pattern'] not in pats or not f['pattern'].startswith(f['sc'] + '-'):
            e(f'{w}: pattern {f["pattern"]} does not belong to {f["sc"]}'); continue
        if f['base_severity'] != pats[f['pattern']]['default_severity'] and not f.get('severity_override_reason'):
            e(f'{w}: base_severity should be {pats[f["pattern"]]["default_severity"]}')
        if f['non_interference'] != M[f['sc']]['non_interference']:
            e(f'{w}: non_interference should be {M[f["sc"]]["non_interference"]}')
        if f['severity'] != expected_severity(f):
            e(f'{w}: severity should be {expected_severity(f)}')
        if f['blocks_critical_path'] and (not f.get('critical_path') or f['severity'] != 'critical'):
            e(f'{w}: blocks_critical_path needs a critical path and Critical severity')
        if f['priority'] != priority(f, len(sampled)):
            e(f'{w}: priority {f["priority"]} should be {priority(f, len(sampled))}')
        if f['evidence_type'] == 'agent_judgment' and f['confidence'] == 'high':
            e(f'{w}: agent judgment is never high confidence')
        if sorted(set(f['screens'])) != sorted({i['screen'] for i in f['instances']}):
            e(f'{w}: screens differ from its instances')
        for cid in f['candidate_ids']:
            c = cands.get(cid)
            if not c:
                e(f'{w}: candidate {cid} not in candidates.json'); continue
            if c.get('sc') != f['sc'] or c.get('pattern') != f['pattern']:
                e(f'{w}: candidate {cid} is {c.get("sc")}/{c.get("pattern")}')
        cand_inst = [i for cid in f['candidate_ids'] for i in cands.get(cid, {}).get('instances', [])]
        for i in f['instances']:
            el = i.get('element') or {}
            if not i.get('screen') or not el.get('bounds') or not (el.get('resource_id') or el.get('class')) or not i.get('screenshot') or not i.get('device'):
                e(f'{w}: an instance needs screen, element (id or class, bounds), screenshot and device'); continue
            if i['screen'] not in sampled:
                e(f'{w}: instance screen {i["screen"]} is not in the sample')
            p = a.run / i['screenshot']
            if not p.exists():
                e(f'{w}: screenshot {i["screenshot"]} is not in the run')
            elif i.get('sha256') and hashlib.sha256(p.read_bytes()).hexdigest() != i['sha256']:
                e(f'{w}: screenshot {i["screenshot"]} does not match its SHA-256')
            if not any(ci['screen'] == i['screen'] and ci['element']['bounds'] == el['bounds'] for ci in cand_inst):
                e(f'{w}: instance on {i["screen"]} {el.get("resource_id")} is not in its candidates')

    # crashes --------------------------------------------------------------------
    for c in d['crashes']:
        w = c['id']
        for cid in c['crash_ids']:
            raw = crashes.get(cid)
            if not raw:
                e(f'{w}: crash {cid} not in crashes.json'); continue
            if not raw.get('steps') or not raw.get('log_excerpt') or not (a.run / raw['screenshot']).exists():
                e(f'{w}: crash {cid} needs steps, a log excerpt and a screenshot')
            pids = {l.split()[0] for l in raw.get('log_excerpt', []) if raw['kind'] == 'crash'}
            if len(pids) > 1:
                e(f'{w}: crash {cid} log excerpt mixes processes {sorted(pids)}')
            if raw.get('seed') is None and c['likelihood'] == 'random_only':
                e(f'{w}: random-input crash without a seed')
        if c.get('log_excerpt') and any(l not in sum((crashes.get(x, {}).get('log_excerpt', []) for x in c['crash_ids']), []) for l in c['log_excerpt']):
            e(f'{w}: log excerpt lines are not from the recorded crash logs')
        if c['confidence'] == 'reproduced' and not any(replays.get(x, {}).get('status') == 'reproduced' for x in c['crash_ids']):
            e(f'{w}: reproduced without a replay record that reproduced it')
        kind = {crashes[x]['kind'] for x in c['crash_ids'] if x in crashes}
        if kind == {'anr'}:
            killed = any(crashes[x].get('after_wait') == 'killed' for x in c['crash_ids'])
            if c['tier'] != ('B1' if killed else 'B2'):
                e(f'{w}: an ANR is B2 when the app recovers after Wait and B1 when it is killed')
        if any(crashes.get(x, {}).get('relaunch') == 'crash_loop' for x in c['crash_ids']) and c['recoverability'] != 'crash_loop':
            e(f'{w}: relaunch crashed again, so recoverability is crash_loop')
        if c['priority'] != crash_priority(c):
            e(f'{w}: priority {c["priority"]} should be {crash_priority(c)}')
        if c.get('candidate_id') not in cands:
            e(f'{w}: candidate {c.get("candidate_id")} not in candidates.json')

    # triage ---------------------------------------------------------------------
    tri: dict = {}
    for t in d['triage']:
        if t['candidate_id'] in tri:
            e(f'candidate {t["candidate_id"]} triaged twice')
        tri[t['candidate_id']] = t
        if t['decision'] == 'dismissed' and len(t.get('reason', '')) < 20:
            e(f'{t["candidate_id"]}: a dismissal needs a specific reason')
        refs = {'finding': fids, 'needs_review': nrids, 'advisory': {x['id']: x for x in d['advisories']},
                'crash': {x['id']: x for x in d['crashes']}}.get(t['decision'])
        if refs is not None and t.get('ref') not in refs:
            e(f'{t["candidate_id"]}: {t["decision"]} {t.get("ref")} does not exist')
    missing = sorted(set(cands) - set(tri))
    if missing:
        e(f'candidates not triaged: {missing}')

    # headline -------------------------------------------------------------------
    want = summary(d, tools_file)
    if a.write_summary:
        d['headline'].update(want)
        a.audit.write_text(json.dumps(d, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    for k, v in want.items():
        if d['headline'].get(k) != v:
            e(f'headline.{k} {d["headline"].get(k)!r} should be {v!r}')
    m = BANNED.search(json.dumps(d, ensure_ascii=False))
    if m:
        e(f'text says {m.group(0)!r}: never claim conformance or crash-freedom')
    if d['scope']['platforms'].get('ios') != 'ran' and not any('iOS' in x for x in d['limitations']):
        e('iOS was not run, but the limitations do not say so')

    if E:
        print(f'{a.audit}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    print(f'{a.audit}: valid. {len(d["findings"])} finding(s), {len(d["crashes"])} crash finding(s), {len(tri)} candidate(s) triaged; '
          f'verdict {want["conformance_verdict"]}, evidence completeness {want["evidence_completeness"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
