#!/usr/bin/env python3
"""Self-test for the WCAG auditor's scripts: merge_findings.py --self-test (colour maths, generated ids),
template grouping for one generated page in many folders, inner scroll panels a full-page screenshot
cannot show (needs Playwright with Chromium; skipped without it), and validator cases (blocked_requests
as run.json records it, an agent finding's re-readable basis). Also: --color never, named states in the
check matrix, critical-path priority and untestable surfaces, one zoom-disabled candidate, and in
Chromium the language script census, a blank canvas behind text, a page with no Tab stops and a touch
viewport. Round 10: targets with expect_status (a 404 page audited, served on loopback port 8660), crawl
shapes from route patterns and root-level siblings, states that navigate (via / ended_on), focus after a
state's actions and keyboard_start, the default failure pattern from the rule, focus measured per line box
with crops kept, hover probe skips, svg xpaths, run.json objects in the schema, --markdown, agent_review
passes resting on probes, the third-party limitation and unreachable pages in the validator.

    python self_test.py

Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import merge_findings  # noqa: E402
import normalize  # noqa: E402
import run_audit  # noqa: E402
import validate_audit  # noqa: E402

EXAMPLES = HERE.parent / 'examples'


def template_checks() -> list[str]:
    fails = []
    bare = {'landmarks': '', 'main_shape': 'div,script'}
    a = run_audit.template_key({'url': 'http://127.0.0.1:8191/run-a/report.html', 'template_sig': bare})
    b = run_audit.template_key({'url': 'http://127.0.0.1:8191/run-b/report.html', 'template_sig': bare})
    if a != b:
        fails.append(f'one generated page with no landmarks in two folders gives two templates ({a} / {b})')
    marked = {'landmarks': 'banner+main+contentinfo', 'main_shape': 'h1,p'}
    c = run_audit.template_key({'url': 'http://127.0.0.1:8191/docs/a.html', 'template_sig': marked})
    d = run_audit.template_key({'url': 'http://127.0.0.1:8191/blog/a.html', 'template_sig': marked})
    if c == d:
        fails.append('pages with landmarks in different sections no longer keep their section in the template key')
    return fails


def crawl_order_checks() -> list[str]:
    """A large repeated menu of one URL pattern (/region/<name>, 40 links) comes before the footer links in every
    page: with 8 loads the crawl must still reach the footer pages, not spend every load on the menu."""
    fails = []
    base = 'http://127.0.0.1:8191'
    menu = [f'{base}/region/{n}' for n in ('alder', 'birch', 'cedar', 'damson', 'elm', 'fir', 'gorse', 'hazel', 'ivy',
                                            'juniper', 'larch', 'maple', 'nettle', 'oak', 'pine', 'quince', 'rowan',
                                            'sorrel', 'teasel', 'umber', 'vetch', 'willow', 'yarrow', 'zelkova') * 1]
    menu += [f'{base}/region/r{i}' for i in range(16)]
    footer = [f'{base}/privacy', f'{base}/terms', f'{base}/contact']
    queue = [base + '/'] + menu + footer
    pick = getattr(run_audit, 'crawl_pick', None)
    loaded, counts = [], {}
    while queue and len(loaded) < 8:
        i = pick(queue, counts) if pick else 0
        url = queue.pop(i)
        loaded.append(url)
        if pick:
            p = run_audit.crawl_pattern(url)
            counts[p] = counts.get(p, 0) + 1
    missed = [u for u in footer if u not in loaded]
    if missed:
        fails.append(f'the crawl spent {sum(u in menu for u in loaded)} of 8 loads on one URL pattern (/region/*) and '
                     f'never loaded {", ".join(u[len(base):] for u in missed)}')
    if run_audit.__dict__.get('crawl_pattern'):
        same = run_audit.crawl_pattern(f'{base}/region/alder') == run_audit.crawl_pattern(f'{base}/region/birch')
        apart = run_audit.crawl_pattern(f'{base}/privacy') != run_audit.crawl_pattern(f'{base}/terms')
        if not (same and apart):
            fails.append('crawl_pattern: /region/a and /region/b must share a pattern; /privacy and /terms must not')
    return fails


SCROLL_PAGE = """<!doctype html><html><body style="margin:0">
<div style="height:60px">Run summary</div>
<div style="display:flex; height:calc(100vh - 60px)">
  <div class="list" style="width:300px; overflow-y:auto">""" + ''.join(f'<p>Row {i}</p>' for i in range(80)) + """</div>
  <div class="detail" style="flex:1; overflow:auto"><p>Short detail</p></div>
</div></body></html>"""


def scroll_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # no browser installed
            return [], f'skipped ({type(exc).__name__})'
        page = browser.new_page(viewport={'width': 1000, 'height': 600})
        page.set_content(SCROLL_PAGE)
        try:
            found = run_audit.inner_scroll_containers(page)
        except AttributeError:
            found = None
        browser.close()
    if not found or len(found) != 1 or 'list' not in json.dumps(found):
        fails.append(f'the inner scroll panel (.list, most rows below its box) was not reported: {found}')
    else:
        note = run_audit.scroll_limitation([{'id': 'p01', 'url': 'http://x/', 'inner_scroll': found}])
        if not note or 'p01' not in note:
            fails.append(f'no limitation text for pages with inner scroll panels: {note}')
    return fails, 'ran'


# A navigation menu that :focus-within reveals: its 80 links are hidden (so not counted) before the traversal,
# then join the Tab order as soon as focus enters the menu. The main content comes after it.
FOCUS_MENU_PAGE = """<!doctype html><html><head><style>
.menu { display: none } .dd:focus-within .menu { display: block } a { display: block }
</style></head><body>
<nav><div class="dd"><button type="button">Places</button><div class="menu">""" + \
    ''.join(f'<a href="/place/{i}">Place {i}</a>' for i in range(80)) + """</div></div></nav>
