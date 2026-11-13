#!/usr/bin/env python3
"""Self-test for the crash hunter's scripts: runs scan_pitfalls.py --self-test (promise chains, scope, recon,
the language guard, vendored trees and HTML in string constants), then checks probe_web.py (file:// targets,
query strings kept in recorded requests, a real file:// probe when Playwright is installed) and the
validator (a web probe against a native site model, a not-analysed item that names the route id first).

    python self_test.py

Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import probe_web  # noqa: E402
import validate_crash_report as vcr  # noqa: E402

EXAMPLES = HERE.parent / 'examples'


class FakePage:
    """Just enough of a Playwright page for log_requests: event handlers to call by hand."""
    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn


class FakeReq:
    def __init__(self, url, method='GET'):
        self.url, self.method = url, method


class FakeResp:
    def __init__(self, url, status=200):
        self.url, self.status, self.request = url, status, FakeReq(url)


def probe_checks() -> list[str]:
    fails = []
    report = Path(tempfile.gettempdir()) / 'harbour' / 'report.html'
    url = report.resolve().as_uri()
    if not probe_web.is_loopback(url):
        fails.append(f'a file:// target is not treated as local: is_loopback({url}) is False')
    if not probe_web.read_only({}, url):
        fails.append('a file:// target does not default to read-only')
    try:
        got = probe_web.target_url(str(report))
    except AttributeError:
        got = None
    if got != url:
        fails.append(f'a local file path given as the base URL is not turned into a file:// URL (got {got})')
    try:
        joined = probe_web.step_url(url, '#flow-3')
    except AttributeError:
        joined = None
    if joined != url + '#flow-3':
        fails.append(f"goto '#flow-3' on a file:// report does not stay on the report (got {joined})")
    page, requests = FakePage(), []
    probe_web.log_requests(page, requests)
    page.handlers['response'](FakeResp('http://127.0.0.1:8191/run/report.json?t=1715'))
    if not requests or requests[0]['url'] != 'http://127.0.0.1:8191/run/report.json?t=1715':
        fails.append(f'the recorded request dropped its query string: {requests[:1]}')
    res = probe_web.check_expectations(None, [{'request': '**/report.json?t=*'}, {'request': '**/run/report.json'}],
                                       {'requests': requests})
    if not all(r['passed'] for r in res):
        fails.append(f'request globs with and without a query did not match the recorded request: {res}')
    return fails


def live_probe_check() -> tuple[list[str], str]:
    """A real probe of a local file:// page, read-only, with a third-party request blocked."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / 'report.html').write_text(
            '<!doctype html><html><body><div id="app"><p>Runs</p></div>'
            '<script>fetch("https://telemetry.example.invalid/hit?id=1").catch(() => {});'
            'document.getElementById("app").dataset.ready = location.protocol;</script></body></html>', encoding='utf-8')
        (t / 'scenario.json').write_text(json.dumps({
            'name': 'file report baseline', 'kind': 'baseline', 'steps': [{'goto': '#flow-1'}],
            'expect': [{'visible': '#app[data-ready="file:"]'}], 'settle_ms': 200}), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'probe_web.py'), str(t / 'scenario.json'), '--out-dir',
                            str(t / 'ev'), '--base-url', str(t / 'report.html')], capture_output=True, text=True,
                           encoding='utf-8')
        try:
            ev = json.loads((t / 'ev' / 'evidence.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [f'probe_web.py on a file:// report failed: {(r.stderr or r.stdout)[-400:]}'], 'ran'
        if not ev.get('read_only') or not ev.get('baseline_ok') or ev.get('inconclusive'):
            fails.append(f"file:// probe: read_only {ev.get('read_only')}, baseline_ok {ev.get('baseline_ok')}, "
                         f"inconclusive {ev.get('inconclusive_reason')}")
        if not any('telemetry.example.invalid/hit?id=1' in b for b in ev.get('blocked_requests', [])):
            fails.append(f"file:// probe: the third-party request was not blocked and recorded with its query: "
                         f"{ev.get('blocked_requests')}")
    return fails, 'ran'


def validator_checks() -> list[str]:
    fails = []
    base = json.loads((EXAMPLES / 'crash_report.example.json').read_text(encoding='utf-8'))
    d = json.loads(json.dumps(base))
    d['recon']['site_model'] = 'native'
    d['recon']['blast_radius'] = {'model': 'per_process', 'note': 'x'}
    errors, warnings = vcr.check(d, EXAMPLES, None, None)
    if not any('native' in w for w in warnings + errors):
        fails.append('validator: browser probes against a "native" site model with platforms [web] pass silently')
    d = json.loads(json.dumps(base))
    routes = d['recon']['routes_screens']
    rid = next((r['id'] for r in routes if not r.get('analysed')), None)
    if rid is None:
        routes[-1]['analysed'] = False
        rid = routes[-1]['id']
        d['coverage']['routes_analysed'] = sum(1 for r in routes if r.get('analysed'))
    d['coverage']['not_analysed'] = [x for x in d['coverage'].get('not_analysed', [])
                                     if x.get('item') not in (rid, routes[[r['id'] for r in routes].index(rid)]['path'])]
    d['coverage']['not_analysed'].append({'item': f'{rid} (the about screen)', 'reason': 'static page with no script'})
    errors, _ = vcr.check(d, EXAMPLES, None, None)
    if any(f'route {rid} was not analysed' in e for e in errors):
        fails.append(f'validator: a not_analysed item that starts with the route id ("{rid} (...)") is rejected')
    return fails


def main() -> int:
    ok = True
    r = subprocess.run([sys.executable, '-B', str(HERE / 'scan_pitfalls.py'), '--self-test'], capture_output=True, text=True,
                       encoding='utf-8')
    print(r.stdout.rstrip())
    ok &= r.returncode == 0
    for name, fn in (('probe_web.py: file:// targets and query strings', probe_checks),
                     ('validate_crash_report.py: site model and not_analysed items', validator_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = live_probe_check()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: a live read-only probe of a file:// report ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
