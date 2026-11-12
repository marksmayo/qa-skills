#!/usr/bin/env python3
"""Turn a run into operation coverage and finding candidates, with the rubric's ratings computed.

    python merge_findings.py contract.json cases.json run/run.json --out run/candidates.json [--diff diff.json]
        [--schemathesis run/schemathesis.json]
    python merge_findings.py contract.json [cases.json] --out candidates.json [--diff diff.json]     # static only

Operations: every operation in the contract, with its plan status (exercised, excluded,
skipped_write, skipped_auth, untested), cases sent, and per check passed / failed / cantTell /
not_applicable / untested.

Candidates: one per (check, cause). The cause is operation-independent for status_documented
(the status), content_type (status and media type), auth_enforced, required_headers (the header),
schema_conformance on a named component schema (the schema and the first error) and graphql_errors
on a field type, so a cause shared by several operations is one candidate with a wider reach.
Other checks are per operation.

Environment outage: when every answer (first sends and re-sends) over two or more operations was a
5xx, the server_error failures are one candidate (`environment_outage: true`, cause "environment
outage") with confidence low, instead of one defect per operation: the run shows the environment
failing, not the handlers.

Static mode: `merge_findings.py contract.json [cases.json] --out candidates.json` without a run lists
every operation as untested (or its plan status) with no candidates, and run_status `not_run`.

Each candidate carries the computed severity, confidence, reach, score and priority (RUBRIC.md); the
validator recomputes them from the run. Schemathesis failures, when given, are added as candidates
with source `schemathesis` and must be triaged like the rest.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

CHECKS = ['status_documented', 'server_error', 'content_type', 'schema_conformance', 'required_headers',
          'negative_accepted', 'positive_rejected', 'auth_enforced', 'graphql_errors']
WEIGHT = {'Critical': 4, 'High': 3, 'Medium': 2, 'Low': 1}
CONF = {'high': 3, 'medium': 2, 'low': 1}
VALID_KINDS = {'valid', 'boundary', 'random', 'graphql', 'added'}


def reach_of(n: int) -> int:
    return 1 if n <= 1 else 2 if n <= 4 else 3


def priority_of(severity: str, reach: int, confidence: str) -> tuple[int, str]:
    score = WEIGHT[severity] * reach * CONF[confidence]
    p = 'P0' if score >= 24 else 'P1' if score >= 12 else 'P2' if score >= 6 else 'P3'
    if severity == 'Critical' and p in ('P2', 'P3'):
        p = 'P1'
    return score, p


def severity_of(check: str, instances: list[dict], exchanges: dict) -> str:
    if check == 'auth_enforced':
        return 'Critical'
    if check == 'server_error':
        kinds = {i['kind'] for i in instances if i['valid']}
        if 'example' in kinds:
            return 'Critical'
        return 'High' if kinds & VALID_KINDS else 'Medium'
    if check == 'schema_conformance':
        return 'High' if any(200 <= i['status'] < 300 for i in instances) else 'Medium'
    if check in ('positive_rejected', 'graphql_errors'):
        return 'High'
    if check in ('status_documented', 'negative_accepted'):
        return 'Medium'
    if check == 'content_type':
        parseable = all('json' in ((exchanges.get(i['exchange']) or {}).get('response', {}).get('body') or {}) for i in instances)
        return 'Low' if parseable else 'Medium'
    if check == 'required_headers':
        return 'Low'
    return 'Medium'


def confidence_of(instances: list[dict]) -> str:
    if not any(i['repeated'] for i in instances):
        return 'low'
    if any(i['write'] for i in instances if i['repeated']):
        return 'medium'
    return 'high'


def cause_of(check: str, r: dict, op: dict | None) -> tuple[str, bool]:
    """(cause, shared across operations?)"""
    d = r['checks'][check]
    if check == 'status_documented':
        return f'status {r["status"]}', True
    if check == 'content_type':
        return f'status {r["status"]}: {d.get("detail", "").split(";")[0]}', True
    if check == 'auth_enforced':
        return 'answers 2xx without credentials', True
    if check == 'required_headers':
        return d.get('detail', ''), True
    if check == 'schema_conformance':
        first = (d.get('errors') or [d.get('detail', '')])[0]
        if d.get('schema'):
            return f'{d["schema"]}: {first.split(": ", 1)[-1]}', True
        return re.sub(r'\[\d+\]', '[]', first), False
    if check == 'graphql_errors':
        first = re.sub(r'^data\.\w+(\[\d+\])?', '', (d.get('errors') or [d.get('detail', '')])[0])
        return first, first.startswith('.')
    if check == 'server_error':
        return f'status {r["status"]}', False
    if check == 'negative_accepted':
        return re.sub(r': answered \d+$', '', d.get('detail', '')), False
    return f'status {r["status"]}', False


def load_exchanges(run_dir: Path, results: list[dict]) -> dict:
    out = {}
    for r in results:
        for p in (r.get('exchange'), (r.get('resend') or {}).get('exchange')):
            if p and (run_dir / p).is_file():
                out[p] = json.loads((run_dir / p).read_text(encoding='utf-8'))
    return out


def operations(contract: dict, cases: dict | None, run: dict | None) -> list[dict]:
    """Every contract operation with its status and check results. Without a run (static mode) every
    operation that was planned or never planned is `untested` and every check `untested`."""
    plan = {p['operation_id']: p for p in (cases or {}).get('plan') or []}
    marked = set(((cases or {}).get('auth_required') or {}).get('operation_ids') or [])
    by_op: dict[str, list[dict]] = {}
    for r in (run or {}).get('results') or []:
        by_op.setdefault(r['operation_id'], []).append(r)
    out = []
    for op in contract['operations']:
        oid = op['operation_id']
        rs = by_op.get(oid, [])
        p = plan.get(oid, {'status': 'untested'})
        status = p['status']
        if status in ('planned',):
            status = 'exercised' if any(r['kind'] != 'no_auth' for r in rs) else 'untested'
        checks = {}
        for c in CHECKS:
            vals = [r['checks'][c]['result'] for r in rs if c in r['checks']]
            if not rs:
                checks[c] = 'untested'
            elif 'failed' in vals:
                checks[c] = 'failed'
            elif 'cantTell' in vals:
                checks[c] = 'cantTell'
            elif 'passed' in vals:
                checks[c] = 'passed'
            else:
                applicable = (c == 'auth_enforced' and (op['security'] or oid in marked)) or (c == 'positive_rejected' and contract['kind'] == 'openapi') \
                    or (c == 'graphql_errors' and contract['kind'] == 'graphql')
                checks[c] = 'untested' if applicable else 'not_applicable'
        out.append({'operation_id': oid, 'method': op['method'], 'path': op['path'], 'status': status,
                    'cases_sent': len(rs), 'checks': checks})
    return out


def coverage(ops: list[dict]) -> dict:
    cov = {'total': len(ops)}
    for k in ('exercised', 'excluded', 'skipped_write', 'skipped_auth', 'untested'):
        cov[k] = sum(o['status'] == k for o in ops)
    return cov


def is_outage(run: dict | None) -> bool:
    """Every answer (first sends and re-sends) was a 5xx, over at least two operations: the environment
    is failing, not N separate handlers."""
    rs = (run or {}).get('results') or []
    statuses = [r['status'] for r in rs] + [(r.get('resend') or {}).get('status') for r in rs if r.get('resend')]
    return len({r['operation_id'] for r in rs}) >= 2 and bool(statuses) and all(st is not None and st >= 500 for st in statuses)


def candidates(contract: dict, run: dict | None, run_dir: Path | None) -> list[dict]:
    if not run:
        return []
    ops = {o['operation_id']: o for o in contract['operations']}
    outage = is_outage(run)
    exchanges = load_exchanges(run_dir, run['results'])
    groups: dict[tuple, dict] = {}
    for r in run['results']:
        for c in r['failed']:
            cause, shared = cause_of(c, r, ops.get(r['operation_id']))
            if outage and c == 'server_error':
                cause, shared = 'environment outage: every request answered 5xx', True
            key = (c, cause) if shared else (c, cause, r['operation_id'])
            g = groups.setdefault(key, {'check': c, 'cause': cause, 'operation_ids': [], 'instances': []})
            if r['operation_id'] not in g['operation_ids']:
                g['operation_ids'].append(r['operation_id'])
            resend = r.get('resend') or {}
            g['instances'].append({'case_id': r['case_id'], 'operation_id': r['operation_id'], 'kind': r['kind'],
                                   'valid': r['valid'], 'write': r['write'], 'status': r['status'], 'exchange': r['exchange'],
                                   'resend_exchange': resend.get('exchange'), 'resend_status': resend.get('status'),
                                   'repeated': bool((resend.get('repeated') or {}).get(c))})
    out = []
    for i, g in enumerate(groups.values(), 1):
        sev = severity_of(g['check'], g['instances'], exchanges)
        env = outage and g['check'] == 'server_error'
        conf = 'low' if env else confidence_of(g['instances'])
        reach = reach_of(len(g['operation_ids']))
        score, prio = priority_of(sev, reach, conf)
        out.append({'id': f'K{i}', 'source': 'stdlib', **{k: g[k] for k in ('check', 'cause', 'operation_ids')},
                    'repeatable': any(x['repeated'] for x in g['instances']), 'instances': g['instances'],
                    **({'environment_outage': True} if env else {}),
                    'severity': sev, 'confidence': conf, 'reach': reach, 'score': score, 'priority': prio,
                    'regression_test_sketch': sketch(g, ops, exchanges, contract)})
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    out.sort(key=lambda c: (order[c['priority']], -c['score'], c['check']))
    for i, c in enumerate(out, 1):
        c['id'] = f'K{i}'
    return out


def sketch(g: dict, ops: dict, exchanges: dict, contract: dict) -> str:
    """A pytest sketch using only the standard library; adapt it to the repo's conventions."""
    inst = next((x for x in g['instances'] if x['repeated']), g['instances'][0])
    ex = exchanges.get(inst['exchange']) or {}
    req = ex.get('request') or {}
    url = re.sub(r'^https?://[^/]+', '', req.get('url', ''))
    name = re.sub(r'\W+', '_', f'{inst["operation_id"]}_{g["check"]}').strip('_').lower()
    op = ops.get(inst['operation_id']) or {}
    body = req.get('body')
    lines = [f'def test_{name}(api):', f'    """{g["check"]}: {g["cause"][:90]}"""']
    call = f'    resp = api.request({req.get("method", "GET")!r}, {url!r}'
    if body is not None:
        call += f', json={json.dumps(body)}'
    if not ex.get('sent_with_credentials', True):
        call += ', auth=None'
    lines.append(call + ')')
    c = g['check']
    documented = sorted(op.get('responses', {}))
    if c == 'status_documented':
        lines.append(f'    assert str(resp.status) in {documented!r} or "default" in {documented!r}')
    elif c == 'server_error':
        lines.append('    assert resp.status < 500')
    elif c == 'auth_enforced':
        lines.append('    assert resp.status in (401, 403)')
    elif c == 'negative_accepted':
        lines.append('    assert 400 <= resp.status < 500')
    elif c == 'positive_rejected':
        lines.append('    assert 200 <= resp.status < 300')
    elif c == 'content_type':
        lines.append("    assert resp.headers['Content-Type'].split(';')[0] == 'application/json'")
    elif c == 'required_headers':
        lines.append(f'    assert {g["cause"].replace("missing ", "")!r} in resp.headers')
    elif c in ('schema_conformance', 'graphql_errors'):
        lines.append('    assert not contract_errors(resp)  # validate the body against the response schema')
    return '\n'.join(lines)


