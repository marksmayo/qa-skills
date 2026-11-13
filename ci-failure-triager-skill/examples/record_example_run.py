#!/usr/bin/env python3
"""Make examples/recorded/ from real local runs of the tidewater sample (stdlib + git + pytest + pip).

    python record_example_run.py <tidewater checkout> --out examples/recorded

No real CI is called. The test and install output inside the logs and the JUnit files are real runs on
this machine: pytest at the failing commit (attempt 1 repeated until the flaky test fails, attempt 2
until it passes), and pip against an index on a closed loopback port. The envelopes around them follow
the shapes of the GitHub Actions REST API (run, attempts/N/jobs, jobs/{id}/logs, artifacts, runs on the
branch) and the Azure DevOps Build REST API (build, timeline, logs, artifacts, builds on the branch), with
fixed ids and timestamps. Windows path separators in the test output are changed to "/", the runner's.
"""
from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys, tempfile
from pathlib import Path

OWNER_REPO = 'example-org/tidewater'
RUN_ID, GREEN_RUN = 109, 101
WORKFLOW_ID = 71  # the id GitHub gives .github/workflows/ci.yml; history is matched to the run by it
JOBS = {1: {'unit': 5101, 'install-check': 5102}, 2: {'unit': 5201, 'install-check': 5202}}
T = {1: '2026-11-12T03:10', 2: '2026-11-12T03:40'}
PIP = [sys.executable, '-m', 'pip', 'install', '--index-url', 'http://127.0.0.1:9/simple/', '--retries', '0', '--timeout', '2',
       '--disable-pip-version-check', '--no-cache-dir', 'tidewater-internal-pricing==2.1']


def sh(args, cwd) -> tuple[int, str]:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.returncode, (r.stdout + r.stderr).replace('\r\n', '\n')


def pytest_until(repo: Path, flaky_fails: bool, junit: Path) -> tuple[int, str]:
    for _ in range(40):
        code, out = sh([sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider', '--tb=short', '-o', 'console_output_style=classic',
                        f'--junitxml={junit}'], repo)
        if ('test_pick_two_gives_two_different_slots FAILED' in out or 'FAILED tests/test_slots.py' in out.replace('\\', '/')) == flaky_fails:
            return code, out
    raise SystemExit("the flaky test did not do what was needed in 40 runs")


def clean(text: str, repo: Path) -> str:
    text = text.replace(str(repo), '/home/runner/work/tidewater/tidewater')
    text = re.sub(r'(?<=[\w.])\\(?=[\w.])', '/', text)
    return re.sub(r'(rootdir|platform) .*', lambda m: m.group(0).split(' ')[0] + ' (runner)', text)


def stamp(t: str, lines: list[str], start_s: int = 0) -> list[str]:
    return [f'{t}:{(start_s + i // 20) % 60:02d}.{(i * 137) % 10_000_000:07d}Z {l}' for i, l in enumerate(lines)]


