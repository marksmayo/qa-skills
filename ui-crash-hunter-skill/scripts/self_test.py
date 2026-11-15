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
    # a page error raised by a fetch the probe itself blocked (a third-party key check) is not the app's error
    errs = [{'after_step': 1, 'name': 'TypeError', 'message': 'Failed to fetch', 'stack': 'TypeError: Failed to fetch\n    at check (app.js:4)'},
            {'after_step': 1, 'name': 'TypeError', 'message': "Cannot read properties of null (reading 'x')", 'stack': ''}]
    split = getattr(probe_web, 'split_blocked_errors', None)
    if split is None:
        fails.append('probe_web has no split_blocked_errors: page errors caused by blocked third-party requests count as the app\'s')
    else:
        own, caused = split(errs, ['https://keys.example.invalid/check'], [])
        if [e['message'] for e in caused] != ['Failed to fetch'] or len(own) != 1:
            fails.append(f'blocked-request page errors not set apart: own {own}, blocked {caused}')
        own, caused = split(errs, [], [])
        if caused:
            fails.append('a network page error with nothing blocked was attributed to the block')
        own, caused = split(errs, ['https://keys.example.invalid/check'],
                            [{'after_step': 1, 'url': 'http://127.0.0.1:9/api', 'failure': 'net::ERR_CONNECTION_REFUSED'}])
        if caused:
            fails.append('a network page error was attributed to the block although an own request failed too')
    # Firefox reports an aborted crossorigin third-party script as a CORS failure, not ERR_BLOCKED_BY_CLIENT; a 404
    # document the scenario expects (document_status 404) logs "Failed to load resource" in Chromium
    cors = ('[JavaScript Error: "Cross-Origin Request Blocked: The Same Origin Policy disallows reading the remote '
            'resource at https://ads.example.net/load.js. (Reason: CORS request did not succeed). Status code: (null)."]')
    console = [{'type': 'error', 'text': 'Failed to load resource: the server responded with a status of 404 (Not Found)',
                'url': 'http://127.0.0.1:8620/no-such-bike'},
               {'type': 'error', 'text': cors},
               {'type': 'error', 'text': 'net::ERR_BLOCKED_BY_CLIENT'},
               {'type': 'error', 'text': "TypeError: can't access property \"name\", bike is undefined"},
               {'type': 'warning', 'text': 'a warning'}]
    split = getattr(probe_web, 'split_console', None)
    if split is None:
        fails.append('probe_web has no split_console: harness blocks reported as CORS failures (Firefox) and an expected '
                     '404 document count as the app\'s console errors')
    else:
        own, blocked, expected = split(console, ['https://ads.example.net/load.js?client=1'],
                                       [{'url': 'http://127.0.0.1:8620/no-such-bike', 'status': 404}])
        if [c['text'][:9] for c in own] != ['TypeError'] or len(blocked) != 2 or len(expected) != 1:
            fails.append(f'split_console: own {[c["text"][:40] for c in own]}, blocked {len(blocked)}, expected {len(expected)}')
        own, _, expected = split(console, [], [])
        if expected or len(own) != 3:
            fails.append('split_console: with no expected document status, the 404 must stay the app\'s error')
    sc = {'steps': [{'goto': '/'}, {'back': True}, {'forward': True}],
          'expect': [{'document_status': 404}, {'service_worker': '**/sw.js', 'state': 'activated'},
                     {'cache_entries': 'spokes-*', 'min': 1}]}
    if probe_web.validate_scenario(sc):
        fails.append(f'validate_scenario rejects forward steps or document_status / service_worker / cache_entries '
                     f'assertions: {probe_web.validate_scenario(sc)}')
    try:
        res = probe_web.check_expectations(None, [{'document_status': 404}], {'documents': [
            {'url': 'http://127.0.0.1:8620/', 'status': 200}, {'url': 'http://127.0.0.1:8620/x', 'status': 404}]})
        if not res[0]['passed'] or res[0]['actual'] != 404:
            fails.append(f'document_status assertion does not read the last document response: {res}')
    except (ValueError, KeyError, TypeError) as exc:
        fails.append(f'document_status assertion unsupported: {exc}')
    # an emitted repro spec is standalone: every global name its functions use is defined in it
    import builtins, dis, types
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / 'repro_x.py'
        probe_web.emit_spec({'name': 'x', 'steps': [{'goto': '/'}], 'expect': [{'document_status': 404}]},
                            'http://127.0.0.1:8620', spec, 'x')
        ns: dict = {'__name__': 'repro_spec'}
        exec(compile(spec.read_text(encoding='utf-8'), str(spec), 'exec'), ns)

    def globals_of(code) -> set:
        out = {i.argval for i in dis.get_instructions(code) if i.opname in ('LOAD_GLOBAL', 'LOAD_NAME')}
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                out |= globals_of(c)
        return out
    missing = sorted({n for v in ns.values() if isinstance(v, types.FunctionType) and v.__module__ == 'repro_spec'
                      for n in globals_of(v.__code__)} - set(ns) - set(dir(builtins)))
    if missing:
        fails.append(f'the emitted repro spec uses names it does not define: {missing}')
    # the strict tsc overlay merges repeated "'x' is possibly 'undefined'" hits on one variable in one file
    import strict_overlay
    group = getattr(strict_overlay, 'group_null_hits', None)
    errs = [{'file': 'src/app/[slug]/page.tsx', 'line': n, 'column': 5, 'code': 'TS18048',
             'message': "'bike' is possibly 'undefined'."} for n in (12, 14, 15)] + \
           [{'file': 'src/app/[slug]/page.tsx', 'line': 20, 'column': 3, 'code': 'TS2532',
             'message': "Object is possibly 'undefined'."},
            {'file': 'src/app/page.tsx', 'line': 4, 'column': 3, 'code': 'TS18048', 'message': "'bike' is possibly 'undefined'."}]
    if group is None:
        fails.append('strict_overlay has no group_null_hits: repeated TS18048 hits on one variable are separate candidates')
    else:
        got = group(errs)
        first = next((e for e in got if e['line'] == 12), {})
        if len(got) != 3 or first.get('also_lines') != [14, 15]:
            fails.append(f'group_null_hits: {[(e["file"], e["line"], e.get("also_lines")) for e in got]}')
    # requests are recorded from the browser context, so a service worker's own fetches are seen and marked
    page, requests, documents = FakePage(), [], []
    try:
        probe_web.log_requests(page, requests, documents)
        resp = FakeResp('http://127.0.0.1:8620/sw.js')
        resp.request.service_worker = object()
        page.handlers['response'](resp)
        if not requests or not requests[0].get('service_worker'):
            fails.append(f'a request a service worker made is not marked service_worker: {requests}')
    except TypeError as exc:
        fails.append(f'log_requests does not record document responses: {exc}')
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
            'fetch("https://keys.example.invalid/check").then(r => r.json());'
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
        if not ev.get('blocked_page_errors') or ev.get('page_errors'):
            fails.append(f"file:// probe: the unhandled rejection from the blocked key check counts as the app's page error: "
                         f"page_errors {ev.get('page_errors')}, blocked_page_errors {ev.get('blocked_page_errors')}")
        if not ev.get('read_only') or not ev.get('baseline_ok') or ev.get('inconclusive'):
            fails.append(f"file:// probe: read_only {ev.get('read_only')}, baseline_ok {ev.get('baseline_ok')}, "
                         f"inconclusive {ev.get('inconclusive_reason')}")
        tm = ev.get('timing') or {}
        if not all(isinstance(tm.get(k), (int, float)) for k in ('launch_s', 'steps_s', 'screenshot_s', 'close_s', 'total_s')):
            fails.append(f'file:// probe: timing does not account for the screenshot and browser shutdown: {tm}')
        if not any('telemetry.example.invalid/hit?id=1' in b for b in ev.get('blocked_requests', [])):
            fails.append(f"file:// probe: the third-party request was not blocked and recorded with its query: "
                         f"{ev.get('blocked_requests')}")
    return fails, 'ran'


