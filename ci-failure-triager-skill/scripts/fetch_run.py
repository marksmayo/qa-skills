#!/usr/bin/env python3
"""Fetch a CI run (GitHub Actions or Azure DevOps) and normalise it to one shape (stdlib only).

    python fetch_run.py --system github --repo owner/name --run-id 123 --out run/
    python fetch_run.py --system azure --org myorg --project proj --build-id 456 --out run/
    python fetch_run.py --system github --from-dir recorded/github --out run/     # offline, recorded API responses
    python fetch_run.py --self-test                                                # redirect and failure handling, on loopback

Live GitHub uses the REST API with a token from GITHUB_TOKEN or GH_TOKEN (or `gh auth token`); live
Azure DevOps uses its REST API with a PAT from AZURE_DEVOPS_EXT_PAT. Only GET requests are made.
Redirects are followed by hand rules: the Authorization header is sent only to the host it was given for
(GitHub answers log and artifact downloads with a 302 to signed blob storage, which rejects a second
credential), and an https URL is never followed to plain http except on loopback. The responses are saved
under <out>/raw/ in the same layout a recorded directory uses, then normalised:

  github: run.json, attempts/<n>/run.json, attempts/<n>/jobs.json, attempts/<n>/artifacts.json,
          attempts/<n>/artifacts/<name>/*.xml, logs/<job id>.txt (or logs/<job id>.error.json),
          history.json (runs of the same workflow on the branch, started before this run, and runs of the
          same commit), history_jobs/<run id>.json (the jobs of every other run of the same commit)
  azure:  build.json, timeline.json, logs/<log id>.txt, artifacts.json, artifacts/<name>/*.xml,
          history.json (builds of the same definition on the branch), history_jobs/<build id>.json

A log or artifact that could not be downloaded is never a silent gap: it is recorded with its HTTP status
(`unavailable` for 404/410, expired or deleted; `fetch_failed` for anything else, such as 401/403 or a
network error), printed as a warning, and listed in run.json `fetch_problems`.

<out>/run.json: {system, repo, repo_api, source, run{id, url, attempt, commit, branch, event, started_at,
conclusion, workflow, workflow_id}, attempts[{n, triggered_by, jobs_rerun, jobs[{id, name, conclusion, url,
started_at, steps[{number, name, conclusion}], log, log_status, log_error, junit[]}]}], history[{id, commit,
conclusion, started_at, event, workflow_id, jobs}], history_scope, last_green{id, commit, event, basis},
fetch_problems[]}. Logs and JUnit are copied into <out>/logs and <out>/junit, so the run directory is
self-contained.
"""
from __future__ import annotations
import argparse, base64, io, ipaddress, json, os, shutil, subprocess, sys, urllib.error, urllib.request, zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

GH_API = 'https://api.github.com'
CREDENTIAL_HEADERS = ('authorization', 'cookie', 'proxy-authorization')


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin(url: str) -> tuple:
    u = urlsplit(url)
    return (u.scheme, (u.hostname or '').lower(), u.port or (443 if u.scheme == 'https' else 80))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send credentials only to the origin they were given for.

    urllib copies every header onto the redirected request, so a bearer token for api.github.com would be
    sent to the storage host a log download redirects to (which then rejects the request, and should never
    see the token). On any change of scheme, host or port the credential headers are dropped; an https URL
    is never followed to plain http unless both ends are loopback.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old, nu = _origin(req.full_url), _origin(newurl)
        if old[0] == 'https' and nu[0] != 'https' and not (_loopback(old[1]) and _loopback(nu[1])):
            raise urllib.error.HTTPError(newurl, code, 'refusing a redirect from https to plain http', headers, fp)
        if old != nu:
            for k in list(new.headers):
                if k.lower() in CREDENTIAL_HEADERS:
                    del new.headers[k]
            for k in list(new.unredirected_hdrs):
                if k.lower() in CREDENTIAL_HEADERS:
                    del new.unredirected_hdrs[k]
        return new


OPENER = urllib.request.build_opener(SafeRedirect)


def _get(url: str, headers: dict, binary: bool = False):
    req = urllib.request.Request(url, headers=headers)
    with OPENER.open(req, timeout=60) as r:
        data = r.read()
    return data if binary else json.loads(data.decode('utf-8')) if data[:1] in (b'{', b'[') else data.decode('utf-8', 'replace')


