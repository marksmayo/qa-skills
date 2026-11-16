#!/usr/bin/env python3
"""Self-test for the cross-browser checker's scripts:

- feature_scan.py on a synthetic Go tool whose only browser code is an HTML page in a Go string constant
  (beside a vendored tree), and on a Go tool with no browser code at all (needs the cached feature data);
- capture.py's page snapshot: computed font family, occlusion by a fixed overlay, inner scroll panels
  (needs Playwright with Chromium);
- compare_engines.py on synthetic runs: a font-family change is its own difference, a box covered by an
  overlay is dismissed, and boxes inside a panel scrolled differently by automation are dismissed;
- validate_report.py ties feature_findings to --features; render_report.py --brief shows needs review
  and limitations.

    python self_test.py

Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import json, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import capture  # noqa: E402
import compare_engines as ce  # noqa: E402

SKILL = HERE.parent
EXAMPLES = SKILL / 'examples'

GO_PAGE = ('package report\n\nimport "html/template"\n\nvar page = template.Must(template.New("r").Parse(pageHTML))\n\n'
           'const pageHTML = `<!DOCTYPE html>\n<html>\n<head>\n<style>\n.list { overflow-y: auto; }\n'
           '.list::-webkit-scrollbar { width: 6px; }\n</style>\n</head>\n<body><div class="list"></div>\n'
           '<script>\nconst q = new URLSearchParams(location.search);\n</script>\n</body>\n</html>`\n')
GO_TREE = {
    'go.mod': 'module example.test/tally\n\ngo 1.22\n',
    'main.go': 'package main\n\nfunc main() {}\n',
    'internal/run/run.go': 'package run\n\n// Run runs.\nfunc Run() {}\n',
    'internal/run/retry.go': 'package run\n\n// Retry retries.\nfunc Retry() {}\n',
    'internal/report/report.go': 'package report\n\n// Write writes the report.\nfunc Write() {}\n',
    'internal/report/page.go': GO_PAGE,
    'vendor/example.test/kit/kit.go': 'package kit\n',
    'third_party/widget/widget.css': '.w::-webkit-scrollbar { width: 2px; }\n',
}


def feature_scan_checks() -> tuple[list[str], str]:
    try:
        from fetch_data import asset_path
        asset_path('web-features'); asset_path('caniuse-db')
    except (FileNotFoundError, ImportError, KeyError):
        return [], 'skipped (feature data not cached: fetch_data.py)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'tally'
        for rel, text in GO_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        out = Path(tmp) / 'features.json'
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'defaults'], capture_output=True, text=True, encoding='utf-8')
        try:
            d = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [f'feature_scan.py failed: {r.stderr[-300:]}'], 'ran'
        line = GO_PAGE.splitlines().index('.list::-webkit-scrollbar { width: 6px; }') + 1
        hits = [(u['file'], u['line']) for f in d['features'] for u in f['files']]
        if ('internal/report/page.go', line) not in hits:
            fails.append(f'the <style> in a Go string constant was not scanned (want internal/report/page.go:{line}; got {hits})')
        if any(h[0].startswith(('vendor/', 'third_party/')) for h in hits):
            fails.append(f'a vendored tree was scanned: {hits}')
        lc = d.get('language_coverage') or {}
        if lc.get('dominant') != 'Go' or lc.get('status') != 'unsupported_dominant' or not lc.get('embedded_html'):
            fails.append(f'language_coverage does not say Go is dominant with HTML in string constants: {lc or "missing"}')
        # a Go tool with no browser code at all: the empty result must say it is not evidence
        (root / 'internal/report/page.go').write_text('package report\n\nconst name = "tally"\n', encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'defaults'], capture_output=True, text=True, encoding='utf-8')
        d = json.loads(out.read_text(encoding='utf-8'))
        if not any('Go' in x for x in d.get('limitations') or []) or 'WARN' not in r.stderr:
            fails.append(f'a Go repository with 0 uses has no loud language limitation: limitations '
                         f'{d.get("limitations")}, stderr {r.stderr.strip()[-200:]!r}')
    return fails, 'ran'


# a server-rendered Python site (a recipe site): Flask routes render Jinja templates named .html, the CSS is under
# static/, the Python modules outnumber everything else, and a data directory holds a few hundred JSON files
FLASK_TREE = {
    'requirements.txt': 'flask==3.1.0\nJinja2>=3.1\n',
    'app.py': 'from flask import Flask, render_template\n\napp = Flask(__name__)\n\n\n@app.route("/")\ndef index():\n'
              '    return render_template("index.html")\n',
    'recipes/__init__.py': '', 'recipes/search.py': 'def search(q):\n    return []\n',
    'recipes/units.py': 'def grams(x):\n    return x\n', 'recipes/store.py': 'def load():\n    return {}\n',
    'tools/rebuild_index.py': 'print("rebuild")\n', 'tools/fetch_images.py': 'print("fetch")\n',
    'templates/base.html': '<!doctype html><html><head><link rel="stylesheet" href="/static/site.css"></head>\n'
                           '<body>{% block body %}{% endblock %}</body></html>\n',
    'templates/index.html': '{% extends "base.html" %}\n{% block body %}\n<style>\n.card:has(> img) { gap: 4px; }\n</style>\n'
                            '<ul>{% for r in recipes %}<li>{{ r.name }}</li>{% endfor %}</ul>\n{% endblock %}\n',
    'static/site.css': '.list::-webkit-scrollbar { width: 6px; }\n',
    'old_build/export.html': '<!doctype html><html><body><p>stale</p></body></html>\n',
}
FLASK_TREE.update({f'data/recipes/r{i:04d}.json': '{"name": "soup %d"}' % i for i in range(260)})


def flask_scan_checks() -> tuple[list[str], str]:
    try:
        from fetch_data import asset_path
        asset_path('web-features'); asset_path('caniuse-db')
    except (FileNotFoundError, ImportError, KeyError):
        return [], 'skipped (feature data not cached: fetch_data.py)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'recipes-site'
        for rel, text in FLASK_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        out = Path(tmp) / 'features.json'
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'defaults', '--exclude', 'old_build'], capture_output=True, text=True, encoding='utf-8')
        try:
            d = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [f'feature_scan.py failed: {r.stderr[-300:]}'], 'ran'
        lc = d.get('language_coverage') or {}
        if lc.get('status') == 'unsupported_dominant' or 'NOT a' in str(lc.get('warning')):
            fails.append(f'a Flask site whose Jinja templates and CSS were scanned is called unsupported: {lc}')
        if any('not read as browser code' in x or 'NOT a feature scan' in x for x in d.get('limitations') or []):
            fails.append(f'a misleading language limitation for a server-rendered site: {d.get("limitations")}')
        if 'WARN' in r.stderr:
            fails.append(f'feature_scan.py warns about the language of a server-rendered site: {r.stderr.strip()[-200:]!r}')
        if 'templates/index.html' not in (lc.get('templates') or []):
            fails.append(f'language_coverage does not name the templates it scanned: {lc.get("templates")}')
        scanned = (d.get('scanned_files') or {}).get('files') or []
        if not {'templates/index.html', 'static/site.css'} <= set(scanned):
            fails.append(f'scanned_files does not list the templates and stylesheets read: {d.get("scanned_files")}')
        if any(f.startswith('old_build/') for f in scanned):
            fails.append(f'--exclude old_build did not leave the directory out: {scanned}')
        data = (d.get('skipped') or {}).get('data_dirs') or []
        if not any(x.get('path') == 'data/recipes' and x.get('files') == 260 for x in data):
            fails.append(f'the data directory of 260 JSON files is not recorded as skipped: {d.get("skipped")}')
    return fails, 'ran'


# a Python API server (FastAPI) mounting StaticFiles on a committed, hand-written dist/ folder: no package.json, no build
# step; the front end uses fullscreen, pointer and keyboard lock, gamepads, the Cache API, IndexedDB, Web Audio and
# WebAssembly, and an Emscripten-style loader guards a require("fs") behind ENVIRONMENT_IS_NODE
SERVED_TREE = {
    'requirements.txt': 'fastapi==0.110.0\nuvicorn==0.29.0\n',
    'server.py': 'from fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles\n\napp = FastAPI()\n\n\n'
                 '@app.get("/health")\ndef health():\n    return {"ok": True}\n\n\n'
                 'app.mount("/", StaticFiles(directory="dist", html=True), name="site")\n',
    'proxy.py': 'def forward(path):\n    return path\n', 'config.py': 'PORT = 8000\n', 'cache_dir.py': 'ROOT = "cache"\n',
    'tools/pack.py': 'print("pack")\n', 'tools/check.py': 'print("check")\n', 'tools/__init__.py': '',
    'dist/index.html': '<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>\n'
                       '<body><canvas id="screen"></canvas><script src="app.js"></script></body></html>\n',
    'dist/style.css': 'canvas { width: 100%; }\n',
    'dist/app.js': ('const screen = document.getElementById("screen");\n'
                    'screen.addEventListener("click", () => { screen.requestFullscreen(); });\n'
                    'screen.requestPointerLock();\n'
                    'navigator.keyboard.lock(["Escape"]);\n'
                    'const pads = navigator.getGamepads();\n'
                    'caches.open("assets-v1").then((c) => c.addAll(["/data.bin"]));\n'
                    'const req = indexedDB.open("saves", 1);\n'
                    'const ctx = new AudioContext();\n'),
    'dist/modules/loader.js': ('var ENVIRONMENT_IS_WEB = typeof window == "object";\n'
                               'var ENVIRONMENT_IS_NODE = typeof process == "object";\n'
                               'if (ENVIRONMENT_IS_NODE) { var fs = require("fs"); var nodePath = require("path"); }\n'
                               'function load(bytes) { return WebAssembly.instantiate(bytes, {}); }\n'
                               'if (ENVIRONMENT_IS_WEB) { var s = document.createElement("script"); document.body.appendChild(s); }\n'),
}
SERVED_FEATURES = {'wasm', 'fullscreen', 'pointer-lock', 'keyboard-lock', 'gamepad', 'cache-storage', 'indexeddb', 'web-audio'}


def served_dist_checks() -> tuple[list[str], str]:
    try:
        from fetch_data import asset_path
        asset_path('web-features'); asset_path('caniuse-db')
    except (FileNotFoundError, ImportError, KeyError):
        return [], 'skipped (feature data not cached: fetch_data.py)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'arcade'
        for rel, text in SERVED_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        out = Path(tmp) / 'features.json'
        def scan(*extra):
            r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                                '--query', 'defaults', *extra], capture_output=True, text=True, encoding='utf-8')
            try:
                return json.loads(out.read_text(encoding='utf-8')), r.stderr
            except (OSError, ValueError):
                return None, r.stderr
        d, err = scan()
        if d is None:
            return [f'feature_scan.py failed: {err[-300:]}'], 'ran'
        scanned = (d.get('scanned_files') or {}).get('files') or []
        if 'skipped' not in d:
            fails.append(f'features.json has no `skipped` record: keys {sorted(d)}')
        if 'dist/app.js' not in scanned:
            pruned = [p for p in (d.get('skipped') or {}).get('pruned') or [] if p.get('path') == 'dist']
            if not pruned or not pruned[0].get('reason'):
                fails.append(f'dist/ was left out without a skipped.pruned entry and reason: skipped {d.get("skipped")}')
            if '--include dist' not in err + ' '.join(d.get('limitations') or []):
                fails.append(f'dist/ (served by StaticFiles) was left out and nothing says "--include dist": {err.strip()[-300:]!r}')
        d, err = scan('--include', 'dist')
        if d is None:
            return fails + [f'feature_scan.py --include dist failed: {err[-300:]}'], 'ran'
        scanned = (d.get('scanned_files') or {}).get('files') or []
        if not {'dist/app.js', 'dist/modules/loader.js', 'dist/index.html'} <= set(scanned):
            fails.append(f'--include dist did not scan the served front end: scanned {scanned}')
        if any('loader.js' in x for x in (d.get('skipped') or {}).get('node_only_files') or []):
            fails.append('an Emscripten-style loader (require("fs") guarded by ENVIRONMENT_IS_NODE, uses document and '
                         'WebAssembly) was skipped as Node-only')
        if (d.get('language_coverage') or {}).get('status') == 'unsupported_dominant':
            fails.append(f'with dist/ included the scan still says the browser code was not read: {d.get("language_coverage")}')
        seen = {f['feature'] for f in d.get('features') or []} | {f['feature'] for f in d.get('supported_features') or []}
        if not SERVED_FEATURES <= seen:
            fails.append(f'APIs the front end uses are not recorded (as findings or supported_features): missing '
                         f'{sorted(SERVED_FEATURES - seen)}')
    return fails, 'ran'


def fake_inks(doc, base, ref, jobs):
    return {i: r.get('_inks') for i, r in enumerate(doc['runs'])}


def round8_compare_checks() -> list[str]:
    """Effective viewport (fullscreen), benign media aborts, failed requests across pages, a second ink pass when the
    reference shows almost no ink, limitations inherited from a font-family difference, inline reflow, and steps that
    fail in every engine."""
    fails = []
    def el(sig, box, tag='div', text='x', font='Verdana'):
        return {'sig': sig, 'tag': tag, 'type': None, 'text': text, 'box': box, 'font': font}
    def mk(page, eng, n, elements, **kw):
        r = {'page': page, 'viewport': '1280x800', 'engine': eng, 'rerun': n, 'run_status': 'ran', 'elements': elements,
             'images': [], 'page_errors': [], 'console_errors': [], 'failed_requests': [], 'steps': [], 'overflow_x': False,
             'size': [1280, 800]}
        r.update(kw)
        return r
    engines = ('chromium', 'firefox', 'webkit')
    # 1. Firefox went fullscreen after "click to play": its effective viewport is 1366x768, the plan's is 1280x800
    runs = []
    for e in engines:
        for n in (1, 2):
            fs = e == 'firefox'
            els = [el('#status', [100 + (43 if fs else 0), 400 - (34 if fs else 0), 300, 20])] + \
                ([el('#progress', [0, 0, 1366, 4])] if fs else [])
            runs.append(mk('home~play', e, n, els, inner=[1366, 768] if fs else [1280, 800], fullscreen=fs,
                           size=[1366, 768] if fs else [1280, 800]))
    doc = {'engines': [{'name': e} for e in engines], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    bad = [d for d in diffs if d['engine'] == 'firefox' and d['kind'] == 'layout']
    if bad or not any('viewport' in d.get('reason', '') for d in dismissed):
        fails.append(f'boxes were compared across different effective viewports (Firefox fullscreen at 1366x768): '
                     f'{[(d["subkind"], d.get("element")) for d in bad]}')
    # 2. media requests cancelled after their metadata (ERR_ABORTED) and Firefox's NS_ERROR_PARSED_DATA_CACHED are not
    #    failures; one real failure on three pages is one candidate
    runs = []
    for p in ('home', 'about', 'help'):
        for e in engines:
            for n in (1, 2):
                fr = []
                if e == 'chromium':
                    fr.append('GET http://127.0.0.1:9/intro.mp4 (net::ERR_ABORTED)')
                if e == 'firefox':
                    fr.append('GET http://127.0.0.1:9/app.js (NS_ERROR_PARSED_DATA_CACHED)')
                if e == 'webkit':
                    fr.append('GET http://127.0.0.1:9/api/news (Could not connect to the server.)')
                runs.append(mk(p, e, n, [el('main', [0, 0, 1280, 800], tag='main')], failed_requests=fr))
    doc = {'engines': [{'name': e} for e in engines], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    rf = [c for c in cands if c['subkind'] == 'request_failed']
    if any('intro.mp4' in c['key'] or 'app.js' in c['key'] for c in rf):
        fails.append(f'a media request aborted after its metadata or a Firefox cache-parse notice is a failed request: '
                     f'{[c["key"] for c in rf]}')
    news = [c for c in rf if 'api/news' in c['key']]
    if len(news) != 1 or sorted(news[0]['pages']) != ['about', 'help', 'home']:
        fails.append(f'one failed request on three pages is not one candidate: {[(c["key"], c["pages"]) for c in news]}')
    if not any('ERR_ABORTED' in str(x) for x in getattr(ce, 'benign_requests', lambda d: [])(doc)):
        fails.append('the benign media aborts are not recorded anywhere (benign_requests)')
    lim = ce.build_limitations({'engines': ['chromium'], 'subkind': 'request_failed', 'element': None, 'detail': '',
                                'messages': {'chromium': ['GET http://127.0.0.1:9/intro.mp4 (net::ERR_ABORTED)']}}, 'win32')
    if 'chromium-proprietary-codecs' in lim:
        fails.append('a media request cancelled with ERR_ABORTED is blamed on missing codecs by its .mp4 URL alone')
    # 3. a video black in WebKit only: Chromium's frame is near-uniform (under the ink floor), Firefox draws a logo
    runs = []
    for e, ink in (('chromium', 0.01), ('firefox', 0.2), ('webkit', 0.0)):
        for n in (1, 2):
            runs.append(mk('home~play', e, n, [el('video#intro', [0, 0, 640, 360], tag='video', text='')], _inks=[ink]))
    doc = {'engines': [{'name': e} for e in engines], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    real = ce.all_inks
    ce.all_inks = fake_inks
    try:
        diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    finally:
        ce.all_inks = real
    if not any(d['subkind'] == 'content_not_rendered' and d['engine'] == 'webkit' and d.get('ref') == 'firefox' for d in diffs):
        fails.append('a video blank in WebKit only is not compared with Firefox when the reference shows almost no ink: '
                     f'{[(d["subkind"], d["engine"], d.get("ref")) for d in diffs]}')
    # 4. a key input drawn in the Windows WebKit default face is also narrower: the box difference inherits the limitation
    runs = []
    for e in ('chromium', 'webkit'):
        for n in (1, 2):
            wk = e == 'webkit'
            runs.append(mk('home', e, n, [el('div.key-input', [10, 10, 157 if wk else 200, 30], text='Key',
                                             font='MS Shell Dlg' if wk else 'Verdana')]))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    box = [c for c in cands if c['subkind'] == 'box_differs']
    if not box or 'webkit-windows-serif-controls' not in box[0]['build_limitations']:
        fails.append(f'a box narrower because its font fell back to the build default face does not inherit the '
                     f'limitation: {[(c["subkind"], c["build_limitations"]) for c in cands]}')
    # 5. an inline link moved along its line because the paragraph breaks at another word: inline reflow
    runs = []
    for e in ('chromium', 'firefox'):
        for n in (1, 2):
            ff = e == 'firefox'
            runs.append(mk('home', e, n, [el('footer>p.repo', [0, 700, 400, 40], tag='p'),
                                          el('footer>p.repo>a.mirror', [262 if ff else 300, 720, 60, 18], tag='a')]))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'firefox'}], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    if any(d.get('element') == 'footer>p.repo>a.mirror' for d in diffs) or \
            not any(d.get('element') == 'footer>p.repo>a.mirror' and 'inline' in d.get('reason', '') for d in dismissed):
        fails.append('an inline link that moved along its line inside an unchanged paragraph (the line breaks at another '
                     'word) is reported as a layout difference')
    # 6. a step that fails in every run of every engine is listed, not silent
    runs = []
    for e in engines:
        for n in (1, 2):
            runs.append(mk('home~play', e, n, [el('main', [0, 0, 1280, 800], tag='main')],
                           steps=[{'index': 0, 'action': {'click': '.intro'}, 'outcome': 'failed',
                                   'error': 'TimeoutError: element is covered by #canvas'}],
                           page_errors=['TypeError: data is not valid']))
    doc = {'engines': [{'name': e} for e in engines], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    same = getattr(ce, 'same_everywhere', lambda d: [])(doc)
    if not any(x.get('page') == 'home~play' and x.get('kind') == 'step_failed' for x in same) or \
            not any(x.get('kind') == 'page_error' for x in same):
        fails.append(f'a step and a page error that fail in every engine are not listed (same_everywhere): {same}')
    return fails


def _mk(page, eng, n, elements, vp='1280x800', **kw):
    r = {'page': page, 'viewport': vp, 'engine': eng, 'run': n, 'run_status': 'ran', 'ok': True, 'elements': elements,
         'images': [], 'page_errors': [], 'console_errors': [], 'failed_requests': [], 'steps': [], 'overflow_x': False,
         'size': [1280, 2000]}
    r.update(kw)
    return r


def _el(sig, box, tag='div', text='x', font='Verdana', **kw):
    return {'sig': sig, 'tag': tag, 'type': None, 'text': text, 'box': box, 'font': font, **kw}


def environment_checks() -> list[str]:
    """A plain-http loopback site whose CSP says upgrade-insecure-requests: one engine rewrites every subresource to
    https and they all fail. That is one environment cause, the runs are not captured (no comparison is made for that
    engine), and nothing is matched to an engine-build limitation. A blank run among good reruns is a failed run, not
    hundreds of flaky entries."""
    fails = []
    screen = getattr(ce, 'screen_runs', None)
    env_of = getattr(ce, 'environment', None)
    if screen is None or env_of is None:
        return ['compare_engines.py has no screen_runs()/environment(): a run whose requests were upgraded to https and '
                'failed, or a blank run, is compared as a good run']
    page = [_el('main', [0, 0, 1280, 900], tag='main'), _el('main>h1', [20, 20, 600, 40], tag='h1', text='Bikes',
                                                          font='ui-sans-serif, system-ui, sans-serif')]
    bare = [_el('main', [0, 0, 1280, 300], tag='main'), _el('main>h1', [8, 8, 300, 37], tag='h1', text='Bikes',
                                                           font='-webkit-standard')]
    runs = []
    for p in ('home', 'about'):
        for e in ('chromium', 'firefox', 'webkit'):
            for n in (1, 2, 3):
                wk = e == 'webkit'
                runs.append(_mk(p, e, n, bare if wk else page, csp_upgrade_insecure_requests=True, failed_requests=[
                    'GET https://127.0.0.1:8681/_next/static/css/app.css (SSL connect error)',
                    'GET https://127.0.0.1:8681/_next/static/chunks/main.js (SSL connect error)'] if wk else []))
    doc = {'site': 'http://127.0.0.1:8681/', 'engines': [{'name': e, 'status': 'ran'} for e in ('chromium', 'firefox', 'webkit')],
           'reruns': 3, 'platform': 'win32', 'runs': runs}
    screened = screen(doc)
    env = env_of(doc)
    if len(env) != 1 or env[0].get('cause') != 'https_upgrade' or env[0].get('engines') != ['webkit'] or \
            'upgrade-insecure-requests' not in env[0].get('detail', ''):
        fails.append(f'requests upgraded to https on an http site are not one environment cause naming the CSP: {env}')
    if any(r['run_status'] == 'ran' for r in doc['runs'] if r['engine'] == 'webkit') or len(screened) != 6:
        fails.append(f'webkit runs whose own requests were upgraded and failed are still recorded as ran: {screened}')
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    if any('webkit' in c['engines'] for c in cands) or any(c['build_limitations'] for c in cands):
        fails.append(f'an environment failure became engine differences: {[(c["subkind"], c["engines"], c["build_limitations"]) for c in cands]}')
    cmp = ce.comparisons(doc, 'chromium')
    if cmp['made'] != 2 or not all('https' in (x['reason'] or '') for x in cmp['not_made']):
        fails.append(f'the webkit comparisons are not "not made" with the https reason: {cmp}')
    # the serif-controls limitation must look at the engine's face, not "serif" inside the reference's ui-sans-serif
    lim = ce.build_limitations({'engines': ['webkit'], 'subkind': 'font_family_differs', 'element': 'h1',
                                'detail': '3 element(s) draw in font family "arial" in webkit but "ui-sans-serif, system-ui" '
                                          'in chromium', 'messages': {}}, 'win32')
    if 'webkit-windows-serif-controls' in lim:
        fails.append('a sans face in WebKit matches the serif-controls limitation because the reference says ui-sans-serif')
    # a stylesheet that failed to load in the engine explains its default face: no build limitation
    runs = []
    for e in ('chromium', 'webkit'):
        for n in (1, 2):
            wk = e == 'webkit'
            runs.append(_mk('home', e, n, [_el('button.go', [10, 10, 80, 30], tag='button', font='Times New Roman' if wk else 'Verdana')],
                            failed_requests=['GET http://127.0.0.1:8681/site.css (Could not connect)'] if wk else []))
    doc = {'site': 'http://127.0.0.1:8681/', 'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2,
           'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    fam = [c for c in cands if c['subkind'] == 'font_family_differs']
    if not fam or fam[0]['build_limitations']:
        fails.append(f'a default face that follows from a failed stylesheet is matched to a build limitation: '
                     f'{[(c["subkind"], c["build_limitations"]) for c in fam]}')
    # 4. a blank rerun (75 elements against 555 in its siblings): one failed run, not one flaky entry per element
    full = [_el(f'main>p:nth-of-type({i})', [0, 30 * i, 600, 20], tag='p') for i in range(1, 80)]
    blank = full[:4]
    runs = []
    for e in ('chromium', 'webkit'):
        for n in (1, 2, 3):
            runs.append(_mk('home', e, n, blank if (e == 'webkit' and n == 3) else full,
                            **({'uniformity': {'dominant_share': 0.995, 'uniform': True}} if (e == 'webkit' and n == 3) else
                               {'uniformity': {'dominant_share': 0.7, 'uniform': False}})))
    doc = {'site': 'http://127.0.0.1:8681/', 'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 3,
           'platform': 'win32', 'runs': runs}
    screened = screen(doc)
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cmp = ce.comparisons(doc, 'chromium')
    if len(screened) != 1 or flaky or 'blank' not in (cmp['not_made'][0]['reason'] if cmp['not_made'] else ''):
        fails.append(f'a blank rerun is not one failed run (screened {screened}, {len(flaky)} flaky, not made {cmp["not_made"]})')
    # capture retries a screened run once and keeps the retry when it is good
    retry = getattr(capture, 'retry_outliers', None)
    if retry is None:
        fails.append('capture.py has no retry_outliers(): a blank run is never captured again')
    else:
        group = [dict(r) for r in runs if r['engine'] == 'webkit']
        for r in group:
            r.update(run_status='ran', ok=True, error=None)
            r.pop('screened', None)
        out = retry(group, lambda r: dict(r, elements=full, uniformity={'dominant_share': 0.7, 'uniform': False}))
        if [r['run_status'] for r in out] != ['ran'] * 3 or not out[2].get('retried'):
            fails.append(f'a blank run retried with a good result is not kept as ran with a "retried" note: '
                         f'{[(r["run_status"], r.get("retried")) for r in out]}')
    return fails


def round10_compare_checks() -> list[str]:
    """Box differences that can be promoted, a decoded dark photo, the system-ui face in WebKit on Windows, an SVG
    stroke box, and an aborted framework image request."""
    fails = []
    # 2. a repeatable box difference: its runs record "differs", so the reference's outcomes are not the engine's
    runs = []
    for e in ('chromium', 'firefox'):
        for n in (1, 2, 3):
            runs.append(_mk('home', e, n, [_el('main>ul.sources', [0, 100, 600, 80 if e == 'firefox' else 120], tag='ul')]))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'firefox'}], 'reruns': 3, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    box = [c for c in cands if c['subkind'] == 'box_differs']
    if not box or box[0]['runs'].get('home|chromium') == box[0]['runs'].get('home|firefox'):
        fails.append(f'a box difference records the same outcomes for both engines, so it can never be promoted: '
                     f'{[c["runs"] for c in box]}')
    # 5a. a dark photo WebKit decoded and drew: the ink metric is not "not rendered"
    runs = []
    for e, v in (('chromium', 0.982), ('webkit', 0.259)):
        for n in (1, 2):
            runs.append(_mk('home', e, n, [_el('main>a>img.thumb', [0, 0, 390, 260], tag='img', text='')], _inks=[v],
                            images=[{'sig': 'main>a>img.thumb', 'src': '/img/trail.jpg', 'complete': True, 'natural': [390, 260]}]))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    real = ce.all_inks
    ce.all_inks = fake_inks
    try:
        diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    finally:
        ce.all_inks = real
    if any(d['subkind'] == 'content_not_rendered' for d in diffs):
        fails.append('a decoded photo drawn by both engines is content_not_rendered because its most common colour differs')
    # 5b. the same computed family resolving to another face (system-ui in Windows WebKit)
    fam = 'ui-sans-serif, system-ui, sans-serif'
    runs = []
    for e in ('chromium', 'webkit'):
        for n in (1, 2):
            wk = e == 'webkit'
            runs.append(_mk('home', e, n, [_el('main>p.lead', [0, 0, 600, 48 if wk else 24], tag='p', font=fam)],
                            faces={fam: 1180.5 if wk else 1101.0}))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    face = [c for c in cands if c['subkind'] == 'font_family_differs']
    if not face or 'webkit-windows-system-ui' not in face[0]['build_limitations']:
        fails.append(f'system-ui resolving to another face in WebKit is not a font difference with its build limitation: '
                     f'{[(c["subkind"], c["build_limitations"]) for c in cands]}')
    box = [c for c in cands if c['subkind'] == 'box_differs']
    if box and 'webkit-windows-system-ui' not in box[0]['build_limitations']:
        fails.append(f'a box resized by the other face does not inherit the limitation: {box[0]["build_limitations"]}')
    # 5c. Firefox includes an SVG shape's stroke in its box
    runs = []
    for e in ('chromium', 'firefox'):
        for n in (1, 2):
            ff = e == 'firefox'
            runs.append(_mk('home', e, n, [_el('svg.ring>circle', [92, 92, 112, 112] if ff else [100, 100, 96, 96],
                                               tag='circle', text='', stroke=16)]))
    doc = {'engines': [{'name': 'chromium'}, {'name': 'firefox'}], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    if any(d.get('element') == 'svg.ring>circle' for d in diffs):
        fails.append('an SVG circle whose box includes its stroke in one engine is a layout difference')
    # 5d. an image request a framework cancels when it swaps a srcset candidate
    if not ce.benign_failure('GET http://127.0.0.1:8681/_next/image?url=%2Fa.jpg&w=640&q=75 (net::ERR_ABORTED)'):
        fails.append('an aborted /_next/image request is compared as a failed request')
    return fails


def grouping_checks() -> list[str]:
    """Repeated cards (nth-of-type siblings) across two viewports, an undecoded AVIF in each with the fallback the page
    shows instead, the same console error on two pages, and a 400 response logged by two engines only."""
    fails = []
    def el(sig, box, tag='div', text='x'):
        return {'sig': sig, 'tag': tag, 'type': None, 'text': text, 'box': box, 'font': 'Verdana'}
    def page_runs(page, vp, eng, n):
        cards = []
        imgs = []
        for i in range(1, 4):
            sig = f'main>div.card:nth-of-type({i})'
            cards.append(el(sig, [0, 100 * i, 300, 90]))
            if eng == 'webkit':
                cards.append(el(sig + '>span.logo-fallback', [0, 100 * i, 40, 40], tag='span', text='AB'))
            else:
                cards.append(el(sig + '>img.logo', [0, 100 * i, 40, 40], tag='img', text=''))
            imgs.append({'sig': sig + '>img.logo', 'src': f'/static/logos/l{i}.avif', 'complete': True,
                         'natural': [0, 0] if eng == 'webkit' else [40, 40]})
        r = {'page': page, 'viewport': vp, 'engine': eng, 'rerun': n, 'run_status': 'ran', 'elements': cards,
             'images': imgs, 'page_errors': [], 'failed_requests': [], 'steps': [], 'overflow_x': False, 'size': [1280, 800],
             'console_errors': (['Refused to apply inline style because it violates the Content Security Policy.']
                                if eng == 'webkit' else [])}
        if page == 'search' and eng in ('chromium', 'webkit'):
            r['console_errors'] = r['console_errors'] + [
                'Failed to load resource: the server responded with a status of 400 (BAD REQUEST)']
        if page == 'search':
            r['http_errors'] = ['400 http://127.0.0.1:8000/api/items?bbox=1']
        return r
    runs = [page_runs(p, vp, e, n) for p in ('home', 'search') for vp in ('1280x800', '390x844')
            for e in ('chromium', 'firefox', 'webkit') for n in (1, 2)]
    doc = {'engines': [{'name': e} for e in ('chromium', 'firefox', 'webkit')], 'reruns': 2, 'platform': 'win32', 'runs': runs}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    cands = ce.candidates(diffs, dismissed, doc, None, None, set())
    by = {}
    for c in cands:
        by.setdefault(c['subkind'], []).append(c)
    for sub in ('image_not_decoded', 'element_missing', 'element_extra'):
        cs = by.get(sub) or []
        if len(cs) != 1:
            fails.append(f'{sub}: repeated cards on two pages at two viewports are not one candidate: '
                         f'{[(c["key"], c["viewport"], c["pages"]) for c in cs]}')
        elif cs[0].get('occurrences') != 12 or sorted(cs[0].get('viewports') or []) != ['1280x800', '390x844']:
            fails.append(f'{sub}: the grouped candidate does not count its occurrences (12) and viewports: '
                         f'occurrences {cs[0].get("occurrences")}, viewports {cs[0].get("viewports")}')
    for sub in ('element_missing', 'element_extra'):
        for c in by.get(sub) or []:
            if 'webkit-windows-avif' not in c['build_limitations']:
                fails.append(f'{sub} {c["key"]} follows from the undecoded AVIF beside it but does not inherit the build '
                             f'limitation: {c["build_limitations"]}')
    csp = [c for c in by.get('console_error') or [] if any('Content Security' in m for ms in c['messages'].values() for m in ms)]
    if len(csp) != 1 or sorted(csp[0]['pages']) != ['home', 'search']:
        fails.append(f'one console message on two pages and two viewports is not one candidate: '
                     f'{[(c["pages"], c["viewport"]) for c in csp]}')
    http = [c for c in by.get('console_error') or [] if any('status of 400' in m for ms in c['messages'].values() for m in ms)]
    if http:
        fails.append(f'a 400 response every engine received, logged to the console by two engines only, is a candidate: '
                     f'{[(c["id"], c["engines"]) for c in http]}')
    # the engine-build limitations: a Windows WebKit control face other than Times, and flag emoji drawn as letters
    lim = ce.build_limitations({'engines': ['webkit'], 'subkind': 'font_family_differs', 'element': 'button.go',
                                'detail': '2 element(s) draw in font family "ms shell dlg" in webkit but "verdana" in chromium',
                                'messages': {}}, 'win32')
    if 'webkit-windows-serif-controls' not in lim:
        fails.append(f'"MS Shell Dlg" in WebKit on Windows does not match the default-face limitation: {lim}')
    lim = ce.build_limitations({'engines': ['firefox'], 'subkind': 'content_not_rendered', 'element': 'h1.title>span',
                                'detail': '', 'messages': {}, '_text': '\U0001F1F3\U0001F1FF'}, 'win32')
    if 'windows-flag-emoji' not in lim:
        fails.append(f'a flag emoji (regional indicators) on Windows does not match a flag-emoji limitation: {lim}')
    return fails


SNAP_PAGE = """<!doctype html><html><head><style>body { font-family: Verdana, sans-serif; margin: 0 }
.btn { font-family: Georgia, serif } .panel { height: 120px; overflow-y: auto }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.6) }</style></head><body>
<p class="intro">Runs</p><button class="btn">Filter</button>
<div class="panel" id="detail">""" + ''.join(f'<p>Step {i}</p>' for i in range(30)) + """</div>
<svg class="ring" width="120" height="120"><circle cx="60" cy="60" r="40" stroke="#333" stroke-width="16" fill="none"/></svg>
<div class="modal"></div></body></html>"""


def snapshot_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            return [], f'skipped ({type(exc).__name__})'
        page = browser.new_page(viewport={'width': 800, 'height': 600})
        page.set_content(SNAP_PAGE)
        snap = page.evaluate(capture.SNAP)
        browser.close()
    by = {e['sig'].split('>')[-1]: e for e in snap['elements']}
    btn, intro = by.get('button.btn') or {}, by.get('p.intro') or {}
    if 'Georgia' not in str(btn.get('font')) or 'Verdana' not in str(intro.get('font')):
        fails.append(f"the snapshot does not record each text element's computed font family: button {btn.get('font')}, "
                     f"p {intro.get('font')}")
    if not intro.get('occluded'):
        fails.append(f'the snapshot does not mark an element under a fixed overlay as occluded: {intro}')
    if snap.get('inner') != [800, 600] or snap.get('fullscreen') is not False:
        fails.append(f'the snapshot does not record the effective viewport and fullscreen state: inner {snap.get("inner")}, '
                     f'fullscreen {snap.get("fullscreen")}')
    faces = snap.get('faces') or {}
    if not any('Verdana' in k and isinstance(v, (int, float)) and v > 0 for k, v in faces.items()):
        fails.append(f'the snapshot does not measure the face each font family resolves to (faces): {faces}')
    circle = by.get('circle') or {}
    if circle.get('stroke') != 16:
        fails.append(f'the snapshot does not record the stroke width of an SVG shape: {circle}')
    sc = snap.get('scrollers') or []
    if not any(s.get('sig') == '#detail' and s.get('hidden_px', 0) > 100 for s in sc):
        fails.append(f'the snapshot does not record the inner scroll panel #detail: {sc}')
    return fails, 'ran'


def run(engine: str, rerun: int, elements: list[dict], scrollers: list | None = None) -> dict:
    r = {'page': 'home', 'viewport': '1280x800', 'engine': engine, 'rerun': rerun, 'run_status': 'ran',
         'elements': elements, 'images': [], 'page_errors': [], 'console_errors': [], 'failed_requests': [],
         'steps': [], 'overflow_x': False, 'size': [1280, 800]}
    if scrollers is not None:
        r['scrollers'] = scrollers
    return r


def compare_checks() -> list[str]:
    fails = []
    def el(sig, box, font='Verdana, sans-serif', occluded=False, tag='div', text='x'):
        return {'sig': sig, 'tag': tag, 'type': None, 'text': text, 'box': box, 'font': font,
                **({'occluded': True} if occluded else {})}
    ref = [el('body>div.bar>div.f1', [0, 0, 100, 20], font='"Segoe UI", sans-serif'),
           el('body>div.bar>div.f2', [0, 30, 100, 20], font='"Segoe UI", sans-serif'),
           el('body>div.under', [0, 100, 300, 50], occluded=True),
           el('#detail>div.step:nth-of-type(9)', [0, 400, 200, 20])]
    eng = [el('body>div.bar>div.f1', [0, 0, 100, 20], font='Times New Roman'),
           el('body>div.bar>div.f2', [0, 30, 100, 20], font='Times New Roman'),
           el('body>div.under', [0, 100, 260, 50], occluded=True),
           el('#detail>div.step:nth-of-type(9)', [0, 347, 200, 20])]
    sc_ref = [{'sig': '#detail', 'scroll_top': 0, 'scroll_left': 0, 'hidden_px': 500}]
    sc_eng = [{'sig': '#detail', 'scroll_top': 53, 'scroll_left': 0, 'hidden_px': 500}]
    doc = {'engines': [{'name': 'chromium'}, {'name': 'webkit'}], 'reruns': 2,
           'runs': [run('chromium', i, ref, sc_ref) for i in (1, 2)] + [run('webkit', i, eng, sc_eng) for i in (1, 2)]}
    diffs, flaky, dismissed = ce.compare(doc, Path(tempfile.gettempdir()), 'chromium', 1)
    fam = [d for d in diffs if d['subkind'] == 'font_family_differs']
    if len(fam) != 1 or len(fam[0].get('elements') or []) != 2:
        fails.append(f'a font-family change is not one font_family_differs difference listing its elements: {fam or diffs}')
    if any(d.get('element') == 'body>div.under' for d in diffs) or \
            not any(d.get('element') == 'body>div.under' and 'covered' in d.get('reason', '') for d in dismissed):
        fails.append(f'a box under an overlay (occluded) was reported, not dismissed: {[d for d in diffs if d.get("element") == "body>div.under"]}')
    if any(d.get('element', '').startswith('#detail>') for d in diffs) or \
            not any(d.get('element', '').startswith('#detail>') and 'scroll' in d.get('reason', '') for d in dismissed):
        fails.append('a box inside a panel the automation scrolled differently was reported as layout')
    return fails


def validator_render_checks() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        feats = json.loads((EXAMPLES / 'run' / 'features.json').read_text(encoding='utf-8'))
        empty = dict(feats, features=[], uses=0)
        (t / 'features.empty.json').write_text(json.dumps(empty), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(EXAMPLES / 'report.static.example.json'),
                            '--candidates', str(EXAMPLES / 'static-only' / 'candidates.json'), '--features',
                            str(t / 'features.empty.json'), '--targets', str(EXAMPLES / 'targets.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0:
            fails.append('validate_report.py accepted feature findings that are not in the --features file passed: '
                         + r.stdout.strip()[-160:])
        rep = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep['needs_review'] = [{'candidate_id': 'K99', 'reason': 'controls draw in a serif face in WebKit only; check by eye'}]
        rep['limitations'] = (rep.get('limitations') or []) + ['Inner scroll panels were compared by box only.']
        (t / 'r.json').write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'render_report.py'), str(t / 'r.json'), '--brief', '--color', 'never'],
                           capture_output=True, text=True, encoding='utf-8')
        if 'K99' not in r.stdout or 'Inner scroll panels were compared by box only.' not in r.stdout:
            fails.append('render_report.py --brief omits the needs-review items or the limitations')
        # a wording error is reported before any screenshot is decoded (the decode takes minutes on a big run)
        rep = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep['headline']['summary'] = rep['headline']['summary'] + ' The date picker works in Safari.'
        (t / 'safari.json').write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(t / 'safari.json'),
                            '--runs', str(EXAMPLES / 'run' / 'capture' / 'runs.json'),
                            '--candidates', str(EXAMPLES / 'run' / 'candidates.json'), '--jobs', '1'],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0 or 'Safari' not in r.stdout or 'recomputing the candidates' in r.stderr:
            fails.append('validate_report.py decoded the screenshots before reporting a summary naming Safari: '
                         f'exit {r.returncode}, stderr {r.stderr.strip()[-120:]!r}')
        # a summary count that does not match the report (3 need review, with none in needs_review)
        rep = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep['headline']['summary'] = rep['headline']['summary'] + ' 3 need review in a real browser.'
        (t / 'count.json').write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(t / 'count.json'),
                            '--runs', str(EXAMPLES / 'run' / 'capture' / 'runs.json'),
                            '--candidates', str(EXAMPLES / 'run' / 'candidates.json'), '--jobs', '1',
                            '--features', str(EXAMPLES / 'run' / 'features.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0 or 'need review' not in r.stdout:
            fails.append(f'validate_report.py accepted a summary saying "3 need review" with no needs_review entry: '
                         f'{r.stdout.strip()[-160:]!r}')
        # a state whose step fails in every engine must be named in the limitations (it is otherwise silent)
        cands = json.loads((EXAMPLES / 'run' / 'candidates.json').read_text(encoding='utf-8'))
        cands['same_everywhere'] = [{'page': 'home~search', 'viewports': ['1280x800'], 'kind': 'step_failed', 'step': 0,
                                     'action': {'click': '#go'}, 'messages': ['TimeoutError: covered']}]
        (t / 'cands.json').write_text(json.dumps(cands), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(EXAMPLES / 'report.example.json'),
                            '--runs', str(EXAMPLES / 'run' / 'capture' / 'runs.json'),
                            '--candidates', str(t / 'cands.json'), '--jobs', '1',
                            '--features', str(EXAMPLES / 'run' / 'features.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0 or 'home~search' not in r.stdout:
            fails.append(f'validate_report.py accepted a report silent about a step that failed in every engine: '
                         f'{r.stdout.strip()[-160:]!r}')
    return fails


def verdict_checks() -> list[str]:
    """A cautious verdict (inconclusive) with nothing promoted is accepted, and --write-summary fills the computed fields
    without overwriting the agent's verdict."""
    fails = []
    rep = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
    ids = [t['candidate_id'] for t in rep['triage']]
    rep['differences'] = []
    rep['triage'] = [{'candidate_id': c, 'decision': 'needs_review', 'difference_id': None,
                      'reason': 'held back for a second look at the screenshots of both engines'} for c in ids]
    rep['needs_review'] = [{'candidate_id': c, 'reason': 'compare the screenshots again in a real browser'} for c in ids]
    rep['headline']['verdict'] = 'inconclusive'
    rep['headline']['summary'] = ('Inconclusive: 5 candidates need review before any is called a difference; '
                                  'real Safari and devices were not tested.')
    for k in ('severity_counts', 'flaky_count', 'checks', 'evidence_completeness', 'overall_confidence'):
        rep['headline'].pop(k, None)
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'r.json'
        p.write_text(json.dumps(rep), encoding='utf-8')
        args = [sys.executable, '-B', str(HERE / 'validate_report.py'), str(p), '--runs', str(EXAMPLES / 'run' / 'capture' / 'runs.json'),
                '--candidates', str(EXAMPLES / 'run' / 'candidates.json'), '--jobs', '1',
                '--features', str(EXAMPLES / 'run' / 'features.json')]
        r = subprocess.run(args + ['--write-summary'], capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        got = json.loads(p.read_text(encoding='utf-8'))['headline'].get('verdict')
        if r.returncode != 0 or got != 'inconclusive':
            fails.append(f'an inconclusive verdict with nothing promoted is rejected or overwritten by --write-summary '
                         f'(exit {r.returncode}, verdict now {got!r}): {r.stdout.strip()[-200:]!r}')
        # webkit's requests upgraded to https on the plain-http site: the report must name the environment cause
        runs = json.loads((EXAMPLES / 'run' / 'capture' / 'runs.json').read_text(encoding='utf-8'))
        site = runs['site'].rstrip('/')
        for x in runs['runs']:
            if x['engine'] == 'webkit':
                x['failed_requests'] = (x.get('failed_requests') or []) + [
                    'GET ' + site.replace('http://', 'https://') + '/style.css (SSL connect error)']
        (Path(tmp) / 'runs.json').write_text(json.dumps(runs), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(EXAMPLES / 'report.example.json'),
                            '--runs', str(Path(tmp) / 'runs.json'), '--candidates', str(EXAMPLES / 'run' / 'candidates.json'),
                            '--jobs', '1', '--features', str(EXAMPLES / 'run' / 'features.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if r.returncode == 0 or 'upgrade-insecure-requests' not in r.stdout or 'environment' not in r.stdout:
            fails.append(f'validate_report.py accepted a report silent about requests upgraded to https on an http site: '
                         f'{r.stdout.strip()[-200:]!r}')
        # a limitation that names the cause in words ("fetched over https") satisfies the rule: \bhttps\b, not
        # the backspace characters a lost escape once left in the pattern
        rep3 = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep3['limitations'].append('WebKit fetched the stylesheet over https on the plain-http site and failed; '
                                   'those runs were not compared.')
        p3 = Path(tmp) / 'r3.json'
        p3.write_text(json.dumps(rep3), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(p3),
                            '--runs', str(Path(tmp) / 'runs.json'), '--candidates', str(EXAMPLES / 'run' / 'candidates.json'),
                            '--jobs', '1', '--features', str(EXAMPLES / 'run' / 'features.json')],
                           capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        if 'fetched the site over https' in r.stdout:
            fails.append('validate_report.py still asks for the https environment cause when a limitation names it '
                         f'("over https"): {r.stdout.strip()[-200:]!r}')
        # differences promoted but the agent wrote no_differences_detected: an error, never silently rewritten
        rep2 = json.loads((EXAMPLES / 'report.example.json').read_text(encoding='utf-8'))
        rep2['headline']['verdict'] = 'no_differences_detected'
        p.write_text(json.dumps(rep2), encoding='utf-8')
        r = subprocess.run(args + ['--write-summary'], capture_output=True, text=True, encoding='utf-8', cwd=SKILL)
        got = json.loads(p.read_text(encoding='utf-8'))['headline'].get('verdict')
        if r.returncode == 0 or got != 'no_differences_detected':
            fails.append(f'--write-summary rewrote or accepted a verdict that contradicts the differences: exit {r.returncode}, '
                         f'verdict now {got!r}')
    return fails


NEXT_TREE = {
    'package.json': json.dumps({'name': 'bike-shop', 'dependencies': {'next': '15.3.0', 'react': '19.0.0'},
                                'devDependencies': {'tailwindcss': '4.1.0'}}),
    'next.config.ts': 'const nextConfig = { async headers() { return [] } };\nexport default nextConfig;\n'
                      'const s = structuredClone({});\n',
    'eslint.config.mjs': 'const all = [].toSorted();\nexport default all;\n',
    'postcss.config.mjs': 'export default { plugins: { "@tailwindcss/postcss": {} } };\nObject.groupBy([], x => x);\n',
    'next-env.d.ts': '/// <reference types="next" />\n',
    'src/app/layout.tsx': 'export default function L({ children }) { return <html><body>{children}</body></html> }\n',
    'src/app/page.tsx': 'export default function P() { return <h1 className="text-balance">Bikes</h1> }\n',
    'src/app/nav.tsx': '"use client";\nexport function Nav() { const l = [1, 2].findLast(x => x); return <nav>{l}</nav> }\n',
    'src/app/robots.ts': 'export default function robots() { return { rules: [].toSorted() } }\n',
    'src/app/feed.xml/route.ts': 'export async function GET() { const x = structuredClone({}); return new Response("") }\n',
    'src/app/[slug]/opengraph-image.tsx': 'import { ImageResponse } from "next/og";\n'
                                          'export default function I() { const g = Object.groupBy([], x => x); return new ImageResponse(<div />) }\n',
    'src/lib/server-data.ts': 'import "server-only";\nexport const rows = [].toSorted();\n',
    '.next/static/chunks/main.js': 'self.x = 1;\n',
    '.next/static/css/app.css': '.a{color:red}\n',
}


def next_scan_checks() -> tuple[list[str], str]:
    """A Next.js App Router project: route handlers, generated metadata routes, config files, type declarations and
    server-only modules don't ship to browsers; .next is the built CSS read with --built-css, its JS is not scanned (and
    the scan says so); minified built CSS with a guarded and an unguarded use keeps them apart; browserslist
    `<browser> all`."""
    try:
        from fetch_data import asset_path
        asset_path('web-features'); asset_path('caniuse-db')
    except (FileNotFoundError, ImportError, KeyError):
        return [], 'skipped (feature data not cached: fetch_data.py)'
    fails = []
    import browserslist_lite as bl
    res = bl.Resolver(json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))).resolve('> 0.3%, not op_mini all, ie all')
    if res['unresolved']:
        fails.append(f'browserslist "<browser> all" is unresolved: {res["unresolved"]}')
    filler = ''.join(f'.f{i}{{margin:{i}px}}' for i in range(120))
    built = (filler + '@supports (color:oklch(0 0 0)){.card{color:oklch(50% .1 200)}}.price{color:oklch(60% .1 100)}'
             '.bar{--tw-gradient-position:to right in oklab}' + filler + '\n')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'bike-shop'
        for rel, text in NEXT_TREE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        (root / '.next/static/css/app.css').write_text(built, encoding='utf-8')
        out = Path(tmp) / 'features.json'
        r = subprocess.run([sys.executable, '-B', str(HERE / 'feature_scan.py'), '--repo', str(root), '--out', str(out),
                            '--query', 'chrome >= 109, firefox >= 115, safari >= 15'], capture_output=True, text=True, encoding='utf-8')
        try:
            d = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return [f'feature_scan.py failed: {r.stderr[-300:]}'], 'ran'
        scanned = set((d.get('scanned_files') or {}).get('files') or [])
        server = {'next.config.ts', 'eslint.config.mjs', 'postcss.config.mjs', 'next-env.d.ts', 'src/app/robots.ts',
                  'src/app/feed.xml/route.ts', 'src/app/[slug]/opengraph-image.tsx', 'src/lib/server-data.ts'}
        if scanned & server:
            fails.append(f'server-only files were scanned as browser code: {sorted(scanned & server)}')
        if not {'src/app/page.tsx', 'src/app/nav.tsx'} <= scanned:
            fails.append(f'App Router pages and client components were not scanned: {sorted(scanned)}')
        listed = {x.get('path') if isinstance(x, dict) else x for x in (d.get('skipped') or {}).get('server_only') or []}
        if not server <= listed:
            fails.append(f'skipped.server_only does not list the server-only files: {sorted(listed)}')
        ex = [x for x in (d.get('skipped') or {}).get('excluded_with_code') or [] if x.get('path') == '.next']
        if ex and not ex[0].get('served_by'):
            fails.append(f'.next is listed as excluded code with no note that its CSS was scanned: {ex}')
        if not any('.next' in x and 'JavaScript' in x for x in d.get('limitations') or []):
            fails.append(f'no limitation says the built JavaScript in .next was not scanned: {d.get("limitations")}')
        feats = {f['feature']: f for f in d['features']}
        ok = feats.get('oklab')
        if not ok or ok['fallback'] or '.price' not in ok['files'][0]['snippet'] or ok['files'][0].get('fallback') is not False \
                or not ok['files'][0].get('guarded'):
            fails.append(f'a guarded and an unguarded oklch() on one minified line are not kept apart (unguarded snippet, '
                         f'guarded count): {ok and ok["files"]}')
        if 'gradient-interpolation' not in feats or (ok and any('in oklab' in u['snippet'] for u in ok['files'])):
            fails.append(f'gradient "in oklab" interpolation is read as an oklab() colour, not gradient interpolation: '
                         f'{sorted(feats)}')
    return fails, 'ran'


def main() -> int:
    ok = True
    for name, fn in (('feature_scan.py: Go string constants, vendored trees, language guard', feature_scan_checks),
                     ('feature_scan.py: a Flask site with Jinja templates, a data directory, --exclude', flask_scan_checks),
                     ('feature_scan.py: a served dist/ folder, --include, app APIs, an Emscripten loader', served_dist_checks),
                     ('capture.py snapshot: font family, occlusion, inner scroll panels', snapshot_checks),
                     ('feature_scan.py: Next.js server-only files, built JS, guarded uses, browserslist all', next_scan_checks)):
        fails, how = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name} ({how})" + ''.join(f'\n  - {f}' for f in fails))
    for name, fn in (('compare_engines.py: font family, occlusion, scroll offsets', compare_checks),
                     ('compare_engines.py: grouping repeats, inherited limitations, logged HTTP errors', grouping_checks),
                     ('compare_engines.py: viewport, media aborts, second ink pass, font limits, inline reflow, '
                      'same everywhere', round8_compare_checks),
                     ('compare_engines.py: https upgrade as one environment cause, blank reruns, failed stylesheet', environment_checks),
                     ('compare_engines.py: promotable boxes, decoded photo, system-ui face, SVG stroke, image aborts',
                      round10_compare_checks),
                     ('validate_report.py: a cautious verdict, --write-summary keeps the verdict', verdict_checks),
                     ('validate_report.py --features and render_report.py --brief', validator_render_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