<main>""" + ''.join(f'<a href="/item/{i}">Item {i}</a>' for i in range(10)) + """</main></body></html>"""


def keyboard_checks() -> tuple[list[str], str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # no browser installed
            return [], f'skipped ({type(exc).__name__})'
        shots = Path(tmp)
        for cfg, want in (({'probes': ['keyboard']}, 'ran'),
                          ({'probes': ['keyboard'], 'keyboard': {'max_presses': 30}}, 'partial')):
            page = browser.new_page(viewport={'width': 1000, 'height': 700})
            page.set_content(FOCUS_MENU_PAGE)
            r = run_audit.Runner(cfg, shots, None)
            r.inject_helpers(page)
            res = r.probe_keyboard(page, shots, 'p01')
            res.setdefault('status', 'ran')
            page.close()
            n_unreached = len(res['unreached'])
            if want == 'ran' and (n_unreached or res['status'] != 'ran'):
                fails.append(f'a focus-revealed menu of 80 links ended the keyboard probe early: {res["presses"]} presses, '
                             f'{n_unreached} of {res["focusable_count"]} focusable elements unreached, status {res["status"]}')
            if want == 'partial' and (res['status'] != 'partial' or not res.get('reason') or res.get('stopped') != 'press_limit'):
                fails.append(f'a keyboard probe stopped by max_presses 30 with {n_unreached} elements unreached is recorded '
                             f'as {res["status"]} (stopped {res.get("stopped")}), not partial with a reason')
        browser.close()
    return fails, 'ran'


def normalize_partial_checks() -> list[str]:
    """A partial keyboard probe: its stops are evidence (an invisible focus indicator), its unreached list is not."""
    fails = []
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    n = normalize.Norm(run)
    stop = {'xpath': '/html/body/nav/a[1]', 'html': '<a href="/x">X</a>', 'sig': 'a', 'tabindex': None, 'doc_y': 10,
            'indicator': {'changed_px': 0, 'perimeter': 120}}
    rec = {'page': 'p01', 'url': run['sample'][0]['url'], 'viewport': '1280x800', 'state': 'initial', 'screenshot': None}
    n.probes(rec, {'keyboard': {'status': 'partial', 'reason': 'press limit', 'stops': [stop], 'trap': None,
                                'unreached': [{'xpath': '/html/body/main/a[1]', 'html': '<a href="/y">Y</a>'}],
                                'pointer_only': [], 'positive_tabindex': [], 'jumps': [], 'hover_only': []}})
    codes = [i['rule_id'] for i in n.items]
    if 'focus.not_visible' not in codes:
        fails.append(f'the stops of a partial keyboard probe are dropped (items: {codes})')
    if 'keyboard.unreachable' in codes:
        fails.append('elements a partial keyboard probe never got to are reported as unreachable by keyboard')
    return fails


def validator_checks() -> list[str]:
    fails = []
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    d = json.loads(json.dumps(base))
    d['metadata']['blocked_requests'] = list(run['blocked_requests'])  # copied from run.json as it is recorded there
    errs = validate_audit.schema_errors(d, schema, schema)
    if errs:
        fails.append(f'blocked_requests copied from run.json (a list) is a schema error: {errs[:2]}')
    d = json.loads(json.dumps(base))
    d['metadata']['blocked_requests'] = 3
    try:
        errs = validate_audit.check(d, EXAMPLES / 'run', None)
    except TypeError:
        errs = validate_audit.check(d, EXAMPLES / 'run', None, None)
    if not any('blocked_requests' in e for e in errs):
        fails.append('blocked_requests 3 against a run.json that blocked nothing is accepted')
    # a keyboard probe cut short by its press limit is recorded as partial: the schema takes it, completeness counts
    # it as not run, confidence can't be high, and the limitations must say which probe was cut short
    d = json.loads(json.dumps(base))
    d['schema_version'] = '1.3'
    rows = [c for c in d['metadata']['checks'] if c['kind'] == 'probe' and c['name'] in ('keyboard', 'focus_visible')]
    for c in rows[:2]:
        c['status'] = 'partial'
    errs = validate_audit.schema_errors(d, schema, schema)
    if errs:
        fails.append(f'a probe check recorded as partial is a schema error: {errs[:2]}')
    s = validate_audit.compute_summary(d)
    if s['overall_confidence'] == 'high':
        fails.append(f'overall confidence is high although {len(rows[:2])} keyboard/focus checks are partial '
                     f'(completeness {s["evidence_completeness"]})')
    d['headline'].update({k: s[k] for k in ('evidence_completeness', 'overall_confidence')})
    errs = validate_audit.check(d, None, None, None)
    if not any('partial' in x for x in errs):
        fails.append('a report with partial keyboard checks and no limitation naming them is accepted')
    d['limitations'].append('The keyboard probe was partial on p01: it stopped at its press limit before a full pass.')
    errs = validate_audit.check(d, None, None, None)
    if any('partial' in x for x in errs):
        fails.append(f'a limitation naming the partial keyboard probe is not accepted: {[x for x in errs if "partial" in x][:1]}')
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / 'report.go').write_text('package report\n\nconst page = `<script>\n'
                                        "document.addEventListener('keydown', (e) => { if (e.key === 'j') next(); });\n"
                                        '</script>`\n', encoding='utf-8')
        agent = next(f for f in base['findings'] if f['evidence_type'] == 'agent_judgment')
        def with_basis(excerpt):
            x = json.loads(json.dumps(base))
            f = next(y for y in x['findings'] if y['id'] == agent['id'])
            f['basis'] = [{'file': 'report.go', 'line': 4, 'excerpt': excerpt}]
            return x
        good, bad = with_basis("if (e.key === 'j') next();"), with_basis("if (e.key === 'k') prev();")
        for x, want_ok in ((good, True), (bad, False)):
            errs = validate_audit.schema_errors(x, schema, schema)
            try:
                errs += validate_audit.check(x, None, None, repo)
            except TypeError:
                errs += ['check() takes no repo']
            ok = not any('basis' in e or 'repo' in e or 'unexpected field' in e for e in errs)
            if ok != want_ok:
                fails.append(f"an agent finding's basis {'matching' if want_ok else 'not matching'} report.go:4 was "
                             f"{'rejected' if want_ok else 'accepted'} with --repo: {[e for e in errs if 'basis' in e or 'repo' in e or 'unexpected' in e][:2]}")
        x = json.loads(json.dumps(base))
        f = next(y for y in x['findings'] if y['id'] == agent['id'])
        f['resolution'] = (f.get('resolution') or '') + ' The keydown handler is at report.go:4.'
        try:
            errs = validate_audit.check(x, None, None, None)
        except TypeError:
            errs = validate_audit.check(x, None, None)
        if not any('basis' in e for e in errs):
            fails.append('an agent finding that cites report.go:4 in prose but has no basis is accepted')
    return fails


def render_checks() -> list[str]:
    """--color never writes no ANSI escape anywhere (the colour flag must not be shadowed by a loop variable)."""
    import subprocess
    fails = []
    out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(EXAMPLES / 'audit.example.json'), '--color', 'never'],
                         capture_output=True, text=True, encoding='utf-8', errors='replace',
                         env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
    n = (out.stdout or '').count('\033')
    if out.returncode or n:
        fails.append(f'render_report.py --color never wrote {n} ANSI escape(s) (exit {out.returncode})')
    return fails


def state_matrix_checks() -> list[str]:
    """A named state's engines and probes are planned checks too: a partial keyboard probe in a state counts against
    completeness and confidence like one on the initial page."""
    fails = []
    sample = [{'id': 'p01', 'reason': 'listed'}]
    pages = [{'page': 'p01', 'viewports': {'1280x800': {
        'engines': {'axe-core': 'ran', 'ibm-equal-access': 'ran', 'htmlcs': 'ran'}, 'probes': {'keyboard': 'ran'},
        'states': [{'name': 'menu_open', 'engines': {'axe-core': 'ran', 'ibm-equal-access': 'ran', 'htmlcs': 'errored'},
                    'probes': {'keyboard': 'partial'}}]}}}]
    fn = getattr(run_audit, 'check_matrix', None)
    if fn is None:
        return ['run_audit.check_matrix is missing: the check matrix is built inline and leaves named states out']
    rows = fn(sample, pages, ['1280x800'], ['keyboard'], ['axe-core', 'ibm-equal-access', 'htmlcs'])
    st = [r for r in rows if r.get('state') == 'menu_open']
    if not any(r['name'] == 'keyboard' and r['status'] == 'partial' for r in st):
        fails.append(f'the partial keyboard probe of state menu_open is not in the check matrix: {st}')
    if not any(r['name'] == 'htmlcs' and r['status'] == 'errored' for r in st):
        fails.append(f'the errored engine of state menu_open is not in the check matrix: {st}')
    return fails


def rating_checks() -> list[str]:
    """RUBRIC: blocks_critical_path makes a finding P0; an agent finding re-readable from a basis keeps it.
    A finding on a critical-path step that does not affect what the step needs (affects_step false) is not raised.
    An untestable main surface caps overall confidence below high."""
    fails = []
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    f = {'severity': 'critical', 'evidence_type': 'agent_judgment', 'blocks_critical_path': True,
         'basis': [{'file': 'index.html', 'line': 3, 'excerpt': 'window.addEventListener'}]}
    if validate_audit.band(f, 4) != 'P0':
        fails.append(f'an agent finding with a basis that blocks a critical path is {validate_audit.band(f, 4)}, not P0')
    f2 = dict(f); f2.pop('basis')
    if validate_audit.band(f2, 4) != 'P1':
        fails.append(f'an agent finding with no basis and no candidate that blocks a critical path is {validate_audit.band(f2, 4)}, not P1')
    g = {'base_severity': 'moderate', 'non_interference': False,
         'critical_path': {'name': 'checkout', 'step': 'pay', 'affects_step': False}}
    if validate_audit.expected_severity(g) != 'moderate':
        fails.append('a critical-path finding marked affects_step false is still raised one level')
    d = json.loads(json.dumps(base))
    d['schema_version'] = '1.4'
    d['metadata']['untestable_surfaces'] = [{'page': d['metadata']['sample'][0]['id'], 'surface': 'canvas.main',
                                             'reason': 'the main canvas never painted: its content was not tested'}]
    schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
    errs = validate_audit.schema_errors(d, schema, schema)
    if errs:
        fails.append(f'metadata.untestable_surfaces is a schema error: {errs[:2]}')
    d['assisted_review']['scs_reviewed'] = d['assisted_review']['scs_reviewed'] or ['1.1.1']
    for c in d['metadata']['checks']:
        c['status'] = 'ran'
    for t in d['metadata']['tools']:
        t['status'] = 'ran'
    if validate_audit.compute_summary(d)['overall_confidence'] == 'high':
        fails.append('overall confidence is high although the main surface of a page is recorded as untestable')
    return fails


def zoom_merge_checks() -> list[str]:
    """axe meta-viewport, the resize_text probe and an HTMLCS message on the viewport meta are one root cause: one
    candidate with the zoom-disabled pattern."""
    fails = []
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    n = normalize.Norm(run)
    rec = {'page': 'p01', 'url': run['sample'][0]['url'], 'viewport': '1280x800', 'state': 'initial', 'screenshot': None}
    meta = '<meta name="viewport" content="width=device-width, user-scalable=no">'
    n.add(rec, tool='axe-core', rule_id='meta-viewport', act_rules=['b4f0c3'], sc=['1.4.4'], kind='wcag', outcome='failed',
          el={'xpath': '/html/head/meta[2]', 'sig': 'head > meta', 'html': meta}, html=meta)
    n.probe(rec, 'resize_text.zoom_disabled', 'failed', message='Viewport meta prevents zoom')
    n.add(rec, tool='htmlcs', rule_id='Guideline1_4.1_4_10.C32', sc=['1.4.10'], kind='wcag', outcome='failed',
          el={'xpath': '/html/head/meta[2]', 'sig': 'head > meta', 'html': meta})
    scs = validate_audit.load_scs()
    cands = [c for c in merge_findings.merge(n.items, scs) if c['kind'] == 'wcag']
    if len(cands) != 1:
        fails.append(f'the zoom-disabled viewport meta came out as {len(cands)} candidates: '
                     f'{[(c["component"], c["sc"], c["pattern"]) for c in cands]}')
    elif cands[0]['pattern'] != '1.4.4-zoom-disabled':
        fails.append(f'the zoom-disabled candidate has pattern {cands[0]["pattern"]}, not 1.4.4-zoom-disabled')
    return fails


LANG_PAGE = """<!doctype html><html lang="en"><body><main><h1>Настройки</h1>
<p>Выберите язык интерфейса и нажмите кнопку, чтобы продолжить работу с приложением.</p>
<p>Введите ключ доступа из пяти букв в поле ниже и сохраните изменения на этой странице.</p>
<button>Сохранить</button></main></body></html>"""

CANVAS_PAGE = """<!doctype html><html lang="en"><body style="margin:0;background:#fff">
<canvas id="c" width="800" height="600" style="position:fixed;inset:0;width:100vw;height:100vh"></canvas>
<p id="t" style="position:relative;color:#767676;margin:40px">Loading the player</p></body></html>"""


def browser_probe_checks() -> tuple[list[str], str]:
    """Probes in a real page: the language probe sees non-Latin text (a Cyrillic page marked lang=en), a blank canvas
    behind text is not an unknown background, a page with no Tab stops is fully traversed (not partial), and a
    mobile viewport emulates touch."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            return [], f'skipped ({type(exc).__name__})'
        shots = Path(tmp)
        r = run_audit.Runner({'probes': ['keyboard', 'language']}, shots, None)
        page = browser.new_page(viewport={'width': 1000, 'height': 700})
        page.set_content(LANG_PAGE)
        r.inject_helpers(page)
        lg = r.probe_language(page)
        lg['status'] = 'ran'
        run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
        n = normalize.Norm(run)
        rec = {'page': 'p01', 'url': run['sample'][0]['url'], 'viewport': '1000x700', 'state': 'initial', 'screenshot': None}
        n.probes(rec, {'language': lg})
        if not any(i['rule_id'] == 'language.mismatch' for i in n.items):
            fails.append(f'a page of Cyrillic text marked lang="en" gives no language.mismatch (probe: detected '
                         f'{lg.get("detected")}, script {lg.get("script")})')
        page.set_content(CANVAS_PAGE)
        r.inject_helpers(page)
        ctx = page.evaluate("() => window.__qa.contrastTarget(['#t'])")
        est = merge_findings.estimate(ctx)
        if not est or est.get('unknown_background'):
            fails.append(f'text over a canvas that was never drawn has an unknown background: {est}')
        page.evaluate("() => { const g = document.getElementById('c').getContext('2d'); g.fillStyle = '#333'; g.fillRect(0, 0, 800, 600); }")
        est = merge_findings.estimate(page.evaluate("() => window.__qa.contrastTarget(['#t'])"))
        if not est or not est.get('unknown_background'):
            fails.append(f'text over a painted canvas is estimated against the CSS background: {est}')
        page.set_content('<!doctype html><html><body><div style="position:fixed;inset:0;cursor:pointer">Click to continue</div></body></html>')
        r.inject_helpers(page)
        res = r.probe_keyboard(page, shots, 'p01')
        if res.get('status') == 'partial':
            fails.append(f'a page with no focusable element is a partial keyboard probe: {res.get("reason")}')
        page.close()
        r = run_audit.Runner({'viewports': ['390x844'], 'mobile_viewports': ['390x844']}, shots, None)
        ctx = r.new_context(browser, '390x844')
        p = ctx.new_page()
        p.set_content('<p>x</p>')
        touch = p.evaluate("() => navigator.maxTouchPoints > 0 && matchMedia('(pointer: coarse)').matches")
        ctx.close()
        if not touch:
            fails.append('a viewport listed in mobile_viewports does not emulate a touch device')
        browser.close()
    return fails, 'ran'


