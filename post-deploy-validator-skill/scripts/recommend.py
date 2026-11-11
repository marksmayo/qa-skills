#!/usr/bin/env python3
"""Turn a post-deploy run into findings and a go / no-go recommendation for a person to decide on.

    python recommend.py runs/<stamp>/run.json [--decision-owner "Release manager"] --out pvt_report.json

Findings: a version mismatch (critical); each failed check (critical when the check is critical, else
major); each performance regression (major on a critical check, else minor). Recommendation:
  no_go            the version doesn't match, a critical check failed, or a critical check regressed
  not_established  otherwise, if the version couldn't be read or a critical check errored, was blocked,
                   was skipped or couldn't sign in
  go               otherwise: no failed checks and no regressions in what was checked
The decision and who made it are left empty for the person named as the decision owner. The agent may
rewrite titles and add context; validate_pvt.py recomputes the findings and the recommendation.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def failed_detail(c: dict) -> str:
    bad = [a for a in c.get('assertions', []) if not a['ok']]
    parts = []
    for a in bad:
        if a['path'] == 'status':
            parts.append(f'answered {a.get("actual")} (expected {a["expected"]})')
        elif a['op'] in ('contains', 'not_contains'):
            parts.append(f'body {"lacks" if a["op"] == "contains" else "contains"} "{a["expected"]}"')
        else:
            parts.append(f'{a["path"]} {a["op"]} {a.get("expected")}: {a.get("detail") or "failed"}' + (f' (values {a["actual"]})' if 'actual' in a else ''))
    return '; '.join(parts)


def analyse(run: dict) -> tuple[list[dict], dict]:
    findings = []
    d = run['deployed']
    if d['matches'] is False:
        findings.append({'kind': 'version_mismatch', 'severity': 'critical', 'check_ids': [], 'title': f'Deployed version {d["version"]} is not the expected {d["expected"]}',
                         'evidence': f'{d["source"]}: {d["version"]}'})
    for c in run['checks']:
        if c['status'] == 'fail':
            findings.append({'kind': 'check_failed', 'severity': 'critical' if c['critical'] else 'major', 'check_ids': [c['id']],
                             'title': f'{c["id"]}: {c["method"]} {c["path"]} {failed_detail(c)}', 'evidence': f'run.json checks[{c["id"]}]'})
    crit = {c['id']: c['critical'] for c in run['checks']}
    for p in run['performance']['comparisons']:
        if p['status'] == 'regressed':
            findings.append({'kind': 'regression', 'severity': 'major' if crit.get(p['check_id']) else 'minor', 'check_ids': [p['check_id']],
                             'title': f'{p["check_id"]} median {p["median_ms"]} ms against {p["baseline_median_ms"]} ms in the previous run ({p["change_pct"]:+}%)',
                             'evidence': f'{run["performance"]["baseline_run"]} -> run.json, {run["performance"]["samples"]} samples each'})
    for i, f in enumerate(findings, 1):
        f['id'] = f'F{i}'
    reasons_no, reasons_ne = [], []
    if d['matches'] is False:
        reasons_no.append(f'deployed version {d["version"]} is not {d["expected"]}')
    if d['version'] is None:
        reasons_ne.append('the deployed version could not be read')
    for c in run['checks']:
        if c['critical'] and c['status'] == 'fail':
            reasons_no.append(f'critical check {c["id"]} failed')
        if c['critical'] and c['status'] in ('error', 'blocked', 'skipped'):
            reasons_ne.append(f'critical check {c["id"]} {c["status"]}: {c.get("reason")}')
    for p in run['performance']['comparisons']:
        if p['status'] == 'regressed' and crit.get(p['check_id']):
            reasons_no.append(f'critical check {p["check_id"]} regressed ({p["change_pct"]:+}%)')
    value = 'no_go' if reasons_no else 'not_established' if reasons_ne else 'go'
    notes = []
    if value != 'not_established' and reasons_ne:
        notes += reasons_ne
    nb = [p['check_id'] for p in run['performance']['comparisons'] if p['status'] == 'no_baseline']
    if nb:
        notes.append(f'performance not established for {", ".join(nb)}: no baseline run' + (f' ({run["performance"]["baseline_note"]})' if run['performance'].get('baseline_note') else ''))
    for b in run['blocked']:
        notes.append(f'{b["method"]} {b["path"]} ({b["check_id"]}) was not sent: {b["reason"]}')
    rec = {'value': value, 'reasons': reasons_no if value == 'no_go' else reasons_ne if value == 'not_established' else
           ['no failed checks and no regressions in what was checked'], 'notes': notes}
    return findings, rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', type=Path)
    ap.add_argument('--decision-owner', default='the release manager')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run = json.loads(a.run.read_text(encoding='utf-8'))
    findings, rec = analyse(run)
    counts = {s: sum(1 for c in run['checks'] if c['status'] == s) for s in ('pass', 'fail', 'error', 'blocked', 'skipped')}
    rep = {'schema_version': '1.0.0', 'skill': {'name': 'post-deploy-validator', 'version': '1.0.0'}, 'run': a.run.as_posix(),
           'target': run['target'], 'plan': run['plan'], 'deployed': run['deployed'],
           'window': {'started_at': run['started_at'], 'finished_at': run['finished_at'], 'requests_sent': len(run['requests']), 'blocked': run['blocked']},
           'credential_sets': run['credential_sets'],
           'checks': [{k: c.get(k) for k in ('id', 'kind', 'critical', 'method', 'path', 'status', 'http_status', 'reason')} for c in run['checks']],
           'performance': run['performance'], 'findings': findings,
           'recommendation': dict(rec, decision_owner=a.decision_owner, decided_by=None, decision=None),
           'summary': {'headline': '', 'checks': counts, 'findings': len(findings)}, 'limitations': []}
    a.out.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{rec["value"]}: ' + '; '.join(rec['reasons']) + f' -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
