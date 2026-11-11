#!/usr/bin/env python3
"""Run a catalogued test suite in CI: plan, trigger (only with authority), poll to completion, collect (stdlib only).

    python run_suite.py plan --catalogue suites.json --suite NAME --environment ENV [--tags EXPR] [--ref REF] --out run/
    python run_suite.py trigger --run run/ (--preauth preauth.json | --confirmed-by "who, how, when")
    python run_suite.py poll --run run/ [--timeout-minutes 60] [--poll-seconds 20]
    python run_suite.py collect --run run/ [--download PATTERN ...]
    (all take --api-base to point at another API host, such as the example's loopback stub)

plan resolves the suite in the catalogue, checks the environment and the tag expression (only the
catalogue's markers with and/or/not and parentheses) and writes the exact request it would send.
trigger sends it once, and only with authority: a pre-authorisation grant that covers the suite and the
environment and hasn't expired, or a confirmation of this request. A protected environment always needs
--confirmed-by. Without authority it records the refusal, prints the request, and exits 2. A run that was
already triggered is never triggered again.
GitHub dispatches return no run id: the run is the newest workflow_dispatch run on the ref created at or
after the trigger. Azure DevOps returns the run id.
poll follows the run with backoff (x1.5 up to 120 s) until it completes or the timeout passes (timed_out;
poll again later). collect reads the jobs, downloads the JUnit artifacts (and any --download patterns),
totals the results with the shared parse_results.py, and fetches failed jobs' logs.
Tokens come from GITHUB_TOKEN / GH_TOKEN or AZURE_DEVOPS_EXT_PAT and are never written.
"""
from __future__ import annotations
import argparse, base64, fnmatch, io, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, zipfile
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402

DEFAULT_API = {'github': 'https://api.github.com', 'azure': 'https://dev.azure.com'}
TAG_WORDS = {'and', 'or', 'not'}


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load(run_dir: Path) -> dict:
    return json.loads((run_dir / 'suite_run.json').read_text(encoding='utf-8'))