def schemathesis_candidates(path: Path, start: int, contract: dict) -> list[dict]:
    """Schemathesis failures as candidates. They have no exchange of ours, so they are never rated here:
    the agent reproduces each with an added case (then it is also a stdlib candidate) or marks it needs_review."""
    data = json.loads(path.read_text(encoding='utf-8'))
    by_route = {f'{o["method"]} {o["path"]}': o['operation_id'] for o in contract['operations']}
    out = []
    for i, f in enumerate(data.get('failures') or [], start):
        msg = re.sub(r'\s+', ' ', f.get('message') or '').strip()
        out.append({'id': f'S{i}', 'source': 'schemathesis', 'check': f.get('check') or f.get('code'),
                    'cause': f'{f.get("code")} {f.get("title")}: {msg[:200]}', 'operation_ids': [by_route.get(f['operation'], f['operation'])],
                    'repeatable': None, 'instances': [], 'severity': None, 'confidence': None, 'reach': None, 'score': None,
                    'priority': None, 'regression_test_sketch': None, 'evidence': f.get('evidence')})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('contract', type=Path)
    ap.add_argument('cases', type=Path, nargs='?', help='omit (with run) for a static-only merge')
    ap.add_argument('run', type=Path, nargs='?', help='omit when the API was not run: every operation is untested')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--diff', type=Path)
    ap.add_argument('--schemathesis', type=Path)
    a = ap.parse_args()
    for p in (a.contract, a.cases, a.run, a.diff, a.schemathesis):
        if p is not None and not p.is_file():
            print(f'error: {p} does not exist', file=sys.stderr)
            return 2
    if a.schemathesis and not a.run:
        print('error: --schemathesis needs the run', file=sys.stderr)
        return 2
    contract = json.loads(a.contract.read_text(encoding='utf-8'))
    cases = json.loads(a.cases.read_text(encoding='utf-8')) if a.cases else None
    run = json.loads(a.run.read_text(encoding='utf-8')) if a.run else None
    ops = operations(contract, cases, run)
    cands = candidates(contract, run, a.run.parent if a.run else None)
    if a.schemathesis:
        cands += schemathesis_candidates(a.schemathesis, 1, contract)
    out = {'contract_sha256': contract['sha256'], 'seed': (cases or {}).get('seed'), 'run_status': run['status'] if run else 'not_run',
           'environment_outage': is_outage(run),
           'operations': ops, 'operation_coverage': coverage(ops), 'candidates': cands,
           'contract_issues': contract['issues'],
           'breaking_changes': json.loads(a.diff.read_text(encoding='utf-8'))['changes'] if a.diff else []}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    sev = {}
    for c in cands:
        k = c['severity'] or c['source']
        sev[k] = sev.get(k, 0) + 1
    print(f'{len(cands)} candidate(s) {sev}; coverage {out["operation_coverage"]} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
