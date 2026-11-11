#!/usr/bin/env python3
"""A loopback stand-in for the GitHub Actions and Azure DevOps APIs, for the CI suite runner's example.

    python server.py [--port 8170] [--work <scratch dir>]

It accepts a workflow_dispatch (GitHub) or a pipeline run (Azure DevOps), then runs the requested tests for
real: `python -m pytest suites/ -m "<tags>" --junitxml=...` in a background thread, while the run moves
from queued to in progress to completed. It serves runs, jobs, logs and the JUnit as a zipped artifact in
each API's response shape (only the fields the runner reads). Any bearer or basic token is accepted, but
one is required. Every request is logged; GET /stub/log returns the log, so the example can show which
triggers arrived. Nothing leaves loopback.
"""
from __future__ import annotations
import argparse, base64, io, json, re, subprocess, sys, tempfile, threading, time, zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS: dict[int, dict] = {}
LOG: list[dict] = []
LOCK = threading.Lock()
WORK = {'dir': None}
NEXT = {'gh': 7001, 'az': 3301}


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def execute(run: dict):
    time.sleep(1.5)
    with LOCK:
        run['status'] = 'in_progress'
    out = Path(WORK['dir']) / f'run-{run["id"]}'
    out.mkdir(parents=True, exist_ok=True)
    junit = out / 'junit.xml'
    r = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-o', 'console_output_style=classic',
                        '-m', run['tags'], f'--junitxml={junit}'], cwd=HERE / 'suites', capture_output=True, text=True)
    xml = re.sub(r'hostname="[^"]*"', 'hostname="ci-runner"', junit.read_text(encoding='utf-8')) if junit.exists() else ''
    time.sleep(1.0)
    with LOCK:
        log = (r.stdout + r.stderr).replace(str(HERE / 'suites'), '/home/runner/work/suites').replace(str(out), '/home/runner/work/_temp')
        run['log'] = re.sub(r'(?<=[\w.])\\(?=[\w.])', '/', log)
        run['junit'] = xml
        run['status'] = 'completed'
        run['conclusion'] = 'success' if r.returncode == 0 else 'failure'
        run['completed_at'] = now()