# ---------------------------------------------------------------- round 10: an App Router content site

TEST_PORT = 8660   # loopback only; the browser checks serve their pages here


class _Site:
    """A tiny loopback site: {path: (status, html)}; anything else answers 404 with a plain not-found page."""

    def __init__(self, pages: dict):
        import http.server, threading
        site = pages

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                status, html = site.get(self.path.split('?')[0], (404, '<!doctype html><html lang="en"><title>Not found</title>'
                                                                       '<body><main><h1>Page not found</h1><a href="/">Home</a></main></body></html>'))
                body = html.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', TEST_PORT), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.base = f'http://127.0.0.1:{TEST_PORT}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def _quiet_runner(cfg, out):
    r = run_audit.Runner({'rate_limit_seconds': 0, 'settle_seconds': 2, **cfg}, out, None)
    return r


def expect_status_checks() -> list[str]:
    """G1: a target listed with expect_status (a not-found page answering 404) is audited, not unreachable; a target
    answering a status nobody expected stays unreachable."""
    fails = []
    norm = getattr(run_audit, 'normalise_targets', None)
    if norm is None:
        return ['run_audit.normalise_targets is missing: a target cannot say which HTTP status it expects (expect_status)']
    urls, expect = norm([{'url': 'http://127.0.0.1:1/missing', 'expect_status': 404}, 'http://127.0.0.1:1/'])
    if urls != ['http://127.0.0.1:1/missing', 'http://127.0.0.1:1/'] or expect != {'http://127.0.0.1:1/missing': 404}:
        fails.append(f'normalise_targets gave {urls} / {expect}')
    return fails


