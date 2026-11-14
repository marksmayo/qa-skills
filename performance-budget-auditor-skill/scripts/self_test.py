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


def main() -> int:
    ok = True
    for name, fn in (('measure.py: CPU calibration against an idle reference', calibration_checks),
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