LIVE_SITE = {
    'index.html': ('<!doctype html><html><head><title>Spokes</title>'
                   '<script src="https://ads.example.net/load.js" crossorigin="anonymous" async></script>'
                   '<script src="https://stats.example.com/tag.js" async></script></head>'
                   '<body><main id="app"><h1>Bikes</h1></main><script>'
                   "if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js').then(function () {"
                   " return navigator.serviceWorker.ready; }).then(function () {"
                   " document.getElementById('app').dataset.sw = '1'; });</script></body></html>"),
    'sw.js': ("self.addEventListener('install', function (e) { self.skipWaiting();"
              " e.waitUntil(caches.open('spokes-v1').then(function (c) { return c.addAll(['/']); })); });\n"
              "self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });\n"
              "self.addEventListener('fetch', function (e) { e.respondWith(fetch(e.request)); });\n"),
    '404.html': '<!doctype html><html><body><main id="app"><h1>No such bike</h1></main></body></html>',
}


def serve(directory: Path):
    """A loopback server on the first free port in 8620-8629 that answers a missing path with 404.html (status 404)."""
    import functools, http.server, threading

    class Handler(http.server.SimpleHTTPRequestHandler):
        def send_error(self, code, message=None, explain=None):
            if code != 404:
                return super().send_error(code, message, explain)
            body = (directory / '404.html').read_bytes()
            self.send_response(404)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    for port in range(8620, 8630):
        try:
            srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), functools.partial(Handler, directory=str(directory)))
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, port
    return None, None