def blocked_veto_checks() -> list[str]:
    """G1: blocked rows of a page that was never reached do not veto a probe-based pass when the limitations name the
    page; blocked or errored rows of a reached page still do, and an unnamed unreachable page is an error."""
    fails = []
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as tmp:
        rd = Path(tmp)
        (rd / 'screenshots').mkdir()
        for f in (EXAMPLES / 'run' / 'screenshots').iterdir():
            (rd / 'screenshots' / f.name).write_bytes(b'')
        gone = {'id': 'p05', 'url': 'http://127.0.0.1:8137/gone.html', 'reason': 'unreachable', 'error': 'HTTP 500', 'title': None}
        run2 = json.loads(json.dumps(run))
        run2['sample'].append({k: gone.get(k) for k in ('id', 'url', 'final_url', 'title', 'template', 'template_key', 'reason', 'error', 'captcha')})
        extra = [{'page': 'p05', 'viewport': vp, 'kind': 'probe', 'name': 'keyboard', 'status': 'blocked'} for vp in run['viewports']]
        run2['checks'] += extra
        (rd / 'run.json').write_text(json.dumps(run2), encoding='utf-8')
        d = json.loads(json.dumps(base))
        d['metadata']['sample'].append({k: v for k, v in gone.items() if k in ('id', 'url', 'reason', 'title')} | {'template': None})
        d['metadata']['checks'] = run2['checks']
        # 2.1.2 passed on probe evidence: the keyboard probe ran on every page that was reached
        row = next(r for r in d['sc_results'] if r['sc'] == '2.1.2')
        if row['outcome'] != 'passed':
            row.update({'outcome': 'passed', 'basis': ['probe'], 'finding_ids': [], 'needs_review_ids': [],
                        'evidence_note': 'The keyboard probe left every component on every reached page.'})
            d['manual_test_plan'] = [m for m in d['manual_test_plan'] if m['sc'] != '2.1.2']
        d['headline'].update(validate_audit.compute_summary(d))
        schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
        errs = [e for e in validate_audit.schema_errors(d, schema, schema) if 'sample' in e]
        if errs:
            return [f'test setup: {errs[:2]}']
        named = json.loads(json.dumps(d))
        named['limitations'].append('p05 (gone.html) answered HTTP 500 and was not audited; no check covers it.')
        errs = validate_audit.check(named, rd, None)
        veto = [e for e in errs if '2.1.2' in e and 'keyboard probe did not run' in e]
        if veto:
            fails.append(f'blocked rows of the unreachable page p05 still veto a probe pass: {veto[0]}')
        errs = validate_audit.check(d, rd, None)
        if not any('p05' in e and 'limitations' in e for e in errs):
            fails.append('an unreachable sample page that the limitations never name is accepted')
        # a reached page whose keyboard probe errored still vetoes
        run3 = json.loads(json.dumps(run2))
        c = next(c for c in run3['checks'] if c['page'] == 'p01' and c['name'] == 'keyboard' and not c.get('state'))
        c['status'] = 'errored'
        (rd / 'run.json').write_text(json.dumps(run3), encoding='utf-8')
        named['metadata']['checks'] = run3['checks']
        errs = validate_audit.check(named, rd, None)
        if not any('2.1.2' in e and 'did not run' in e for e in errs):
            fails.append('an errored keyboard probe on a reached page no longer vetoes a probe-based pass')
    return fails


