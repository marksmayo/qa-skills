#!/usr/bin/env python3
"""Validate an API contract test report against the schema, the run and the rubric.

    python validate_report.py report.json --run run/run.json --candidates run/candidates.json
        --contract run/contract.json --cases run/cases.json [--diff run/diff.json] [--handlers run/handlers.json]
        [--repo DIR] [--write-summary]
    python validate_report.py report.json --contract run/contract.json [--cases run/cases.json] [--diff run/diff.json]
        [--handlers run/handlers.json] [--repo DIR]                  # a static-only report (run_status not_run)

Recomputed and compared (any difference is an error):
  * operations, their check results and the coverage counts (from contract, cases and run);
  * every candidate (merge_findings.py run again), and each finding's check, operations, severity,
    confidence, reach, score, priority and repeatability against its candidate;
  * the headline: verdict, severity counts, flaky count, breaking-change counts, evidence
    completeness and overall confidence (an environment outage caps confidence at low);
  * the top-level run_status (complete, partial or not_run) against the run;
  * breaking changes against the diff, contract issues against the loaded contract (issues found by
    review are extra entries with source "agent" and a basis file and line, re-read with --repo), and the
    handler check against handlers.json (with --repo, every cited handler file and line is re-read);
  * scope.contract against contract.json (the source is compared as a path, the title when given).

Also fails when:
  * a finding has no saved exchange, an exchange file is missing or isn't one of its candidate's,
    or its status differs from the file; a finding's `actual` doesn't state a status its exchanges show;
  * a configured secret (environment variables named in the run's secret_env) appears in any
    exchange or the report, or a credential header, API-key parameter or password/writeOnly field
    in an exchange isn't [REDACTED];
  * a write was sent with writes not allowed, or to a production host; a non-loopback target has no
    authorised_by;
  * an operation is passed with zero cases, or a check reports passed where it couldn't be evaluated;
  * the verdict or summary claims conformance, the summary is a placeholder, or (not_run) it claims
    anything a run would have shown;
  * a candidate is missing from triage or triaged twice, a Schemathesis candidate is promoted without
    being reproduced, a reason is generic ("not a real issue"), or three or more triage entries share
    one reason that names no candidate or finding id;
  * limitations are empty although the run was partial, not run, had excluded or untested operations,
    or earlier attempts; an earlier attempt the run resumed is missing from metadata.aborted_attempts;
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
from send_cases import REDACTED, SENSITIVE_HEADERS, SENSITIVE_NAME, SAFE_METHODS  # noqa: E402

import urllib.parse  # noqa: E402

CONFORMANCE_CLAIMS = re.compile(r'\b(conforms|is conformant|fully conformant|fully compliant|contract[- ]compliant|'
                                r'meets the contract|matches the contract|no (contract )?violations exist|passes the contract)\b', re.I)
PLACEHOLDER = re.compile(r'\b(placeholder|todo|tbd|lorem ipsum|fill (this|me) in|rewritten below|to be written|xxx+)\b', re.I)
GENERIC_REASON = re.compile(r"^\W*(not an? (real )?(issue|bug|problem|finding|defect)( at all)?|false positive|not relevant|"
                            r"irrelevant|noise|no issue|n/?a|none|ok|fine|ignore(d)?|won'?t fix|dismissed|not important)\W*$", re.I)
ID_REF = re.compile(r'\b[KSFH]\d+\b', re.I)
# a not_run report can't say what the API did
RUNTIME_CLAIMS = re.compile(r'\b(no (contract )?violations? (were )?found|all (checks|operations) passed|checks? passed|'
                            r'(responded|answered|returned) (with )?\d{3}|exercised \d+|'
                            r'the api (returns|answers|responds|works|is healthy))\b', re.I)
STATUS_TOKEN = re.compile(r'\b([1-5]\d\d)\b')
FINDING_FIELDS = ('check', 'operation_ids', 'severity', 'confidence', 'reach', 'score', 'priority', 'repeatable')


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def top_status(run: dict | None, outage: bool) -> str:
    if run is None:
        return 'not_run'
    return 'complete' if run['status'] == 'complete' and not outage else 'partial'


def headline_expected(rep: dict, ops: list, coverage: dict, diff_changes: list, run: dict | None, outage: bool) -> dict:
    sev = {k: 0 for k in ('Critical', 'High', 'Medium', 'Low')}
    for f in rep['findings']:
        sev[f['severity']] += 1
    br = {k: sum(c['rating'] == k for c in diff_changes) for k in ('breaking', 'potentially_breaking', 'compatible')}
    if run is None:
        completeness = 'none'
    elif run['status'] != 'complete':
        completeness = 'incomplete'
    elif outage or coverage['untested'] or coverage['skipped_auth'] or any(v == 'cantTell' for o in ops for v in o['checks'].values()) \
            or any(t['status'] == 'errored' for t in rep['metadata']['tools']):
        completeness = 'partial'
    else:
        completeness = 'complete'
    if completeness in ('incomplete', 'none') or outage:
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


def check_triage_reasons(rep: dict, errs: list[str]):
    by_reason: dict[str, list[str]] = {}
    for t in rep['triage']:
        if GENERIC_REASON.match(t['reason']):
            errs.append(f'triage: {t["candidate_id"]} has a generic reason ({t["reason"]!r}); say what was checked (a guard, '
                        f'a file:line, the candidate it duplicates)')
        if t['decision'] != 'promoted':
            by_reason.setdefault(re.sub(r'\s+', ' ', t['reason'].strip().lower()), []).append(t['candidate_id'])
    for n in rep['needs_review']:
        if GENERIC_REASON.match(n['reason']):
            errs.append(f'needs_review: {n["candidate_id"]} has a generic reason ({n["reason"]!r})')
    for reason, ids in by_reason.items():
        if len(ids) >= 3 and not ID_REF.search(reason):
            errs.append(f'triage: {len(ids)} candidates ({", ".join(ids[:5])}{"..." if len(ids) > 5 else ""}) share the reason '
                        f'{reason!r}; give each its own reason, or name the shared cause by its candidate or finding id')


def same_source(reported: str, recorded: str | None, contract_path: Path) -> bool:
    """The report's contract source names the same contract as contract.json's. load_contract.py records the
    path as it was given, so a relative path, ./path, backslashes and an absolute path to the same file all
    match. (The contract's identity is its sha256, which is checked separately.)"""
    if recorded is None:
        return False
    if reported == recorded:
        return True
    if re.match(r'^[a-z]+://', reported, re.I) or re.match(r'^[a-z]+://', recorded, re.I):
        return reported.rstrip('/') == recorded.rstrip('/')

    def parts(p: str) -> list[str]:
        return [x for x in p.replace('\\', '/').split('/') if x not in ('', '.')]
    a, b = parts(reported), parts(recorded)
    if a == b:
        return True
    for base in (Path.cwd(), contract_path.resolve().parent):
        try:
            if (base / reported).resolve() == (base / recorded).resolve():
                return True
        except OSError:
            pass
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 2 and longer[-len(shorter):] == shorter   # the same trailing directory and file name


def check_contract_issues(rep: dict, contract: dict, repo: Path | None, errs: list[str]):
    """Loader issues must equal contract.json's; agent issues need a basis, re-read under --repo."""
    loader = [{k: v for k, v in i.items() if k != 'source'} for i in rep['contract_issues'] if i.get('source', 'loader') == 'loader']
    agent = [i for i in rep['contract_issues'] if i.get('source') == 'agent']
    if loader != contract['issues']:
        errs.append('contract_issues do not match the loaded contract (copy contract.json\'s issues, then add any found by '
                    'review with source "agent")')
    ids = [i['id'] for i in rep['contract_issues']]
    for dup in sorted({x for x in ids if ids.count(x) > 1}):
        errs.append(f'contract_issues: id {dup} is used twice')
    for i in rep['contract_issues']:
        if i.get('source', 'loader') == 'loader' and 'basis' in i:
            errs.append(f'contract issue {i["id"]}: a basis is only for issues found by review (source "agent")')
    for i in agent:
        b = i.get('basis')
        if not b:
            errs.append(f'contract issue {i["id"]}: an issue found by review needs a basis (file and line)')
            continue
        if repo is None:
            continue
        p = repo / b['file']
        if not p.is_file():
            errs.append(f'contract issue {i["id"]}: basis file {b["file"]} does not exist under --repo')
            continue
        lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        if b['line'] > len(lines) or not lines[b['line'] - 1].strip():
            errs.append(f'contract issue {i["id"]}: {b["file"]}:{b["line"]} is not a line of code')
        elif b.get('excerpt') and ' '.join(b['excerpt'].split()) not in ' '.join(lines[b['line'] - 1].split()):
            errs.append(f'contract issue {i["id"]}: excerpt {b["excerpt"]!r} is not on {b["file"]}:{b["line"]}')


def check_handler_check(rep: dict, handlers: dict | None, contract: dict, repo: Path | None, errs: list[str]):
    hc = rep['handler_check']
    if handlers is None:
        if hc is not None:
            errs.append('handler_check is reported without --handlers; pass the handlers.json it came from')
        return
    if handlers.get('contract_sha256') != contract['sha256']:
        errs.append('handlers.json was made from a different contract; rerun handler_map.py')
    expected = {'source': handlers['tool'], 'frameworks': handlers['frameworks'], 'contract_prefix': handlers['contract_prefix'],
                'handlers_found': len(handlers['handlers']), 'counts': handlers['counts'], 'issues': handlers['issues']}
    if hc is None:
        errs.append('handler_check is null but --handlers was given; copy its summary and issues into the report')
        return
    for k in ('handler_prefix', 'unresolved_mounts'):
        if k in hc or k in handlers:
            expected[k] = handlers.get(k)
    for k, v in expected.items():
        if hc.get(k) != v:
            errs.append(f'handler_check.{k} does not match handlers.json')
    if repo:
        for i in hc['issues']:
            if not i['file']:
                continue
            p = repo / i['file']
            if not p.is_file():
                errs.append(f'handler_check {i["id"]}: {i["file"]} does not exist under --repo')
                continue
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
            if not i['line'] or i['line'] > len(lines) or not lines[i['line'] - 1].strip():
                errs.append(f'handler_check {i["id"]}: {i["file"]}:{i["line"]} is not a line of code')


def check_actual(f: dict, errs: list[str]):
    statuses = {e['status'] for e in f['exchanges']}
    cited = {int(x) for x in STATUS_TOKEN.findall(f['actual'])}
    if not cited & statuses:
        errs.append(f'{f["id"]}: actual must state what the saved exchange shows, including its status '
                    f'({", ".join(map(str, sorted(statuses)))}); it says {f["actual"][:80]!r}')
    elif any(200 <= c < 300 for c in cited - statuses) and not any(200 <= s < 300 for s in statuses):
        errs.append(f'{f["id"]}: actual cites a 2xx that none of its exchanges shows')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--run', type=Path)
    ap.add_argument('--candidates', type=Path)
    ap.add_argument('--contract', type=Path, required=True)
    ap.add_argument('--cases', type=Path)
    ap.add_argument('--diff', type=Path)
    ap.add_argument('--handlers', type=Path, help='handlers.json from handler_map.py, when the report has a handler_check')
    ap.add_argument('--repo', type=Path, help='directory the handler file paths are relative to')
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()

    for name in ('report', 'run', 'candidates', 'contract', 'cases', 'diff', 'handlers'):
        p = getattr(a, name)
        if p is not None and not p.is_file():
            print(f'INVALID: {name} file {p} does not exist')
            return 2
    rep = load(a.report)
    contract = load(a.contract)
    errs: list[str] = []
    warns: list[str] = []
    schema = load(HERE.parent / 'schemas' / 'api_contract_report.schema.json')
    errs += [f'schema: {e}' for e in schema_check.errors(rep, schema)]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1

    static = rep['run_status'] == 'not_run'
    if static and a.run:
        print('INVALID: the report says run_status not_run, but --run was given; a report with a run must use it')
        return 1
    if not static and not (a.run and a.candidates and a.cases):
        print('INVALID: a report with a run needs --run, --candidates and --cases (or run_status not_run for a static-only report)')
        return 2
    run = load(a.run) if a.run else None
    cands_doc = load(a.candidates) if a.candidates else None
    cases = load(a.cases) if a.cases else None
    diff = load(a.diff) if a.diff else None
    handlers = load(a.handlers) if a.handlers else None
    run_dir = a.run.parent if a.run else None

    sc, md, hl = rep['scope'], rep['metadata'], rep['headline']
    # identity of the inputs
    if sc['contract']['sha256'] != contract['sha256']:
        errs.append('scope.contract.sha256 is not the loaded contract')
    for k in ('kind', 'version', 'api_version'):
        if sc['contract'][k] != contract.get(k):
            errs.append(f'scope.contract.{k} {sc["contract"][k]!r} != contract {contract.get(k)!r}')
    if 'title' in sc['contract'] and sc['contract']['title'] != contract.get('title'):
        errs.append(f'scope.contract.title {sc["contract"]["title"]!r} != contract {contract.get("title")!r}')
    if not same_source(sc['contract']['source'], contract.get('source'), a.contract):
        errs.append(f'scope.contract.source {sc["contract"]["source"]!r} is not the contract\'s source {contract.get("source")!r} '
                    f'(copy the source from contract.json)')
    if cases is not None:
        if cases['contract_sha256'] != contract['sha256']:
            errs.append('cases.json was made from a different contract')
        if sc['seed'] != cases['seed']:
            errs.append('scope.seed does not match the cases')
        if sc['max_cases_per_operation'] != cases['max_cases_per_operation']:
            errs.append('scope.max_cases_per_operation does not match cases.json')
    elif sc['seed'] is not None or sc['max_cases_per_operation'] is not None:
        errs.append('scope.seed / max_cases_per_operation are set without --cases')
    if not static:
        if None in (sc['seed'], sc['max_cases_per_operation'], sc['base_url'], md['run'], md['generator']):
            errs.append('scope.seed, max_cases_per_operation, base_url, metadata.run and metadata.generator are needed when the API was run')

    outage = mf.is_outage(run)
    # run status, top level and metadata
    exp_top = top_status(run, outage)
    if rep['run_status'] != exp_top:
        errs.append(f'run_status is {rep["run_status"]!r}; the run gives {exp_top!r}')
    if rep['run_status'] != 'complete' and not (rep['run_status_reason'] or '').strip():
        errs.append('run_status_reason is required when the run is partial or not run')

    if static:
        if md['run'] is not None or md['requests_sent'] != 0 or md['run_status'] != 'not_run' \
                or md['rate_limited'] != {'events': 0, 'backoff_seconds': 0} or md['secret_env']:
            errs.append('a not_run report has metadata.run null, requests_sent 0, run_status not_run, no rate limiting and no secret_env')
        if rep['findings'] or rep['needs_review'] or rep['triage']:
            errs.append('a not_run report has no findings, needs_review or triage: every finding needs a saved exchange')
        if any(t['name'] == 'send_cases.py' and t['status'] == 'ran' for t in md['tools']):
            errs.append('metadata.tools says send_cases.py ran, but the report is not_run')
        if cands_doc and (cands_doc['candidates'] or cands_doc.get('run_status') != 'not_run'):
            errs.append('candidates.json has a run; a not_run report takes the static merge (no run)')
        m = RUNTIME_CLAIMS.search(hl['summary'])
        if m:
            errs.append(f'the API was not run, but the summary says {m.group(0)!r}')
        if 'not run' not in hl['summary'].lower() and 'not_run' not in hl['summary'].lower():
            errs.append('headline.summary must say the API was not run')
    else:
        if sc['base_url'] != run['base_url']:
            errs.append('scope.base_url does not match the run')
        if run['contract_sha256'] != contract['sha256']:
            errs.append('run.json was made from a different contract')
        if run['seed'] != (cases or {}).get('seed'):
            errs.append('scope.seed does not match the run')
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
        records = {x['record'] for x in md['aborted_attempts']}
        for prev in run.get('resumed_from') or []:
            if prev['file'] not in records:
                errs.append(f'the run resumed {prev["file"]} ({prev["requests_sent"]} requests); list it in metadata.aborted_attempts')
    if contract['kind'] == 'openapi' and sc['previous_contract'] and not any(t['name'] == 'oasdiff' for t in md['tools']):
        errs.append('metadata.tools must record oasdiff when a previous contract was diffed')

    if run is not None:
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
    if cands_doc and (cands_doc['operations'] != ops or cands_doc['operation_coverage'] != coverage):
        errs.append('candidates.json operations are stale; rerun merge_findings.py')

    # candidates
    cands = {}
    if cands_doc:
        fresh = mf.candidates(contract, run, run_dir)
        stdlib_cands = [c for c in cands_doc['candidates'] if c['source'] == 'stdlib']
        if json.dumps(fresh, sort_keys=True) != json.dumps(stdlib_cands, sort_keys=True):
            errs.append('candidates.json does not match the run; rerun merge_findings.py')
        cands = {c['id']: c for c in cands_doc['candidates']}
        if cands_doc['contract_sha256'] != contract['sha256'] or cands_doc['seed'] != (cases or {}).get('seed'):
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
    check_triage_reasons(rep, errs)
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
        check_actual(f, errs)
        if f['check'] == 'auth_enforced' and not f['security_relevant']:
            errs.append(f'{f["id"]}: an auth_enforced failure is security relevant')
        if f['handler'] and a.repo:
            hp = a.repo / f['handler']['file']
            if not hp.is_file():
                errs.append(f'{f["id"]}: handler file {f["handler"]["file"]} does not exist')
            elif f['handler']['line'] > len(hp.read_text(encoding='utf-8').splitlines()):
                errs.append(f'{f["id"]}: handler line {f["handler"]["line"]} is past the end of {f["handler"]["file"]}')

    # contract issues, handler check and breaking changes
    check_contract_issues(rep, contract, a.repo, errs)
    check_handler_check(rep, handlers, contract, a.repo, errs)
    diff_changes = diff['changes'] if diff else []
    if diff:
        if rep['breaking_changes'] != diff_changes:
            errs.append('breaking_changes do not match diff.json')
        prev = sc['previous_contract']
        if not prev or prev['sha256'] != diff['old']['sha256'] or diff['new']['sha256'] != contract['sha256']:
            errs.append('scope.previous_contract does not match the diff')
    elif rep['breaking_changes'] or sc['previous_contract']:
        errs.append('breaking changes or a previous contract are reported without --diff')

    # limitations
    partial_reasons = []
    if rep['run_status'] != 'complete':
        partial_reasons.append(f'run_status {rep["run_status"]}')
    for k in ('excluded', 'untested', 'skipped_write', 'skipped_auth'):
        if coverage[k]:
            partial_reasons.append(f'{coverage[k]} operation(s) {k.replace("_", " ")}')
    if md['aborted_attempts']:
        partial_reasons.append(f'{len(md["aborted_attempts"])} earlier attempt(s)')
    if partial_reasons and not [x for x in rep['limitations'] if x.strip()]:
        errs.append(f'limitations are empty although {", ".join(partial_reasons)}; say what was not covered and why')
    for x in rep['limitations'] + rep['next_steps']:
        if PLACEHOLDER.search(x):
            errs.append(f'placeholder text in limitations or next_steps: {x[:60]!r}')

    # headline
    exp = headline_expected(rep, ops, coverage, diff_changes, run, outage)
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if PLACEHOLDER.search(hl['summary']):
        errs.append('headline.summary is a placeholder; write the summary (--write-summary does not)')
    if CONFORMANCE_CLAIMS.search(hl['summary']) and not re.search(r'conformance not established', hl['summary'], re.I):
        errs.append('headline.summary claims conformance; the best a run can say is "conformance not established"')
    if exp['verdict'] == 'not_established' and 'conformance not established' not in hl['summary'].lower():
        errs.append('headline.summary must say "conformance not established" when nothing was found')
    if outage and not re.search(r'outage|environment|every (operation|request)', hl['summary'], re.I):
        errs.append('every request answered 5xx (an environment outage); the summary must say so rather than read as API defects')

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
          f'{len(rep["triage"])} candidate(s) triaged, verdict {hl["verdict"]}, run {rep["run_status"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