def live_site_check() -> tuple[list[str], str]:
    """Real probes of a loopback site with a service worker, a 404 page and crossorigin third-party scripts: the
    worker's requests and state are assertable, an expected 404 document is a clean not-found baseline, two
    scenarios share one browser, and Firefox's CORS reports of the harness's own blocks are not the app's errors."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / 'site').mkdir()
        for name, text in LIVE_SITE.items():
            (t / 'site' / name).write_text(text, encoding='utf-8')
        srv, port = serve(t / 'site')
        if srv is None:
            return [], 'skipped (ports 8620-8629 busy)'
        base = f'http://127.0.0.1:{port}'
        try:
            scenarios = {
                'sw': {'name': 'service worker baseline', 'kind': 'baseline',
                       'steps': [{'goto': '/'}, {'wait_for': "#app[data-sw='1']"}, {'reload': True}],
                       'expect': [{'request': '**/sw.js', 'made': True},
                                  {'service_worker': '**/sw.js', 'state': 'activated'},
                                  {'cache_entries': 'spokes-*', 'min': 1}], 'settle_ms': 300},
                'notfound': {'name': 'unknown bike is the not-found page', 'kind': 'baseline',
                             'steps': [{'goto': '/no-such-bike'}],
                             'expect': [{'document_status': 404}, {'text_visible': 'No such bike'}], 'settle_ms': 200},
                'firefox': {'name': 'firefox baseline', 'kind': 'baseline', 'browser': 'firefox',
                            'service_workers': 'block', 'steps': [{'goto': '/'}],
                            'expect': [{'visible': '#app'}], 'settle_ms': 1000},
            }
            for k, sc in scenarios.items():
                (t / f'{k}.json').write_text(json.dumps(sc), encoding='utf-8')
            r = subprocess.run([sys.executable, '-B', str(HERE / 'probe_web.py'), str(t / 'sw.json'), str(t / 'notfound.json'),
                                '--out-dir', str(t / 'ev'), '--base-url', base], capture_output=True, text=True,
                               encoding='utf-8')
            evs = {}
            for k in ('sw', 'notfound'):
                try:
                    evs[k] = json.loads((t / 'ev' / k / 'evidence.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    fails.append(f'two scenarios in one run did not write ev/{k}/evidence.json: {(r.stderr or r.stdout)[-300:]}')
            ev = evs.get('sw')
            if ev:
                if not ev.get('baseline_ok'):
                    fails.append(f"service worker baseline not clean: {[e for e in ev.get('expectations', []) if not e['passed']]}, "
                                 f"console {ev.get('console_errors')}")
                if not any(w.get('state') == 'activated' for w in ev.get('service_workers') or []):
                    fails.append(f"evidence does not record the activated worker: {ev.get('service_workers')}")
            ev = evs.get('notfound')
            if ev and (not ev.get('baseline_ok') or not ev.get('expected_console_errors')):
                fails.append(f"a not-found baseline that asserts document_status 404 is not clean: console "
                             f"{ev.get('console_errors')}, expected {ev.get('expected_console_errors')}")
            if evs and not all((e.get('timing') or {}).get('shared_browser') == 2 for e in evs.values()):
                fails.append(f"the two scenarios did not share one browser: {[e.get('timing') for e in evs.values()]}")
            r = subprocess.run([sys.executable, '-B', str(HERE / 'probe_web.py'), str(t / 'firefox.json'), '--out-dir',
                                str(t / 'ff'), '--base-url', base], capture_output=True, text=True, encoding='utf-8')
            try:
                ev = json.loads((t / 'ff' / 'evidence.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                ev = None
                if 'Executable doesn' not in (r.stderr or ''):
                    fails.append(f'Firefox probe failed: {(r.stderr or r.stdout)[-300:]}')
            if ev and (not ev.get('baseline_ok') or ev.get('console_errors')):
                fails.append(f"Firefox baseline: the CORS reports of the harness's own third-party blocks count as the "
                             f"app's errors: {ev.get('console_errors')}")
        finally:
            srv.shutdown()
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
    # --recon: the report must agree with recon.json's site model, or say why; a dominant language no pack reads
    # must be named in the limitations; a report whose static tools found nothing on unread UI is all manual
    go_recon = {'site': {'kind': 'unknown'}, 'blast_radius': {'model': 'unknown'},
                'language_coverage': {'status': 'unsupported_dominant', 'dominant_unsupported_language': 'Go'}}
    d = json.loads(json.dumps(base))
    try:
        errors, warnings = vcr.check(d, EXAMPLES, None, None, recon_json=go_recon)
    except TypeError as exc:
        return fails + [f'validator: no recon input ({exc})']
    if not any('site_model' in w and 'recon' in w for w in warnings + errors):
        fails.append('validator --recon: a site_model (spa) that differs from recon.json (unknown) passes silently')
    if not any('Go' in e and 'limitation' in e for e in errors):
        fails.append('validator --recon: a report on a Go-dominant repository has no limitation naming Go and passes')
    d['limitations'].append('This repository is mostly Go, which no pack reads; the Go code was not assessed.')
    d['limitations'].append('The site model differs from recon (a bundled fixture misled it); set by hand.')
    d['triage'].update(total=0, promoted=[], dismissed=[])
    errors, warnings = vcr.check(d, EXAMPLES, None, None, recon_json=go_recon)
    if any('Go' in e and 'limitation' in e for e in errors) or any('site_model' in w and 'recon' in w for w in warnings):
        fails.append(f'validator --recon: limitations naming Go and the site-model change are not accepted: {errors}')
    if not any('manual' in w for w in warnings):
        fails.append('validator --recon: 0 tool candidates on a repository whose UI the tools did not read is not flagged '
                     'as entirely manual coverage')
    # --write-summary fills the ranks it already computes
    d = json.loads(json.dumps(base))
    for f in d['crashes']:
        f['rank'] = 0
    d['crashes'].reverse()
    fill = getattr(vcr, 'apply_ranks', None)
    if fill is None:
        fails.append('validator: --write-summary does not fill crash ranks (no apply_ranks)')
    else:
        fill(d)
        errors, _ = vcr.check(d, EXAMPLES, None, None)
        if any('rank' in e for e in errors):
            fails.append(f'validator: ranks still wrong after apply_ranks: {[e for e in errors if "rank" in e]}')
    # a page blanked by hiding its content (no visible text, no image) counts as blank, though body has children
    blank = getattr(probe_web, 'page_blank', None)
    if blank is None or not blank({'root_empty': False, 'visible_text_len': 0, 'visible_media': False}) or \
            blank({'root_empty': False, 'visible_text_len': 0, 'visible_media': True}):
        fails.append('probe_web: a page with no visible text and no visible image is not reported as blank (page_blank)')
    blanked = getattr(vcr, 'probe_blanked', None)
    if blanked is None or not blanked({'page_blank': True}, 'B1', {}):
        fails.append('validator: page_blank evidence is not accepted as the app blanking')
    # a dismissed group's match.path is a glob whose brackets are literal (App Router paths are full of them); the
    # fnmatch escape [[]id] for a literal [id] is accepted too
    rx = getattr(vcr, 'glob_rx')
    if not rx('src/app/[slug]/page.tsx').match('src/app/[slug]/page.tsx') or \
            not rx('src/app/[[]slug]/page.tsx').match('src/app/[slug]/page.tsx') or \
            not rx('src/app/**/page.tsx').match('src/app/[slug]/page.tsx'):
        fails.append('validator: a group path with a bracketed App Router segment ([slug] or the fnmatch escape [[]slug]) '
                     'does not match the file')
    # a repro spec listed for a baseline probe (baselines never get one) is a stale files_written entry
    d = json.loads(json.dumps(base))
    d['files_written'] = [f for f in d['files_written'] if f != 'repro/repro_baseline.py'] + ['repro/repro_baseline.py']
    errors, _ = vcr.check(d, EXAMPLES, None, None)
    if not any('repro_baseline.py' in e for e in errors):
        fails.append('validator: files_written lists a repro spec for a baseline probe (baselines never get one)')
    d['files_written'].remove('repro/repro_baseline.py')
    d['metadata']['skill_version'] = '0.9.0'
    _, warnings = vcr.check(d, EXAMPLES, None, None)
    if not any('skill_version' in w for w in warnings):
        fails.append('validator: metadata.skill_version older than SKILL.md passes silently')
    flask = {'site': {'kind': 'multi_page'}, 'blast_radius': {'model': 'per_script'},
             'language_coverage': {'status': 'server_rendered', 'dominant_unsupported_language': 'Python'}}
    d = json.loads(json.dumps(base))
    d['recon']['site_model'], d['recon']['blast_radius'] = 'multi_page', {'model': 'per_script', 'note': 'x'}
    errors, _ = vcr.check(d, EXAMPLES, None, None, recon_json=flask)
    if any('Python' in e for e in errors):
        fails.append(f'validator --recon: a server-rendered Flask site is held to the unsupported-language rule: {errors}')
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
    fails, how = live_site_check()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: live probes of a loopback site with a service worker, a "
          f"404 page and crossorigin third-party scripts ({how})" + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