def route_pattern_checks() -> list[str]:
    """G2: root-level dynamic routes (/[slug]) share one crawl shape, from the app's route patterns or, without them,
    once many root-level siblings are seen; static routes and file names keep their own shape."""
    fails = []
    base = 'http://127.0.0.1:8191'
    try:
        shaper = run_audit.Shaper(routes=['/', '/about', '/{slug}', '/guides/{parts*}'])
    except AttributeError:
        return ['run_audit.Shaper is missing: crawl shapes cannot use route patterns']
    got = {u: shaper.shape(base + u) for u in ('/heron-lake', '/marsh-walk', '/about', '/guides/a/b', '/')}
    want = {'/heron-lake': '/{slug}', '/marsh-walk': '/{slug}', '/about': '/about', '/guides/a/b': '/guides/{parts*}', '/': '/'}
    if got != want:
        fails.append(f'route shapes: {got}')
    # no routes: 20 root-level slugs (a collection under a root dynamic segment) and one nested section
    slugs = [f'{base}/spot-{n}' for n in ('alder', 'birch', 'cedar', 'damson', 'elm', 'fir', 'gorse', 'hazel', 'ivy', 'juniper',
                                          'larch', 'maple', 'nettle', 'oak', 'pine', 'quince', 'rowan', 'sorrel', 'teasel', 'willow')]
    nested = [f'{base}/guides/owls', f'{base}/guides/waders']
    queue = [base + '/'] + slugs + nested
    shaper = run_audit.Shaper()
    for u in queue:
        shaper.observe(u)
    loaded, counts = [], {}
    while queue and len(loaded) < 6:
        url = queue.pop(run_audit.crawl_pick(queue, counts, 2, (), shape=shaper.shape))
        loaded.append(url)
        counts[shaper.shape(url)] = counts.get(shaper.shape(url), 0) + 1
    if not any(u in loaded for u in nested):
        fails.append(f'20 root-level slug pages took every load ({[u[len(base):] for u in loaded]}); the nested section was never reached')
    if shaper.shape(base + '/index.html') == shaper.shape(base + '/contact.html'):
        fails.append('root-level file names (index.html, contact.html) were folded into one shape')
    return fails


def next_routes_checks() -> list[str]:
    """G2: crawl.next_app reads the page routes of a Next.js App Router project (shared next_app.py)."""
    fails = []
    fn = getattr(run_audit, 'read_routes', None)
    if fn is None:
        return ['run_audit.read_routes is missing: crawl.next_app cannot read route patterns']
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / 'package.json').write_text('{"dependencies": {"next": "15.0.0"}}', encoding='utf-8')
        for f in ('app/page.tsx', 'app/[slug]/page.tsx', 'app/about/page.tsx', 'app/api/feed/route.ts'):
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text('export default function P() { return null }\n', encoding='utf-8')
        routes = fn(repo)
    if sorted(routes) != ['/', '/about', '/{slug}']:
        fails.append(f'read_routes gave {routes}, not the three page routes')
    return fails


