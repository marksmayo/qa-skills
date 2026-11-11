#!/usr/bin/env python3
"""Fetch a CI run (GitHub Actions or Azure DevOps) and normalise it to one shape (stdlib only).

    python fetch_run.py --system github --repo owner/name --run-id 123 --out run/
    python fetch_run.py --system azure --org myorg --project proj --build-id 456 --out run/
    python fetch_run.py --system github --from-dir recorded/github --out run/     # offline, recorded API responses

Live GitHub uses the REST API with a token from GITHUB_TOKEN or GH_TOKEN (or `gh auth token`); live
Azure DevOps uses its REST API with a PAT from AZURE_DEVOPS_EXT_PAT. Only GET requests are made. The
responses are saved under <out>/raw/ in the same layout a recorded directory uses, then normalised:

  github: run.json, attempts/<n>/jobs.json, attempts/<n>/artifacts.json, attempts/<n>/artifacts/<name>/*.xml,
          logs/<job id>.txt, history.json (runs on the branch)
  azure:  build.json, timeline.json, logs/<log id>.txt, artifacts.json, artifacts/<name>/*.xml, history.json

<out>/run.json: {system, repo, source, run{id, url, attempt, commit, branch, event, started_at, conclusion},
attempts[{n, jobs[{id, name, conclusion, url, steps[{number, name, conclusion}], log, junit[]}]}],
history[{id, commit, conclusion, started_at}], last_green{id, commit}}. Logs and JUnit are copied into
<out>/logs and <out>/junit, so the run directory is self-contained.
"""
from __future__ import annotations
import argparse, base64, io, json, os, shutil, subprocess, sys, urllib.request, zipfile
from pathlib import Path

GH_API = 'https://api.github.com'


def _get(url: str, headers: dict, binary: bool = False):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else json.loads(data.decode('utf-8')) if data[:1] in (b'{', b'[') else data.decode('utf-8', 'replace')


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


def fetch_github(repo: str, run_id: int, raw: Path, history: int):
    h = {'Authorization': f'Bearer {gh_token()}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    run = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}', h)
    save(raw / 'run.json', run)
    for n in range(1, int(run.get('run_attempt') or 1) + 1):
        jobs = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/attempts/{n}/jobs?per_page=100', h)
        save(raw / 'attempts' / str(n) / 'jobs.json', jobs)
        for j in jobs['jobs']:
            if j.get('conclusion') not in ('success', 'skipped'):
                try:
                    save(raw / 'logs' / f'{j["id"]}.txt', _get(f'{GH_API}/repos/{repo}/actions/jobs/{j["id"]}/logs', h))
                except Exception as e:  # logs expire (90 days by default)
                    save(raw / 'logs' / f'{j["id"]}.missing', f'{type(e).__name__}: {e}')
    arts = _get(f'{GH_API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100', h)
    n = int(run.get('run_attempt') or 1)
    save(raw / 'attempts' / str(n) / 'artifacts.json', arts)
    for a in arts.get('artifacts', []):
        if not a.get('expired'):
            unzip_xml(_get(a['archive_download_url'], h, binary=True), raw / 'attempts' / str(n) / 'artifacts' / a['name'])
    save(raw / 'history.json', _get(f'{GH_API}/repos/{repo}/actions/runs?branch={run["head_branch"]}&per_page={history}', h))


def fetch_azure(org: str, project: str, build_id: int, raw: Path, history: int):
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
            save(raw / 'logs' / f'{r["log"]["id"]}.txt', _get(f'{base}/{build_id}/logs/{r["log"]["id"]}?api-version=7.1', h))
    arts = _get(f'{base}/{build_id}/artifacts?api-version=7.1', h)
    save(raw / 'artifacts.json', arts)
    for a in arts.get('value', []):
        url = a.get('resource', {}).get('downloadUrl')
        if url:
            unzip_xml(_get(url, h, binary=True), raw / 'artifacts' / a['name'])
    save(raw / 'history.json', _get(f'{base}?branchName={b["sourceBranch"]}&$top={history}&api-version=7.1', h))


def junit_for(job_name: str, art_dir: Path) -> list[Path]:
    if not art_dir.exists():
        return []
    out = []
    for d in sorted(p for p in art_dir.iterdir() if p.is_dir()):
        if job_name.lower() in d.name.lower():
            out += sorted(d.glob('*.xml'))
    return out