def save(run_dir: Path, d: dict):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'suite_run.json').write_text(json.dumps(d, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def tags_ok(expr: str, markers: list[str]) -> str | None:
    for tok in re.findall(r'[()]|[^\s()]+', expr):
        if tok not in TAG_WORDS and tok not in ('(', ')') and tok not in markers:
            return f'"{tok}" is not a marker in the catalogue ({", ".join(markers)})'
    return None


def headers(system: str) -> dict:
    if system == 'github':
        tok = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        if not tok and shutil.which('gh'):
            tok = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True).stdout.strip()
        if not tok:
            raise SystemExit('no GitHub token: set GITHUB_TOKEN or GH_TOKEN')
        return {'Authorization': f'Bearer {tok}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    pat = os.environ.get('AZURE_DEVOPS_EXT_PAT') or sys.exit('no Azure DevOps PAT: set AZURE_DEVOPS_EXT_PAT')
    return {'Authorization': 'Basic ' + base64.b64encode(f':{pat}'.encode()).decode(), 'Accept': 'application/json'}


def call(method: str, url: str, system: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(url, method=method, headers=headers(system), data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        data, code = e.read(), e.code
    if raw:
        return code, data
    return code, (json.loads(data) if data and data[:1] in (b'{', b'[') else None)


def cmd_plan(a) -> int:
    cat = json.loads(a.catalogue.read_text(encoding='utf-8'))
    s = cat['suites'].get(a.suite) or sys.exit(f'suite {a.suite!r} is not in the catalogue ({", ".join(cat["suites"])})')
    if a.environment not in s['environments']:
        raise SystemExit(f'environment {a.environment!r} is not allowed for {a.suite} ({", ".join(s["environments"])})')
    tags = a.tags or s.get('default_tags', '')
    bad = tags_ok(tags, s.get('markers', []))
    if bad:
        raise SystemExit(f'tags refused: {bad}')
    ref = a.ref or s['default_ref']
    api = (a.api_base or DEFAULT_API[s['system']]).rstrip('/')
    inputs = {s['inputs']['environment']: a.environment, s['inputs']['tags']: tags}
    if s['system'] == 'github':
        plan = {'method': 'POST', 'url': f'{api}/repos/{s["repo"]}/actions/workflows/{s["workflow"]}/dispatches', 'body': {'ref': ref, 'inputs': inputs}}
        pipeline = f'{s["repo"]} {s["workflow"]}'
    else:
        plan = {'method': 'POST', 'url': f'{api}/{s["org"]}/{s["project"]}/_apis/pipelines/{s["pipeline_id"]}/runs?api-version=7.1',
                'body': {'resources': {'repositories': {'self': {'refName': ref}}}, 'templateParameters': inputs}}
        pipeline = f'{s["org"]}/{s["project"]} pipeline {s["pipeline_id"]}'
    d = {'schema_version': '1.0.0', 'skill': {'name': 'ci-suite-runner', 'version': '1.0.0'}, 'catalogue': a.catalogue.as_posix(),
         'request': {'suite': a.suite, 'system': s['system'], 'pipeline': pipeline, 'environment': a.environment, 'protected': a.environment in s.get('protected', []),
                     'tags': tags, 'ref': ref, 'inputs': {k: ('[redacted]' if k in s.get('secret_inputs', []) else v) for k, v in inputs.items()}},
         'api_base': api, 'plan': plan, 'authorisations': [], 'trigger': None, 'run': None, 'polling': [], 'outcome': {'status': 'not_started', 'conclusion': None},
         'results': None, 'artifacts': [], 'logs': [], 'next_steps': [], 'limitations': []}
    save(a.out, d)
    print(f'plan: {plan["method"]} {plan["url"]}\n      {json.dumps(plan["body"])}' + ('\n      environment is protected: needs --confirmed-by' if d['request']['protected'] else ''))
    return 0


def covering(grants: list[dict], suite: str, env: str, on: date) -> dict | None:
    for g in grants:
        if suite in g.get('suites', []) and env in g.get('environments', []) and date.fromisoformat(g['expires']) >= on:
            return g
    return None


def cmd_trigger(a) -> int:
    d = load(a.run)
    r = d['request']
    if d.get('trigger') and d['trigger'].get('sent'):
        raise SystemExit(f'already triggered at {d["trigger"]["at"]} (run {(d.get("run") or {}).get("id")}); poll it instead of triggering again')
    today = datetime.now(timezone.utc).date()
    auth, reason = None, None
    if a.confirmed_by:
        auth = {'kind': 'confirmation', 'by': a.confirmed_by, 'at': now()}
    elif a.preauth:
        pa = json.loads(a.preauth.read_text(encoding='utf-8'))
        g = covering(pa.get('grants', []), r['suite'], r['environment'], today)
        if r['protected']:
            reason = f'{r["environment"]} is protected: a pre-authorisation never covers it; confirm this run with --confirmed-by'
        elif g:
            auth = {'kind': 'preauth', 'by': g['granted_by'], 'at': now(), 'grant_id': g['id'], 'scope': {'suites': g['suites'], 'environments': g['environments']},
                    'expires': g['expires'], 'preauth_file': a.preauth.as_posix()}
        else:
            reason = f'no grant in {a.preauth.as_posix()} covers {r["suite"]} on {r["environment"]} on {today}'
    else:
        reason = 'no pre-authorisation and no confirmation'
    if r['protected'] and auth and auth['kind'] != 'confirmation':
        auth, reason = None, f'{r["environment"]} is protected: confirm this run with --confirmed-by'
    if not auth:
        d['authorisations'].append({'at': now(), 'decision': 'refused', 'reason': reason})
        d['trigger'] = {'sent': False, 'refused_reason': reason, 'at': now()}
        save(a.run, d)
        print(f'NOT TRIGGERED: {reason}\nThe request would be:\n  {d["plan"]["method"]} {d["plan"]["url"]}\n  {json.dumps(d["plan"]["body"])}')
        return 2
    d['authorisations'].append(dict(auth, decision='sent'))
    sent_at = now()
    code, body = call(d['plan']['method'], d['plan']['url'], r['system'], d['plan']['body'])
    d['trigger'] = {'sent': True, 'method': d['plan']['method'], 'url': d['plan']['url'], 'body': d['plan']['body'], 'status': code, 'at': sent_at}
    if code >= 300:
        d['outcome'] = {'status': 'not_started', 'conclusion': None, 'reason': f'trigger answered {code}: {(body or {}).get("message")}'}
        save(a.run, d)
        print(f'trigger answered {code}'); return 1
    if r['system'] == 'github':
        cat = json.loads(Path(d['catalogue']).read_text(encoding='utf-8'))['suites'][r['suite']]
        base = d['plan']['url'].split('/actions/workflows/')[0]
        run = None
        for _ in range(10):
            _, lst = call('GET', f'{base}/actions/runs?event=workflow_dispatch&branch={r["ref"]}&per_page=20', 'github')
            cands = [x for x in (lst or {}).get('workflow_runs', []) if x.get('created_at', '') >= sent_at[:19] and x.get('path', '').endswith(cat['workflow'])]
            if cands:
                run = max(cands, key=lambda x: x['created_at'])
                break
            time.sleep(2)
        if not run:
            d['outcome'] = {'status': 'not_started', 'conclusion': None, 'reason': 'dispatched, but no new run appeared on the ref within 20 seconds'}
            save(a.run, d); return 1
        d['run'] = {'id': run['id'], 'url': run.get('html_url'), 'attempt': run.get('run_attempt', 1), 'found_by': 'newest workflow_dispatch run on the ref created after the trigger'}
    else:
        d['run'] = {'id': body['id'], 'url': (body.get('_links') or {}).get('web', {}).get('href'), 'attempt': 1, 'found_by': 'returned by the trigger'}
    d['outcome'] = {'status': 'in_progress', 'conclusion': None}
    save(a.run, d)
    print(f'triggered {r["suite"]} on {r["environment"]} ({auth["kind"]} by {auth["by"]}): run {d["run"]["id"]} {d["run"]["url"]}')
    return 0


def status_of(d: dict) -> tuple[str, str | None]:
    r = d['request']
    if r['system'] == 'github':
        base = d['plan']['url'].split('/actions/workflows/')[0]
        _, x = call('GET', f'{base}/actions/runs/{d["run"]["id"]}', 'github')
        return x['status'], x.get('conclusion')
    base = d['plan']['url'].split('/runs?')[0]
    _, x = call('GET', f'{base}/runs/{d["run"]["id"]}?api-version=7.1', 'azure')
    st = {'inProgress': 'in_progress', 'completed': 'completed', 'canceling': 'in_progress', 'unknown': 'queued'}.get(x['state'], x['state'])
    return st, {'succeeded': 'success', 'failed': 'failure', 'canceled': 'cancelled'}.get(x.get('result'), x.get('result'))


def cmd_poll(a) -> int:
    d = load(a.run)
    if not d.get('run'):
        raise SystemExit('nothing to poll: the run was not triggered')
    wait, deadline, last = float(a.poll_seconds), time.time() + a.timeout_minutes * 60, None
    while True:
        st, concl = status_of(d)
        if st != last:
            d['polling'].append({'at': now(), 'status': st, 'conclusion': concl})
            last = st
            save(a.run, d)
            print(f'{now()} {st}{" " + concl if concl else ""}', flush=True)
        if st == 'completed':
            d['outcome'] = {'status': 'completed', 'conclusion': concl}
            break
        if time.time() > deadline:
            d['outcome'] = {'status': 'timed_out', 'conclusion': None, 'reason': f'not completed after {a.timeout_minutes} minutes; poll again later'}
            break
        time.sleep(wait)
        wait = min(wait * 1.5, 120)
    save(a.run, d)
    return 0 if d['outcome']['status'] == 'completed' else 3


def cmd_collect(a) -> int:
    d = load(a.run)
    if d['outcome']['status'] != 'completed':
        raise SystemExit(f'the run is {d["outcome"]["status"]}: poll until it completes before collecting')
    r = d['request']
    cat = json.loads(Path(d['catalogue']).read_text(encoding='utf-8'))['suites'][r['suite']]
    want = cat.get('junit_artifacts', ['junit']) + (a.download or [])
    out = a.run / 'artifacts'
    jobs, arts = [], []
    if r['system'] == 'github':
        base = d['plan']['url'].split('/actions/workflows/')[0]
        _, js = call('GET', f'{base}/actions/runs/{d["run"]["id"]}/jobs', 'github')
        jobs = [{'id': j['id'], 'name': j['name'], 'conclusion': j.get('conclusion')} for j in js.get('jobs', [])]
        _, al = call('GET', f'{base}/actions/runs/{d["run"]["id"]}/artifacts', 'github')
        arts = [{'name': x['name'], 'size': x.get('size_in_bytes'), 'url': x['archive_download_url']} for x in al.get('artifacts', [])]
        log_url = lambda j: f'{base}/actions/jobs/{j["id"]}/logs'  # noqa: E731
    else:
        org_proj = d['plan']['url'].split('/_apis/')[0]
        _, tl = call('GET', f'{org_proj}/_apis/build/builds/{d["run"]["id"]}/timeline?api-version=7.1', 'azure')
        jobs = [{'id': x['id'], 'name': x['name'], 'conclusion': {'succeeded': 'success', 'failed': 'failure'}.get(x.get('result'), x.get('result')), 'log_id': (x.get('log') or {}).get('id')}
                for x in tl.get('records', []) if x.get('type') == 'Job']
        _, al = call('GET', f'{org_proj}/_apis/build/builds/{d["run"]["id"]}/artifacts?api-version=7.1', 'azure')
        arts = [{'name': x['name'], 'size': None, 'url': x['resource']['downloadUrl']} for x in al.get('value', [])]
        log_url = lambda j: f'{org_proj}/_apis/build/builds/{d["run"]["id"]}/logs/{j["log_id"]}?api-version=7.1'  # noqa: E731
    junit = []
    for x in arts:
        if any(fnmatch.fnmatch(x['name'], p) for p in want):
            code, data = call('GET', x['url'], r['system'], raw=True)
            dest = out / x['name']
            if code == 200:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for n in z.namelist():
                        p = dest / Path(n).name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(z.read(n))
                        if p.suffix == '.xml' and x['name'] in cat.get('junit_artifacts', ['junit']):
                            junit.append(p.relative_to(a.run).as_posix())
                x['downloaded_to'] = dest.relative_to(a.run).as_posix()
        x.pop('url', None)
    d['artifacts'] = arts
    cases = []
    for j in junit:
        cases += parse_junit(a.run / j, 'R')[0]
    if junit:
        tot = {'tests': len(cases), 'passed': sum(c['status'] == 'passed' for c in cases), 'failed': sum(c['status'] == 'failed' for c in cases),
               'errors': sum(c['status'] == 'error' for c in cases), 'skipped': sum(c['status'] == 'skipped' for c in cases)}
        d['results'] = {'status': 'collected', 'junit': junit, 'totals': tot,
                        'failed_tests': [{'id': c['id'], 'status': c['status'], 'message': (c.get('message_excerpt') or '').split('\n')[0][:200]} for c in cases if c['status'] in ('failed', 'error')]}
    else:
        d['results'] = {'status': 'not_collected', 'reason': 'no JUnit artifact in the run', 'junit': [], 'totals': None, 'failed_tests': []}
    d['jobs'] = [{k: v for k, v in j.items() if k != 'log_id'} for j in jobs]
    d['logs'] = []
    for j in jobs:
        if j['conclusion'] not in ('success', None):
            code, data = call('GET', log_url(j), r['system'], raw=True)
            if code == 200:
                p = a.run / 'logs' / f'{j["name"]}.txt'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                d['logs'].append({'job': j['name'], 'path': p.relative_to(a.run).as_posix()})
    save(a.run, d)
    t = d['results']['totals']
    print(f'{d["outcome"]["conclusion"]}: ' + (f'{t["passed"]} of {t["tests"]} passed, {t["failed"]} failed, {t["errors"]} errors, {t["skipped"]} skipped' if t else 'no JUnit')
          + f'; {len(arts)} artifact(s), {len(d["logs"])} failed-job log(s)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan'); p.add_argument('--catalogue', type=Path, required=True); p.add_argument('--suite', required=True)
    p.add_argument('--environment', required=True); p.add_argument('--tags'); p.add_argument('--ref'); p.add_argument('--out', type=Path, required=True); p.add_argument('--api-base')
    t = sub.add_parser('trigger'); t.add_argument('--run', type=Path, required=True); t.add_argument('--preauth', type=Path); t.add_argument('--confirmed-by')
    o = sub.add_parser('poll'); o.add_argument('--run', type=Path, required=True); o.add_argument('--timeout-minutes', type=float, default=60); o.add_argument('--poll-seconds', type=float, default=20)
    c = sub.add_parser('collect'); c.add_argument('--run', type=Path, required=True); c.add_argument('--download', action='append')
    a = ap.parse_args()
    return {'plan': cmd_plan, 'trigger': cmd_trigger, 'poll': cmd_poll, 'collect': cmd_collect}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