def state_attribution_checks() -> list[str]:
    """G3: a state that ends on another sampled page (client-side navigation) files its results under that page,
    with where it came from; a state ending on an unsampled URL keeps its page but says where it ended."""
    fails = []
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    p1, p2 = run['sample'][0], run['sample'][1]
    n = normalize.Norm(run)
    rec = {'page': p1['id'], 'url': p1['url'], 'final_url': p2['url'] + '#top', 'viewport': '1280x800', 'state': 'nav_to_book',
           'screenshot': 'x.png'}
    n.axe(rec, {'violations': [{'id': 'image-alt', 'impact': 'critical', 'help': 'Images must have alternate text',
                                'nodes': [{'target': ['img'], 'html': '<img src="a.png">', 'el': {'xpath': '/html/body/img[1]', 'sig': 'body > img'}}]}],
                'incomplete': [], 'passes': []})
    it = n.items[-1]
    if it['page'] != p2['id'] or it['url'] != p2['url'] or (it.get('via') or {}).get('page') != p1['id']:
        fails.append(f"a state that ended on {p2['id']} filed its result under {it['page']} (via {it.get('via')})")
    rec2 = dict(rec, final_url='http://127.0.0.1:8137/elsewhere.html')
    n.axe(rec2, {'violations': [{'id': 'image-alt', 'impact': 'critical', 'help': 'x', 'nodes': [
        {'target': ['img'], 'html': '<img>', 'el': {'xpath': '/html/body/img[2]', 'sig': 'body > img'}}]}], 'incomplete': [], 'passes': []})
    it = n.items[-1]
    if it['page'] != p1['id'] or it.get('ended_on') != 'http://127.0.0.1:8137/elsewhere.html':
        fails.append(f"a state that ended on an unsampled URL lost where it ended: page {it['page']}, ended_on {it.get('ended_on')}")
    cands = merge_findings.merge(n.items, validate_audit.load_scs())
    insts = [i for c in cands for i in c['instances']]
    if not any((i.get('via') or {}).get('page') == p1['id'] and i['page'] == p2['id'] for i in insts):
        fails.append(f'merged instances lost the via of a navigated state: {[(i["page"], i.get("via")) for i in insts]}')
    schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
    inst = {'page': p2['id'], 'url': p2['url'], 'viewport': '1280x800', 'state': 'nav_to_book', 'selector': 'img',
            'html_snippet': '<img>', 'screenshot': None, 'via': {'page': p1['id'], 'state': 'nav_to_book'}}
    errs = validate_audit.schema_errors(inst, schema['$defs']['instance'], schema)
    if errs:
        fails.append(f'a report instance with via is a schema error: {errs[:2]}')
    return fails


def pattern_checks() -> list[str]:
    """G5: the default failure pattern is the one the rule implies (axe link-in-text-block: a colour-only link), and
    a rule that implies none gets no pattern but the choices, never the most severe pattern of the SC."""
    fails = []
    run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
    p1 = run['sample'][0]
    rec = {'page': p1['id'], 'url': p1['url'], 'viewport': '1280x800', 'state': 'initial', 'screenshot': None}
    n = normalize.Norm(run)
    n.axe(rec, {'violations': [{'id': 'link-in-text-block', 'impact': 'serious', 'help': 'Links must be distinguishable',
                                'nodes': [{'target': ['p > a'], 'html': '<a href="/x">x</a>', 'el': {'xpath': '/html/body/p[1]/a[1]', 'sig': 'body p > a'}}]}],
                'incomplete': [], 'passes': []})
    n.ibm(rec, {'results': [{'ruleId': 'text_block_heading', 'value': ['VIOLATION', 'POTENTIAL'], 'message': 'heading?',
                             'snippet': '<p><b>Opening hours</b></p>', 'el': {'xpath': '/html/body/p[2]', 'sig': 'body > p'}},
                            {'ruleId': 'svg_graphics_labelled', 'value': ['VIOLATION', 'FAIL'], 'message': 'svg',
                             'snippet': '<svg width="40">', 'el': {'xpath': '/html/body/svg[1]', 'sig': 'body > svg'}}], 'passes': {}})
    n.add(rec, tool='htmlcs', rule_id='Guideline1_3.1_3_1.H48', sc=['1.3.1'], kind='wcag', outcome='failed',
          el={'xpath': '/html/body/ul[1]', 'sig': 'body > ul'})
    cands = {c['sources'][0]['rule_id']: c for c in merge_findings.merge(n.items, validate_audit.load_scs())}
    want = {'link-in-text-block': '1.4.1-color-only-link', 'text_block_heading': '1.3.1-visual-heading-not-marked',
            'svg_graphics_labelled': '1.1.1-informative-image-no-alt', 'Guideline1_3.1_3_1.H48': None}
    got = {k: (cands.get(k) or {}).get('pattern', 'missing') for k in want}
    if got != want:
        fails.append(f'default patterns: {got}')
    h48 = cands.get('Guideline1_3.1_3_1.H48') or {}
    if len(h48.get('pattern_choices') or []) != 4:
        fails.append(f'a candidate with no rule pattern does not list the SC patterns to choose from: {h48.get("pattern_choices")}')
    svg = next((i for i in n.items if i['rule_id'] == 'svg_graphics_labelled'), {})
    if svg.get('act_rules'):
        fails.append(f'IBM svg_graphics_labelled on an svg with no explicit role still cites ACT {svg["act_rules"]} (outside its applicability)')
    return fails


def schema_copy_checks() -> list[str]:
    """G9: blocked_requests and events copied from run.json as recorded (objects) are not schema errors."""
    fails = []
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    schema = json.loads(validate_audit.SCHEMA.read_text(encoding='utf-8'))
    d = json.loads(json.dumps(base))
    d['metadata']['blocked_requests'] = [{'method': 'GET', 'url': 'https://analytics.example.com/c.js', 'reason': 'third_party',
                                          'at': '2026-11-15T10:00:00Z'}]
    d['metadata']['events'] = [{'url': 'http://127.0.0.1:8137/', 'event': 'DOM still changing after 5s', 'at': '2026-11-15T10:00:00Z'}]
    errs = validate_audit.schema_errors(d, schema, schema)
    if errs:
        fails.append(f'blocked_requests/events copied from run.json as objects are schema errors: {errs[:2]}')
    return fails


def small_items_checks() -> list[str]:
    """G10: render --markdown writes a file; an agent_review pass whose note rests on probe results needs the probe
    basis (so the probe-coverage check applies); run.json names blocked third parties in its limitations."""
    import subprocess
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / 'audit.md'
        out = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(EXAMPLES / 'audit.example.json'), '--markdown', str(md)],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
        text = md.read_text(encoding='utf-8') if md.exists() else ''
        if out.returncode or not text.startswith('# WCAG') or '\033' in text:
            fails.append(f'render_report.py --markdown wrote no plain Markdown file (exit {out.returncode}: {out.stderr[-200:]})')
    base = json.loads((EXAMPLES / 'audit.example.json').read_text(encoding='utf-8'))
    d = json.loads(json.dumps(base))
    row = next(r for r in d['sc_results'] if r['sc'] == '3.2.3')
    row['evidence_note'] = 'The keyboard probe reached the same navigation in the same order on every page.'
    errs = validate_audit.check(d, None, None)
    if not any('3.2.3' in e and 'probe' in e for e in errs):
        fails.append('an agent_review pass whose evidence is probe results (without the probe basis) is accepted')
    fn = getattr(run_audit, 'third_party_limitation', None)
    note = fn([{'method': 'GET', 'url': 'https://analytics.example.com/c.js', 'reason': 'third_party', 'at': 'x'},
               {'method': 'GET', 'url': 'https://ads.example.net/a.js', 'reason': 'third_party', 'at': 'x'},
               {'method': 'POST', 'url': 'http://127.0.0.1:1/form', 'reason': 'write', 'at': 'x'}]) if fn else None
    if not note or 'analytics.example.com' not in note or '2' not in note:
        fails.append(f'no run.json limitation naming the blocked third-party hosts: {note}')
    return fails


