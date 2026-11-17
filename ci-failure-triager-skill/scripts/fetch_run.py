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
          history.json (runs of the same workflow on the branch and on the default branch, started before this
          run, and runs of the same commit), repository.json (the repository, read for its default branch: the
          repository object inside a workflow run is the minimal one and does not carry it),
          history_jobs/<run id>.json (the jobs of every other run of the same commit),
          same_commit_runs.json (only for a run in which no job ran: every workflow's runs of the commit),
          annotations/<job id>.json (the check-run annotations of every failed job: the CI's only
          explanation of a job that never started on a runner, such as a billing or spending-limit stop)
  azure:  build.json, timeline.json, logs/<log id>.txt, artifacts.json, artifacts/<name>/*.xml,
          history.json (builds of the same definition on the branch), history_jobs/<build id>.json

A log or artifact that could not be downloaded is never a silent gap: it is recorded with its HTTP status
(`unavailable` for 404/410, expired or deleted; `fetch_failed` for anything else, such as 401/403 or a
network error), printed as a warning, and listed in run.json `fetch_problems`.

<out>/run.json: {system, repo, repo_api, source, run{id, url, attempt, commit, branch, event, started_at,
conclusion, workflow, workflow_id}, attempts[{n, triggered_by, jobs_rerun, jobs[{id, name, conclusion, url,
started_at, steps[{number, name, conclusion}], log, log_status, log_error, junit[], never_started (no steps and no
runner: log_status never_started, no log exists), annotations[{level, title, message}]}]}], history[{id, commit,
conclusion, started_at, event, workflow_id, jobs}], history_scope, last_green{id, commit, event, basis},
history_query{..., default_branch, default_branch_source (option, run, repository or unknown)}, same_commit_runs[{id,
workflow, event, conclusion}] (a run with no job), fetch_problems[]}. Logs and JUnit are copied into <out>/logs and <out>/junit, so the run directory is
self-contained.
"""
from __future__ import annotations
import argparse, base64, io, json, os, shutil, subprocess, sys, urllib.error, urllib.request, zipfile
from pathlib import Path
from urllib.parse import quote

GH_API = 'https://api.github.com'


sys.path.insert(0, str(Path(__file__).resolve().parent))
# SafeRedirect (shared/scripts/safe_http.py): credentials are sent only to the origin they were given for; an https
# URL is never followed to plain http except on loopback. OPENER is module-level so the self-test can swap it.
from safe_http import CREDENTIAL_HEADERS, SafeRedirect, _loopback, origin as _origin  # noqa: E402,F401
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
        why = {404: 'not found: expired, deleted, never written (a job that never started) or not visible to this token',
               410: 'gone: expired'}.get(e.code, e.reason)
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


def fetch_annotations(api: str, repo: str, job_id, h: dict, raw: Path) -> None:
    """The check-run annotations of one GitHub job: the only explanation for a job that failed before a runner picked
    it up (a billing or spending-limit stop, no runner with the labels), which has no log at all."""
    try:
        save(raw / 'annotations' / f'{job_id}.json', _get(f'{api}/repos/{repo}/check-runs/{job_id}/annotations?per_page=50', h))
    except Exception as e:  # noqa: BLE001 - recorded, never swallowed
        d = describe_error(e)
        save(raw / 'annotations' / f'{job_id}.error.json', d)
        warn(f'annotations of job {job_id} not downloaded ({d["status"]}): {d["error"]}')


def never_started(j: dict) -> bool:
    """A GitHub job that concluded without a runner ever picking it up: no steps, and a runner name the API gives as
    empty or null (a recording without the field says nothing either way)."""
    return (j.get('conclusion') not in ('success', 'skipped', None) and not j.get('steps')
            and 'runner_name' in j and not j['runner_name'])


def _ts(t: str | None):
    from datetime import datetime
    try:
        return datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None
    except ValueError:
        return None


def queued_for(j: dict) -> int | None:
    """Seconds a never-started job spent between being queued and the CI stopping it: GitHub gives such a job a
    started_at and completed_at, so its "duration" (16m, 2h) is time waiting for a runner, never time running."""
    begin = [x for x in (_ts(j.get('created_at')), _ts(j.get('started_at'))) if x]
    end = _ts(j.get('completed_at'))
    return max(0, int((end - min(begin)).total_seconds())) if begin and end else None


def annotations_of(raw: Path, job_id) -> tuple[list[dict], dict | None]:
    """(annotations, error record) saved for one job; each annotation as {level, title, message}, untrusted text."""
    p = raw / 'annotations' / f'{job_id}.json'
    if p.exists():
        data = json.loads(p.read_text(encoding='utf-8'))
        return [{'level': a.get('annotation_level'), 'title': a.get('title') or '', 'message': (a.get('message') or '')[:2000]}
                for a in (data if isinstance(data, list) else [])], None
    err = raw / 'annotations' / f'{job_id}.error.json'
    return [], (json.loads(err.read_text(encoding='utf-8')) if err.exists() else None)


def fetch_job_evidence(api: str, repo: str, j: dict, h: dict, raw: Path) -> None:
    """The log and check-run annotations of one failed GitHub job. A job that never started on a runner (no steps, no
    runner name) never wrote a log, so none is requested: asking would only give a 404 that reads like an expired log."""
    if j.get('conclusion') in ('success', 'skipped'):
        return
    if never_started(j):
        print(f'note: job {j["id"]} ({j.get("name") or "unnamed"}) never started on a runner: no log exists; reading its annotations',
              file=sys.stderr)
    else:
        fetch_log(f'{api}/repos/{repo}/actions/jobs/{j["id"]}/logs', h, raw, j['id'])
    fetch_annotations(api, repo, j['id'], h, raw)


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


SAME_COMMIT_RUNS = 50   # runs of the same commit asked for (before and after the failing run)


def find_default_branch(api: str, repo: str, run: dict, h: dict, raw: Path, given: str | None) -> tuple[str | None, str]:
    """(default branch, where it came from). --default-branch wins; a recording may carry it on the run; live GitHub
    does not (the repository inside a workflow-run response is the minimal object), so the repository itself is read
    (one GET, saved as repository.json). When that fails too it is `unknown`, said on stderr with what to do."""
    if given:
        return given, 'option'
    named = (run.get('repository') or {}).get('default_branch')
    if named:
        return named, 'run'
    try:
        doc = _get(f'{api}/repos/{repo}', h)
        save(raw / 'repository.json', doc)
        if isinstance(doc, dict) and doc.get('default_branch'):
            return doc['default_branch'], 'repository'
        why = 'the repository record names none'
    except Exception as e:  # noqa: BLE001 - said, never swallowed
        why = describe_error(e)['error']
    warn(f'the default branch of {repo} could not be read ({why}), so its runs are not searched for a last green run: '
         'give it with --default-branch NAME')
    return None, 'unknown'


def last_green_hint(run: dict) -> str | None:
    """What to say when a run has no last green run and the default branch was never searched."""
    hq = run.get('history_query') or {}
    if run.get('last_green') or hq.get('default_branch') or hq.get('default_branch_source') != 'unknown':
        return None
    return (f'no last green run of this workflow on the branch {run["run"].get("branch")}, and the default branch is not known, so its '
            'runs were not searched: fetch again with --default-branch NAME (the default branch\'s newest green run before this one '
            'then starts the range of changes)')


def history_queries(base: str, run: dict, history: int, default_branch: str | None) -> list[str]:
    """The history a GitHub run is read against, each query bounded: up to `history` runs of the same workflow on the
    branch, created up to this run; as many on the default branch (a pull request branch often has no green run of its
    own, and the default branch's newest one before this run is then the range start); and up to SAME_COMMIT_RUNS runs
    of the same commit, before or after this run (a later pass of the same job on the same code is evidence too)."""
    created = run.get('created_at') or run.get('run_started_at') or ''
    q = lambda br: f'{base}?branch={quote(br or "", safe="")}&created={quote("<=" + created, safe="")}&per_page={history}'  # noqa: E731
    out = [q(run.get('head_branch'))]
    default = default_branch or (run.get('repository') or {}).get('default_branch')
    if default and default != run.get('head_branch'):
        out.append(q(default))
    return out + [f'{base}?head_sha={run["head_sha"]}&per_page={SAME_COMMIT_RUNS}']


def fetch_github(repo: str, run_id: int, raw: Path, history: int, max_mb: int, default_branch: str | None = None):
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
            fetch_job_evidence(GH_API, repo, j, h, raw)
    arts = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100', h)
    save(raw / 'attempts' / str(last) / 'artifacts.json', arts)
    for a in arts.get('artifacts', []):
        if not a.get('expired'):
            fetch_artifact(a['archive_download_url'], h, raw / 'attempts' / str(last) / 'artifacts' / a['name'], raw, a['name'],
                           a.get('size_in_bytes'), max_mb)
    # history: the same workflow, on the branch and on the default branch, created up to this run; and runs of the same commit
    wid, created = run.get('workflow_id'), run.get('created_at') or run.get('run_started_at')
    base = f'{GH_API}/repos/{repo}/actions/workflows/{wid}/runs' if wid else f'{GH_API}/repos/{repo}/actions/runs'
    default, default_source = find_default_branch(GH_API, repo, run, h, raw, default_branch)
    runs: dict = {}
    for q in history_queries(base, run, history, default):
        for r in _get(q, h).get('workflow_runs', []):
            runs[r['id']] = r
    save(raw / 'history.json', {'query': {'workflow_id': wid, 'branch': run['head_branch'], 'created_before': created, 'head_sha': run['head_sha'],
                                          'per_page': history, 'default_branch': default if default != run['head_branch'] else None,
                                          'default_branch_source': default_source, 'same_commit_per_page': SAME_COMMIT_RUNS},
                                'workflow_runs': sorted(runs.values(), key=lambda r: r.get('run_started_at') or '', reverse=True)})
    for r in runs.values():
        if r['head_sha'] == run['head_sha'] and r['id'] != run['id']:
            try:
                save(raw / 'history_jobs' / f'{r["id"]}.json', _get(f'{GH_API}/repos/{repo}/actions/runs/{r["id"]}/jobs?filter=latest&per_page=100', h))
            except Exception as e:  # noqa: BLE001
                warn(f'jobs of history run {r["id"]} not downloaded: {describe_error(e)["error"]}')
    # a run in which no job ran has no log to explain it: whether the commit's other workflows stopped the same way is the
    # only clue to a common cause (a workflow file or repository setting), so their runs are read (one GET, only then)
    if not jobs['jobs']:
        try:
            save(raw / 'same_commit_runs.json', _get(f'{GH_API}/repos/{repo}/actions/runs?head_sha={run["head_sha"]}&per_page={SAME_COMMIT_RUNS}', h))
        except Exception as e:  # noqa: BLE001
            warn(f'the other workflows\' runs of this commit not downloaded: {describe_error(e)["error"]}')


def fetch_azure(org: str, project: str, build_id: int, raw: Path, history: int, max_mb: int, default_branch: str | None = None):
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
    default = f'refs/heads/{default_branch.removeprefix("refs/heads/")}' if default_branch else None
    if default and default != b['sourceBranch']:     # the default branch's builds: the range start when the branch has no green one
        seen = {r['id'] for r in hist.get('value', [])}
        more = _get(q.replace(f'branchName={quote(b["sourceBranch"], safe="")}', f'branchName={quote(default, safe="")}'), h)
        hist['value'] = hist.get('value', []) + [r for r in more.get('value', []) if r['id'] not in seen]
    hist['query'] = {'definition_id': did, 'branch': b['sourceBranch'], 'started_before': b.get('startTime'), 'per_page': history,
                     'default_branch': default if default != b['sourceBranch'] else None}
    save(raw / 'history.json', hist)
    for r in hist.get('value', []):
        if r.get('sourceVersion') == b['sourceVersion'] and r['id'] != b['id']:
            try:
                save(raw / 'history_jobs' / f'{r["id"]}.json', _get(f'{base}/{r["id"]}/timeline?api-version=7.1', h))
            except Exception as e:  # noqa: BLE001
                warn(f'timeline of history build {r["id"]} not downloaded: {describe_error(e)["error"]}')


def same_way(run: dict) -> list[dict]:
    """Other workflows' runs of the same commit and event that concluded as this run did (recorded only for a run in
    which no job ran)."""
    r = run['run']
    return [x for x in run.get('same_commit_runs') or [] if x.get('event') == r.get('event') and x.get('conclusion') == r.get('conclusion')
            and r.get('conclusion') not in ('success', None)]


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


def _pick_green(history: list[dict], started: str | None, event: str | None, failed_jobs: set | None = None,
                branch: str | None = None, default_branch: str | None = None) -> dict | None:
    """The newest successful run of the workflow started before this one that ran (and passed) the jobs that failed here:
    on the run's own branch (a run recorded without its branch counts as the branch's), preferring the same event; when
    the branch has none, on the default branch, whose green run then starts the range of changes."""
    on_branch = [r for r in history if branch is None or r.get('branch') in (None, branch)]
    g = _pick_green_in(on_branch, started, event, failed_jobs)
    if g:
        return {**g, 'branch': branch} if branch else g
    if default_branch and default_branch != branch:
        g = _pick_green_in([r for r in history if r.get('branch') == default_branch], started, event, failed_jobs)
        if g:
            return {**g, 'branch': default_branch,
                    'basis': f'the branch {branch} has no successful run of the workflow before this one, so this is the newest on the default '
                             f'branch {default_branch} started before it{g["basis"][g["basis"].index(","):]}; the range of changes starts there'}
    return None


def _pick_green_in(history: list[dict], started: str | None, event: str | None, failed_jobs: set | None = None) -> dict | None:
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
            unstarted = never_started(j)
            notes, ann_err = annotations_of(raw, j['id'])
            if ann_err:
                problems.append({'kind': 'annotations', 'attempt': n, 'job': j['name'], 'job_id': j['id'], 'status': ann_err.get('status'),
                                 'error': ann_err.get('error')})
            if status == 'fetched':
                save(dest_log, log.read_text(encoding='utf-8'))
            elif unstarted:
                # no runner ever picked the job up, so no log was ever written: not a failed download
                status, err = 'never_started', 'the job never started on a runner (no steps, no runner), so no log exists'
            elif j.get('conclusion') not in ('success', 'skipped', None):
                problems.append({'kind': 'log', 'attempt': n, 'job': j['name'], 'job_id': j['id'], 'status': status, 'error': err})
            junit = []
            for x in junit_for(j['name'], ad / 'artifacts'):
                d = out / 'junit' / str(n) / x.parent.name / x.name
                save(d, x.read_bytes())
                junit.append(d.relative_to(out).as_posix())
            waited = queued_for(j) if unstarted else None
            jobs.append({'id': j['id'], 'name': j['name'], 'conclusion': j.get('conclusion'), 'url': j.get('html_url'), 'started_at': j.get('started_at'),
                         **{k: j[k] for k in ('created_at', 'completed_at') if j.get(k)},
                         **({'queued_for_s': waited} if waited is not None else {}),
                         'steps': [{'number': s['number'], 'name': s['name'], 'conclusion': s.get('conclusion')} for s in j.get('steps', [])],
                         'log': dest_log.relative_to(out).as_posix() if status == 'fetched' else None, 'log_status': status, 'log_error': err,
                         'junit': junit, **({'never_started': True} if unstarted else {}), **({'annotations': notes} if notes else {})})
        starts = sorted(j.get('started_at') for j in raw_jobs if j.get('started_at'))
        meta = json.loads((ad / 'run.json').read_text(encoding='utf-8')) if (ad / 'run.json').exists() else (run if n == int(run.get('run_attempt') or 1) else {})
        actor = (meta.get('triggering_actor') or {}).get('login')
        attempts.append({'n': n, 'started_at': starts[0] if starts else None, 'triggered_by': actor,
                         'jobs_rerun': _jobs_rerun(prev, raw_jobs), 'jobs': jobs})
        prev = raw_jobs
    for e in sorted((raw / 'artifact_errors').glob('*.json')) if (raw / 'artifact_errors').exists() else []:
        d = json.loads(e.read_text(encoding='utf-8'))
        problems.append({'kind': 'artifact', 'artifact': d.get('artifact'), 'status': d.get('status'), 'error': d.get('error')})
    hist_doc = json.loads((raw / 'history.json').read_text(encoding='utf-8')) if (raw / 'history.json').exists() else {}
    hist, hq = hist_doc.get('workflow_runs', []), hist_doc.get('query') or {}
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
                        'event': r.get('event'), 'workflow_id': r.get('workflow_id'), 'jobs': jobs,
                        **({'branch': r['head_branch']} if r.get('head_branch') else {}),
                        **({'after_run': True} if (r.get('run_started_at') or '') > (run.get('run_started_at') or '~') else {})})
    if hist and not (wid or wpath):
        warn('the run names no workflow, so no history run can be matched to it: history is empty')
    elif other:
        print(f'note: {other} history run(s) of other workflows ignored', file=sys.stderr)
    api_repo = run.get('repository', {}).get('full_name')
    repo_doc = json.loads((raw / 'repository.json').read_text(encoding='utf-8')) if (raw / 'repository.json').exists() else {}
    default = hq.get('default_branch') or (run.get('repository') or {}).get('default_branch') or (repo_doc.get('default_branch') if isinstance(repo_doc, dict) else None)
    sib_doc = json.loads((raw / 'same_commit_runs.json').read_text(encoding='utf-8')) if (raw / 'same_commit_runs.json').exists() else None
    siblings = [{'id': r['id'], 'workflow': r.get('path') or r.get('name'), 'event': r.get('event'), 'conclusion': r.get('conclusion')}
                for r in (sib_doc or {}).get('workflow_runs', []) if r['id'] != run['id'] and not same_wf(r)]
    out_run = {'system': 'github', 'repo': repo or api_repo,
               'run': {'id': run['id'], 'url': run.get('html_url'), 'attempt': int(run.get('run_attempt') or 1), 'commit': run['head_sha'],
                       'branch': run.get('head_branch'), 'event': run.get('event'), 'started_at': run.get('run_started_at'),
                       'conclusion': run.get('conclusion'), 'workflow': run.get('path') or run.get('name'), 'workflow_id': wid},
               'attempts': attempts, 'history': history, 'history_scope': 'same workflow' if (wid or wpath) else 'unknown workflow',
               'last_green': _pick_green(history, run.get('created_at') or run.get('run_started_at'), run.get('event'),
                                         {j['name'] for j in attempts[0]['jobs'] if j['conclusion'] not in ('success', 'skipped', None)} if attempts else None,
                                         run.get('head_branch'), default),
               'fetch_problems': problems, **({'history_query': hq} if hq else {}),
               **({'same_commit_runs': siblings} if sib_doc is not None else {})}
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
                        'event': r.get('reason'), 'workflow_id': did, 'jobs': jobs,
                        **({'branch': r['sourceBranch'].removeprefix('refs/heads/')} if r.get('sourceBranch') else {}),
                        **({'after_run': True} if (r.get('startTime') or '') > (b.get('startTime') or '~') else {})})
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
                                      {j['name'] for j in atts[0]['jobs'] if j['conclusion'] not in ('success', 'skipped', None)} if atts else None,
                                      (b.get('sourceBranch') or '').removeprefix('refs/heads/') or None,
                                      ((hist_doc.get('query') or {}).get('default_branch') or '').removeprefix('refs/heads/') or None),
            'fetch_problems': problems, **({'history_query': hist_doc['query']} if hist_doc.get('query') else {})}


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
            routes = {'/jobs/1/logs': f'http://127.0.0.1:{sport}/blob/log?sig=fake', '/artifacts/1/zip': f'http://127.0.0.1:{sport}/blob/zip?sig=fake',
                      '/jobs/2/logs': '/moved/2/logs'}
            if self.path in routes:
                self.send_response(302); self.send_header('Location', routes[self.path]); self.end_headers(); return
            if self.path == '/moved/2/logs':
                ok = self.headers.get('Authorization') == FAKE
                self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b'same-host log' if ok else b''); return
            if self.path == '/repos/acme/x/check-runs/7/annotations?per_page=50':
                body = json.dumps([{'annotation_level': 'failure', 'title': '', 'message': 'The job was not started because of a billing stop.'}])
                self.send_response(200); self.end_headers(); self.wfile.write(body.encode()); return
            if self.path == '/jobs/3/logs':
                self.send_response(410); self.end_headers(); return
            doc = live_api(self.path)
            if doc is not None:
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(doc).encode()); return
            if self.path == '/jobs/4/logs':
                self.send_response(302); self.send_header('Location', f'http://127.0.0.1:{sport}/blob/log'); self.end_headers(); return
            self.send_response(404); self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            self.do_GET()

    def live_api(path: str):
        """GitHub's REST shapes for three repositories, as the live API gives them: the repository object inside a
        workflow run is the minimal one, with no default_branch. acme/y: a pull request branch with no green run;
        acme/w: the same, and the repository endpoint is not readable; acme/z: a run that concluded failure with no
        job, beside other workflows of the same commit that did the same."""
        import re
        m = re.match(r'^/repos/acme/([wyz])(?:/([^?]*))?(?:\?(.*))?$', path)
        if not m:
            return None
        repo, rest, q = m.group(1), m.group(2), m.group(3) or ''
        sha, t = 'f' * 40, '2026-01-05T00:00:00Z'
        wr = lambda i, c, br, started, wid=7, ev='pull_request', head=sha, **k: {  # noqa: E731
            'id': i, 'head_sha': head, 'head_branch': br, 'conclusion': c, 'run_started_at': started, 'created_at': started, 'workflow_id': wid,
            'event': ev, 'repository': {'full_name': f'acme/{repo}'}, **k}
        run = wr(60, 'failure', 'feature/tides', t, run_attempt=1, path='.github/workflows/checks.yml')
        if rest is None:
            return {'full_name': f'acme/{repo}', 'default_branch': 'main'} if repo != 'w' else None
        if rest in ('actions/runs/60', 'actions/runs/60/attempts/1'):
            return run
        if rest == 'actions/runs/60/attempts/1/jobs':
            return {'jobs': [] if repo == 'z' else [{'id': 1, 'name': 'test', 'conclusion': 'failure', 'started_at': t, 'runner_name': 'hosted 1',
                                                     'steps': [{'number': 1, 'name': 'Unit tests', 'conclusion': 'failure'}]}]}
        if rest == 'actions/runs/60/artifacts':
            return {'artifacts': []}
        if rest == 'actions/workflows/7/runs':
            if 'branch=main' in q:
                return {'workflow_runs': [wr(58, 'success', 'main', '2026-01-03T00:00:00Z', ev='push', head='d' * 40),
                                          wr(57, 'success', 'main', '2026-01-02T00:00:00Z', ev='push', head='a' * 40)]}
            if 'branch=feature' in q:
                return {'workflow_runs': [run, wr(59, 'failure', 'feature/tides', '2026-01-04T00:00:00Z', head='e' * 40)]}
            return {'workflow_runs': [run]}
        if rest == 'actions/runs' and 'head_sha=' in q:
            return {'workflow_runs': [run, wr(71, 'failure', 'feature/tides', t, wid=8, path='.github/workflows/lint.yml'),
                                      wr(72, 'failure', 'feature/tides', t, wid=9, path='.github/workflows/docs.yml'),
                                      wr(73, 'success', 'feature/tides', t, wid=10, path='.github/workflows/labels.yml')]}
        return None

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
        # rerun.py's POST: a redirect to another origin must not carry the token either
        import rerun
        seen['storage'] = []
        try:
            rerun._send(f'{base}/jobs/1/logs', 'POST', h)
        except urllib.error.HTTPError:
            pass
        check(seen['storage'] == [None], f'rerun._send: cross-host redirect forwarded Authorization to the storage host ({seen["storage"]})')
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
            fetch_annotations(base, 'acme/x', 7, h, raw)
            fetch_annotations(base, 'acme/x', 8, h, raw)
            got, err = annotations_of(raw, 7)
            check([a['message'] for a in got] == ['The job was not started because of a billing stop.'] and err is None,
                  f'check-run annotations of a job should be saved and read back, got {got}')
            got, err = annotations_of(raw, 8)
            check(got == [] and err and err['status'] == 'unavailable', 'a failed annotations download should be recorded')
            # a job that never started (no steps, no runner) has no log to download: no request, no "404 expired" warning
            import contextlib
            err, before = io.StringIO(), len(seen.get('api', []))
            with contextlib.redirect_stderr(err):
                fetch_job_evidence(base, 'acme/x', {'id': 7, 'conclusion': 'failure', 'steps': [], 'runner_name': ''}, h, raw)
            asked = [p for p, _ in seen.get('api', [])[before:]]
            check('not downloaded' not in err.getvalue() and not (raw / 'logs' / '7.error.json').exists()
                  and not any('/logs' in p for p in asked) and any('annotations' in p for p in asked),
                  f'a never-started job: its log should not be requested or warned about, its annotations should (asked {asked}, '
                  f'stderr {err.getvalue().strip()!r})')
            fetch_artifact(f'{base}/artifacts/404/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'gone', raw, 'gone', None, 50)
            check(any((raw / 'artifact_errors').glob('*.json')), 'a failed artifact download should be recorded, not crash')
            fetch_artifact(f'{base}/artifacts/1/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'junit-a', raw, 'junit-a', 10, 50)
            check((raw / 'attempts' / '1' / 'artifacts' / 'junit-a' / 'junit.xml').exists(), 'artifact xml not unzipped')
            fetch_artifact(f'{base}/artifacts/1/zip', h, raw / 'attempts' / '1' / 'artifacts' / 'big', raw, 'big', 80 * 1024 * 1024, 50)
            check(not (raw / 'attempts' / '1' / 'artifacts' / 'big').exists(), 'an artifact over the size cap should be skipped')
        # live GitHub: the default branch is read from the repository (the run's own repository object does not have it)
        import contextlib
        saved_api, saved_tok = GH_API, os.environ.get('GITHUB_TOKEN')
        globals()['GH_API'], os.environ['GITHUB_TOKEN'] = base, 'fake-token-0000'
        try:
            with tempfile.TemporaryDirectory() as td:
                got = {}
                for repo in 'ywz':
                    raw, err, before = Path(td) / repo / 'raw', io.StringIO(), len(seen['api'])
                    with contextlib.redirect_stderr(err):
                        fetch_github(f'acme/{repo}', 60, raw, 20, 50)
                        r = normalise_github(raw, Path(td) / repo / 'out', f'acme/{repo}')
                        said = last_green_hint(r) if 'last_green_hint' in globals() else None
                    got[repo] = (r, err.getvalue(), [p for p, _ in seen['api'][before:]], said)
                r, _, asked, said = got['y']
                lg, hq = r['last_green'] or {}, r.get('history_query') or {}
                check(lg.get('id') == 58 and lg.get('branch') == 'main' and hq.get('default_branch') == 'main'
                      and hq.get('default_branch_source') == 'repository' and any('branch=main' in p for p in asked) and said is None,
                      f'live GitHub, a branch with no green run: the default branch comes from the repository endpoint and its newest green run '
                      f'(58) starts the range; got last_green {r["last_green"]}, history_query {hq}')
                check(not any(p.startswith('/repos/acme/y/actions/runs?head_sha') for p in asked) and 'same_commit_runs' not in r,
                      f'a run whose jobs ran: other workflows of the commit are not asked for (asked {asked})')
                r, err, asked, said = got['w']
                hq = r.get('history_query') or {}
                check(r['last_green'] is None and hq.get('default_branch') is None and hq.get('default_branch_source') == 'unknown'
                      and '--default-branch' in err and said and '--default-branch' in said,
                      f'the default branch could not be read: recorded as unknown, with the hint to give --default-branch, on stderr and '
                      f'in the record; got history_query {hq}, hint {said!r}, stderr {err.strip()[-200:]!r}')
                r, _, asked, _ = got['z']
                sib = r.get('same_commit_runs') or []
                check([(x.get('id'), x.get('workflow'), x.get('conclusion')) for x in sib] ==
                      [(71, '.github/workflows/lint.yml', 'failure'), (72, '.github/workflows/docs.yml', 'failure'), (73, '.github/workflows/labels.yml', 'success')],
                      f'a run with no job: the other workflows\' runs of the same commit are recorded (the only clue to a common cause); got {sib}')
        finally:
            globals()['GH_API'] = saved_api
            os.environ.pop('GITHUB_TOKEN') if saved_tok is None else os.environ.__setitem__('GITHUB_TOKEN', saved_tok)
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
    # a pull request branch with no green run of its own: the last green run is the default branch's, said so and recorded
    with tempfile.TemporaryDirectory() as td:
        raw, out = Path(td) / 'raw', Path(td) / 'out'
        save(raw / 'run.json', {'id': 60, 'head_sha': 'f' * 40, 'head_branch': 'feature/tides', 'event': 'pull_request', 'run_attempt': 1,
                                'workflow_id': 7, 'run_started_at': '2026-01-05T00:00:00Z', 'repository': {'full_name': 'acme/x', 'default_branch': 'main'}})
        save(raw / 'attempts' / '1' / 'jobs.json', {'jobs': [{'id': 1, 'name': 'test', 'conclusion': 'failure', 'started_at': 'T1', 'steps': []}]})
        wr = lambda i, sha, c, t, br, ev='push': {'id': i, 'head_sha': sha, 'head_branch': br, 'conclusion': c, 'run_started_at': t,  # noqa: E731
                                                  'workflow_id': 7, 'event': ev}
        runs = [wr(59, 'e' * 40, 'failure', '2026-01-04T00:00:00Z', 'feature/tides', 'pull_request'),
                wr(58, 'd' * 40, 'success', '2026-01-03T00:00:00Z', 'main'), wr(57, 'a' * 40, 'success', '2026-01-02T00:00:00Z', 'main'),
                wr(61, 'f' * 40, 'success', '2026-01-06T00:00:00Z', 'feature/tides', 'pull_request')]   # later, same commit
        save(raw / 'history.json', {'query': {'workflow_id': 7, 'branch': 'feature/tides', 'default_branch': 'main', 'per_page': 20,
                                              'head_sha': 'f' * 40, 'same_commit_per_page': 50}, 'workflow_runs': runs})
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            r = normalise_github(raw, out, 'acme/x')
        lg = r['last_green'] or {}
        check(lg.get('id') == 58 and lg.get('branch') == 'main' and 'default branch' in (lg.get('basis') or ''),
              f'no green run on the branch: the last green should be the default branch\'s newest before the run (58), said so, got {lg}')
        check((r.get('history_query') or {}).get('default_branch') == 'main', f'the history query should be recorded, got {r.get("history_query")}')
        later = next((x for x in r['history'] if x['id'] == 61), {})
        check(later.get('after_run') is True and later.get('branch') == 'feature/tides', f'a later run of the same commit should be kept and marked, got {later}')
        runs.append(wr(56, 'b' * 40, 'success', '2026-01-01T00:00:00Z', 'feature/tides', 'pull_request'))
        save(raw / 'history.json', {'workflow_runs': runs})
        with contextlib.redirect_stderr(io.StringIO()):
            r = normalise_github(raw, out, 'acme/x')
        check((r['last_green'] or {}).get('id') == 56, f'a green run on the branch itself comes first, got {r["last_green"]}')
        qs = history_queries('https://api.example.test/w/7/runs', {'head_branch': 'feature/tides', 'head_sha': 'f' * 40, 'created_at': '2026-01-05T00:00:00Z',
                                                                     'repository': {'default_branch': 'main'}}, 20, None)
        check(any('branch=main' in q for q in qs) and any('branch=feature%2Ftides' in q for q in qs) and any('head_sha=' in q for q in qs),
              f'the fetch should ask for the branch, the default branch and the same commit: {qs}')
    if fails:
        print('fetch_run self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('fetch_run self-test: ok (cross-host redirect drops the token, same-host keeps it, https->http refused, '
          'failed log/artifact downloads recorded, history limited to the workflow, re-run jobs from started_at, the default '
          'branch read from the repository on a live fetch and said when unknown, other workflows of a commit read for a run with no job)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--system', choices=['github', 'azure'])
    ap.add_argument('--repo', help='github: owner/name')
    ap.add_argument('--run-id', type=int)
    ap.add_argument('--org'); ap.add_argument('--project'); ap.add_argument('--build-id', type=int)
    ap.add_argument('--from-dir', type=Path, help='a recorded directory of API responses (offline)')
    ap.add_argument('--history', type=int, default=20, help='runs of the workflow asked for on the branch (and as many on the default branch)')
    ap.add_argument('--default-branch', help='the default branch, whose newest green run before this one starts the range when the branch has '
                                             'none (GitHub: read from the repository, and needed only when that cannot be read; '
                                             'Azure DevOps: give it)')
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
            fetch_github(a.repo, a.run_id, raw, a.history, a.max_artifact_mb, a.default_branch)
        else:
            if not (a.org and a.project and a.build_id):
                raise SystemExit('--org, --project and --build-id are needed for Azure DevOps')
            fetch_azure(a.org, a.project, a.build_id, raw, a.history, a.max_artifact_mb, a.default_branch)
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
    hint = last_green_hint(run) if run['system'] == 'github' else None
    if hint:
        print(f'WARNING: {hint}', file=sys.stderr)
    for j in (run['attempts'][0]['jobs'] if run['attempts'] else []):
        if j.get('queued_for_s') is not None:
            m, s = divmod(j['queued_for_s'], 60)
            print(f'note: job {j["name"]} never started on a runner; its {m // 60}h{m % 60:02d}m{s:02d}s between start and completion is '
                  'time waiting for a runner, not time running', file=sys.stderr)
    if run['attempts'] and not [j for j in run['attempts'][0]['jobs'] if j.get('conclusion') not in ('skipped', None) and not j.get('never_started')]:
        said = [a['message'] for j in run['attempts'][0]['jobs'] for a in j.get('annotations', [])]
        print(f'WARNING: the run concluded {r["conclusion"]} but no job ran on a runner (a workflow file error, a startup failure, a '
              'billing or runner stop, or a fork pull request whose run was never approved): there is nothing to triage; the triage '
              'is run_status not_run' + (f'. The CI says: {said[0][:300]}' if said else ''), file=sys.stderr)
        same = same_way(run)
        if same:
            print(f'WARNING: {len(same)} other workflow run(s) of this commit also concluded {r["conclusion"]} on the same event '
                  f'({", ".join(str(x["workflow"]) for x in same)}): a cause common to them (a workflow file, a repository or account '
                  'setting) is likelier than one of this workflow alone; the triage must say so', file=sys.stderr)
    if run['fetch_problems']:
        print(f'WARNING: {len(run["fetch_problems"])} download(s) failed; the triage must say so:', file=sys.stderr)
        for p in run['fetch_problems']:
            print(f'  {p["kind"]} {p.get("job") or p.get("artifact")}: {p["status"]} ({p.get("error")})', file=sys.stderr)
        return 2 if any(p['kind'] == 'log' and p['status'] == 'fetch_failed' for p in run['fetch_problems']) else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
