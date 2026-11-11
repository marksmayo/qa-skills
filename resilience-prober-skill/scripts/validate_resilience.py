#!/usr/bin/env python3
"""Validate a resilience report against the schema, the run, the plan, the candidates and the rules.

    python validate_resilience.py report.json --run run/run.json --scenarios run/scenarios.json --candidates run/candidates.json [--write-summary]

Fails when:
  * a finding has no scenario, its scenario didn't run, or a run has no screenshot or request log;
  * a scenario has no expected outcome, or it was planned after the scenario started (planned_at in
    scenarios.json against started_at in run.json);
  * a flow whose baseline failed or was unstable has scenarios marked ran;
  * a finding's ratings differ from its candidate, a P0 isn't reproduced, or console_only appears in findings;
  * a retry_storm has no request counts, or a lost_input has no before and after lengths and hashes (or
    contains raw text);
  * coverage counts don't match the scenario list, or the headline counts differ;
  * the summary calls the app resilient or says it handles all failures;
  * a candidate is missing from triage or triaged twice, or a repro script is missing.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check  # noqa: E402

CLAIMS = re.compile(r'\b(is resilient|fully resilient|handles all (failures|errors)|robust against|fault[- ]tolerant|never breaks)\b', re.I)
FIELDS = ('effect', 'on_write', 'impact', 'likelihood', 'reach', 'score', 'priority', 'confidence', 'floors', 'flow', 'step', 'endpoints',
          'faults', 'scenarios')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--scenarios', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep, run, plan, cands_doc = load(a.report), load(a.run), load(a.scenarios), load(a.candidates)
    errs: list[str] = []
    errs += [f'schema: {e}' for e in schema_check.errors(rep, load(HERE.parent / 'schemas' / 'resilience_report.schema.json'))]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1
    base = a.run.parent
    planned = {s['id']: s for s in plan['scenarios']}
    ran = {s['id']: s for s in run['scenarios']}
    # scenarios
    if [s['id'] for s in rep['scenarios']] != [s['id'] for s in plan['scenarios']]:
        errs.append('scenarios do not match the plan')
    for s in rep['scenarios']:
        p, r = planned.get(s['id']), ran.get(s['id'])
        if not p or not p.get('expected'):
            errs.append(f'{s["id"]}: no expected outcome in the plan')
            continue
        if s['expected'] != p['expected']:
            errs.append(f'{s["id"]}: the expected outcome differs from the one planned before the run')
        if r:
            if s['status'] != r['status']:
                errs.append(f'{s["id"]}: status {s["status"]}, run.json says {r["status"]}')
            for rr in r['runs']:
                if rr['started_at'] < plan['planned_at']:
                    errs.append(f'{s["id"]}: started {rr["started_at"]} before the plan was written ({plan["planned_at"]})')
            flow_status = run['baselines'][p['flow']]['status']
            if flow_status != 'passed' and s['status'] == 'ran':
                errs.append(f'{s["id"]}: the {p["flow"]} baseline {flow_status}, so its scenarios are untested')
        for shot in s['screenshots']:
            if not (base / 'screenshots' / shot).is_file():
                errs.append(f'{s["id"]}: screenshot {shot} is missing')
    # triage and findings
    cands = {c['id']: c for c in cands_doc['candidates']}
    seen = {}
    for t in rep['triage']:
        if t['candidate_id'] in seen:
            errs.append(f'triage: {t["candidate_id"]} is triaged twice')
        seen[t['candidate_id']] = t
    for cid in cands:
        if cid not in seen:
            errs.append(f'triage: candidate {cid} is missing')
    fmap = {f['id']: f for f in rep['findings']}
    for t in rep['triage']:
        if t['decision'] == 'promoted':
            f = fmap.get(t['finding_id'] or '')
            if not f or f['candidate_id'] != t['candidate_id']:
                errs.append(f'triage: {t["candidate_id"]} is promoted to {t["finding_id"]}, which is not its finding')
        elif t['finding_id'] is not None:
            errs.append(f'triage: {t["candidate_id"]} is {t["decision"]} but names {t["finding_id"]}')
    for f in rep['findings']:
        c = cands.get(f['candidate_id'])
        if not c:
            errs.append(f'{f["id"]}: candidate {f["candidate_id"]} does not exist')
            continue
        for k in FIELDS:
            if f[k] != c[k]:
                errs.append(f'{f["id"]}: {k} differs from candidate {c["id"]}')
        if f['priority'] == 'P0' and f['confidence'] != 'reproduced':
            errs.append(f'{f["id"]}: a P0 must be reproduced')
        for sid in f['scenarios']:
            r = ran.get(sid)
            if not r or r['status'] != 'ran':
                errs.append(f'{f["id"]}: scenario {sid} did not run')
                continue
            for rr in r['runs']:
                if not rr['steps'] or not rr['steps'][-1].get('screenshots'):
                    errs.append(f'{f["id"]}: a run of {sid} has no screenshots')
                if 'requests' not in rr:
                    errs.append(f'{f["id"]}: a run of {sid} has no request log')
        for e in f['evidence']:
            m = e['measure']
            if f['effect'] == 'retry_storm' and not (isinstance(m.get('requests'), int) and m.get('gaps_ms') is not None):
                errs.append(f'{f["id"]}: a retry storm needs request counts and timings')
            if f['effect'] == 'lost_input':
                for side in ('before', 'after'):
                    v = m.get(side) or {}
                    if set(v) - {'length', 'sha256', 'note'} or 'length' not in v or 'sha256' not in v:
                        errs.append(f'{f["id"]}: lost_input must record {side} as length and SHA-256 only')
            for shot in e['screenshots']:
                if not (base / 'screenshots' / shot).is_file():
                    errs.append(f'{f["id"]}: evidence screenshot {shot} is missing')
        if not (base / f['repro']['script']).is_file():
            errs.append(f'{f["id"]}: repro script {f["repro"]["script"]} is missing')
    if any(f['effect'] == 'console_only' for f in rep['findings']):
        errs.append('console_only is a note, never a finding')
    # coverage
    cov: dict[str, dict] = {}
    for s in rep['scenarios']:
        g = planned[s['id']]['group']
        c = cov.setdefault(g, {'ran': 0, 'untested': 0, 'errored': 0})
        c[s['status']] += 1
    if rep['coverage'] != cov:
        errs.append(f'coverage should be {cov}')
    # headline
    hl = rep['headline']
    exp = {'priority_counts': dict(Counter(f['priority'] for f in rep['findings'])),
           'flows_with_p0': sorted({f['flow'] for f in rep['findings'] if f['priority'] == 'P0'}),
           'scenarios_run': sum(s['status'] == 'ran' for s in rep['scenarios']), 'scenarios_planned': len(rep['scenarios'])}
    untested = exp['scenarios_run'] < exp['scenarios_planned'] or any(b['status'] != 'passed' for b in run['baselines'].values())
    exp['overall_confidence'] = 'medium' if untested or any(f['confidence'] != 'reproduced' for f in rep['findings']) or rep['needs_review'] else 'high'
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if CLAIMS.search(hl['summary']):
        errs.append('headline.summary calls the app resilient; say which faults and flows were tried instead')
    if a.write_summary and not errs:
        a.report.write_text(json.dumps(rep, indent=1) + '\n', encoding='utf-8')
        print(f'headline written to {a.report}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["findings"])} finding(s) from {exp["scenarios_run"]}/{exp["scenarios_planned"]} scenario(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