TWO_LINE_PAGE = """<!doctype html><html lang="en"><body style="font:16px sans-serif;margin:20px">
<p style="width:700px"><span style="display:inline-block;width:640px">Opening words</span><a href="/x">Be right back</a> after.</p>
<p><a href="/y">Short</a></p>
<svg width="40" height="40"><circle r="10" cx="20" cy="20"></circle></svg>
<a class="sr-only" href="#main" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Skip</a>
<div style="position:relative;display:inline-block;margin:40px"><button id="cov">Covered</button>
<div style="position:absolute;inset:0"></div></div>
<main id="main"><h1>Spots</h1></main></body></html>"""


def focus_and_hover_checks() -> tuple[list[str], str]:
    """G6: a link wrapping onto two lines is measured per line box (no weak-indicator result for a full ring) and a
    weak indicator keeps its focus screenshots; G7: hover candidates leave visually hidden elements out and an element
    the pointer cannot reach makes the probe partial, naming it; G10: an svg is described (component), probes print."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    import contextlib, io
    fails = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            return [], f'skipped ({type(exc).__name__})'
        shots = Path(tmp)
        page = browser.new_page(viewport={'width': 900, 'height': 600})
        page.set_content(TWO_LINE_PAGE)
        r = run_audit.Runner({'probes': ['keyboard', 'focus_visible', 'hover_focus']}, shots, None)
        r.inject_helpers(page)
        if not page.evaluate("() => window.__qa.describeXpath('/html[1]/body[1]/svg[1]')"):
            fails.append('an svg element cannot be described from its xpath (its component becomes "document")')
        res = r.probe_keyboard(page, shots, 'p01')
        stop = next((s for s in res['stops'] if 'Be right back' in (s.get('html') or '')), None)
        if not stop:
            fails.append('the two-line link was not reached')
        else:
            run = json.loads((EXAMPLES / 'run' / 'run.json').read_text(encoding='utf-8'))
            n = normalize.Norm(run)
            n.probes({'page': 'p01', 'url': run['sample'][0]['url'], 'viewport': '900x600', 'state': 'initial', 'screenshot': None},
                     {'keyboard': {**res, 'status': 'ran', 'stops': [stop], 'unreached': [], 'pointer_only': []}})
            weak = [i for i in n.items if i['rule_id'] == 'focus.not_visible']
            if weak:
                fails.append(f"a full focus ring round a link on two lines is reported weak: {weak[0]['message']} "
                             f"(indicator {stop.get('indicator')})")
        # a stop with no visible change keeps its focused and unfocused crops
        page.set_content('<!doctype html><html><head><style>a:focus{outline:none}</style></head><body>'
                         '<a href="/z" style="margin:30px;display:inline-block">No ring</a></body></html>')
        r.inject_helpers(page)
        res = r.probe_keyboard(page, shots, 'p02')
        ind = (res['stops'] or [{}])[0].get('indicator') or {}
        if not ind.get('screenshot') or not (shots / ind['screenshot']).exists():
            fails.append(f'a stop with no visible focus change kept no focus screenshot: {ind}')
        page.set_content(TWO_LINE_PAGE)
        r.inject_helpers(page)
        cands = page.evaluate('() => window.__qa.hoverCandidates()')
        if any('sr-only' in (c.get('html') or '') for c in cands):
            fails.append('hover candidates include a visually hidden skip link')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = r.run_probe('hover_focus', page, browser, 'http://x/', '900x600', shots, shots, 'p01__900x600')
        if res.get('status') != 'partial' or not any('cov' in json.dumps(s) or 'Covered' in json.dumps(s) for s in res.get('skipped') or []):
            fails.append(f"a hover candidate the pointer cannot reach is skipped silently: status {res.get('status')}, "
                         f"skipped {res.get('skipped')}")
        if 'hover_focus' not in err.getvalue():
            fails.append('run_probe prints no progress line naming the probe')
        page.close()
        browser.close()
    return fails, 'ran'


STATE_PAGES = {
    '/': (200, '<!doctype html><html lang="en"><body><a href="#main" class="skip">Skip to content</a>'
               '<nav><a id="go" href="/about" onclick="event.preventDefault(); history.pushState({}, \'\', \'/about\');'
               'document.querySelector(\'main\').innerHTML = \'<h1>About</h1><a href=/x>More</a>\'; document.querySelector(\'main\').focus();">About</a>'
               '<a href="/b">B</a></nav><main id="main" tabindex="-1"><h1>Home</h1><a href="/c">C</a></main></body></html>'),
    '/about': (200, '<!doctype html><html lang="en"><body><main><h1>About</h1></main></body></html>'),
}


def state_focus_checks() -> tuple[list[str], str]:
    """G1 in a browser: a listed not-found page with expect_status 404 is sampled and audited. G4/G8: a state records
    where focus is after its actions and its keyboard probe can start from the focused element."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    fails = []
    with tempfile.TemporaryDirectory() as tmp, _Site(STATE_PAGES) as site, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            return [], f'skipped ({type(exc).__name__})'
        out = Path(tmp)
        for d in ('raw', 'screenshots', 'aria'):
            (out / d).mkdir()
        cfg = {'targets': [site.base + '/', {'url': site.base + '/no-such-page', 'expect_status': 404}],
               'viewports': ['1000x700'], 'probes': ['keyboard', 'focus_visible'],
               'states': [{'url': site.base + '/', 'name': 'nav_about', 'actions': [{'click': '#go'}],
                           'probes': ['keyboard'], 'keyboard_start': 'active_element'}]}
        try:
            r = _quiet_runner(cfg, out)
            sample = r.explore(browser)
        except Exception as exc:
            browser.close()
            return [f'explore with an expect_status target failed: {type(exc).__name__}: {exc}'], 'ran'
        nf = next((s for s in sample if s['url'].endswith('/no-such-page')), {})
        if nf.get('reason') == 'unreachable' or nf.get('status') != 404:
            fails.append(f"a target listed with expect_status 404 was {nf.get('reason')} (status {nf.get('status')})")
        home = next(s for s in sample if s['url'] == site.base + '/')
        st = r.audit_state(browser, home, '1000x700', cfg['states'][0])
        rec = json.loads((out / 'raw' / f"{home['id']}__1000x700__nav_about.json").read_text(encoding='utf-8'))
        fa = rec.get('focus_after_actions') or {}
        if 'main' not in (fa.get('html') or ''):
            fails.append(f'the state did not record where focus was after its actions: {fa}')
        kb = rec['probes'].get('keyboard') or {}
        stops = kb.get('stops') or []
        if kb.get('start') != 'active_element' or len(stops) < 2 or 'More' not in (stops[1].get('html') or ''):
            fails.append(f"keyboard_start active_element did not start from the focused element: start {kb.get('start')}, "
                         f"stops {[(s.get('html') or '')[:30] for s in stops[:3]]}")
        ind0 = (stops[0].get('indicator') or {}) if stops else {}
        if stops and ind0.get('changed_px') is not None:
            fails.append(f'the start element <main tabindex="-1"> (focused by script, not in the Tab order) had its focus '
                         f'indicator measured: {ind0}')
        if not st.get('final_url', '').endswith('/about'):
            fails.append(f"the state's summary does not say where it ended: {st}")
        browser.close()
    return fails, 'ran'


