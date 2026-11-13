#!/usr/bin/env python3
"""Re-run the failed jobs of a CI run once, with permission, and record it (stdlib only).

    python rerun.py --run run/ --authorised-by "name, how (chat 2026-11-12 / allow_rerun in the task)" [--wait-minutes 30]
    python rerun.py --run run/ --authorised-by "..." --recorded      # the re-run attempt is already in a recorded run
    python rerun.py --run run/ --observed                             # someone else already re-ran it; nothing is triggered

Re-running is an outward action: it starts jobs on the CI system. The script refuses without
--authorised-by. It re-runs only the jobs that failed in the latest attempt, once (GitHub:
POST .../actions/runs/{id}/rerun-failed-jobs; Azure DevOps: PATCH .../builds/{id}?retry=true), waits for
the new attempt to finish, fetches it again with fetch_run.py, and writes <run>/reruns.json with who
authorised it, what was re-run and the new attempt number. A run that was already re-run is not re-run
again. With --recorded nothing is triggered: the re-run attempt in the recorded responses is recorded.
With --observed nothing is triggered either: a later attempt someone else started (the run's
triggering actor) is recorded as `observed`, with who started it and no authoriser, because this
triage did not ask for it. The jobs re-run are those with a new start time in the new attempt (GitHub
lists every job in a later attempt, carrying over the ones it did not re-run).
"""
from __future__ import annotations
import argparse, base64, json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fetch_run import GH_API, gh_token, _get  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _send(url: str, method: str, headers: dict, body: dict | None = None) -> int:
    req = urllib.request.Request(url, method=method, headers=dict(headers, **{'Content-Type': 'application/json'}),
                                 data=json.dumps(body or {}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def trigger(run: dict, wait_min: int) -> None:
    r = run['run']
    if run['system'] == 'github':
        h = {'Authorization': f'Bearer {gh_token()}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        _send(f'{GH_API}/repos/{run["repo"]}/actions/runs/{r["id"]}/rerun-failed-jobs', 'POST', h)
        status = lambda: _get(f'{GH_API}/repos/{run["repo"]}/actions/runs/{r["id"]}', h)  # noqa: E731
        done = lambda s: int(s.get('run_attempt') or 1) > r['attempt'] and s.get('status') == 'completed'  # noqa: E731
    else:
        org, project = run['repo'].split('/', 1)
        pat = os.environ.get('AZURE_DEVOPS_EXT_PAT') or sys.exit('no Azure DevOps PAT: set AZURE_DEVOPS_EXT_PAT')
        h = {'Authorization': 'Basic ' + base64.b64encode(f':{pat}'.encode()).decode(), 'Accept': 'application/json'}
        base = f'https://dev.azure.com/{org}/{project}/_apis/build/builds/{r["id"]}'
        _send(f'{base}?retry=true&api-version=7.1', 'PATCH', h)
        status = lambda: _get(f'{base}?api-version=7.1', h)  # noqa: E731
        done = lambda s: s.get('status') == 'completed'  # noqa: E731
        time.sleep(15)
    deadline = time.time() + wait_min * 60
    while time.time() < deadline:
        if done(status()):
            return
        time.sleep(20)
    raise SystemExit(f'the re-run had not finished after {wait_min} minutes; fetch the run again later')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--authorised-by', help='who allowed the re-run, and how (required unless --observed)')
    ap.add_argument('--recorded', action='store_true')
    ap.add_argument('--observed', action='store_true', help='record a re-run someone else already started; triggers nothing')
    ap.add_argument('--wait-minutes', type=int, default=30)
    a = ap.parse_args()
    if not a.observed and len((a.authorised_by or '').strip()) < 3:
        raise SystemExit('--authorised-by must say who allowed the re-run (or use --observed for a re-run someone else started)')
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    first = run['attempts'][0]
    failed = [j['name'] for j in first['jobs'] if j['conclusion'] not in ('success', 'skipped')]
    if not failed:
        raise SystemExit('no failed jobs to re-run')
    if a.recorded or a.observed:
        if len(run['attempts']) < 2:
            raise SystemExit('the run has no re-run attempt to record')
    else:
        if len(run['attempts']) > 1:
            raise SystemExit(f'run {run["run"]["id"]} was already re-run (attempt {run["attempts"][-1]["n"]}); not re-running again '
                             '(record it with --observed if someone else started it)')
        trigger(run, a.wait_minutes)
        sys_args = (['--repo', run['repo'], '--run-id', str(run['run']['id'])] if run['system'] == 'github'
                    else ['--org', run['repo'].split('/')[0], '--project', run['repo'].split('/')[1], '--build-id', str(run['run']['id'])])
        r = subprocess.run([sys.executable, str(HERE / 'fetch_run.py'), '--system', run['system'], *sys_args, '--out', str(a.run)])
        if r.returncode not in (0, 2):  # 2: written, but a log download failed (fetch_run.py printed which)
            raise SystemExit(f'fetch_run.py failed ({r.returncode}) after the re-run; fetch the run again')
        run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    new = run['attempts'][-1]
    rerun_jobs = new['jobs_rerun'] if new.get('jobs_rerun') is not None else [j['name'] for j in new['jobs']]
    mode = 'observed' if a.observed else 'recorded' if a.recorded else 'live'
    rec = {'run_id': run['run']['id'], 'from_attempt': first['n'], 'new_attempt': new['n'], 'jobs_rerun': rerun_jobs,
           'jobs_failed_before': failed, 'authorised_by': None if a.observed else a.authorised_by, 'mode': mode,
           'at': (new.get('started_at') or 'unknown') if mode != 'live' else now(),
           'outcomes': {j['name']: j['conclusion'] for j in new['jobs'] if j['name'] in rerun_jobs}}
    if a.observed:
        rec['triggered_by'] = new.get('triggered_by') or 'unknown'
    extra = sorted(set(rec['jobs_rerun']) - set(failed))
    if extra:
        rec['note'] = f'jobs in the new attempt that had not failed: {extra}'
    (a.run / 'reruns.json').write_text(json.dumps({'reruns': [rec]}, indent=1) + '\n', encoding='utf-8', newline='\n')
    who = f'started by {rec["triggered_by"]}, not by this triage' if a.observed else f'authorised by {a.authorised_by}'
    print(f'attempt {new["n"]}: ' + ', '.join(f'{k} {v}' for k, v in rec['outcomes'].items()) + f' ({who})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
