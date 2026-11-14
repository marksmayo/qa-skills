#!/usr/bin/env python3
"""Self-test for the WCAG auditor's scripts: merge_findings.py --self-test (colour maths, generated ids),
template grouping for one generated page in many folders, inner scroll panels a full-page screenshot
cannot show (needs Playwright with Chromium; skipped without it), and validator cases (blocked_requests
as run.json records it, an agent finding's re-readable basis). Also: --color never, named states in the
check matrix, critical-path priority and untestable surfaces, one zoom-disabled candidate, and in
Chromium the language script census, a blank canvas behind text, a page with no Tab stops and a touch
viewport.

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


def main() -> int:
    ok = merge_findings.self_test() == 0
    for name, fn in (('render_report.py: --color never', render_checks),
                     ('run_audit.py: named states in the check matrix', state_matrix_checks),
                     ('validate_audit.py: critical-path priority, affects_step and untestable surfaces', rating_checks),
                     ('normalize.py/merge_findings.py: one zoom-disabled candidate', zoom_merge_checks),
                     ('run_audit.py: template grouping without landmarks', template_checks),
                     ('validate_audit.py: blocked_requests, agent basis and partial probes', validator_checks),
                     ('normalize.py: a partial keyboard probe', normalize_partial_checks),
                     ('run_audit.py: crawl order across URL patterns', crawl_order_checks)):
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
    fails, how = keyboard_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: keyboard probe press limit and partial status ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
