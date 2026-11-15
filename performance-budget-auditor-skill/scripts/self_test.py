#!/usr/bin/env python3
"""Self-test for the performance budget auditor's scripts (synthetic data only).

    python self_test.py

Checks: the CPU calibration flags a machine that is steadily slower than its idle reference (a low CV
can't show a sustained load) and never claims an idle machine without a reference; INP causes name the
largest subpart (an input delay is not blamed on the click handler) with the long tasks that overlap the
interaction; an LCP image that is a CSS background with no preload is named as such; and the LCP element
probe records a CSS background and whether a preload exists (needs Playwright with Chromium; skipped
without it). Round 10: the plan's http_cache, service-worker responses and traffic, s-maxage, the mobile
pixel ratio, image budgets and oversized images, prefetches in weight causes, a loaded calibration,
--no-reference-update, --only matching nothing, the report seeder, near-tied TBT scripts, overruns within the
machine's slowdown and the limitations the validator requires; and one real warm load in Chromium through
measure.Runner against a local page whose service worker answers every request (on a port in 8670-8679).
Writes only to a temporary directory; never touches the stored calibration reference. Exit 0 when every check
passes.
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


HARNESS_DISABLED = {'mode': 'disabled', 'interception': 'Playwright context.route',
                    'reason': 'requests are guarded with Playwright routing (context.route), which disables the HTTP cache'}


def tbt_run(run_id, url, template, scripts: dict, tbt=420.0):
    return {'id': run_id, 'url': url, 'template': template, 'profile': 'mobile-mid', 'cache': 'cold', 'browser': 'chromium',
            'kind': 'measured', 'ok': True, 'status': 200, 'error': None,
            'metrics': {'LCP': 900.0, 'CLS': 0.0, 'INP': None, 'TBT': tbt, 'FCP': 300.0, 'TTFB': 20.0,
                        'transfer_total': 300000, 'transfer_script': 200000},
            'metric_sources': {}, 'transfer_by_type': {'script': 200000, 'document': 30000, 'image': 70000},
            'attribution': {'loaf_scripts': [{'url': u, 'duration': d} for u, d in scripts.items()], 'long_tasks': [],
                            'render_blocking': []},
            'resources': []}


def tbt_doc(slowdown=None, tbt=420.0) -> dict:
    """Two templates whose TBT comes from one hydration task in two framework chunks that nearly tie on one page."""
    a, b = 'http://127.0.0.1:9/_next/static/chunks/255-aaa.js', 'http://127.0.0.1:9/_next/static/chunks/4bd1-bbb.js'
    runs = []
    for i in range(1, 6):
        runs.append(tbt_run(f'home/mobile-mid/cold/chromium/r{i}', 'http://127.0.0.1:9/', 'home', {a: 407.0, b: 395.0}, tbt))
        runs.append(tbt_run(f'trail/mobile-mid/cold/chromium/r{i}', 'http://127.0.0.1:9/trail', 'trail', {b: 354.0, a: 90.0}, tbt))
    cal = {'runs_ms': [150.0, 151.0, 149.0], 'median_ms': 150.0, 'cv': 0.01, 'busy': False, 'note': 'x'}
    if slowdown:
        cal.update(reference_ms=round(150.0 / slowdown, 1), slowdown=slowdown)
    return {'schema': 1, 'machine': MACHINE, 'calibration': cal,
            'config': {'runs': 5, 'min_runs': 5, 'max_runs': 5, 'cache': ['cold'],
                       'profiles': {'mobile-mid': {'viewport': '412x915', 'mobile': True, 'device_scale_factor': 2.625}}},
            'sample': [{'url': 'http://127.0.0.1:9/', 'template': 'home', 'reason': 'entry', 'critical_path': False, 'interaction': False},
                       {'url': 'http://127.0.0.1:9/trail', 'template': 'trail', 'reason': 'template', 'critical_path': False,
                        'interaction': False}],
            'runs': runs, 'events': [], 'blocked_requests': []}


def round10_checks() -> list[str]:
    """Gaps found measuring a Next.js App Router site with a service worker (round 10)."""
    import json, os, subprocess
    fails = []
    tmp_dir = tempfile.TemporaryDirectory()
    tmp = Path(tmp_dir.name)
    # 1. the plan sets http_cache; a warm Chromium run keeps the cache by default; route with warm runs is refused
    hc = getattr(measure, 'harness_config', None)
    if not hc:
        fails.append('1: measure.py has no harness_config: the plan\'s http_cache never reaches the harness')
    else:
        cfg, err = hc({'targets': [{'url': 'http://127.0.0.1:9/'}]}, 'chromium', ['cold', 'warm'])
        if err or cfg.get('http_cache') != 'keep':
            fails.append(f'1: a Chromium plan with warm runs does not keep the HTTP cache by default: {cfg.get("http_cache")} {err}')
        cfg, err = hc({'targets': [{'url': 'http://127.0.0.1:9/'}], 'http_cache': 'keep'}, 'chromium', ['cold'])
        if err or cfg.get('http_cache') != 'keep':
            fails.append(f'1: "http_cache": "keep" in the plan is not passed to the harness: {cfg.get("http_cache")} {err}')
        cfg, err = hc({'targets': [{'url': 'http://127.0.0.1:9/'}], 'http_cache': 'route'}, 'chromium', ['warm'])
        if not err:
            fails.append('1: warm runs with "http_cache": "route" (cache off) are not refused')
    # 2. a response a service worker answered is not an HTTP-cache hit
    rf = getattr(measure, 'response_flags', None)
    flags = rf({'fromServiceWorker': True, 'fromDiskCache': True}) if rf else {}
    if not rf or flags.get('from_cache') or not flags.get('from_service_worker'):
        fails.append(f'2: a response served by a service worker is counted as from the HTTP cache: {flags}')
    got = measure.cache_check([{'url': 'u', 'from_cache': False, 'from_service_worker': True, 'status': 200},
                               {'url': 'v', 'from_cache': False, 'from_service_worker': True, 'status': 200}], 'warm', {})
    if got.get('effective') is not False or got.get('from_service_worker') != 2:
        fails.append(f'2: a warm load answered by a service worker that fetched from the network is called effective: {got}')
    # 3. the harness records http_cache as a dict ({mode: disabled}): the check must read its mode
    got = measure.cache_check([{'url': 'u', 'from_cache': True, 'status': 200}], 'warm', {'http_cache': HARNESS_DISABLED})
    if got.get('effective') is not False:
        fails.append(f'3: the harness\'s http_cache {{mode: disabled}} does not make the warm run ineffective: {got}')
    doc = synthetic_doc([500.0] * 5, warm=True)
    for r in doc['runs']:
        r.pop('cache_check', None)
        r['resources'][0]['from_cache'] = True
    doc['harness'] = {'http_cache': HARNESS_DISABLED, 'bypass_csp': False, 'requests': {}}
    wc = attribute.warm_cache(doc)
    if not wc or wc[0]['effective'] is not False:
        fails.append(f'3: attribute.warm_cache calls warm runs effective under a disabled HTTP cache (dict shape): {wc}')
    # 4. what a service worker fetches on a warm load is measured (transfer_sw) and summarised
    sn = getattr(measure, 'sw_network', None)
    net = sn([{'url': 'a', 'body': 253, 'headers': 187}, {'url': 'b', 'body': -185, 'headers': 185}]) if sn else {}
    if not sn or net.get('requests') != 2 or net.get('bytes') != 440 or net.get('from_cache') != 1:
        fails.append(f'4: a service worker\'s own network requests are not counted (bytes, cache hits): {net}')
    doc = synthetic_doc([500.0] * 5, warm=True)
    for r in doc['runs']:
        if r['cache'] == 'warm':
            r['metrics']['transfer_sw'] = 60000
            r['service_worker'] = {'controlled': True, 'responses': 2, 'workers': ['http://127.0.0.1:9/sw.js'],
                                   'network': {'requests': 2, 'bytes': 60000, 'from_cache': 0}, 'network_measured': True}
    sws = getattr(attribute, 'service_workers', lambda d: [])(doc)
    if not sws or not sws[0].get('runs_controlled') or sws[0].get('cache') != 'warm':
        fails.append(f'4: warm runs a service worker answered are not summarised: {sws}')
    meas = attribute.measurements(doc, attribute.Budget(None))
    if not any(m['metric'] == 'transfer_sw' and m['cache'] == 'warm' and m['median'] == 60000 for m in meas):
        fails.append('4: transfer_sw (what the service worker downloaded) is not a measurement of the warm runs')
    # 5. s-maxage is for shared caches only: it is not a browser cache lifetime
    doc = synthetic_doc([500.0] * 5)
    for r in doc['runs']:
        for x in r['resources']:
            x['headers']['cache-control'] = 'public, s-maxage=86400, stale-while-revalidate=43200'
    dl = attribute.delivery(doc)
    if not dl or not any(u.endswith('app.js') for u in dl[0]['no_cache_lifetime']):
        fails.append(f'5: a script with only s-maxage is read as having a browser cache lifetime: {dl and dl[0]["no_cache_lifetime"]}')
    # 6. the mobile profile has a device pixel ratio
    profiles = json.loads((SKILL / 'data' / 'profiles.json').read_text(encoding='utf-8'))
    if not (profiles['mobile-mid'].get('device_scale_factor') or 1) > 2:
        fails.append(f'6: mobile-mid has no device pixel ratio (Chromium then uses 1): {profiles["mobile-mid"]}')
    # 7. image (and font, stylesheet, document) weight can be budgeted, in both budget formats
    lh = tmp / 'lh.json'
    lh.write_text(json.dumps([{'path': '/*', 'resourceSizes': [{'resourceType': 'image', 'budget': 50},
                                                               {'resourceType': 'media', 'budget': 10}]}]), encoding='utf-8')
    b = attribute.Budget(lh)
    if b.for_('http://127.0.0.1:9/', 'mobile-mid').get('transfer_image') != 50 * 1024:
        fails.append(f'7: a Lighthouse image resourceSizes budget is ignored: {b.for_("http://127.0.0.1:9/", "mobile-mid")}')
    if 'media' not in str(getattr(b, 'ignored', '')):
        fails.append('7: a Lighthouse resourceSizes type the skill cannot budget (media) is dropped silently')
    meas = attribute.measurements(tbt_doc(), b)
    img = [m for m in meas if m['metric'] == 'transfer_image']
    if not img or img[0]['status'] != 'over':
        fails.append(f'7: 70 KB of images against a 50 KB image budget is not over: {img[:1]}')
    # 8. images delivered far larger than their slot (natural width vs rendered width x DPR)
    doc = synthetic_doc([500.0] * 5)
    for r in doc['runs']:
        r['images'] = [{'url': 'http://127.0.0.1:9/thumb-640.avif', 'natural': [640, 360], 'rendered': [128, 72], 'dpr': 1},
                       {'url': 'http://127.0.0.1:9/hero.avif', 'natural': [1080, 600], 'rendered': [412, 229], 'dpr': 2.625}]
    dl = attribute.delivery(doc)
    over = (dl[0].get('oversized_images') or []) if dl else []
    if [o['url'].rsplit('/', 1)[-1] for o in over] != ['thumb-640.avif']:
        fails.append(f'8: an image 5x wider than its slot is not reported as oversized (or a right-sized one is): {over}')
    # 9. framework prefetches in a weight overrun are named
    doc = tbt_doc()
    for r in doc['runs']:
        r['resources'] = [{'url': 'http://127.0.0.1:9/hero.jpg', 'type': 'image', 'bytes': 120000, 'from_cache': False}]
        r['request_summary'] = {'classes': {'rsc_prefetch': {'requests': 9, 'bytes': 90000}}, 'repeated': []}
    cause, _, _ = attribute.attribute('transfer_total', [r for r in doc['runs'] if r['template'] == 'home'], None)
    if 'prefetch' not in cause:
        fails.append(f'9: 90 KB of framework prefetches in a 300 KB page weight overrun are not named: "{cause}"')
    # 10. a calibration between 1.2x and 1.5x the idle reference is "loaded", not "no sign of load"
    c = measure.assess_calibration([223.5, 224.0, 223.0], reference_ms=150.0)
    if c.get('busy') or c.get('state') != 'loaded' or 'no sign of load' in c.get('note', ''):
        fails.append(f'10: a calibration 1.49x the idle reference is not graded loaded: {c.get("state")}, {c.get("note")}')
    # 11. a run can be kept from touching the stored idle reference
    calm = measure.assess_calibration([145.0, 145.6, 144.9], end_runs_ms=[145.2, 145.0, 145.9])
    try:
        stored = measure.should_store_reference(calm, 160.0, no_update=True)
    except TypeError:
        stored = (True, 'no no_update parameter')
    if stored[0]:
        fails.append(f'11: a calm run lowers the stored idle reference even when asked not to: {stored}')
    r = subprocess.run([sys.executable, '-B', str(HERE / 'measure.py'), '--help'], capture_output=True, text=True, encoding='utf-8')
    if '--no-reference-update' not in r.stdout:
        fails.append('11: measure.py has no --no-reference-update flag')
    # 12. --only that matches no target exits non-zero and names the filter
    plan = tmp / 'plan.json'
    plan.write_text(json.dumps({'targets': [{'url': 'http://127.0.0.1:9/about', 'template': 'about', 'reason': 'x'}]}), encoding='utf-8')
    env = dict(os.environ, PERF_ASSET_CACHE=str(tmp / 'cache'))   # never the real cache (its calibration reference)
    r = subprocess.run([sys.executable, '-B', str(HERE / 'measure.py'), str(plan), '--out', str(tmp / 'm.json'),
                        '--only', 'C:/Program Files/Git/about'], capture_output=True, text=True, encoding='utf-8', env=env)
    if r.returncode != 2 or 'C:/Program Files/Git/about' not in r.stderr:
        fails.append(f'12: --only matching no target exits {r.returncode}: {r.stderr.strip()[-160:]!r}')
    # 13. a seeder builds the report from the run, the candidates and the agent's triage
    seeder = HERE / 'build_report.py'
    if not seeder.is_file():
        fails.append('13: there is no report seeder (build_report.py)')
    else:
        ex = json.loads((EXAMPLES / 'perf_report.example.json').read_text(encoding='utf-8'))
        fids = {f['candidate_id']: f for f in ex['findings']}
        triage = {'summary': ex['headline']['summary'], 'limitations': ex['limitations'], 'next_steps': ex['next_steps'],
                  'tools': [t for t in ex['metadata']['tools'] if t['name'].startswith('lighthouse')],
                  'decisions': {t['candidate_id']: {'decision': t['decision'], 'reason': t['reason'],
                                                    **({k: fids[t['candidate_id']][k] for k in ('title', 'remediation', 'verification_step')}
                                                       if t['candidate_id'] in fids else {})} for t in ex['triage']}}
        (tmp / 'triage.json').write_text(json.dumps(triage), encoding='utf-8')
        out = tmp / 'seeded.json'
        r = subprocess.run([sys.executable, '-B', str(seeder), '--measurements', str(EXAMPLES / 'run' / 'measurements.json'),
                            '--candidates', str(EXAMPLES / 'run' / 'candidates.json'), '--triage', str(tmp / 'triage.json'),
                            '--regressions', str(EXAMPLES / 'run' / 'regressions.json'), '--out', str(out)],
                           capture_output=True, text=True, encoding='utf-8')
        v = subprocess.run([sys.executable, '-B', str(HERE / 'validate_report.py'), str(out), '--measurements',
                            str(EXAMPLES / 'run' / 'measurements.json'), '--candidates', str(EXAMPLES / 'run' / 'candidates.json'),
                            '--budget', str(EXAMPLES / 'budget.json'), '--regressions', str(EXAMPLES / 'run' / 'regressions.json')],
                           capture_output=True, text=True, encoding='utf-8') if r.returncode == 0 else r
        if r.returncode != 0 or v.returncode != 0:
            fails.append(f'13: the seeded report is not valid: {(r.stderr + v.stdout).strip()[-300:]!r}')
    # 14. the example is the current skill version
    ver = next(l.split(':', 1)[1].strip() for l in (SKILL / 'SKILL.md').read_text(encoding='utf-8').splitlines() if l.startswith('version:'))
    exv = json.loads((EXAMPLES / 'perf_report.example.json').read_text(encoding='utf-8'))['skill']['version']
    if exv != ver:
        fails.append(f'14: the example report says skill version {exv}, SKILL.md is {ver}')
    # 15. two framework chunks that nearly tie for one hydration task are one cause, site-wide
    doc = tbt_doc()
    meas = attribute.measurements(doc, attribute.Budget(None))
    tbt = [c for c in attribute.candidates(doc, meas) if c['metric'] == 'TBT']
    if len(tbt) != 1 or not tbt[0]['site_wide'] or len(tbt[0]['pages']) != 2:
        fails.append(f'15: one hydration task split into {len(tbt)} TBT candidates by a near tie: '
                     f'{[(c["cause_key"], c["pages"]) for c in tbt]}')
    # 16. an overrun smaller than the machine's own slowdown is low confidence (needs_review)
    doc = tbt_doc(slowdown=1.49, tbt=357.0)   # 1.19x the 300 ms budget on a machine 1.49x slower than idle
    meas = attribute.measurements(doc, attribute.Budget(None))
    tbt = [c for c in attribute.candidates(doc, meas) if c['metric'] == 'TBT']
    if not tbt or any(c['confidence'] != 'low' for c in tbt):
        fails.append(f'16: a TBT 1.19x the budget on a machine 1.49x slower than idle is not low confidence: '
                     f'{[c["confidence"] for c in tbt]}')
    doc = tbt_doc(slowdown=1.49, tbt=900.0)
    meas = attribute.measurements(doc, attribute.Budget(None))
    if any(c['confidence'] == 'low' for c in attribute.candidates(doc, meas) if c['metric'] == 'TBT'):
        fails.append('16: a TBT 3x the budget is made low confidence by a 1.49x slowdown')
    # 17. the validator demands the limitations these runs need
    import validate_report as vr
    rules = getattr(vr, 'limitation_rules', None)
    if not rules:
        fails.append('17: validate_report.py has no limitation_rules')
    else:
        doc = tbt_doc()
        doc['calibration'].update(reference_ms=150.0, slowdown=1.3)
        doc['runs'][0]['cache'] = 'warm'
        doc['runs'][0]['service_worker'] = {'controlled': True, 'responses': 3, 'workers': ['http://127.0.0.1:9/sw.js'],
                                            'network': {'requests': 0, 'bytes': 0, 'from_cache': 0}, 'network_measured': False}
        for r in doc['runs']:
            r['throttling'] = {'applied': {'viewport': '412x915'}, 'not_applied': []}
        rep = {'scope': {'targets': [{'url': s['url']} for s in doc['sample']]}, 'limitations': ['Lab data only.'],
               'metadata': {'tools': []}}
        meas = attribute.measurements(doc, attribute.Budget(None))
        need = {x['key'] for x in rules(rep, doc, meas) if x['needed'] and not x['satisfied']}
        for k in ('inp_untested', 'machine_loaded', 'service_worker', 'device_pixel_ratio'):
            if k not in need:
                fails.append(f'17: the validator does not demand a limitation for {k} (needs: {sorted(need)})')
    # renderer: the service-worker summary, oversized images, a budgeted image weight and a loaded machine are shown
    rep = json.loads((EXAMPLES / 'perf_report.example.json').read_text(encoding='utf-8'))
    rep['service_workers'] = [{'url': 'http://127.0.0.1:9/', 'profile': 'desktop', 'cache': 'warm', 'browser': 'chromium',
                               'variant': None, 'runs': 5, 'runs_controlled': 5, 'responses_median': 12, 'workers': ['http://127.0.0.1:9/sw.js'],
                               'network_measured': True, 'network_requests_median': 12, 'network_bytes_median': 81920,
                               'network_from_cache_median': 0, 'requests_guarded': False}]
    rep['delivery'] = [{'url': 'http://127.0.0.1:9/', 'profile': 'desktop', 'cache': 'cold', 'browser': 'chromium', 'variant': None,
                        'run': 'r1', 'checked': 3, 'uncompressed': [], 'uncompressed_bytes': 0, 'no_cache_lifetime': [],
                        'oversized_images': [{'url': 'http://127.0.0.1:9/thumb-640.avif', 'natural': [640, 360], 'rendered': [128, 72],
                                              'dpr': 1, 'ratio': 5.0, 'bytes': 40000, 'waste_bytes_estimate': 38400}]}]
    rep['measurements'].append(dict(rep['measurements'][0], metric='transfer_image', unit='bytes', median=70000, budget=50000,
                                    status='over'))
    rep['metadata']['calibration'] = dict(rep['metadata']['calibration'], state='loaded', note='machine loaded: 1.3x')
    (tmp / 'r10.json').write_text(json.dumps(rep), encoding='utf-8')
    r = subprocess.run([sys.executable, '-B', str(HERE / 'render_report.py'), str(tmp / 'r10.json'), '--color', 'never'],
                       capture_output=True, text=True, encoding='utf-8')
    for want in ('Service workers', 'thumb-640.avif', 'images', 'LOADED'):
        if want not in r.stdout:
            fails.append(f'render: the report does not show "{want}"')
    tmp_dir.cleanup()
    return fails


SW_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/site.css"></head>
<body><h1 id="title">Bird log</h1><img id="thumb" src="/thumb.png" style="width:100px;height:50px">
<script>navigator.serviceWorker && navigator.serviceWorker.register('/sw.js');</script></body></html>"""
SW_JS = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => { e.respondWith(caches.open('v1').then(async c => {
  const hit = await c.match(e.request); const net = fetch(e.request).then(r => { c.put(e.request, r.clone()); return r; });
  return hit || net; })); });"""
WV_STUB = 'window.webVitals = {onLCP(){}, onCLS(){}, onINP(){}, onFCP(){}, onTTFB(){}};'


def runner_checks() -> tuple[list[str], str]:
    """A real warm load in Chromium of a page whose service worker answers every request (stale-while-revalidate),
    through measure.py's own Runner: the cache is kept, SW answers are not HTTP-cache hits, the worker's own network
    requests are counted, the mobile profile's pixel ratio is applied, and every image's size is recorded."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return [], 'skipped (Playwright not installed)'
    import http.server, json, socketserver, threading
    from browser_harness import Harness, png_encode
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp)
        (site / 'index.html').write_text(SW_PAGE, encoding='utf-8')
        (site / 'sw.js').write_text(SW_JS, encoding='utf-8')
        (site / 'site.css').write_text('body { color: #123 }', encoding='utf-8')
        (site / 'thumb.png').write_bytes(png_encode(800, 400, [bytes([200, 30, 30]) * 800] * 400))

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(site), **k)

            def log_message(self, *a):
                pass
        srv = None
        for port in range(8670, 8680):
            try:
                srv = socketserver.ThreadingTCPServer(('127.0.0.1', port), Quiet)
                break
            except OSError:
                continue
        if srv is None:
            return ['no free port in 8670-8679'], 'not run'
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{port}/'
        plan = {'targets': [{'url': url, 'template': 'home', 'reason': 'x'}], 'service_workers': 'cold'}
        profiles = json.loads((SKILL / 'data' / 'profiles.json').read_text(encoding='utf-8'))
        hc = getattr(measure, 'harness_config', None)
        cfg = hc(plan, 'chromium', ['warm'])[0] if hc else {'targets': [url]}
        try:
            with Harness(cfg) as h:
                try:
                    h.new_page().close()
                except Exception as exc:  # noqa: BLE001  no browser installed
                    return [], f'skipped ({type(exc).__name__})'
                r = measure.Runner(h, plan, profiles, WV_STUB, None, 'chromium')
                rec = r.one(plan['targets'][0], 'mobile-mid', 'warm', 'home/mobile-mid/warm/chromium/r1', 'measured')
        finally:
            srv.shutdown()
            srv.server_close()
    if rec.get('error'):
        return [f'the warm run failed: {rec["error"]}'], 'ran'
    cc = rec.get('cache_check') or {}
    if not cc.get('from_service_worker'):
        fails.append(f'2: the warm load\'s responses from the service worker are not recorded apart: {cc}')
    sw = rec.get('service_worker') or {}
    if not sw.get('controlled') or not sw.get('network_measured') or not (sw.get('network') or {}).get('requests'):
        fails.append(f'4: the service worker\'s own requests on the warm load are not measured: {sw}')
    if 'transfer_sw' not in (rec.get('metrics') or {}):
        fails.append(f'4: no transfer_sw metric on a warm run with a service worker: {sorted(rec.get("metrics") or {})}')
    applied = (rec.get('throttling') or {}).get('applied') or {}
    if applied.get('device_scale_factor') != profiles['mobile-mid'].get('device_scale_factor'):
        fails.append(f'6: the mobile profile\'s device pixel ratio is not applied: {applied}')
    imgs = rec.get('images') or []
    thumb = next((i for i in imgs if i['url'].endswith('/thumb.png')), None)
    if not thumb or thumb.get('natural') != [800, 400] or thumb.get('rendered') != [100, 50] or not thumb.get('dpr'):
        fails.append(f'8: the image sizes (natural, rendered, DPR) are not recorded: {imgs}')
    return fails, 'ran'


def main() -> int:
    ok = True
    for name, fn in (('measure.py: CPU calibration against an idle reference', calibration_checks),
                     ('measure/attribute/validate: warm cache, headers, interaction transfer, unstable, summary, brief',
                      round8_checks),
                     ('round 10: http_cache from the plan, service workers, s-maxage, DPR, image budget and sizes, '
                      'prefetches, loaded machine, reference, --only, seeder, near ties, slowdown, limitations',
                      round10_checks),
                     ('attribute.py: INP cause by largest subpart', inp_checks),
                     ('attribute.py: LCP CSS background without preload', lcp_checks)):
        fails = fn()
        ok &= not fails
        print(f"self-test {'passed' if not fails else 'FAILED'}: {name}" + ''.join(f'\n  - {f}' for f in fails))
    fails, how = lcp_element_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: measure.py: LCP element background and preload ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    fails, how = runner_checks()
    ok &= not fails
    print(f"self-test {'passed' if not fails else 'FAILED'}: measure.py: a warm load answered by a service worker ({how})"
          + ''.join(f'\n  - {f}' for f in fails))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