def zipped(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('junit.xml', xml)
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code: int, obj=None, raw: bytes | None = None, ctype='application/json'):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b'')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method: str):
        path = self.path.split('?')[0]
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        with LOCK:
            LOG.append({'at': now(), 'method': method, 'path': path, 'body': body if method != 'GET' else None})
        if path == '/stub/log':
            return self.reply(200, LOG)
        if not self.headers.get('Authorization'):
            return self.reply(401, {'message': 'Requires authentication'})
        base = f'http://{self.headers.get("Host")}'
        # GitHub ---------------------------------------------------------------------------------------
        m = re.fullmatch(r'/github/repos/([^/]+)/([^/]+)/actions/workflows/([^/]+)/dispatches', path)
        if m and method == 'POST':
            if not body.get('ref'):
                return self.reply(422, {'message': 'ref is required'})
            with LOCK:
                rid = NEXT['gh']; NEXT['gh'] += 1
                run = {'id': rid, 'system': 'github', 'repo': f'{m.group(1)}/{m.group(2)}', 'workflow': m.group(3), 'ref': body['ref'], 'inputs': body.get('inputs', {}),
                       'tags': body.get('inputs', {}).get('tags') or 'not slow', 'status': 'queued', 'conclusion': None, 'created_at': now(), 'event': 'workflow_dispatch'}
                RUNS[rid] = run
            threading.Thread(target=execute, args=(run,), daemon=True).start()
            return self.reply(204)
        m = re.fullmatch(r'/github/repos/([^/]+)/([^/]+)/actions/runs', path)
        if m and method == 'GET':
            with LOCK:
                rs = [r for r in RUNS.values() if r['system'] == 'github']
            return self.reply(200, {'total_count': len(rs), 'workflow_runs': [
                {'id': r['id'], 'status': r['status'], 'conclusion': r['conclusion'], 'event': r['event'], 'head_branch': r['ref'], 'created_at': r['created_at'],
                 'path': f'.github/workflows/{r["workflow"]}', 'run_attempt': 1, 'html_url': f'https://github.com/{r["repo"]}/actions/runs/{r["id"]}'}
                for r in sorted(rs, key=lambda r: -r['id'])]})
        m = re.fullmatch(r'/github/repos/([^/]+)/([^/]+)/actions/runs/(\d+)(/jobs|/artifacts)?', path)
        if m and method == 'GET':
            r = RUNS.get(int(m.group(3)))
            if not r:
                return self.reply(404, {'message': 'Not Found'})
            if m.group(4) == '/jobs':
                return self.reply(200, {'total_count': 1, 'jobs': [{'id': r['id'] * 10, 'name': 'tests', 'status': r['status'], 'conclusion': r['conclusion'],
                                                                    'steps': [{'number': 1, 'name': 'Run pytest', 'status': r['status'], 'conclusion': r['conclusion']}]}]})
            if m.group(4) == '/artifacts':
                arts = [{'id': r['id'] * 100, 'name': 'junit', 'size_in_bytes': len(r.get('junit', '')), 'expired': False,
                         'archive_download_url': f'{base}/github/artifacts/{r["id"] * 100}/zip'}] if r['status'] == 'completed' else []
                return self.reply(200, {'total_count': len(arts), 'artifacts': arts})
            return self.reply(200, {'id': r['id'], 'status': r['status'], 'conclusion': r['conclusion'], 'head_branch': r['ref'], 'event': r['event'],
                                    'created_at': r['created_at'], 'run_attempt': 1, 'html_url': f'https://github.com/{r["repo"]}/actions/runs/{r["id"]}'})
        m = re.fullmatch(r'/github/artifacts/(\d+)/zip', path)
        if m:
            r = RUNS.get(int(m.group(1)) // 100)
            return self.reply(200, raw=zipped(r['junit']), ctype='application/zip') if r and r.get('junit') else self.reply(404, {'message': 'Not Found'})
        m = re.fullmatch(r'/github/repos/[^/]+/[^/]+/actions/jobs/(\d+)/logs', path)
        if m:
            r = RUNS.get(int(m.group(1)) // 10)
            return self.reply(200, raw=(r.get('log') or '').encode(), ctype='text/plain') if r else self.reply(404, {'message': 'Not Found'})
        # Azure DevOps --------------------------------------------------------------------------------
        m = re.fullmatch(r'/azure/([^/]+)/([^/]+)/_apis/pipelines/(\d+)/runs', path)
        if m and method == 'POST':
            params = body.get('templateParameters', {})
            ref = (((body.get('resources') or {}).get('repositories') or {}).get('self') or {}).get('refName')
            with LOCK:
                rid = NEXT['az']; NEXT['az'] += 1
                run = {'id': rid, 'system': 'azure', 'org': m.group(1), 'project': m.group(2), 'pipeline': int(m.group(3)), 'ref': ref, 'inputs': params,
                       'tags': params.get('tags') or 'smoke', 'status': 'queued', 'conclusion': None, 'created_at': now()}
                RUNS[rid] = run
            threading.Thread(target=execute, args=(run,), daemon=True).start()
            return self.reply(200, {'id': rid, 'state': 'inProgress', 'result': None, 'createdDate': run['created_at'],
                                    '_links': {'web': {'href': f'https://dev.azure.com/{m.group(1)}/{m.group(2)}/_build/results?buildId={rid}'}}})
        m = re.fullmatch(r'/azure/([^/]+)/([^/]+)/_apis/pipelines/(\d+)/runs/(\d+)', path)
        if m and method == 'GET':
            r = RUNS.get(int(m.group(4)))
            if not r:
                return self.reply(404, {'message': 'not found'})
            state = {'queued': 'inProgress', 'in_progress': 'inProgress', 'completed': 'completed'}[r['status']]
            return self.reply(200, {'id': r['id'], 'state': state, 'result': {'success': 'succeeded', 'failure': 'failed'}.get(r['conclusion']),
                                    'createdDate': r['created_at'], 'finishedDate': r.get('completed_at')})
        m = re.fullmatch(r'/azure/([^/]+)/([^/]+)/_apis/build/builds/(\d+)/(artifacts|timeline|logs/1)', path)
        if m and method == 'GET':
            r = RUNS.get(int(m.group(3)))
            if not r:
                return self.reply(404, {'message': 'not found'})
            if m.group(4) == 'artifacts':
                return self.reply(200, {'count': 1, 'value': [{'name': 'junit', 'resource': {'downloadUrl': f'{base}/azure/zip/{r["id"]}'}}]} if r['status'] == 'completed' else {'count': 0, 'value': []})
            if m.group(4) == 'timeline':
                res = {'success': 'succeeded', 'failure': 'failed'}.get(r['conclusion'])
                return self.reply(200, {'records': [{'id': 'job-1', 'type': 'Job', 'name': 'tests', 'state': 'completed' if res else 'inProgress', 'result': res, 'log': {'id': 1}}]})
            return self.reply(200, raw=(r.get('log') or '').encode(), ctype='text/plain')
        m = re.fullmatch(r'/azure/zip/(\d+)', path)
        if m:
            r = RUNS.get(int(m.group(1)))
            return self.reply(200, raw=zipped(r['junit']), ctype='application/zip') if r and r.get('junit') else self.reply(404, {'message': 'not found'})
        return self.reply(404, {'message': f'no stub for {method} {path}'})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PATCH(self):
        self.handle_any('PATCH')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8170)
    ap.add_argument('--work', type=Path)
    a = ap.parse_args()
    WORK['dir'] = str(a.work or tempfile.mkdtemp(prefix='ci-stub-'))
    ThreadingHTTPServer.allow_reuse_address = sys.platform != 'win32'
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), H)
    print(f'CI stub on http://127.0.0.1:{a.port} (github under /github, azure under /azure)', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