def unpinned_engine_checks() -> list[str]:
    """An engine whose file SHA-256 is not pinned in the lock (vnu.jar) is never trusted from the cache's own
    verified.json: the cached copy is accepted only when the cached npm tarball still matches the lock's pinned
    integrity and holds exactly that file. No network: the tarball is built here and read with --from-dir."""
    import base64, hashlib, io, tarfile
    import fetch_engines
    fails = []
    jar = b'qa-marker fake jar\n'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        info = tarfile.TarInfo('package/build/dist/vnu.jar')
        info.size = len(jar)
        tf.addfile(info, io.BytesIO(jar))
    tgz = buf.getvalue()
    eng = {'name': 'vnu', 'version': '0.0.1', 'license': 'MIT', 'required': False, 'sha256': None,
           'tarball': 'https://registry.example.test/vnu-jar/-/vnu-jar-0.0.1.tgz',
           'tarball_integrity': 'sha512-' + base64.b64encode(hashlib.sha512(tgz).digest()).decode(),
           'member': 'package/build/dist/vnu.jar', 'file': 'vnu.jar'}
    swapped = b'qa-marker swapped jar\n'
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        ed = fetch_engines.engine_dir(cache, eng)
        ed.mkdir(parents=True)
        (ed / 'vnu.jar').write_bytes(swapped)   # a jar swapped in the cache, with a manifest that vouches for it
        (ed / 'verified.json').write_text(json.dumps({'sha256': hashlib.sha256(swapped).hexdigest()}), encoding='utf-8')
        path, why = fetch_engines.verify_cached(cache, eng)
        if path:
            fails.append("an unpinned jar was trusted because the cache's own verified.json vouched for it")
        elif 'not pinned' not in why:
            fails.append(f'the refusal should say the SHA-256 is not pinned, said {why!r}')
        (cache / 'tarballs').mkdir()
        (cache / 'tarballs' / 'vnu-jar-0.0.1.tgz').write_bytes(tgz)
        st = fetch_engines.fetch(cache, eng, from_dir=cache / 'tarballs')
        path, why = fetch_engines.verify_cached(cache, eng)
        if st.get('status') != 'ready' or not path or path.read_bytes() != jar or 'not pinned' not in (st.get('reason') or ''):
            fails.append(f'a jar from a tarball matching the pinned integrity should verify, and say its SHA-256 is not '
                         f'pinned ({st.get("status")}, {st.get("reason")!r}, {why!r})')
        (ed / 'vnu.jar').write_bytes(swapped)
        if fetch_engines.verify_cached(cache, eng)[0]:
            fails.append('a jar changed after the fetch should fail against the cached pinned tarball')
    return fails


def main() -> int:
    ok = merge_findings.self_test() == 0
    fails = unpinned_engine_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: fetch_engines.py: an unpinned jar is checked against the "
          'pinned tarball' + ''.join(f'\n  - {f}' for f in fails))
    for name, fn in (('render_report.py: --color never', render_checks),
                     ('run_audit.py: named states in the check matrix', state_matrix_checks),
                     ('validate_audit.py: critical-path priority, affects_step and untestable surfaces', rating_checks),
                     ('normalize.py/merge_findings.py: one zoom-disabled candidate', zoom_merge_checks),
                     ('run_audit.py: template grouping without landmarks', template_checks),
                     ('validate_audit.py: blocked_requests, agent basis and partial probes', validator_checks),
                     ('normalize.py: a partial keyboard probe', normalize_partial_checks),
                     ('run_audit.py: crawl order across URL patterns', crawl_order_checks),
                     ('run_audit.py: targets with expect_status', expect_status_checks),
                     ('validate_audit.py: unreachable pages and probe-based passes', blocked_veto_checks),
                     ('run_audit.py: crawl shapes for root-level dynamic routes', route_pattern_checks),
                     ('run_audit.py: route patterns from a Next.js App Router project', next_routes_checks),
                     ('normalize.py/merge_findings.py: states that navigate', state_attribution_checks),
                     ('normalize.py/merge_findings.py: default failure pattern from the rule', pattern_checks),
                     ('schema: blocked_requests and events as run.json records them', schema_copy_checks),
                     ('render --markdown, agent_review passes, third-party limitation', small_items_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = scroll_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: inner scroll panels ({how})" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = browser_probe_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: language script, blank canvas, no Tab stops, touch viewport ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    for name, fn in (('focus per line box, focus screenshots, hover skips, svg, probe progress', focus_and_hover_checks),
                     ('expect_status in a browser, focus after state actions, keyboard_start', state_focus_checks)):
        fails, how = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name} ({how})" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = keyboard_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: keyboard probe press limit and partial status ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
