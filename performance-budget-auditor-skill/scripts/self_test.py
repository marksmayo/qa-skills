#!/usr/bin/env python3
"""Self-test for the performance budget auditor's scripts (synthetic data only).

    python self_test.py

Checks: the CPU calibration flags a machine that is steadily slower than its idle reference (a low CV
can't show a sustained load) and never claims an idle machine without a reference; INP causes name the
largest subpart (an input delay is not blamed on the click handler) with the long tasks that overlap the
interaction; an LCP image that is a CSS background with no preload is named as such; and the LCP element
probe records a CSS background and whether a preload exists (needs Playwright with Chromium; skipped
without it). Writes only to a temporary directory. Exit 0 when every check passes.
"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import attribute  # noqa: E402
import measure  # noqa: E402

MACHINE = {'cpu': 'Synthetic CPU family 6', 'cores': 8, 'os': 'TestOS 1'}


def calibration_checks() -> list[str]:
    fails = []
    assess = getattr(measure, 'assess_calibration', None)
    if not assess:
        cal = [376.0, 380.2, 372.9]  # a steady 2.6x slowdown against an idle 145 ms: CV is tiny
        busy = measure.cv(cal) > 0.1
        return [f'a calibration of {cal} ms on a machine whose idle median is 145 ms is recorded busy={busy}: '
                'a sustained load gives a low CV and nothing compares the median with a reference']
    c = assess([376.0, 380.2, 372.9], reference_ms=145.0)
    if not c['busy'] or not c.get('slowdown') or c['slowdown'] < 2.5:
        fails.append(f'a median 2.6x the idle reference is not flagged busy: {c}')
    c = assess([150.0, 149.0, 151.0], end_runs_ms=[380.0, 377.0, 384.0], reference_ms=None)
    if not c['busy']:
        fails.append(f'a calibration 2.5x slower at the end of the run than at the start is not flagged busy: {c}')
    c = assess([150.0, 149.0, 151.0], reference_ms=None)
    if c['busy'] or 'idle' in (c.get('note') or '').lower().replace('no idle reference', ''):
        fails.append(f'without a reference the calibration must not claim an idle machine: {c.get("note")}')
    if 'no idle reference' not in (c.get('note') or ''):
        fails.append(f'without a reference the note must say a steady load cannot be seen: {c.get("note")}')
    c = assess([150.0, 149.0, 151.0], reference_ms=145.0)
    if c['busy']:
        fails.append(f'a median within 1.5x of the reference is flagged busy: {c}')
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        if measure.calibration_reference(MACHINE, cache) is not None:
            fails.append('an empty cache has a calibration reference')
        measure.update_calibration_reference(MACHINE, cache, 145.0)
        measure.update_calibration_reference(MACHINE, cache, 376.0)   # a slower, loaded run never raises it
        ref = measure.calibration_reference(MACHINE, cache)
        if not ref or ref.get('median_ms') != 145.0:
            fails.append(f'the reference is not the fastest calibration seen on this machine: {ref}')
        if measure.calibration_reference({**MACHINE, 'cores': 4}, cache) is not None:
            fails.append('a reference from another machine is used')
    return fails


def inp_run(input_delay, processing, presentation, long_tasks, script=None):
    a = {'interactionTarget': '#menu-btn', 'interactionType': 'pointer', 'interactionTime': 2000.0,
         'inputDelay': input_delay, 'processingDuration': processing, 'presentationDelay': presentation,
         'totalStyleAndLayoutDuration': 300.0}
    if script:
        a['longestScript'] = script
    return {'attribution': {'INP': a, 'long_tasks': long_tasks, 'loaf_scripts': []}}


def inp_checks() -> list[str]:
    fails = []
    handler = {'sourceURL': 'http://127.0.0.1:9/app.js', 'invoker': 'DOMWindow.onclick', 'duration': 9.6,
               'subpart': 'processing-duration'}
    runs = [inp_run(348.9, 9.6, 34.4, [{'start': 1700, 'duration': 420}, {'start': 400, 'duration': 80}], handler)
            for _ in range(3)]
    cause, key, attr = attribute.attribute('INP', runs, None)
    if 'input delay' not in cause or 'processing in DOMWindow.onclick' in cause:
        fails.append(f'INP with 349 ms input delay and 9.6 ms processing is blamed on the handler: "{cause}"')
    if attr.get('dominant_subpart') != 'input_delay':
        fails.append(f'the largest INP subpart is not recorded: {attr.get("dominant_subpart")}')
    if [t['start'] for t in attr.get('overlapping_long_tasks') or []] != [1700]:
        fails.append(f'the long task overlapping the interaction is not named: {attr.get("overlapping_long_tasks")}')
    if key != 'interaction:#menu-btn':
        fails.append(f'the INP cause key changed: {key}')
    runs = [inp_run(0.6, 3188.2, 3.2, [], dict(handler, duration=3183.9, invoker='BUTTON#add.onclick')) for _ in range(3)]
    cause, _, _ = attribute.attribute('INP', runs, None)
    if cause != 'pointer on #menu-btn: 3188.2 ms processing in BUTTON#add.onclick':
        fails.append(f'a processing-bound INP cause changed: "{cause}"')
    return fails


def lcp_run(element):
    return {'attribution': {'LCP': {'target': 'section.hero', 'url': 'http://127.0.0.1:9/static/hero.webp',
                                    'resourceTransferSize': 92000, 'timeToFirstByte': 40, 'resourceLoadDelay': 1070,
                                    'resourceLoadDuration': 300, 'elementRenderDelay': 60, 'element': element},
                            'render_blocking': []}}


def lcp_checks() -> list[str]:
    fails = []
    bg = {'tag': 'section', 'background_image': 'http://127.0.0.1:9/static/hero.webp', 'preloaded': False}
    cause, key, attr = attribute.attribute('LCP', [lcp_run(bg) for _ in range(3)], None)
    if 'CSS background' not in cause or 'not preloaded' not in cause:
        fails.append(f'an LCP image that is a CSS background with no preload is not named as one: "{cause}"')
    if attr.get('css_background') is not True or attr.get('preloaded') is not False:
        fails.append(f'the LCP attribution does not record css_background / preloaded: {attr.get("css_background")}, {attr.get("preloaded")}')
    if key != 'http://127.0.0.1:9/static/hero.webp':
        fails.append(f'the LCP cause key changed: {key}')
    img = {'tag': 'img', 'natural': [800, 400], 'rendered': [800, 400]}
    cause, _, _ = attribute.attribute('LCP', [lcp_run(img) for _ in range(3)], None)
    if cause != 'LCP image /static/hero.webp (90 KB)':
        fails.append(f'an <img> LCP cause changed: "{cause}"')
    return fails


LCP_PAGE = """<!doctype html><html><head>{preload}<style>
#hero {{ width: 600px; height: 300px; background-image: url('/static/hero.webp') }}</style></head>
<body><section id="hero"></section></body></html>"""


def lcp_element_checks() -> tuple[list[str], str]:
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
        for preload, want in (('', False), ('<link rel="preload" as="image" href="/static/hero.webp">', True)):
            page = browser.new_page()   # served from a route: nothing listens on the port
            body = LCP_PAGE.format(preload=preload)
            page.route('**/*', lambda r: r.fulfill(status=200, content_type='text/html', body=body)
                       if r.request.resource_type == 'document' else r.fulfill(status=404, body=''))
            page.goto('http://127.0.0.1:9/')
            el = page.evaluate(measure.LCP_ELEMENT, '#hero') or {}
            page.close()
            if not (el.get('background_image') or '').endswith('/static/hero.webp') or el.get('preloaded') is not want:
                fails.append(f'LCP element probe on a CSS background {"with" if want else "without"} a preload: '
                             f'background_image {el.get("background_image")}, preloaded {el.get("preloaded")}')
        browser.close()
    return fails, 'ran'


SKILL = HERE.parent
EXAMPLES = SKILL / 'examples'


def synthetic_doc(lcp: list[float], warm: bool = False, headers: bool = True, interaction_bytes: int | None = None) -> dict:
    """A measurements document of one page measured on desktop: runs with the given LCP values; resources with the
    response headers a static server sends without compression or Cache-Control; optionally a warm mode whose second
    load transferred everything again, and the bytes an interaction fetched."""
    url = 'http://127.0.0.1:9/'
    def res(path, typ, n, enc=None):
        r = {'url': url + path, 'type': typ, 'bytes': n, 'from_cache': False, 'third_party': False}
        if headers:
            r['headers'] = {'content-encoding': enc, 'cache-control': None, 'validator': True}
        return r
    runs = []
    for cache in (['cold', 'warm'] if warm else ['cold']):
        for i, v in enumerate(lcp, 1):
            r = {'id': f'home/desktop/{cache}/chromium/r{i}', 'url': url, 'template': 'home', 'profile': 'desktop', 'cache': cache,
                 'browser': 'chromium', 'kind': 'measured', 'ok': True, 'status': 200, 'error': None,
                 'metrics': {'LCP': v, 'CLS': 0.0, 'INP': None, 'TBT': 0.0, 'FCP': 300.0, 'TTFB': 20.0,
                             'transfer_total': 160000, 'transfer_script': 120000},
                 'metric_sources': {}, 'transfer_by_type': {'script': 120000, 'document': 40000},
                 'attribution': {'LCP': {'target': '#cover'}, 'render_blocking': []},
                 'resources': [res('app.js', 'script', 120000), res('', 'document', 40000)]}
            if warm and cache == 'warm':
                r['cache_check'] = {'resources': 2, 'from_cache': 0, 'revalidated': 0, 'effective': False,
                                    'reason': 'no resource came from the HTTP cache on the second load'}
            if interaction_bytes is not None:
                r['metrics']['transfer_interaction'] = interaction_bytes
                r['interaction_resources'] = [{'url': url + 'intro.mp4', 'type': 'other', 'bytes': interaction_bytes - 560000},
                                              {'url': url + 'modules/runtime.js', 'type': 'script', 'bytes': 560000}]
            runs.append(r)
    return {'schema': 1, 'machine': MACHINE, 'calibration': {'runs_ms': [150.0, 151.0, 149.0], 'median_ms': 150.0, 'cv': 0.01,
                                                              'busy': False, 'note': 'no sign of load'},
            'config': {'runs': len(lcp), 'min_runs': len(lcp), 'max_runs': len(lcp), 'cache': ['cold']},
            'sample': [{'url': url, 'template': 'home', 'reason': 'entry', 'critical_path': False, 'interaction': True}],
            'runs': runs, 'events': [], 'blocked_requests': []}


def round8_checks() -> list[str]:
    import json, subprocess
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        bpath = Path(tmp) / 'budget.json'
        bpath.write_text(json.dumps({'profiles': {'desktop': {'LCP': 2500, 'transfer_total': 1000000,
                                                              'transfer_interaction': 1000000}}}), encoding='utf-8')
        budget = attribute.Budget(bpath)
        # 1. warm runs that were not warm: the cache check says so, and the measurements carry it
        doc = synthetic_doc([500.0] * 5, warm=True)
        wc = getattr(attribute, 'warm_cache', lambda d: [])(doc)
        if not wc or wc[0].get('effective') is not False:
            fails.append(f'warm runs whose second load transferred everything again are not reported as ineffective: {wc}')
        check = getattr(measure, 'cache_check', None)
        got = check([{'url': 'u', 'from_cache': False, 'status': 200}], 'warm', {'http_cache': 'disabled'}) if check else None
        if not got or got.get('effective') is not False or 'disabled' not in str(got.get('reason')):
            fails.append(f'a harness that disabled the HTTP cache does not make the warm runs ineffective: {got}')
        got = check([{'url': 'u', 'from_cache': True, 'status': 200}, {'url': 'v', 'from_cache': False, 'status': 304}],
                    'warm', {}) if check else None
        if not got or got.get('effective') is not True:
            fails.append(f'a warm load served from the cache is not effective: {got}')
        # 2. compression and caching headers
        hf = getattr(measure, 'header_fields', None)
        h = hf({'Content-Type': 'text/javascript', 'ETag': '"abc"', 'Content-Length': '33841'}) if hf else None
        if not h or h.get('content-encoding') is not None or h.get('cache-control') is not None or h.get('validator') is not True:
            fails.append(f'the response headers of a resource (encoding, cache-control, validator) are not recorded: {h}')
        dl = getattr(attribute, 'delivery', lambda d: [])(doc)
        if not dl or not any(u['url'].endswith('app.js') for u in dl[0].get('uncompressed') or []) or \
                not dl[0].get('no_cache_lifetime'):
            fails.append(f'an uncompressed 120 KB script and resources with no cache lifetime are not reported: {dl}')
        # 3. what an interaction downloads is budgeted
        doc = synthetic_doc([500.0] * 5, interaction_bytes=8560000)
        meas = attribute.measurements(doc, budget)
        ti = [m for m in meas if m['metric'] == 'transfer_interaction']
        if not ti or ti[0]['status'] != 'over':
            fails.append(f'8.5 MB fetched by the interaction is not measured against a transfer_interaction budget: {ti}')
        cands = attribute.candidates(doc, meas)
        if not any(c['metric'] == 'transfer_interaction' and 'intro.mp4' in c['cause'] for c in cands):
            fails.append(f'the interaction download has no candidate naming its largest resource: '
                         f'{[(c["metric"], c["cause"]) for c in cands]}')
        old = attribute.measurements(synthetic_doc([500.0] * 5), budget)
        if any(m['metric'] == 'transfer_interaction' for m in old):
            fails.append('runs that never recorded transfer_interaction gained a measurement of it (older examples change)')
        # 4. an unstable metric under its budget still gets a needs-review candidate naming the outliers
        doc = synthetic_doc([487.0, 2074.0, 600.0, 1900.0, 700.0, 2000.0, 520.0])
        meas = attribute.measurements(doc, budget)
        cands = attribute.candidates(doc, meas)
        un = [c for c in cands if c['metric'] == 'LCP']
        if not un or 'unstable' not in un[0]['cause'] or 'r2' not in un[0]['cause'] or un[0]['confidence'] != 'low':
            fails.append(f'an unstable LCP (487-2074 ms) under its budget has no low-confidence candidate naming the slow '
                         f'runs: {[(c["metric"], c["cause"], c["confidence"]) for c in un]}')
        # 5. --write-summary fills a draft headline that leaves out the computed fields
        rep = json.loads((EXAMPLES / 'perf_report.example.json').read_text(encoding='utf-8'))
        for k in ('over_budget_counts', 'unstable_metrics', 'evidence_completeness', 'overall_confidence'):
            rep['headline'].pop(k, None)
        draft = Path(tmp) / 'draft.json'
        draft.write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(draft), '--measurements',
                            str(EXAMPLES / 'run' / 'measurements.json'), '--candidates', str(EXAMPLES / 'run' / 'candidates.json'),
                            '--budget', str(EXAMPLES / 'budget.json'), '--regressions', str(EXAMPLES / 'run' / 'regressions.json'),
                            '--write-summary'], capture_output=True, text=True, encoding='utf-8')
        if r.returncode != 0:
            fails.append(f'--write-summary cannot fill a draft headline without the computed fields: {r.stdout.strip()[-200:]!r}')
        # 6. render --brief keeps the limitations
        rep = json.loads((EXAMPLES / 'perf_report.example.json').read_text(encoding='utf-8'))
        rep['limitations'] = rep['limitations'] + ['Warm runs were not warm: the HTTP cache was off.']
        (Path(tmp) / 'r.json').write_text(json.dumps(rep), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(HERE / 'render_report.py'), str(Path(tmp) / 'r.json'), '--brief',
                            '--color', 'never'], capture_output=True, text=True, encoding='utf-8')
        if 'Warm runs were not warm' not in r.stdout:
            fails.append('render_report.py --brief omits the limitations')
    # 7. the copy-through fields the validator demands are named in SKILL.md
    skill = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
    for field in ('metadata.harness', 'warm_cache', 'delivery'):
        if field not in skill:
            fails.append(f'SKILL.md does not name `{field}`, which the validator requires in the report')
    # 8. the idle reference is stored only from a calm calibration, and an append keeps each pass's calibration
    store = getattr(measure, 'should_store_reference', None)
    if not store:
        fails.append('there is no rule for when a calibration may become the idle reference (any non-busy run seeds it)')
    else:
        jittery = measure.assess_calibration([240.0, 262.0, 245.0], end_runs_ms=[250.0, 246.0, 251.0])
        calm = measure.assess_calibration([145.0, 145.6, 144.9], end_runs_ms=[145.2, 145.0, 145.9])
        if store(jittery, 300.0)[0] or not store(calm, 160.0)[0]:
            fails.append(f'reference storing: CV {jittery["cv"]} stored {store(jittery, 300.0)}, CV {calm["cv"]} stored '
                         f'{store(calm, 160.0)}')
        # a steady load gives a low CV too: the first reference is never seeded without an explicit request
        steady = measure.assess_calibration([245.0, 245.5, 245.2], end_runs_ms=[245.4, 245.1, 245.6])
        if store(steady, None)[0] or not store(steady, None, forced=True)[0]:
            fails.append(f'the first idle reference is seeded without --record-reference: {store(steady, None)}')
    comb = getattr(measure, 'combine_passes', None)
    if not comb:
        fails.append('an --append pass overwrites the first pass\'s calibration (no combine_passes)')
    else:
        first = measure.assess_calibration([145.0, 145.5, 145.2], end_runs_ms=[146.0, 145.0, 145.5], reference_ms=145.0)
        second = measure.assess_calibration([245.0, 250.0, 246.0], end_runs_ms=[251.0, 249.0, 250.0], reference_ms=145.0)
        c = comb(first, second)
        if not c.get('busy') or len(c.get('passes') or []) != 2 or c['passes'][0].get('end_median_ms') != 145.5:
            fails.append(f'combined calibration does not keep both passes (first idle, second busy): {c}')
    return fails


def main() -> int:
    ok = True
    for name, fn in (('measure.py: CPU calibration against an idle reference', calibration_checks),
                     ('measure/attribute/validate: warm cache, headers, interaction transfer, unstable, summary, brief',
                      round8_checks),
                     ('attribute.py: INP cause by largest subpart', inp_checks),
                     ('attribute.py: LCP CSS background without preload', lcp_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = lcp_element_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: measure.py: LCP element background and preload ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
