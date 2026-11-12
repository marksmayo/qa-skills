#!/usr/bin/env python3
"""Turn a post-deploy run into findings and a go / no-go recommendation for a person to decide on.

    python recommend.py runs/<stamp>/run.json [--decision-owner "Release manager"] --out pvt_report.json

Findings: a version mismatch (critical); each failed check (critical when the check is critical, else
major); each performance regression (major on a critical check, else minor); a Content-Security-Policy
that differs between the page checks (minor). Recommendation:
  no_go            the version doesn't match, a critical check failed, or a critical check regressed
                   against a baseline from another deployment
  not_established  otherwise, if the version couldn't be read, a critical check errored, was blocked,
                   was skipped or couldn't sign in, or the target rate-limited or blocked the run
  go               otherwise: no failed checks and no regressions in what was checked
A regression against a baseline of the same deployment is drift, not a release change: it stays a
finding but is not a no_go reason, and the notes say so. Timings of failing responses are
`not_comparable` and never a regression.
The decision and who made it are left empty for the person named as the decision owner. The agent may
rewrite titles and add context; validate_pvt.py recomputes the findings, evidence, notes and the
recommendation, and checks the wording.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

STOP_TEXT = {'rate_limited': 'the target rate-limited the run', 'waf': 'a WAF or bot challenge blocked the run',
             'app_5xx': 'the run stopped after three 5xx answers in a row (the application looks down)'}


def failed_detail(c: dict) -> str:
    bad = [a for a in c.get('assertions', []) if not a['ok']]
    parts = []
    for a in bad:
        k = a.get('kind')
        if k == 'http_status':
            parts.append(f'answered HTTP {a.get("actual")} (expected {a["expected"]})')
        elif k == 'body':
            parts.append(f'body {"lacks" if a["op"] == "contains" else "contains"} "{a["expected"]}"')
        elif k == 'header':
            parts.append(f'header {a["path"]} ' + {'present': 'missing', 'absent': 'present (expected absent)',
                                                   'equals': f'is not "{a["expected"]}"', 'contains': f'lacks "{a["expected"]}"'}[a['op']]
                         + ('' if a['op'] in ('present', 'absent') or a['detail'] == 'header present' else ' (header missing)'))
        elif k == 'redirect':
            parts.append({'none': f'redirected ({a.get("actual")} hop(s)) where no redirect was expected',
                          'eq': f'ended at {a.get("actual")} (expected {a["expected"]})',
                          'not_login': f'redirected to the sign-in page {a.get("actual")}',
                          'same_origin': f'redirected off the site to {a.get("actual")}'}[a['op']])
        else:
            parts.append(f'JSON {a["path"]} {a["op"]} {a.get("expected")}: {a.get("detail") or "failed"}' + (f' (values {a["actual"]})' if 'actual' in a else ''))
    return '; '.join(parts)


def csp_groups(run: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for c in run['checks']:
        if c.get('kind') == 'page' and c.get('http_status') and c['http_status'] < 400 and 'headers' in c:
            groups.setdefault(c['headers'].get('content-security-policy') or '(none)', []).append(c['id'])
    return groups


def analyse(run: dict) -> tuple[list[dict], dict]:
    findings = []
    d = run['deployed']
    perf = run['performance']
    same = perf.get('baseline_relation') in ('same_deployment', 'same_version')
    if d['matches'] is False:
        got = ' / '.join(str(x) for x in (d['version'], d.get('deployment_id')) if x is not None)
        findings.append({'kind': 'version_mismatch', 'severity': 'critical', 'check_ids': [], 'title': f'Deployed {got} is not the expected {d["expected"]}',
                         'evidence': f'{d["source"] or d.get("deployment_source")}: {got}'})
    for c in run['checks']:
        if c['status'] == 'fail':
            findings.append({'kind': 'check_failed', 'severity': 'critical' if c['critical'] else 'major', 'check_ids': [c['id']],
                             'title': f'{c["id"]}: {c["method"]} {c["path"]} {failed_detail(c)}', 'evidence': f'run.json checks[{c["id"]}]'})
    crit = {c['id']: c['critical'] for c in run['checks']}
    for p in perf['comparisons']:
        if p['status'] == 'regressed':
            findings.append({'kind': 'regression', 'severity': 'major' if crit.get(p['check_id']) else 'minor', 'check_ids': [p['check_id']],
                             'title': f'{p["check_id"]} median {p["median_ms"]} ms against {p["baseline_median_ms"]} ms in the previous run ({p["change_pct"]:+}%)'
                                      + (' on the same deployment (drift, not a release change)' if same else ''),
                             'evidence': f'{perf["baseline_run"]} -> run.json, {perf["samples"]} samples each'})
    groups = csp_groups(run)
    if len(groups) > 1:
        ids = sorted(i for v in groups.values() for i in v)
        findings.append({'kind': 'header_inconsistent', 'severity': 'minor', 'check_ids': ids,
                         'title': f'Content-Security-Policy differs between pages: {len(groups)} variants (' +
                                  ' | '.join(', '.join(v) for v in groups.values()) + ')',
                         'evidence': 'run.json checks[*].headers.content-security-policy'})
    for i, f in enumerate(findings, 1):
        f['id'] = f'F{i}'
    reasons_no, reasons_ne = [], []
    if d['matches'] is False:
        reasons_no.append(f'deployed {d["version"] if d["version"] is not None else d.get("deployment_id")} is not {d["expected"]}')
    if d['matches'] is None:
        reasons_ne.append('the deployed version could not be read')
    for c in run['checks']:
        if c['critical'] and c['status'] == 'fail':
            reasons_no.append(f'critical check {c["id"]} failed')
        if c['critical'] and c['status'] in ('error', 'blocked', 'skipped'):
            reasons_ne.append(f'critical check {c["id"]} {c["status"]}: {c.get("reason")}')
    st = run.get('stopped')
    if st and st['cause'] in ('rate_limited', 'waf'):
        reasons_ne.append(f'{STOP_TEXT[st["cause"]]} at check {st["after_check"]} (HTTP {st["http_status"]})')
    for p in perf['comparisons']:
        if p['status'] == 'regressed' and crit.get(p['check_id']) and not same:
            reasons_no.append(f'critical check {p["check_id"]} regressed ({p["change_pct"]:+}%)')
    value = 'no_go' if reasons_no else 'not_established' if reasons_ne else 'go'
    notes = []
    if value != 'not_established' and reasons_ne:
        notes += reasons_ne
    if st and st['cause'] == 'app_5xx':
        notes.append(f'{STOP_TEXT["app_5xx"]}: the checks after {st["after_check"]} were not sent')
    if perf.get('baseline_note'):
        notes.append(perf['baseline_note'])
    nb = [p['check_id'] for p in perf['comparisons'] if p['status'] == 'no_baseline']
    if nb:
        notes.append(f'performance not established for {", ".join(nb)}: no baseline run')
    nc = [p['check_id'] for p in perf['comparisons'] if p['status'] == 'not_comparable']
    if nc:
        notes.append(f'timings not compared for {", ".join(nc)}: a failing response (here or in the baseline) is not a performance measure')
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
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    run = json.loads(a.run.read_text(encoding='utf-8'))
    findings, rec = analyse(run)
    counts = {s: sum(1 for c in run['checks'] if c['status'] == s) for s in ('pass', 'fail', 'error', 'blocked', 'skipped')}
    rep = {'schema_version': '1.1.0', 'skill': {'name': 'post-deploy-validator', 'version': '1.1.0'}, 'run': a.run.as_posix(),
           'target': run['target'], 'plan': run['plan'], 'deployed': run['deployed'],
           'window': {'started_at': run['started_at'], 'finished_at': run['finished_at'], 'requests_sent': len(run['requests']), 'blocked': run['blocked'],
                      'stopped': run.get('stopped')},
           'credential_sets': run['credential_sets'],
           'checks': [{k: c.get(k) for k in REPORT_CHECK_FIELDS} for c in run['checks']],
           'performance': run['performance'], 'findings': findings,
           'recommendation': dict(rec, decision_owner=a.decision_owner, decided_by=None, decision=None),
           'summary': {'headline': '', 'checks': counts, 'findings': len(findings)}, 'limitations': []}
    a.out.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{rec["value"]}: ' + '; '.join(rec['reasons']) + f' -> {a.out}')
    return 0


REPORT_CHECK_FIELDS = ('id', 'kind', 'critical', 'method', 'path', 'status', 'http_status', 'final_path', 'reason')

if __name__ == '__main__':
    sys.exit(main())