def job_log(attempt: int, name: str, body: str, code: int, command: str) -> str:
    pre = ['##[group]Runner Image', 'Image: ubuntu-24.04', '##[endgroup]', '##[group]Run actions/checkout@v5', '##[endgroup]']
    if name == 'unit':
        pre += ['##[group]Run actions/setup-python@v6', '##[endgroup]', '##[group]Run pip install pytest', '##[endgroup]']
    lines = pre + [f'##[group]Run {command}', f'{command}', '##[endgroup]'] + body.rstrip('\n').split('\n')
    if code:
        lines.append(f'##[error]Process completed with exit code {code}.')
    return '\n'.join(stamp(T[attempt], lines)) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    repo = a.repo.resolve()
    head = sh(['git', 'rev-parse', 'HEAD'], repo)[1].strip()
    green = sh(['git', 'rev-list', '--max-parents=0', 'HEAD'], repo)[1].strip()
    if a.out.exists():
        shutil.rmtree(a.out)
    gh, az = a.out / 'github', a.out / 'azure'
    for d in (gh / 'logs', az / 'logs'):
        d.mkdir(parents=True)
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for attempt in (1, 2):
            junit = repo / 'junit.xml'  # inside the checkout, so its path in the output is the runner's
            code, out = pytest_until(repo, flaky_fails=(attempt == 1), junit=junit)
            pcode, pout = sh(PIP, repo)
            xml = clean(re.sub(r'hostname="[^"]*"', 'hostname="runner"', junit.read_text(encoding='utf-8')), repo)
            art = gh / 'attempts' / str(attempt) / 'artifacts' / 'junit-unit'
            art.mkdir(parents=True)
            (art / 'junit.xml').write_text(xml, encoding='utf-8', newline='\n')
            junit.unlink()
            results[attempt] = (code, clean(out, repo), pcode, clean(pout, repo), xml)
    pytest_cmd, pip_cmd = 'python -m pytest --junitxml=junit.xml', 'pip install --index-url http://127.0.0.1:9/simple/ tidewater-internal-pricing==2.1'

    def jobs_doc(attempt):
        code, _, pcode, _, _ = results[attempt]
        js = []
        for name, steps in (('unit', ['Set up job', 'Run actions/checkout@v5', 'Run actions/setup-python@v6', 'Run pip install pytest',
                                      f'Run {pytest_cmd}', 'Run actions/upload-artifact@v5', 'Complete job']),
                            ('install-check', ['Set up job', 'Run actions/checkout@v5', 'Install internal pricing package', 'Complete job'])):
            failed_step = 4 if name == 'unit' else 2
            ok = (code if name == 'unit' else pcode) == 0
            js.append({'id': JOBS[attempt][name], 'run_id': RUN_ID, 'run_attempt': attempt, 'name': name, 'status': 'completed',
                       'conclusion': 'success' if ok else 'failure', 'head_sha': head,
                       'started_at': f'{T[attempt]}:00Z', 'completed_at': f'{T[attempt]}:59Z',
                       'html_url': f'https://github.com/{OWNER_REPO}/actions/runs/{RUN_ID}/job/{JOBS[attempt][name]}',
                       'steps': [{'number': i + 1, 'name': s, 'status': 'completed',
                                  'conclusion': ('failure' if i == failed_step and not ok else 'skipped' if i > failed_step and not ok and s != 'Complete job' and 'upload' not in s else 'success')}
                                 for i, s in enumerate(steps)]})
        return {'total_count': len(js), 'jobs': js}

    for attempt in (1, 2):
        code, out, pcode, pout, _ = results[attempt]
        (gh / 'attempts' / str(attempt) / 'jobs.json').write_text(json.dumps(jobs_doc(attempt), indent=1) + '\n', encoding='utf-8', newline='\n')
        (gh / 'logs' / f'{JOBS[attempt]["unit"]}.txt').write_text(job_log(attempt, 'unit', out, code, pytest_cmd), encoding='utf-8', newline='\n')
        (gh / 'logs' / f'{JOBS[attempt]["install-check"]}.txt').write_text(job_log(attempt, 'install-check', pout, pcode, pip_cmd), encoding='utf-8', newline='\n')
        (gh / 'attempts' / str(attempt) / 'artifacts.json').write_text(json.dumps({'total_count': 1, 'artifacts': [
            {'id': 9000 + attempt, 'name': 'junit-unit', 'workflow_run': {'id': RUN_ID}, 'expired': False,
             'archive_download_url': f'https://api.github.com/repos/{OWNER_REPO}/actions/artifacts/{9000 + attempt}/zip'}]}, indent=1) + '\n', encoding='utf-8', newline='\n')
    run = {'id': RUN_ID, 'name': 'ci', 'head_branch': 'main', 'head_sha': head, 'event': 'push', 'status': 'completed', 'conclusion': 'failure',
           'run_attempt': 2, 'run_number': 37, 'run_started_at': f'{T[1]}:00Z', 'html_url': f'https://github.com/{OWNER_REPO}/actions/runs/{RUN_ID}',
           'repository': {'full_name': OWNER_REPO}, 'path': '.github/workflows/ci.yml', 'workflow_id': WORKFLOW_ID}
    (gh / 'run.json').write_text(json.dumps(run, indent=1) + '\n', encoding='utf-8', newline='\n')
    hist = {'total_count': 2, 'workflow_runs': [
        {'id': RUN_ID, 'head_sha': head, 'head_branch': 'main', 'conclusion': 'failure', 'run_attempt': 1, 'run_started_at': f'{T[1]}:00Z', 'event': 'push',
         'workflow_id': WORKFLOW_ID, 'path': '.github/workflows/ci.yml'},
        {'id': GREEN_RUN, 'head_sha': green, 'head_branch': 'main', 'conclusion': 'success', 'run_attempt': 1, 'run_started_at': '2026-11-09T01:20:00Z', 'event': 'push',
         'workflow_id': WORKFLOW_ID, 'path': '.github/workflows/ci.yml'}]}
    (gh / 'history.json').write_text(json.dumps(hist, indent=1) + '\n', encoding='utf-8', newline='\n')

    # Azure DevOps: build 2207 of the same commit (attempt 1 output), and the earlier green build
    code, out, pcode, pout, xml = results[1]
    build = {'id': 2207, 'buildNumber': '20261112.3', 'status': 'completed', 'result': 'failed', 'sourceBranch': 'refs/heads/main', 'sourceVersion': head,
             'startTime': f'{T[1]}:00Z', 'reason': 'individualCI', 'definition': {'id': 4, 'name': 'tidewater-ci'},
             'repository': {'id': 'tidewater', 'type': 'TfsGit'}, '_links': {'web': {'href': 'https://dev.azure.com/example-org/tidewater/_build/results?buildId=2207'}}}
    tl = {'records': [
        {'id': 'stage-1', 'parentId': None, 'type': 'Stage', 'name': 'Build', 'state': 'completed', 'result': 'failed', 'attempt': 1, 'log': None},
        {'id': 'job-unit', 'parentId': 'stage-1', 'type': 'Job', 'name': 'unit', 'state': 'completed', 'result': 'failed', 'attempt': 1, 'log': {'id': 5}},
        {'id': 'task-pytest', 'parentId': 'job-unit', 'type': 'Task', 'name': 'pytest', 'order': 3, 'state': 'completed', 'result': 'failed', 'attempt': 1, 'log': {'id': 7}},
        {'id': 'job-install', 'parentId': 'stage-1', 'type': 'Job', 'name': 'install-check', 'state': 'completed', 'result': 'failed', 'attempt': 1, 'log': {'id': 8}},
        {'id': 'task-install', 'parentId': 'job-install', 'type': 'Task', 'name': 'Install internal pricing package', 'order': 2, 'state': 'completed', 'result': 'failed', 'attempt': 1, 'log': {'id': 9}}]}
    (az / 'build.json').write_text(json.dumps(build, indent=1) + '\n', encoding='utf-8', newline='\n')
    (az / 'timeline.json').write_text(json.dumps(tl, indent=1) + '\n', encoding='utf-8', newline='\n')
    (az / 'logs' / '7.txt').write_text('\n'.join(stamp(T[1], [f'##[section]Starting: pytest', pytest_cmd] + out.rstrip('\n').split('\n') + [f'##[error]Bash exited with code \'{code}\'.'])) + '\n', encoding='utf-8', newline='\n')
    (az / 'logs' / '9.txt').write_text('\n'.join(stamp(T[1], ['##[section]Starting: Install internal pricing package', pip_cmd] + pout.rstrip('\n').split('\n') + [f'##[error]Bash exited with code \'{pcode}\'.'])) + '\n', encoding='utf-8', newline='\n')
    (az / 'artifacts' / 'junit-unit').mkdir(parents=True)
    (az / 'artifacts' / 'junit-unit' / 'junit.xml').write_text(xml, encoding='utf-8', newline='\n')
    (az / 'artifacts.json').write_text(json.dumps({'count': 1, 'value': [{'id': 31, 'name': 'junit-unit', 'resource': {'type': 'Container',
        'downloadUrl': 'https://dev.azure.com/example-org/tidewater/_apis/build/builds/2207/artifacts?artifactName=junit-unit&$format=zip'}}]}, indent=1) + '\n', encoding='utf-8', newline='\n')
    (az / 'history.json').write_text(json.dumps({'count': 2, 'value': [
        {'id': 2207, 'sourceVersion': head, 'sourceBranch': 'refs/heads/main', 'result': 'failed', 'startTime': f'{T[1]}:00Z', 'reason': 'individualCI', 'definition': {'id': 4}},
        {'id': 2201, 'sourceVersion': green, 'sourceBranch': 'refs/heads/main', 'result': 'succeeded', 'startTime': '2026-11-09T01:20:00Z', 'reason': 'individualCI',
         'definition': {'id': 4}}]},
        indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'recorded run {RUN_ID} (attempts 1 and 2) and build 2207 for {head[:12]} -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