def describe_error(e: BaseException) -> dict:
    """A fetch failure as {status, http_status, error}; never includes a URL (signed URLs carry keys)."""
    if isinstance(e, urllib.error.HTTPError):
        st = 'unavailable' if e.code in (404, 410) else 'fetch_failed'
        why = {404: 'not found: expired, deleted or not visible to this token', 410: 'gone: expired'}.get(e.code, e.reason)
        return {'status': st, 'http_status': e.code, 'error': f'HTTP {e.code}: {why}'}
    if isinstance(e, urllib.error.URLError):
        return {'status': 'fetch_failed', 'http_status': None, 'error': f'network error: {e.reason}'}
    return {'status': 'fetch_failed', 'http_status': None, 'error': f'{type(e).__name__}: {e}'}


def warn(msg: str):
    print(f'WARNING: {msg}', file=sys.stderr)


def gh_token() -> str:
    t = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not t and shutil.which('gh'):
        t = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True).stdout.strip()
    if not t:
        raise SystemExit('no GitHub token: set GITHUB_TOKEN or GH_TOKEN, or sign in with gh auth login')
    return t


def save(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (bytes, bytearray)):
        p.write_bytes(obj)
    elif isinstance(obj, str):
        p.write_text(obj, encoding='utf-8', newline='\n')
    else:
        p.write_text(json.dumps(obj, indent=1) + '\n', encoding='utf-8', newline='\n')


