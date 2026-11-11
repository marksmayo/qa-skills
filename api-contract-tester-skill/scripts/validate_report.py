#!/usr/bin/env python3
"""Validate an API contract test report against the schema, the run and the rubric.

    python validate_report.py report.json --run run/run.json --candidates run/candidates.json
        --contract run/contract.json --cases run/cases.json [--diff run/diff.json] [--repo DIR] [--write-summary]

Recomputed and compared (any difference is an error):
  * operations, their check results and the coverage counts (from contract, cases and run);
  * every candidate (merge_findings.py run again), and each finding's check, operations, severity,
    confidence, reach, score, priority and repeatability against its candidate;
  * the headline: verdict, severity counts, flaky count, breaking-change counts, evidence
    completeness and overall confidence;
  * breaking changes against the diff, and contract issues against the loaded contract.

Also fails when:
  * a finding has no saved exchange, an exchange file is missing or isn't one of its candidate's,
    or its status differs from the file;
  * a configured secret (environment variables named in the run's secret_env) appears in any
    exchange or the report, or a credential header, API-key parameter or password/writeOnly field
    in an exchange isn't [REDACTED];
  * a write was sent with writes not allowed, or to a production host; a non-loopback target has no
    authorised_by;
  * an operation is passed with zero cases, or a check reports passed where it couldn't be evaluated;
  * the verdict or summary claims conformance;
  * a candidate is missing from triage or triaged twice, a Schemathesis candidate is promoted without
    being reproduced, or a dismissal has no reason;
  * the fix direction is not marked agent_judgment, or (with --repo) a handler line doesn't exist.

--write-summary rewrites the computed headline fields and exits 0 if nothing else is wrong.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import merge_findings as mf  # noqa: E402
import schema_check  # noqa: E402
from send_cases import REDACTED, SENSITIVE_HEADERS, SENSITIVE_NAME, SAFE_METHODS, is_loopback  # noqa: E402

import urllib.parse  # noqa: E402

CONFORMANCE_CLAIMS = re.compile(r'\b(conforms|is conformant|fully conformant|fully compliant|contract[- ]compliant|'
                                r'meets the contract|matches the contract|no (contract )?violations exist|passes the contract)\b', re.I)
FINDING_FIELDS = ('check', 'operation_ids', 'severity', 'confidence', 'reach', 'score', 'priority', 'repeatable')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def headline_expected(rep: dict, ops: list, coverage: dict, diff_changes: list, run: dict) -> dict:
    sev = {k: 0 for k in ('Critical', 'High', 'Medium', 'Low')}
    for f in rep['findings']:
        sev[f['severity']] += 1
    br = {k: sum(c['rating'] == k for c in diff_changes) for k in ('breaking', 'potentially_breaking', 'compatible')}
    if run['status'] != 'complete':
        completeness = 'incomplete'
    elif coverage['untested'] or coverage['skipped_auth'] or any(v == 'cantTell' for o in ops for v in o['checks'].values()) \
            or any(t['status'] == 'errored' for t in rep['metadata']['tools']):
        completeness = 'partial'
    else:
        completeness = 'complete'
    if completeness == 'incomplete':
        conf = 'low'
    elif completeness == 'partial' or any(f['confidence'] != 'high' for f in rep['findings']):
        conf = 'medium'
    else:
        conf = 'high'
    return {'verdict': 'violations_found' if rep['findings'] else 'not_established', 'severity_counts': sev,
            'flaky_findings': sum(not f['repeatable'] for f in rep['findings']), 'breaking_change_counts': br,
            'operation_coverage': coverage, 'evidence_completeness': completeness, 'overall_confidence': conf}


def scan_exchange(ex: dict, path: str, contract: dict, secrets: list[str], errs: list[str]):
    text = json.dumps(ex)
    for s in secrets:
        if s and s in text:
            errs.append(f'{path}: contains the value of a configured secret')
    for side in ('request', 'response'):
        for k, v in ((ex.get(side) or {}).get('headers') or {}).items():
            if (k.lower() in SENSITIVE_HEADERS or SENSITIVE_NAME.search(k)) and v != REDACTED:
                errs.append(f'{path}: {side} header {k} is not redacted')
    q = urllib.parse.parse_qsl(urllib.parse.urlsplit((ex.get('request') or {}).get('url', '')).query, keep_blank_values=True)
    for k, v in q:
        if SENSITIVE_NAME.search(k) and v != REDACTED:
            errs.append(f'{path}: query parameter {k} is not redacted')
    if re.search(r'Bearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/-]{8,}', text):
        errs.append(f'{path}: an unredacted bearer token')
    op = next((o for o in contract['operations'] if o['operation_id'] == ex.get('operation_id')), None)
    body = (ex.get('request') or {}).get('body')
    if op and op.get('request_body') and isinstance(body, dict):
        for c in op['request_body']['content'].values():
            for k in sensitive_fields(c.get('schema') or {}, contract['root']):
                if k in body and body[k] != REDACTED:
                    errs.append(f'{path}: request body field {k!r} is format password or writeOnly and not redacted')


def sensitive_fields(schema, root, depth=0) -> set[str]:
    from jsonschema_lite import resolve
    out = set()
    while isinstance(schema, dict) and '$ref' in schema and depth < 20:
        schema = resolve(schema['$ref'], root)
        depth += 1
    for k, ps in ((schema or {}).get('properties') or {}).items():
        while isinstance(ps, dict) and '$ref' in ps:
            ps = resolve(ps['$ref'], root)
        if isinstance(ps, dict) and (ps.get('format') == 'password' or ps.get('writeOnly')):
            out.add(k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--contract', type=Path, required=True)
    ap.add_argument('--cases', type=Path, required=True)
    ap.add_argument('--diff', type=Path)
    ap.add_argument('--repo', type=Path, help='directory the handler file paths are relative to')
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()

    rep, run, cands_doc = load(a.report), load(a.run), load(a.candidates)
    contract, cases = load(a.contract), load(a.cases)
    diff = load(a.diff) if a.diff else None
    run_dir = a.run.parent
    errs: list[str] = []
    warns: list[str] = []

    schema = load(HERE.parent / 'schemas' / 'api_contract_report.schema.json')
    errs += [f'schema: {e}' for e in schema_check.errors(rep, schema)]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1

    sc, md, hl = rep['scope'], rep['metadata'], rep['headline']
    # identity of the inputs
    if sc['contract']['sha256'] != contract['sha256']:
        errs.append('scope.contract.sha256 is not the loaded contract')
    for k in ('kind', 'version', 'api_version', 'source'):
        if sc['contract'][k] != contract.get(k):
            errs.append(f'scope.contract.{k} {sc["contract"][k]!r} != contract {contract.get(k)!r}')
    if cases['contract_sha256'] != contract['sha256'] or run['contract_sha256'] != contract['sha256']:
        errs.append('cases.json or run.json was made from a different contract')
    if sc['seed'] != cases['seed'] or run['seed'] != cases['seed']:
        errs.append('scope.seed does not match the cases and the run')
    if sc['max_cases_per_operation'] != cases['max_cases_per_operation']:
        errs.append('scope.max_cases_per_operation does not match cases.json')
    if sc['base_url'] != run['base_url']:
        errs.append('scope.base_url does not match the run')
    if sc['writes_allowed'] != run['writes_allowed'] or sc['authorised_by'] != run['authorised_by']:
        errs.append('scope.writes_allowed / authorised_by do not match the run')
    if sorted(sc['production_hosts']) != sorted(run['production_hosts']):
        errs.append('scope.production_hosts does not match the run')
    if not run['loopback'] and not run['authorised_by']:
        errs.append(f'{run["host"]} is not loopback and the run has no authorised_by')
    if md['requests_sent'] != run['requests_sent'] or md['rate_limited'] != run['rate_limited'] or md['run_status'] != run['status']:
        errs.append('metadata.requests_sent / rate_limited / run_status do not match the run')
    if sorted(md['secret_env']) != sorted(run['secret_env']):
        errs.append('metadata.secret_env does not match the run')
    gen = cases['generator']
    st_ran = any(t['name'] == 'schemathesis' and t['status'] == 'ran' for t in md['tools'])
    expected_gen = 'both' if st_ran else gen
    if md['generator'] != expected_gen:
        errs.append(f'metadata.generator is {md["generator"]!r}; expected {expected_gen!r}')
    if not any(t['name'] == 'schemathesis' for t in md['tools']):
        errs.append('metadata.tools must record schemathesis (ran, not_run or errored)')
    if contract['kind'] == 'openapi' and sc['previous_contract'] and not any(t['name'] == 'oasdiff' for t in md['tools']):
        errs.append('metadata.tools must record oasdiff when a previous contract was diffed')

    # writes and production hosts
    for r in run['results']:
        if r['write'] and not run['writes_allowed']:
            errs.append(f'{r["case_id"]}: a write ({r["method"]}) was sent with writes not allowed')
        if r['write'] and run['host'] in run['production_hosts']:
            errs.append(f'{r["case_id"]}: a write was sent to production host {run["host"]}')

    # exchanges: secrets and redaction
    secrets = []
    for e in run['secret_env']:
        v = os.environ.get(e)
        if v:
            secrets.append(v)
        else:
            warns.append(f'{e} is not set here, so exchanges were not scanned for its value (headers and fields were still checked)')
    ex_files = sorted((run_dir / 'exchanges').glob('*.json')) if (run_dir / 'exchanges').is_dir() else []
    for p in ex_files:
        ex = load(p)
        scan_exchange(ex, p.relative_to(run_dir).as_posix(), contract, secrets, errs)
        if ex['request']['method'].upper() not in SAFE_METHODS and not (ex['request'].get('url', '').endswith('/graphql')) \
                and not run['writes_allowed']:
            errs.append(f'{p.name}: a {ex["request"]["method"]} exchange with writes not allowed')
        host = urllib.parse.urlsplit(ex['request'].get('url', '')).hostname or ''
        if ex['request']['method'].upper() not in SAFE_METHODS and host in run['production_hosts']:
            errs.append(f'{p.name}: a write exchange to production host {host}')
    rep_text = json.dumps(rep)
    for s in secrets:
        if s in rep_text:
            errs.append('the report contains the value of a configured secret')

    # operations and coverage
    ops = mf.operations(contract, cases, run)
    coverage = mf.coverage(ops)
    if rep['operations'] != ops:
        mine = {o['operation_id']: o for o in rep['operations']}
        for o in ops:
            if mine.get(o['operation_id']) != o:
                errs.append(f'operations: {o["operation_id"]} differs from the run (expected {json.dumps(o)})')
        if len(mine) != len(ops):
            errs.append('operations: not every contract operation is listed exactly once')
    for o in rep['operations']:
        if o['cases_sent'] == 0 and any(v == 'passed' for v in o['checks'].values()):
            errs.append(f'operations: {o["operation_id"]} reports a passed check with zero cases sent')
    if sum(coverage[k] for k in ('exercised', 'excluded', 'skipped_write', 'skipped_auth', 'untested')) != coverage['total']:
        errs.append('coverage does not add up')
    if cands_doc['operations'] != ops or cands_doc['operation_coverage'] != coverage:
        errs.append('candidates.json operations are stale; rerun merge_findings.py')

    # candidates
    fresh = mf.candidates(contract, run, run_dir)
    stdlib_cands = [c for c in cands_doc['candidates'] if c['source'] == 'stdlib']
    if json.dumps(fresh, sort_keys=True) != json.dumps(stdlib_cands, sort_keys=True):
        errs.append('candidates.json does not match the run; rerun merge_findings.py')
    cands = {c['id']: c for c in cands_doc['candidates']}
    if cands_doc['contract_sha256'] != contract['sha256'] or cands_doc['seed'] != cases['seed']:
        errs.append('candidates.json was made from a different contract or seed')

    # triage
    seen: dict[str, dict] = {}
    for t in rep['triage']:
        if t['candidate_id'] in seen:
            errs.append(f'triage: {t["candidate_id"]} is triaged twice')
        seen[t['candidate_id']] = t
        if t['candidate_id'] not in cands:
            errs.append(f'triage: {t["candidate_id"]} is not a candidate')
    for cid in cands:
        if cid not in seen:
            errs.append(f'triage: candidate {cid} is missing')
    findings = {f['id']: f for f in rep['findings']}
    review = {n['candidate_id'] for n in rep['needs_review']}
    for t in rep['triage']:
        c = cands.get(t['candidate_id'])
        if t['decision'] == 'promoted':
            f = findings.get(t['finding_id'] or '')
            if not f or f['candidate_id'] != t['candidate_id']:
                errs.append(f'triage: {t["candidate_id"]} is promoted to {t["finding_id"]}, which is not its finding')
            if c and c['source'] != 'stdlib':
                errs.append(f'triage: {t["candidate_id"]} came from {c["source"]} with no saved exchange; reproduce it with an added case first')
        elif t['finding_id'] is not None:
            errs.append(f'triage: {t["candidate_id"]} is {t["decision"]} but names finding {t["finding_id"]}')
        if t['decision'] == 'needs_review' and t['candidate_id'] not in review:
            errs.append(f'triage: {t["candidate_id"]} is needs_review but not in needs_review[]')
        if t['decision'] == 'dismissed' and c and c['source'] == 'stdlib' and c['severity'] == 'Critical':
            warns.append(f'triage: Critical candidate {t["candidate_id"]} was dismissed; make sure the reason is checkable')
    for cid in review:
        if seen.get(cid, {}).get('decision') != 'needs_review':
            errs.append(f'needs_review: {cid} is not triaged as needs_review')
    promoted = {t['finding_id'] for t in rep['triage'] if t['decision'] == 'promoted'}
    for fid in findings:
        if fid not in promoted:
            errs.append(f'{fid}: not promoted from any candidate in triage')

    # findings
    for f in rep['findings']:
        c = cands.get(f['candidate_id'])
        if not c:
            errs.append(f'{f["id"]}: candidate {f["candidate_id"]} does not exist')
            continue
        for k in FINDING_FIELDS:
            if f[k] != c[k]:
                errs.append(f'{f["id"]}: {k} is {f[k]!r}; the run gives {c[k]!r}')
        if f['cause'] != c['cause']:
            errs.append(f'{f["id"]}: cause differs from candidate {c["id"]}')
        allowed = {}
        for i in c['instances']:
            allowed[i['exchange']] = i['status']
            if i['resend_exchange']:
                allowed[i['resend_exchange']] = i['resend_status']
        for e in f['exchanges']:
            if e['path'] not in allowed:
                errs.append(f'{f["id"]}: exchange {e["path"]} is not one of candidate {c["id"]}\'s')
                continue
            p = run_dir / e['path']
            if not p.is_file():
                errs.append(f'{f["id"]}: exchange file {e["path"]} is missing')
                continue
            if load(p)['response']['status'] != e['status'] or allowed[e['path']] != e['status']:
                errs.append(f'{f["id"]}: exchange {e["path"]} status {e["status"]} does not match the file')
        if f['check'] == 'auth_enforced' and not f['security_relevant']:
            errs.append(f'{f["id"]}: an auth_enforced failure is security relevant')
        if f['handler'] and a.repo:
            hp = a.repo / f['handler']['file']
            if not hp.is_file():
                errs.append(f'{f["id"]}: handler file {f["handler"]["file"]} does not exist')
            elif f['handler']['line'] > len(hp.read_text(encoding='utf-8').splitlines()):
                errs.append(f'{f["id"]}: handler line {f["handler"]["line"]} is past the end of {f["handler"]["file"]}')

    # contract issues and breaking changes
    if rep['contract_issues'] != contract['issues']:
        errs.append('contract_issues do not match the loaded contract')
    diff_changes = diff['changes'] if diff else []
    if diff:
        if rep['breaking_changes'] != diff_changes:
            errs.append('breaking_changes do not match diff.json')
        prev = sc['previous_contract']
        if not prev or prev['sha256'] != diff['old']['sha256'] or diff['new']['sha256'] != contract['sha256']:
            errs.append('scope.previous_contract does not match the diff')
    elif rep['breaking_changes'] or sc['previous_contract']:
        errs.append('breaking changes or a previous contract are reported without --diff')

    # headline
    exp = headline_expected(rep, ops, coverage, diff_changes, run)
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if CONFORMANCE_CLAIMS.search(hl['summary']) and not re.search(r'conformance not established', hl['summary'], re.I):
        errs.append('headline.summary claims conformance; the best a run can say is "conformance not established"')
    if exp['verdict'] == 'not_established' and 'conformance not established' not in hl['summary'].lower():
        errs.append('headline.summary must say "conformance not established" when nothing was found')

    if a.write_summary and not errs:
        a.report.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'headline written to {a.report}')
    for w in warns:
        print(f'warning: {w}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["findings"])} finding(s) over {coverage["exercised"]}/{coverage["total"]} operations, '
          f'{len(rep["triage"])} candidate(s) triaged, verdict {hl["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