def normalise_github(raw: Path, out: Path, repo: str | None) -> dict:
    run = json.loads((raw / 'run.json').read_text(encoding='utf-8'))
    attempts = []
    for ad in sorted((raw / 'attempts').iterdir(), key=lambda p: int(p.name)):
        n = int(ad.name)
        jobs = []
        for j in json.loads((ad / 'jobs.json').read_text(encoding='utf-8'))['jobs']:
            log = raw / 'logs' / f'{j["id"]}.txt'
            dest_log = out / 'logs' / f'{j["id"]}.txt'
            if log.exists():
                save(dest_log, log.read_text(encoding='utf-8'))
            junit = []
            for x in junit_for(j['name'], ad / 'artifacts'):
                d = out / 'junit' / str(n) / x.parent.name / x.name
                save(d, x.read_bytes())
                junit.append(d.relative_to(out).as_posix())
            jobs.append({'id': j['id'], 'name': j['name'], 'conclusion': j.get('conclusion'), 'url': j.get('html_url'),
                         'steps': [{'number': s['number'], 'name': s['name'], 'conclusion': s.get('conclusion')} for s in j.get('steps', [])],
                         'log': dest_log.relative_to(out).as_posix() if log.exists() else None, 'junit': junit})
        starts = sorted(j.get('started_at') for j in json.loads((ad / 'jobs.json').read_text(encoding='utf-8'))['jobs'] if j.get('started_at'))
        attempts.append({'n': n, 'started_at': starts[0] if starts else None, 'jobs': jobs})
    hist = json.loads((raw / 'history.json').read_text(encoding='utf-8')).get('workflow_runs', []) if (raw / 'history.json').exists() else []
    history = [{'id': r['id'], 'commit': r['head_sha'], 'conclusion': r.get('conclusion'), 'started_at': r.get('run_started_at')} for r in hist]
    green = next((r for r in sorted(history, key=lambda r: r['started_at'] or '', reverse=True)
                  if r['conclusion'] == 'success' and (r['started_at'] or '') < (run.get('run_started_at') or '~')), None)
    return {'system': 'github', 'repo': repo or run.get('repository', {}).get('full_name'),
            'run': {'id': run['id'], 'url': run.get('html_url'), 'attempt': int(run.get('run_attempt') or 1), 'commit': run['head_sha'],
                    'branch': run.get('head_branch'), 'event': run.get('event'), 'started_at': run.get('run_started_at'),
                    'conclusion': run.get('conclusion'), 'workflow': run.get('path') or run.get('name')},
            'attempts': attempts, 'history': history, 'last_green': {'id': green['id'], 'commit': green['commit']} if green else None}


def normalise_azure(raw: Path, out: Path, repo: str | None) -> dict:
    b = json.loads((raw / 'build.json').read_text(encoding='utf-8'))
    recs = json.loads((raw / 'timeline.json').read_text(encoding='utf-8'))['records']
    res = {'succeeded': 'success', 'failed': 'failure', 'canceled': 'cancelled', 'skipped': 'skipped', 'succeededWithIssues': 'success', None: None}
    by_attempt: dict = {}
    for j in [r for r in recs if r['type'] == 'Job']:
        tasks = sorted((r for r in recs if r['type'] == 'Task' and r.get('parentId') == j['id']), key=lambda r: r.get('order', 0))
        text = ''.join((raw / 'logs' / f'{t["log"]["id"]}.txt').read_text(encoding='utf-8') for t in tasks if t.get('log') and (raw / 'logs' / f'{t["log"]["id"]}.txt').exists())
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
            'steps': [{'number': i + 1, 'name': t['name'], 'conclusion': res.get(t.get('result'), t.get('result'))} for i, t in enumerate(tasks)],
            'log': dest_log.relative_to(out).as_posix() if text else None, 'junit': junit})
    hist = json.loads((raw / 'history.json').read_text(encoding='utf-8')).get('value', []) if (raw / 'history.json').exists() else []
    history = [{'id': r['id'], 'commit': r['sourceVersion'], 'conclusion': res.get(r.get('result'), r.get('result')), 'started_at': r.get('startTime')} for r in hist]
    green = next((r for r in sorted(history, key=lambda r: r['started_at'] or '', reverse=True)
                  if r['conclusion'] == 'success' and (r['started_at'] or '') < (b.get('startTime') or '~')), None)
    return {'system': 'azure', 'repo': repo or b.get('repository', {}).get('id'),
            'run': {'id': b['id'], 'url': b.get('_links', {}).get('web', {}).get('href'), 'attempt': max(by_attempt) if by_attempt else 1,
                    'commit': b['sourceVersion'], 'branch': (b.get('sourceBranch') or '').removeprefix('refs/heads/'), 'event': b.get('reason'),
                    'started_at': b.get('startTime'), 'conclusion': res.get(b.get('result'), b.get('result')), 'workflow': b.get('definition', {}).get('name')},
            'attempts': [{'n': n, 'jobs': js} for n, js in sorted(by_attempt.items())], 'history': history,
            'last_green': {'id': green['id'], 'commit': green['commit']} if green else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--system', choices=['github', 'azure'], required=True)
    ap.add_argument('--repo', help='github: owner/name')
    ap.add_argument('--run-id', type=int)
    ap.add_argument('--org'); ap.add_argument('--project'); ap.add_argument('--build-id', type=int)
    ap.add_argument('--from-dir', type=Path, help='a recorded directory of API responses (offline)')
    ap.add_argument('--history', type=int, default=20)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.from_dir:
        raw, source = a.from_dir, 'recorded'
    else:
        raw, source = a.out / 'raw', 'live'
        if a.system == 'github':
            if not (a.repo and a.run_id):
                raise SystemExit('--repo and --run-id are needed for GitHub')
            fetch_github(a.repo, a.run_id, raw, a.history)
        else:
            if not (a.org and a.project and a.build_id):
                raise SystemExit('--org, --project and --build-id are needed for Azure DevOps')
            fetch_azure(a.org, a.project, a.build_id, raw, a.history)
    for d in ('logs', 'junit'):
        if (a.out / d).exists():
            shutil.rmtree(a.out / d)
    run = (normalise_github if a.system == 'github' else normalise_azure)(raw, a.out, a.repo or (f'{a.org}/{a.project}' if a.org else None))
    run['source'] = source
    run['fetched_from'] = Path(os.path.relpath(raw, a.out)).as_posix() if source == 'recorded' else 'raw'
    save(a.out / 'run.json', run)
    r = run['run']
    print(f'{run["system"]} run {r["id"]} attempt {r["attempt"]} on {r["branch"]} @ {r["commit"][:12]}: {r["conclusion"]}; '
          f'{sum(len(x["jobs"]) for x in run["attempts"])} job(s) over {len(run["attempts"])} attempt(s); last green {run["last_green"]} -> {a.out / "run.json"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