def unzip_xml(data: bytes, dest: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.lower().endswith('.xml'):
                save(dest / Path(n).name, z.read(n))


def fetch_log(url: str, h: dict, raw: Path, key) -> None:
    try:
        save(raw / 'logs' / f'{key}.txt', _get(url, h))
    except Exception as e:  # noqa: BLE001 - every failure is recorded, never swallowed
        d = describe_error(e)
        save(raw / 'logs' / f'{key}.error.json', d)
        warn(f'log {key} not downloaded ({d["status"]}): {d["error"]}')


def fetch_artifact(url: str, h: dict, dest: Path, raw: Path, name: str, size: int | None, max_mb: int) -> None:
    if size and size > max_mb * 1024 * 1024:
        d = {'status': 'skipped', 'http_status': None, 'error': f'{size // (1024 * 1024)} MB, over --max-artifact-mb {max_mb}'}
    else:
        try:
            unzip_xml(_get(url, h, binary=True), dest)
            return
        except Exception as e:  # noqa: BLE001
            d = describe_error(e)
    d['artifact'] = name
    save(raw / 'artifact_errors' / f'{name}.json', d)
    warn(f'artifact {name} not downloaded ({d["status"]}): {d["error"]}')


def fetch_github(repo: str, run_id: int, raw: Path, history: int, max_mb: int):
    h = {'Authorization': f'Bearer {gh_token()}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    run = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}', h)
    save(raw / 'run.json', run)
    last = int(run.get('run_attempt') or 1)
    for n in range(1, last + 1):
        ad = raw / 'attempts' / str(n)
        try:
            save(ad / 'run.json', _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/attempts/{n}', h))
        except Exception as e:  # noqa: BLE001 - who triggered an attempt is useful, not essential
            warn(f'attempt {n} metadata not downloaded: {describe_error(e)["error"]}')
        jobs = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/attempts/{n}/jobs?per_page=100', h)
        save(ad / 'jobs.json', jobs)
        for j in jobs['jobs']:
            if j.get('conclusion') not in ('success', 'skipped'):
                fetch_log(f'{GH_API}/repos/{repo}/actions/jobs/{j["id"]}/logs', h, raw, j['id'])
    arts = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100', h)
    save(raw / 'attempts' / str(last) / 'artifacts.json', arts)
    for a in arts.get('artifacts', []):
        if not a.get('expired'):
            fetch_artifact(a['archive_download_url'], h, raw / 'attempts' / str(last) / 'artifacts' / a['name'], raw, a['name'],
                           a.get('size_in_bytes'), max_mb)
    # history: the same workflow, on the branch, created up to this run; and every run of the same commit
    wid, created = run.get('workflow_id'), run.get('created_at') or run.get('run_started_at')
    base = f'{GH_API}/repos/{repo}/actions/workflows/{wid}/runs' if wid else f'{GH_API}/repos/{repo}/actions/runs'
    q_branch = f'{base}?branch={quote(run["head_branch"] or "", safe="")}&created={quote("<=" + (created or ""), safe="")}&per_page={history}'
    q_commit = f'{base}?head_sha={run["head_sha"]}&per_page=50'
    runs: dict = {}
    for q in (q_branch, q_commit):
        for r in _get(q, h).get('workflow_runs', []):
            runs[r['id']] = r
    save(raw / 'history.json', {'query': {'workflow_id': wid, 'branch': run['head_branch'], 'created_before': created, 'head_sha': run['head_sha']},
                                'workflow_runs': sorted(runs.values(), key=lambda r: r.get('run_started_at') or '', reverse=True)})
    for r in runs.values():
        if r['head_sha'] == run['head_sha'] and r['id'] != run['id']:
            try:
                save(raw / 'history_jobs' / f'{r["id"]}.json', _get(f'{GH_API}/repos/{repo}/actions/runs/{r["id"]}/jobs?filter=latest&per_page=100', h))
            except Exception as e:  # noqa: BLE001
                warn(f'jobs of history run {r["id"]} not downloaded: {describe_error(e)["error"]}')


def fetch_azure(org: str, project: str, build_id: int, raw: Path, history: int, max_mb: int):
    pat = os.environ.get('AZURE_DEVOPS_EXT_PAT')
    if not pat:
        raise SystemExit('no Azure DevOps PAT: set AZURE_DEVOPS_EXT_PAT')
    h = {'Authorization': 'Basic ' + base64.b64encode(f':{pat}'.encode()).decode(), 'Accept': 'application/json'}
    base = f'https://dev.azure.com/{org}/{project}/_apis/build/builds'
    b = _get(f'{base}/{build_id}?api-version=7.1', h)
    save(raw / 'build.json', b)
    tl = _get(f'{base}/{build_id}/timeline?api-version=7.1', h)
    save(raw / 'timeline.json', tl)
    for r in tl.get('records', []):
        if r.get('log') and r.get('result') not in ('succeeded', 'skipped'):
            fetch_log(f'{base}/{build_id}/logs/{r["log"]["id"]}?api-version=7.1', h, raw, r['log']['id'])
    arts = _get(f'{base}/{build_id}/artifacts?api-version=7.1', h)
    save(raw / 'artifacts.json', arts)
    for a in arts.get('value', []):
        url = a.get('resource', {}).get('downloadUrl')
        if url:
            fetch_artifact(url, h, raw / 'artifacts' / a['name'], raw, a['name'], None, max_mb)
    did = (b.get('definition') or {}).get('id')
    q = (f'{base}?branchName={quote(b["sourceBranch"], safe="")}&queryOrder=startTimeDescending&maxTime={quote(b.get("startTime") or "", safe="")}'
         f'&$top={history}&api-version=7.1' + (f'&definitions={did}' if did else ''))
    hist = _get(q, h)
    hist['query'] = {'definition_id': did, 'branch': b['sourceBranch'], 'started_before': b.get('startTime')}
    save(raw / 'history.json', hist)
    for r in hist.get('value', []):
        if r.get('sourceVersion') == b['sourceVersion'] and r['id'] != b['id']:
            try:
                save(raw / 'history_jobs' / f'{r["id"]}.json', _get(f'{base}/{r["id"]}/timeline?api-version=7.1', h))
            except Exception as e:  # noqa: BLE001
                warn(f'timeline of history build {r["id"]} not downloaded: {describe_error(e)["error"]}')


def junit_for(job_name: str, art_dir: Path) -> list[Path]:
    if not art_dir.exists():
        return []
    out = []
    for d in sorted(p for p in art_dir.iterdir() if p.is_dir()):
        if job_name.lower() in d.name.lower():
            out += sorted(d.glob('*.xml'))
    return out


def log_state(raw: Path, key) -> tuple[str, str | None]:
    """(log_status, log_error) for one recorded log: fetched, unavailable, fetch_failed or not_requested."""
    if (raw / 'logs' / f'{key}.txt').exists():
        return 'fetched', None
    err = raw / 'logs' / f'{key}.error.json'
    if err.exists():
        d = json.loads(err.read_text(encoding='utf-8'))
        return d.get('status', 'fetch_failed'), d.get('error')
    old = raw / 'logs' / f'{key}.missing'  # recordings made before 1.1.0
    if old.exists():
        t = old.read_text(encoding='utf-8').strip()
        return ('fetch_failed' if any(f'Error {c}' in t or f'HTTP {c}' in t for c in (401, 403)) else 'unavailable'), t[:300]
    return 'not_requested', None


def _jobs_rerun(prev: list[dict] | None, cur: list[dict]) -> list[str] | None:
    """Jobs that ran again in an attempt: GitHub lists every job in a later attempt, carrying over the
    started_at of jobs it did not re-run."""
    if prev is None:
        return None
    before = {j['name']: j.get('started_at') for j in prev}
    return [j['name'] for j in cur if j['name'] not in before or (j.get('started_at') and j.get('started_at') != before[j['name']])]


def _pick_green(history: list[dict], started: str | None, event: str | None, failed_jobs: set | None = None) -> dict | None:
    def ran_them(r):  # a run whose jobs are known must have run (and passed) the jobs that failed here
        if not failed_jobs or r.get('jobs') is None:
            return True
        ok = {j['name'] for j in r['jobs'] if j['conclusion'] == 'success'}
        return failed_jobs <= ok
    cands = sorted((r for r in history if r['conclusion'] == 'success' and (r['started_at'] or '') < (started or '~') and ran_them(r)),
                   key=lambda r: r['started_at'] or '', reverse=True)
    same_event = [r for r in cands if event and r.get('event') == event]
    g = (same_event or cands or [None])[0]
    if not g:
        return None
    return {'id': g['id'], 'commit': g['commit'], 'event': g.get('event'),
            'basis': 'the newest successful run of the same workflow on the branch started before this run'
                     + (', same event' if g in same_event else ', another event (none of the same event)')}


def normalise_github(raw: Path, out: Path, repo: str | None) -> dict:
    run = json.loads((raw / 'run.json').read_text(encoding='utf-8'))
    attempts, problems, prev = [], [], None
    for ad in sorted((raw / 'attempts').iterdir(), key=lambda p: int(p.name)):
        n = int(ad.name)
        raw_jobs = json.loads((ad / 'jobs.json').read_text(encoding='utf-8'))['jobs']
        jobs = []
        for j in raw_jobs:
            log = raw / 'logs' / f'{j["id"]}.txt'
            dest_log = out / 'logs' / f'{j["id"]}.txt'
            status, err = log_state(raw, j['id'])
            if status == 'fetched':
                save(dest_log, log.read_text(encoding='utf-8'))
            elif j.get('conclusion') not in ('success', 'skipped', None):
                problems.append({'kind': 'log', 'attempt': n, 'job': j['name'], 'job_id': j['id'], 'status': status, 'error': err})
            junit = []
            for x in junit_for(j['name'], ad / 'artifacts'):
                d = out / 'junit' / str(n) / x.parent.name / x.name
                save(d, x.read_bytes())
                junit.append(d.relative_to(out).as_posix())
            jobs.append({'id': j['id'], 'name': j['name'], 'conclusion': j.get('conclusion'), 'url': j.get('html_url'), 'started_at': j.get('started_at'),
                         'steps': [{'number': s['number'], 'name': s['name'], 'conclusion': s.get('conclusion')} for s in j.get('steps', [])],
                         'log': dest_log.relative_to(out).as_posix() if status == 'fetched' else None, 'log_status': status, 'log_error': err,
                         'junit': junit})
        starts = sorted(j.get('started_at') for j in raw_jobs if j.get('started_at'))
        meta = json.loads((ad / 'run.json').read_text(encoding='utf-8')) if (ad / 'run.json').exists() else (run if n == int(run.get('run_attempt') or 1) else {})
        actor = (meta.get('triggering_actor') or {}).get('login')
        attempts.append({'n': n, 'started_at': starts[0] if starts else None, 'triggered_by': actor,
                         'jobs_rerun': _jobs_rerun(prev, raw_jobs), 'jobs': jobs})
        prev = raw_jobs
    for e in sorted((raw / 'artifact_errors').glob('*.json')) if (raw / 'artifact_errors').exists() else []:
        d = json.loads(e.read_text(encoding='utf-8'))
        problems.append({'kind': 'artifact', 'artifact': d.get('artifact'), 'status': d.get('status'), 'error': d.get('error')})
    hist = json.loads((raw / 'history.json').read_text(encoding='utf-8')).get('workflow_runs', []) if (raw / 'history.json').exists() else []
    wid, wpath = run.get('workflow_id'), run.get('path')
    same_wf = lambda r: (wid is not None and r.get('workflow_id') == wid) or (wpath is not None and r.get('path') == wpath)  # noqa: E731
    history, other = [], 0
    for r in hist:
        if r['id'] == run['id']:
            continue
        if not same_wf(r):
            other += 1
            continue
        hj = raw / 'history_jobs' / f'{r["id"]}.json'
        jobs = ([{'name': j['name'], 'conclusion': j.get('conclusion')} for j in json.loads(hj.read_text(encoding='utf-8'))['jobs']]
                if hj.exists() else None)
        history.append({'id': r['id'], 'commit': r['head_sha'], 'conclusion': r.get('conclusion'), 'started_at': r.get('run_started_at'),
                        'event': r.get('event'), 'workflow_id': r.get('workflow_id'), 'jobs': jobs})
    if hist and not (wid or wpath):
        warn('the run names no workflow, so no history run can be matched to it: history is empty')
    elif other:
        print(f'note: {other} history run(s) of other workflows ignored', file=sys.stderr)
    api_repo = run.get('repository', {}).get('full_name')
    out_run = {'system': 'github', 'repo': repo or api_repo,
               'run': {'id': run['id'], 'url': run.get('html_url'), 'attempt': int(run.get('run_attempt') or 1), 'commit': run['head_sha'],
                       'branch': run.get('head_branch'), 'event': run.get('event'), 'started_at': run.get('run_started_at'),
                       'conclusion': run.get('conclusion'), 'workflow': run.get('path') or run.get('name'), 'workflow_id': wid},
               'attempts': attempts, 'history': history, 'history_scope': 'same workflow' if (wid or wpath) else 'unknown workflow',
               'last_green': _pick_green(history, run.get('created_at') or run.get('run_started_at'), run.get('event'),
                                         {j['name'] for j in attempts[0]['jobs'] if j['conclusion'] not in ('success', 'skipped', None)} if attempts else None), 'fetch_problems': problems}
    if repo and api_repo and api_repo.lower() != repo.lower():
        out_run['repo_api'] = api_repo
    return out_run


AZ_RES = {'succeeded': 'success', 'failed': 'failure', 'canceled': 'cancelled', 'skipped': 'skipped', 'succeededWithIssues': 'success', None: None}


def normalise_azure(raw: Path, out: Path, repo: str | None) -> dict:
    b = json.loads((raw / 'build.json').read_text(encoding='utf-8'))
    recs = json.loads((raw / 'timeline.json').read_text(encoding='utf-8'))['records']
    res = AZ_RES
    by_attempt: dict = {}
    problems = []
    for j in [r for r in recs if r['type'] == 'Job']:
        tasks = sorted((r for r in recs if r['type'] == 'Task' and r.get('parentId') == j['id']), key=lambda r: r.get('order', 0))
        states = {t['log']['id']: log_state(raw, t['log']['id']) for t in tasks if t.get('log')}
        text = ''.join((raw / 'logs' / f'{k}.txt').read_text(encoding='utf-8') for k, (st, _) in states.items() if st == 'fetched')
        bad = [(k, st, er) for k, (st, er) in states.items() if st not in ('fetched', 'not_requested')]
        status = 'fetched' if text and not bad else 'partial' if text else bad[0][1] if bad else 'not_requested'
        err = '; '.join(f'log {k}: {er}' for k, _, er in bad) or None
        if bad and res.get(j.get('result'), j.get('result')) not in ('success', 'skipped', None):
            problems.append({'kind': 'log', 'attempt': j.get('attempt', 1), 'job': j['name'], 'job_id': j['id'], 'status': status, 'error': err})
        dest_log = out / 'logs' / f'{j["id"]}.txt'
        if text:
            save(dest_log, text)
        junit = []
        for x in junit_for(j['name'], raw / 'artifacts'):
            d = out / 'junit' / str(j.get('attempt', 1)) / x.parent.name / x.name
            save(d, x.read_bytes())
            junit.append(d.relative_to(out).as_posix())
        by_attempt.setdefault(j.get('attempt', 1), []).append({
            'id': j['id'], 'name': j['name'], 'conclusion': res.get(j.get('result'), j.get('result')), 'url': b.get('_links', {}).get('web', {}).get('href'),
            'started_at': j.get('startTime'),
            'steps': [{'number': i + 1, 'name': t['name'], 'conclusion': res.get(t.get('result'), t.get('result'))} for i, t in enumerate(tasks)],
            'log': dest_log.relative_to(out).as_posix() if text else None, 'log_status': status, 'log_error': err, 'junit': junit})
    for e in sorted((raw / 'artifact_errors').glob('*.json')) if (raw / 'artifact_errors').exists() else []:
        d = json.loads(e.read_text(encoding='utf-8'))
        problems.append({'kind': 'artifact', 'artifact': d.get('artifact'), 'status': d.get('status'), 'error': d.get('error')})
    hist_doc = json.loads((raw / 'history.json').read_text(encoding='utf-8')) if (raw / 'history.json').exists() else {}
    did = (b.get('definition') or {}).get('id')
    history, other = [], 0
    for r in hist_doc.get('value', []):
        if r['id'] == b['id']:
            continue
        if did is None or (r.get('definition') or {}).get('id') != did:
            other += 1
            continue
        hj = raw / 'history_jobs' / f'{r["id"]}.json'
        jobs = ([{'name': x['name'], 'conclusion': res.get(x.get('result'), x.get('result'))} for x in json.loads(hj.read_text(encoding='utf-8'))['records'] if x['type'] == 'Job']
                if hj.exists() else None)
        history.append({'id': r['id'], 'commit': r['sourceVersion'], 'conclusion': res.get(r.get('result'), r.get('result')), 'started_at': r.get('startTime'),
                        'event': r.get('reason'), 'workflow_id': did, 'jobs': jobs})
    if other:
        print(f'note: {other} history build(s) of other definitions (or naming none) ignored', file=sys.stderr)
    atts, prev = [], None
    for n, js in sorted(by_attempt.items()):
        atts.append({'n': n, 'triggered_by': None, 'jobs_rerun': _jobs_rerun(prev, js) if prev is not None else None, 'jobs': js})
        prev = js
    return {'system': 'azure', 'repo': repo or b.get('repository', {}).get('id'),
            'run': {'id': b['id'], 'url': b.get('_links', {}).get('web', {}).get('href'), 'attempt': max(by_attempt) if by_attempt else 1,
                    'commit': b['sourceVersion'], 'branch': (b.get('sourceBranch') or '').removeprefix('refs/heads/'), 'event': b.get('reason'),
                    'started_at': b.get('startTime'), 'conclusion': res.get(b.get('result'), b.get('result')), 'workflow': b.get('definition', {}).get('name'),
                    'workflow_id': did},
            'attempts': atts, 'history': history, 'history_scope': 'same workflow' if did is not None else 'unknown workflow',
            'last_green': _pick_green(history, b.get('startTime'), b.get('reason'),
                                      {j['name'] for j in atts[0]['jobs'] if j['conclusion'] not in ('success', 'skipped', None)} if atts else None), 'fetch_problems': problems}


# --- self-test -------------------------------------------------------------------------------------

def self_test() -> int:
    """Two loopback servers: an "API" that redirects downloads to a "storage" host, which rejects any
    Authorization header (as signed blob storage does). Checks the token is dropped on a cross-host
    redirect, kept on a same-host one, and that failed downloads are recorded loudly, never as success."""
    import http.server, tempfile, threading
    seen: dict = {}
    FAKE = 'Bearer fake-token-0000'

    class Storage(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen.setdefault('storage', []).append(self.headers.get('Authorization'))
            if self.headers.get('Authorization'):
                self.send_response(401); self.end_headers(); self.wfile.write(b'Server failed to authenticate the request'); return
            if self.path.startswith('/blob/log'):
                body = b'2026-01-01T00:00:00.0000000Z ##[group]Run make check\n2026-01-01T00:00:00.0000001Z ##[error]Process completed with exit code 2.\n'
            elif self.path.startswith('/blob/zip'):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w') as z:
                    z.writestr('results/junit.xml', '<testsuite name="s" tests="1"><testcase classname="a" name="b"/></testsuite>')
                body = buf.getvalue()
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.end_headers(); self.wfile.write(body)

    st = http.server.HTTPServer(('127.0.0.1', 0), Storage)
    sport = st.server_address[1]

    class Api(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen.setdefault('api', []).append((self.path, self.headers.get('Authorization')))
            routes = {'/jobs/1/logs': f'http://localhost:{sport}/blob/log?sig=fake', '/artifacts/1/zip': f'http://localhost:{sport}/blob/zip?sig=fake',
                      '/jobs/2/logs': '/moved/2/logs'}
            if self.path in routes:
                self.send_response(302); self.send_header('Location', routes[self.path]); self.end_headers(); return
            if self.path == '/moved/2/logs':
                ok = self.headers.get('Authorization') == FAKE
                self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b'same-host log' if ok else b''); return
            if self.path == '/jobs/3/logs':
                self.send_response(410); self.end_headers(); return
            if self.path == '/jobs/4/logs':
                self.send_response(302); self.send_header('Location', f'http://localhost:{sport}/blob/log'); self.end_headers(); return
            self.send_response(404); self.end_headers()

    api = http.server.HTTPServer(('127.0.0.1', 0), Api)
    aport = api.server_address[1]
    for s in (st, api):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    fails = []
    check = lambda cond, what: None if cond else fails.append(what)  # noqa: E731
    h = {'Authorization': FAKE}
    base = f'http://127.0.0.1:{aport}'
    try:
        txt = _get(f'{base}/jobs/1/logs', h)
        check('##[error]Process completed' in txt, 'cross-host redirect: log body not downloaded')
        check(all(a is None for a in seen.get('storage', [])), 'cross-host redirect: Authorization was forwarded to the storage host')
        z = _get(f'{base}/artifacts/1/zip', h, binary=True)
        check(z[:2] == b'PK', 'cross-host redirect: artifact zip not downloaded')
        check(_get(f'{base}/jobs/2/logs', h) == 'same-host log', 'same-host redirect: Authorization was not kept')
        # the unfixed behaviour: a plain urllib opener forwards the header and the storage host rejects it
        seen['storage'] = []
        try:
            urllib.request.build_opener().open(urllib.request.Request(f'{base}/jobs/1/logs', headers=h), timeout=10).read()
            check(False, 'control: a default opener should have been rejected by the storage host')
        except urllib.error.HTTPError as e:
            check(e.code == 401 and seen['storage'] == [FAKE], 'control: default opener did not reproduce the 401')
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td)
            fetch_log(f'{base}/jobs/1/logs', h, raw, 1)
            fetch_log(f'{base}/jobs/3/logs', h, raw, 3)
            fetch_log(f'{base}/jobs/9/logs', {'Authorization': FAKE}, raw, 9)
            check(log_state(raw, 1)[0] == 'fetched', 'log 1 should be fetched')
            check(log_state(raw, 3)[0] == 'unavailable' and '410' in (log_state(raw, 3)[1] or ''), 'HTTP 410 should be recorded as unavailable')
            check(log_state(raw, 9)[0] == 'unavailable', 'HTTP 404 should be recorded as unavailable')
            # a storage host that rejects the request is fetch_failed, never a missing log
            plain = urllib.request.build_opener()
            globals()['OPENER'], saved = plain, OPENER
            try:
                fetch_log(f'{base}/jobs/1/logs', h, raw, 11)
            finally:
                globals()['OPENER'] = saved
            check(log_state(raw, 11)[0] == 'fetch_failed' and '401' in (log_state(raw, 11)[1] or ''), 'a 401 should be recorded as fetch_failed')
            check('sig=' not in (raw / 'logs' / '11.error.json').read_text(encoding='utf-8'), 'error record must not hold the signed URL')
            fetch_artifact(f'{base}/artifacts/404/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'gone', raw, 'gone', None, 50)
            check(any((raw / 'artifact_errors').glob('*.json')), 'a failed artifact download should be recorded, not crash')
            fetch_artifact(f'{base}/artifacts/1/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'junit-a', raw, 'junit-a', 10, 50)
            check((raw / 'attempts' / '1' / 'artifacts' / 'junit-a' / 'junit.xml').exists(), 'artifact xml not unzipped')
            fetch_artifact(f'{base}/artifacts/1/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'big', raw, 'big', 80 * 1024 * 1024, 50)
            check(not (raw / 'attempts' / '1' / 'artifacts' / 'big').exists(), 'an artifact over the size cap should be skipped')
        # https -> http downgrade is refused (checked on the handler directly, no TLS server needed)
        req = urllib.request.Request('https://api.example.test/x', headers=h)
        try:
            SafeRedirect().redirect_request(req, None, 302, 'Found', {}, 'http://storage.example.test/y')
            check(False, 'https -> http redirect should be refused')
        except urllib.error.HTTPError:
            pass
        new = SafeRedirect().redirect_request(req, None, 302, 'Found', {}, 'https://storage.example.test/y')
        check(new is not None and new.get_header('Authorization') is None, 'https cross-host: Authorization kept')
        new = SafeRedirect().redirect_request(req, None, 301, 'Moved', {}, 'https://api.example.test/repositories/1/x')
        check(new is not None and new.get_header('Authorization') == FAKE, 'https same-host: Authorization dropped')
    finally:
        st.shutdown(); api.shutdown()
    # history: other workflows ignored; last green is the same workflow, before the run
    with tempfile.TemporaryDirectory() as td:
        raw, out = Path(td) / 'raw', Path(td) / 'out'
        save(raw / 'run.json', {'id': 50, 'head_sha': 'c' * 40, 'head_branch': 'main', 'event': 'schedule', 'run_attempt': 2, 'workflow_id': 7,
                                'path': '.github/workflows/nightly.yml', 'run_started_at': '2026-01-05T00:00:00Z', 'repository': {'full_name': 'acme/renamed'},
                                'triggering_actor': {'login': 'someone'}})
        j = lambda i, name, c, t: {'id': i, 'name': name, 'conclusion': c, 'started_at': t, 'steps': []}  # noqa: E731
        save(raw / 'attempts' / '1' / 'jobs.json', {'jobs': [j(1, 'build', 'success', 'T1'), j(2, 'publish', 'failure', 'T1')]})
        save(raw / 'attempts' / '2' / 'jobs.json', {'jobs': [j(3, 'build', 'success', 'T1'), j(4, 'publish', 'success', 'T2')]})
        save(raw / 'logs' / '2.error.json', {'status': 'fetch_failed', 'http_status': 401, 'error': 'HTTP 401: Unauthorized'})
        wr = lambda i, sha, c, t, wid, ev='push': {'id': i, 'head_sha': sha, 'conclusion': c, 'run_started_at': t, 'workflow_id': wid, 'event': ev}  # noqa: E731
        save(raw / 'history.json', {'workflow_runs': [wr(51, 'c' * 40, 'success', '2026-01-05T00:00:01Z', 8),   # other workflow, same commit
                                                      wr(49, 'c' * 40, 'success', '2026-01-04T23:00:00Z', 7),   # same workflow, same commit, push
                                                      wr(48, 'b' * 40, 'success', '2026-01-04T00:00:00Z', 7, 'schedule'),
                                                      wr(47, 'a' * 40, 'success', '2026-01-03T00:00:00Z', 7)]})
        save(raw / 'history_jobs' / '49.json', {'jobs': [{'name': 'version', 'conclusion': 'success'}]})
        r = normalise_github(raw, out, 'acme/old-name')
        check([x['id'] for x in r['history']] == [49, 48, 47], f'history should hold only the same workflow: {[x["id"] for x in r["history"]]}')
        check(r['last_green'] and r['last_green']['id'] == 48, f'last green should be the same-event run 48, got {r["last_green"]}')
        check(r['attempts'][1]['jobs_rerun'] == ['publish'], f'jobs_rerun should be [publish], got {r["attempts"][1]["jobs_rerun"]}')
        check(r['attempts'][1]['triggered_by'] == 'someone', 'triggering actor not recorded')
        check(r['repo'] == 'acme/old-name' and r.get('repo_api') == 'acme/renamed', 'requested and API repository names not both kept')
        check(any(p['kind'] == 'log' and p['status'] == 'fetch_failed' for p in r['fetch_problems']), 'failed log fetch not in fetch_problems')
        pub = next(x for x in r['attempts'][0]['jobs'] if x['name'] == 'publish')
        check(pub['log'] is None and pub['log_status'] == 'fetch_failed', 'failed log fetch not on the job')
    if fails:
        print('fetch_run self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('fetch_run self-test: ok (cross-host redirect drops the token, same-host keeps it, https->http refused, '
          'failed log/artifact downloads recorded, history limited to the workflow, re-run jobs from started_at)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--system', choices=['github', 'azure'])
    ap.add_argument('--repo', help='github: owner/name')
    ap.add_argument('--run-id', type=int)
    ap.add_argument('--org'); ap.add_argument('--project'); ap.add_argument('--build-id', type=int)
    ap.add_argument('--from-dir', type=Path, help='a recorded directory of API responses (offline)')
    ap.add_argument('--history', type=int, default=20)
    ap.add_argument('--max-artifact-mb', type=int, default=50, help='skip (and record) larger artifacts; only their XML is kept')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.system and a.out):
        ap.error('--system and --out are required')
    a.out.mkdir(parents=True, exist_ok=True)
    if a.from_dir:
        raw, source = a.from_dir, 'recorded'
    else:
        raw, source = a.out / 'raw', 'live'
        if a.system == 'github':
            if not (a.repo and a.run_id):
                raise SystemExit('--repo and --run-id are needed for GitHub')
            fetch_github(a.repo, a.run_id, raw, a.history, a.max_artifact_mb)
        else:
            if not (a.org and a.project and a.build_id):
                raise SystemExit('--org, --project and --build-id are needed for Azure DevOps')
            fetch_azure(a.org, a.project, a.build_id, raw, a.history, a.max_artifact_mb)
    for d in ('logs', 'junit'):
        if (a.out / d).exists():
            shutil.rmtree(a.out / d)
    run = (normalise_github if a.system == 'github' else normalise_azure)(raw, a.out, a.repo or (f'{a.org}/{a.project}' if a.org else None))
    run['source'] = source
    run['fetched_from'] = Path(os.path.relpath(raw, a.out)).as_posix() if source == 'recorded' else 'raw'
    save(a.out / 'run.json', run)
    r = run['run']
    print(f'{run["system"]} run {r["id"]} attempt {r["attempt"]} on {r["branch"]} @ {r["commit"][:12]}: {r["conclusion"]}; '
          f'{sum(len(x["jobs"]) for x in run["attempts"])} job(s) over {len(run["attempts"])} attempt(s); '
          f'{len(run["history"])} history run(s) of the same workflow; last green {run["last_green"]} -> {a.out / "run.json"}')
    if run.get('repo_api'):
        print(f'note: requested {run["repo"]}, the API names it {run["repo_api"]} (renamed or transferred)')
    if run['attempts'] and not [j for j in run['attempts'][0]['jobs'] if j.get('conclusion') not in ('skipped', None)]:
        print(f'WARNING: the run concluded {r["conclusion"]} but no job ran (a workflow file error, a startup failure, or a fork '
              'pull request whose run was never approved): there is nothing to triage; the triage is run_status not_run', file=sys.stderr)
    if run['fetch_problems']:
        print(f'WARNING: {len(run["fetch_problems"])} download(s) failed; the triage must say so:', file=sys.stderr)
        for p in run['fetch_problems']:
            print(f'  {p["kind"]} {p.get("job") or p.get("artifact")}: {p["status"]} ({p.get("error")})', file=sys.stderr)
        return 2 if any(p['kind'] == 'log' and p['status'] == 'fetch_failed' for p in run['fetch_problems']) else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
