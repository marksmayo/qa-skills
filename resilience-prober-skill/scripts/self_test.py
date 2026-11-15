#!/usr/bin/env python3
"""Self-test for the resilience prober's offline steps (stdlib; no browser).

    python self_test.py

Builds small synthetic runs and checks classify.py, build_report.py and the runner's pure helpers:
  * a fault on the page's own HTML document is a note, never a blank_screen or crash rated on the
    catalogue's injected body;
  * type-ahead queries (different query strings) are not a retry storm, while repeats of one URL
    within Retry-After (and cache-busting repeats) still are;
  * a step whose only action types into a field is recovered by re-sending that input, and an older
    run that "repeated" it with nothing left to do does not count as no_recovery;
  * the report is written even when run text holds a lone UTF-16 surrogate (an emoji cut in half),
    and the runner's control names are cut by code point.
Exit 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations
import json, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))

FAILS: list[str] = []


def quiet():
    """Keep the runner's progress lines (stderr) out of the self-test's output."""
    import contextlib, io
    return contextlib.redirect_stderr(io.StringIO())


def check(name: str, cond: bool, detail: str = '') -> None:
    print(f'{"ok  " if cond else "FAIL"} {name}' + (f': {detail}' if detail and not cond else ''))
    if not cond:
        FAILS.append(name)


def observed(body_len=200, root_len=150, text='Recipe list\nPancakes\nSoup', **kw) -> dict:
    o = {'body_len': body_len, 'root_text': text, 'root_len': root_len, 'spinner': False, 'loading_words': [], 'error_element': False,
         'error_words': [], 'empty_words': [], 'fallback_words': [], 'boundary_element': False, 'controls': ['Home', 'Search'], 'text': text}
    o.update(kw)
    return o


def target_step(name, ob, *, recovered=True, recovery='repeat_step', error_shown=True, completed=False) -> dict:
    return {'name': name, 'target': True, 'error': None, 'completed_under_fault': completed, 'first_error_ms': 900 if error_shown else None,
            'spinner_seen': False, 'watched_ms': 10200, 'observed': ob, 'inputs': {'before': {}, 'after_fault': {}}, 'retry_controls': [],
            'recovery': recovery, 'recovered': recovered, 'screenshots': ['x.fault.png', 'x.recovery.png']}


def scenario_run(tag, steps, requests, faulted=1) -> dict:
    return {'tag': tag, 'started_at': '2026-11-12T07:00:00Z', 'steps': steps, 'ok': True, 'fault_triggered': faulted > 0,
            'faulted_requests': faulted, 'requests': requests, 'page_errors': [], 'console_errors': [], 'console_filtered': 0,
            'finished_at': '2026-11-12T07:00:20Z', 'requests_sent': 5}


def classify(tmp: Path, flows: dict, scenarios: list[dict], runs: dict[str, list[dict]], base_steps: dict[str, list[dict]]) -> dict:
    budgets = json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))['budgets']
    plan = {'planned_at': '2026-11-12T06:00:00Z', 'budgets': budgets, 'unreachable': [], 'scenarios': scenarios}
    run = {'site': 'http://127.0.0.1:9', 'started_at': '2026-11-12T06:30:00Z', 'runs_per_scenario': 2, 'indicators': {},
           'baselines': {f: {'status': 'passed', 'critical_path': True,
                             'runs': [scenario_run(f'baseline{i}', [dict(s, completed=True) for s in base_steps[f]], [], 0) for i in (1, 2)]}
                         for f in base_steps},
           'scenarios': [{'id': sid, 'status': 'ran', 'runs': rs} for sid, rs in runs.items()],
           'blocked_requests': [], 'requests_sent': 10, 'browser': {'name': 'chromium', 'version': '0'}}
    paths = {n: tmp / f'{n}.json' for n in ('run', 'scenarios', 'flows')}
    for n, d in (('run', run), ('scenarios', plan), ('flows', flows)):
        paths[n].write_text(json.dumps(d), encoding='utf-8')
    out = tmp / 'candidates.json'
    p = subprocess.run([sys.executable, str(HERE / 'classify.py'), str(paths['run']), str(paths['scenarios']), str(paths['flows']), '--out', str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return json.loads(out.read_text(encoding='utf-8'))


def scen(sid, flow, step, endpoint, fault, group, **kw) -> dict:
    s = {'id': sid, 'flow': flow, 'step': step, 'endpoint': endpoint, 'fault': fault, 'group': group, 'likelihood': 3,
         'critical_path': True, 'write': False, 'expected': 'An error shows within 10 s.'}
    s.update(kw)
    return s


def test_document_fault(tmp: Path) -> None:
    """A 502 on the page's own document shows the catalogue's injected body: not a blank screen of the app."""
    flows = {'site': 'http://127.0.0.1:9/', 'flows': [{'name': 'recipe', 'critical_path': True, 'steps': [
        {'name': 'open list', 'actions': [{'goto': '/'}], 'done': 'h1'},
        {'name': 'open recipe', 'actions': [{'goto': '/recipe/pancakes'}], 'done': '.ingredients'}]}]}
    base = [{'name': 'open list', 'observed': observed()}, {'name': 'open recipe', 'observed': observed()}]
    shown = observed(body_len=15, root_len=0, text='502 Bad Gateway', controls=[])
    req = {'method': 'GET', 't_ms': 400, 'url': 'http://127.0.0.1:9/recipe/pancakes', 'injected': True, 'fault_action': 'fulfill',
           'status': 502, 'ms': 3, 'retry_after': None, 'navigation': True, 'resource_type': 'document'}
    runs = {'S01': [scenario_run(f'run{i}', [{'name': 'open list', 'completed': True, 'error': None, 'observed': observed()},
                                              target_step('open recipe', shown, error_shown=False)], [req]) for i in (1, 2)]}
    c = classify(tmp, flows, [scen('S01', 'recipe', 'open recipe', '**/recipe/*', 'http_502', 'server_error')], runs, {'recipe': base})
    effs = [k['effect'] for k in c['candidates']]
    check('document fault: no blank_screen or crash candidate', not any(e in ('blank_screen', 'crash_b1', 'crash_b2', 'silent_failure') for e in effs),
          f'candidates {effs}')
    check('document fault: recorded as a document_fault note', any(n['kind'] == 'document_fault' for n in c['notes']),
          f'notes {[n["kind"] for n in c["notes"]]}')


def _storm_case(tmp: Path, urls: list[str], retry_after='5') -> list[str]:
    flows = {'site': 'http://127.0.0.1:9/', 'flows': [{'name': 'find', 'critical_path': True, 'steps': [
        {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
        {'name': 'type query', 'actions': [{'fill': '#q', 'value': 'pan'}, {'press': '#q', 'key': 'Backspace'}], 'done': '.suggestions'}]}]}
    base = [{'name': 'open', 'observed': observed()}, {'name': 'type query', 'observed': observed()}]
    reqs = [{'method': 'GET', 't_ms': 100 + i * 300, 'url': u, 'injected': True, 'fault_action': 'fulfill', 'status': 429, 'ms': 2,
             'retry_after': retry_after} for i, u in enumerate(urls)]
    runs = {'S01': [scenario_run(f'run{i}', [{'name': 'open', 'completed': True, 'error': None, 'observed': observed()},
                                              target_step('type query', observed())], reqs) for i in (1, 2)]}
    c = classify(tmp, flows, [scen('S01', 'find', 'type query', '**/api/suggest*', 'http_429_retry_after', 'rate_limit', likelihood=2)],
                 runs, {'find': base})
    return [k['effect'] for k in c['candidates']]


def test_storm(tmp: Path) -> None:
    api = 'http://127.0.0.1:9/api/suggest'
    effs = _storm_case(tmp, [f'{api}?q=', f'{api}?q=', f'{api}?q=panca', f'{api}?q=panc', f'{api}?q=pan'])
    check('type-ahead queries: not a retry storm', 'retry_storm' not in effs, f'candidates {effs}')
    effs = _storm_case(tmp, [f'{api}?q=pan'] * 4)
    check('one URL repeated inside Retry-After: still a retry storm', 'retry_storm' in effs, f'candidates {effs}')
    effs = _storm_case(tmp, [f'{api}?q=pan&_={1700000000000 + i}' for i in range(4)])
    check('cache-busting repeats of one query: still a retry storm', 'retry_storm' in effs, f'candidates {effs}')


def test_fill_only_recovery(tmp: Path) -> None:
    import run_resilience as rr
    step = {'name': 'type query', 'actions': [{'fill': '#q', 'value': 'pan'}], 'done': '.suggestions'}
    acts = rr.recovery_actions(step)
    check('fill-only step: recovery re-sends the typed input', bool(acts) and all('refill' in a for a in acts), f'actions {acts}')
    both = {'name': 'save', 'actions': [{'fill': '#name', 'value': 'x'}, {'click': 'button'}], 'done': '.ok'}
    check('fill then click: recovery repeats only the click', rr.recovery_actions(both) == [{'click': 'button'}], f'{rr.recovery_actions(both)}')
    # an older run: recovery "repeated" a fill-only step with nothing left to do, and the step stayed incomplete
    flows = {'site': 'http://127.0.0.1:9/', 'flows': [{'name': 'find', 'critical_path': True, 'steps': [
        {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'}, step]}]}
    base = [{'name': 'open', 'observed': observed()}, {'name': 'type query', 'observed': observed()}]
    req = {'method': 'GET', 't_ms': 300, 'url': 'http://127.0.0.1:9/api/suggest?q=pan', 'injected': True, 'fault_action': 'fulfill',
           'status': 500, 'ms': 2, 'retry_after': None}
    runs = {'S01': [scenario_run(f'run{i}', [{'name': 'open', 'completed': True, 'error': None, 'observed': observed()},
                                              target_step('type query', observed(), recovered=False)], [req]) for i in (1, 2)]}
    c = classify(tmp, flows, [scen('S01', 'find', 'type query', '**/api/suggest*', 'http_500', 'server_error')], runs, {'find': base})
    effs = [k['effect'] for k in c['candidates']]
    check('fill-only step repeated with nothing to do: no no_recovery', 'no_recovery' not in effs, f'candidates {effs}')
    check('fill-only step repeated with nothing to do: a recovery_not_tried note',
          any(n['kind'] == 'recovery_not_tried' for n in c['notes']), f'notes {[n["kind"] for n in c["notes"]]}')


def test_surrogates(tmp: Path) -> None:
    ex = SKILL / 'examples'
    for n in ('run.json', 'scenarios.json'):
        shutil.copy(ex / 'run' / n, tmp / n)
    (tmp / 'screenshots').mkdir(exist_ok=True)
    cands = json.loads((ex / 'run' / 'candidates.json').read_text(encoding='utf-8'))
    cands['notes'].append({'kind': 'console_only', 'detail': ['link "\ud83c" cut in half'], 'scenario': None, 'flow': 'track'})
    (tmp / 'candidates.json').write_text(json.dumps(cands), encoding='utf-8')
    out = tmp / 'report.json'
    p = subprocess.run([sys.executable, str(HERE / 'build_report.py'), '--run', str(tmp / 'run.json'), '--scenarios', str(tmp / 'scenarios.json'),
                        '--candidates', str(tmp / 'candidates.json'), '--flows', str(ex / 'flows.json'), '--out', str(out)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    check('lone surrogate in run text: build_report writes the report', p.returncode == 0 and out.is_file(),
          (p.stderr.strip().splitlines() or [''])[-1])
    import run_resilience as rr
    check('control names are cut by code point, not UTF-16 unit', 'Array.from(' in rr.CONTROLS and '.slice(0, 40)' not in rr.CONTROLS.split('Array.from(')[0],
          'CONTROLS slices the string with String.prototype.slice')
    node = shutil.which('node')
    if node:
        js = 'const clip = ' + rr.CLIP_JS + ';\nconst s = clip("\\u{1F1FA}\\u{1F1F8}" + " ".repeat(38) + "\\u{1F1FA}\\u{1F1F8} Ferry");\n' \
             'process.stdout.write(/[\\uD800-\\uDBFF](?![\\uDC00-\\uDFFF])|(?<![\\uD800-\\uDBFF])[\\uDC00-\\uDFFF]/.test(s) ? "lone" : "ok");'
        r = subprocess.run([node, '-e', js], capture_output=True, text=True)
        check('clip in node leaves no lone surrogate', r.stdout == 'ok', r.stdout + r.stderr[-200:])


def _one_step(tmp: Path, ob: dict, *, fault='http_500', group='server_error', base_ob: dict | None = None, completed=False,
              recovered=True, actions=None, step_extra=None, target_extra=None, page_errors=None, endpoint='**/api/data*',
              requests=None, run_extra=None) -> dict:
    """One flow with a single step under a fault, classified."""
    step = {'name': 'load', 'actions': actions or [{'click': '#go'}], 'done': '.ready', **(step_extra or {})}
    flows = {'site': 'http://127.0.0.1:9/', 'flows': [{'name': 'main', 'critical_path': True, 'steps': [
        {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'}, step]}]}
    base = [{'name': 'open', 'observed': observed()}, {'name': 'load', 'observed': base_ob or observed()}]
    req = {'method': 'GET', 't_ms': 300, 'url': 'http://127.0.0.1:9/api/data.bin', 'injected': True, 'fault_action': 'fulfill',
           'status': 500, 'ms': 2, 'retry_after': None, 'phase': 'fault'}
    runs = []
    for i in (1, 2):
        t = dict(target_step('load', ob, recovered=recovered, error_shown=False, completed=completed), **(target_extra or {}))
        r = scenario_run(f'run{i}', [{'name': 'open', 'completed': True, 'error': None, 'observed': observed()}, t],
                         requests if requests is not None else [req])
        r['page_errors'] = list(page_errors or [])
        r.update(run_extra or {})
        runs.append(r)
    return classify(tmp, flows, [scen('S01', 'main', 'load', endpoint, fault, group)], {'S01': runs}, {'main': base})


def test_read_shown_as_done(tmp: Path) -> None:
    """A failing fault on a read, no error shown, and the step's `done` appeared anyway: the failure was shown as success."""
    c = _one_step(tmp, observed(), completed=True)
    effs = [k['effect'] for k in c['candidates']]
    check('read completed under a 500 with no error: misleading_state (shown as done)', 'misleading_state' in effs and
          any(e['measure'].get('shown_as_done') for k in c['candidates'] if k['effect'] == 'misleading_state' for e in k['evidence']),
          f'candidates {effs}')
    c = _one_step(tmp, observed(), completed=True, step_extra={'optional_endpoints': ['**/api/data*']})
    effs = [k['effect'] for k in c['candidates']]
    check('read completed under a fault on an optional endpoint: not misleading_state', 'misleading_state' not in effs, f'candidates {effs}')


def test_done_not(tmp: Path) -> None:
    import run_resilience as rr

    class Loc:
        def __init__(self, vis):
            self.first = self
            self.vis = vis

        def is_visible(self):
            return self.vis

    class Page:
        def __init__(self, visible):
            self.visible = visible

        def locator(self, sel):
            return Loc(sel in self.visible)

        def wait_for_selector(self, sel, timeout=0, state='visible'):
            if sel not in self.visible:
                raise TimeoutError(sel)

    step = {'done': 'text=Click to continue', 'done_not': ['text=Error 500']}
    try:
        both = rr.step_done(Page({'text=Click to continue', 'text=Error 500'}), step, 1)
        only = rr.step_done(Page({'text=Click to continue'}), step, 1)
    except AttributeError as e:
        check('done_not: a step is not done while a done_not selector shows', False, f'{e}')
        return
    check('done_not: a step is not done while a done_not selector shows', both is False and only is True, f'both {both}, only {only}')


def test_persisted_failure(tmp: Path) -> None:
    """After the fault is removed and the page reloaded, the step made no request to the endpoint and never got a good
    answer: the app stored the failure (Cache Storage, IndexedDB, localStorage)."""
    reload = {'completed': False, 'endpoint_requests': 0, 'ok_responses': 0,
              'cache_storage': [{'cache': 'data-v1', 'entries': 1, 'urls': ['http://127.0.0.1:9/api/data.bin'], 'statuses': [500]}]}
    c = _one_step(tmp, observed(), completed=True, target_extra={'reload': reload})
    effs = [k['effect'] for k in c['candidates']]
    check('failure cached and replayed after a reload: persisted_failure', 'persisted_failure' in effs, f'candidates {effs}')
    ok_reload = dict(reload, endpoint_requests=1, ok_responses=1, completed=True)
    c = _one_step(tmp, observed(), completed=True, target_extra={'reload': ok_reload})
    effs = [k['effect'] for k in c['candidates']]
    check('reload fetched the endpoint again and got a good answer: no persisted_failure', 'persisted_failure' not in effs,
          f'candidates {effs}')
    failed_reload = dict(reload, error='TimeoutError: Page.goto: Timeout 30000ms exceeded.', cache_storage=None)
    c = _one_step(tmp, observed(), completed=True, recovered=False, target_extra={'reload': failed_reload})
    effs = [k['effect'] for k in c['candidates']]
    check('a reload that never loaded is not a persisted_failure: a reload_not_checked note', 'persisted_failure' not in effs
          and any(n['kind'] == 'reload_not_checked' for n in c['notes']), f'candidates {effs}, notes {[n["kind"] for n in c["notes"]]}')
    import run_resilience as rr
    check('the runner reloads after recovery for failing faults by default',getattr(rr, 'reload_check', None) is not None and
          rr.reload_check({}, {'group': 'server_error'}) and not rr.reload_check({}, {'group': 'latency'})
          and not rr.reload_check({'reload_after_recovery': False}, {'group': 'server_error'}), 'no reload_check helper')


def test_blank_baseline(tmp: Path) -> None:
    """A step whose baseline already shows under 20 characters (a canvas game, a video) is not a blank screen or crash."""
    blank = observed(body_len=0, root_len=0, text='', controls=[])
    c = _one_step(tmp, blank, base_ob=blank, page_errors=['CompileError: wasm validation failed'])
    effs = [k['effect'] for k in c['candidates']]
    check('baseline already blank: no crash_b1, crash_b2 or blank_screen', not {'crash_b1', 'crash_b2', 'blank_screen'} & set(effs),
          f'candidates {effs}')
    check('baseline already blank: a baseline_blank note', any(n['kind'] == 'baseline_blank' for n in c['notes']),
          f'notes {[n["kind"] for n in c["notes"]]}')
    video = observed(body_len=14, root_len=14, text='Downloading...', media_visible=True)
    c = _one_step(tmp, video, fault='hang', group='timeout')
    effs = [k['effect'] for k in c['candidates']]
    check('a full-screen video with 14 characters of text: not blank_screen', 'blank_screen' not in effs, f'candidates {effs}')
    no_root = observed(root_len=0, root_text='')
    c = _one_step(tmp, observed(root_len=0, root_text=''), base_ob=no_root, page_errors=['ReferenceError: x is not defined'],
                  completed=True, fault='asset_abort', group='static_asset')
    effs = [k['effect'] for k in c['candidates']]
    check('a page error with an empty root that was empty in the baseline too: not a crash', not {'crash_b1', 'crash_b2'} & set(effs),
          f'candidates {effs}')


def test_static_asset_incomplete(tmp: Path) -> None:
    """A script that failed to load left the step incomplete although the static HTML stayed: chunk_failure, not a note."""
    c = _one_step(tmp, observed(body_len=513, root_len=513), fault='asset_abort', group='static_asset', completed=False)
    effs = [k['effect'] for k in c['candidates']]
    check('static asset aborted, step incomplete, page text kept: chunk_failure', 'chunk_failure' in effs, f'candidates {effs}')


def test_goto_recovery(tmp: Path) -> None:
    """Repeating a step whose only action is a goto is a reload: recovery not tried, never `recovered`."""
    import run_resilience as rr
    kind = getattr(rr, 'recovery_kind', None)
    check('goto-only step: recovery kind is reload', kind is not None and kind({'actions': [{'goto': '/'}]}) == 'reload'
          and kind({'actions': [{'click': '#go'}]}) == 'repeat_step', 'no recovery_kind helper')
    c = _one_step(tmp, observed(), fault='asset_abort', group='static_asset', completed=False, recovered=True,
                  actions=[{'goto': '/game'}])
    check('goto-only step that "recovered" by navigating again: a recovery_not_tried note',
          any(n['kind'] == 'recovery_not_tried' for n in c['notes']), f'notes {[n["kind"] for n in c["notes"]]}')


def test_confidence_cap(tmp: Path) -> None:
    """A documented reason the agent gives (stand-in data, effects the classifier cannot measure) may lower the
    headline confidence; it never raises it."""
    ex = SKILL / 'examples'
    rep = json.loads((ex / 'resilience_report.example.json').read_text(encoding='utf-8'))
    rep['confidence_caps'] = [{'cap': 'low', 'reason': 'the data package was a synthetic stand-in, so the last step cannot be judged'}]
    rep['headline']['overall_confidence'] = 'low'
    out = tmp / 'report.json'
    out.write_text(json.dumps(rep), encoding='utf-8')
    args = ['--run', str(ex / 'run' / 'run.json'), '--scenarios', str(ex / 'run' / 'scenarios.json'),
            '--candidates', str(ex / 'run' / 'candidates.json')]
    p = subprocess.run([sys.executable, str(HERE / 'validate_resilience.py'), str(out), *args], capture_output=True, text=True,
                       encoding='utf-8')
    check('a confidence cap with a reason lowers the headline confidence', p.returncode == 0, (p.stdout.strip().splitlines() or [''])[-1])
    rep['confidence_caps'] = [{'cap': 'low', 'reason': 'x'}]
    out.write_text(json.dumps(rep), encoding='utf-8')
    p = subprocess.run([sys.executable, str(HERE / 'validate_resilience.py'), str(out), *args], capture_output=True, text=True,
                       encoding='utf-8')
    check('a confidence cap without a real reason is rejected', p.returncode == 1, (p.stdout.strip().splitlines() or [''])[-1])


class _Timeout(Exception):
    pass


class FakePage:
    """A page whose plain evaluate() and is_visible() never return (as on a page stuck in a navigation that timed out):
    calling them is a test failure. wait_for_function honours its timeout, as Playwright's does."""
    def __init__(self, url='http://127.0.0.1:9/', stuck=False, value=None):
        self.url, self.stuck, self.value = url, stuck, value
        self.unbounded: list[str] = []
        self.calls: list[str] = []

    def evaluate(self, *_a, **_k):
        self.unbounded.append('evaluate')
        raise _Timeout('page.evaluate has no timeout: on a stuck page it never returns')

    def locator(self, _sel):
        page = self

        class Loc:
            first = None

            def is_visible(self):
                page.unbounded.append('is_visible')
                raise _Timeout('is_visible has no timeout')
        loc = Loc()
        loc.first = loc
        return loc

    def wait_for_function(self, js, arg=None, timeout=None, polling=None):
        self.calls.append(f'wait_for_function:{timeout}')
        if timeout is None:
            self.unbounded.append('wait_for_function without timeout')
        if self.stuck:
            raise _Timeout(f'Page.wait_for_function: Timeout {timeout}ms exceeded.')
        v = self.value(js, arg) if callable(self.value) else self.value

        class Handle:
            def json_value(self):
                return {'v': v}

            def dispose(self):
                pass
        return Handle()

    def wait_for_selector(self, sel, timeout=None, state='visible'):
        self.calls.append(f'wait_for_selector:{timeout}')
        if timeout is None:
            self.unbounded.append('wait_for_selector without timeout')
        raise _Timeout(f'Page.wait_for_selector: Timeout {timeout}ms exceeded.')

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, full_page=False, timeout=None):
        self.calls.append(f'screenshot:{timeout}')
        if timeout is None:
            self.unbounded.append('screenshot without timeout')
        raise _Timeout('Page.screenshot: Timeout exceeded.')

    def go_back(self, timeout=None, wait_until=None):
        self.calls.append(f'back:{timeout}')

    def go_forward(self, timeout=None, wait_until=None):
        self.calls.append(f'forward:{timeout}')

    def reload(self, timeout=None, wait_until=None):
        self.calls.append(f'reload:{timeout}')


def _ind() -> dict:
    import run_resilience as rr
    return rr.merge_indicators(json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8')), None)


def test_bounded_reads(tmp: Path) -> None:
    """A page stuck in a navigation that timed out never answers page.evaluate (Playwright gives it no timeout), so
    the runner hung forever. Every read of the page has a deadline, and a stuck page gives an observation marked
    unavailable instead of blocking the run."""
    import run_resilience as rr
    page = FakePage(stuck=True)
    try:
        ob = rr.observe(page, _ind(), '#app')
    except _Timeout as e:
        check('observe on a stuck page: bounded, never a plain evaluate', False, f'{e}')
        return
    check('observe on a stuck page: bounded, never a plain evaluate', not page.unbounded, f'unbounded calls {page.unbounded}')
    check('observe on a stuck page: marked unavailable', bool(ob.get('unavailable')), f'{ob}')
    page = FakePage(stuck=True)
    try:
        got = rr.done(page, '.ready', 1)
    except _Timeout as e:
        got = f'{e}'
    check('done() on a stuck page: bounded (no is_visible)', got is False and not page.unbounded, f'{got} {page.unbounded}')
    page = FakePage(stuck=True)
    try:
        shot = rr.shoot(page, tmp / 'x.png')
    except (_Timeout, AttributeError) as e:
        shot = f'{type(e).__name__}: {e}'
    check('a screenshot that times out is skipped, not fatal', shot is None and not page.unbounded, f'{shot} {page.unbounded}')


def test_browser_error_page(tmp: Path) -> None:
    """Offline during a click that turned into a full page load: Chromium shows its own offline page. That is what the
    user saw, so it is recorded (browser_error_page), and classify makes it a note with the screenshot, never an
    effect rated on the app (no app code ran) nor a run that 'did not reach the target step'."""
    import run_resilience as rr

    def val(js, arg):
        if 'innerText' in js and 'root_exists' in js:
            return {'body': 'No internet\nTry: checking the network cables', 'root': None, 'root_exists': False}
        return [] if 'querySelectorAll' in js and 'new Set' in js else False
    page = FakePage(url='chrome-error://chromewebdata/', value=val)
    try:
        ob = rr.observe(page, _ind(), '#app')
    except Exception as e:  # noqa: BLE001
        ob = {'error': f'{type(e).__name__}: {e}'}
    check('observe on the browser\'s offline page: browser_error_page', ob.get('browser_error_page') is True, f'{ob}')
    shown = observed(body_len=40, root_len=0, root_text='', text='No internet', controls=[], browser_error_page=True,
                     page_url='chrome-error://chromewebdata/')
    req = {'method': 'GET', 't_ms': 300, 'url': 'http://127.0.0.1:9/two', 'injected': False, 'status': None, 'ms': 2,
           'failure': 'net::ERR_INTERNET_DISCONNECTED', 'phase': 'fault', 'navigation': True, 'resource_type': 'document'}
    c = _one_step(tmp, shown, fault='offline', group='offline', endpoint='**/two', requests=[req])
    effs = [k['effect'] for k in c['candidates']]
    check('browser offline page: no effect rated on the app', not effs, f'candidates {effs}')
    check('browser offline page: a browser_error_page note', any(n['kind'] == 'browser_error_page' for n in c['notes']),
          f'notes {[n["kind"] for n in c["notes"]]}')


def test_full_page_fallback(tmp: Path) -> None:
    """A failed route payload (?_rsc=) that the framework answered with a full page load of the same path, which the
    fault did not touch, showed the right page: a full_page_fallback note, not misleading_state P0."""
    reqs = [{'method': 'GET', 't_ms': 300, 'url': 'http://127.0.0.1:9/trails?_rsc=abc', 'injected': True, 'fault_action': 'fulfill',
             'status': 500, 'ms': 2, 'phase': 'fault', 'navigation': False, 'resource_type': 'fetch'}]
    docs = [{'url': 'http://127.0.0.1:9/trails', 't_ms': 420, 'status': 200, 'phase': 'fault'}]
    c = _one_step(tmp, observed(), completed=True, endpoint='**/trails?_rsc=*', requests=reqs, run_extra={'documents': docs})
    effs = [k['effect'] for k in c['candidates']]
    check('payload failure answered by a full page load: not misleading_state', 'misleading_state' not in effs, f'candidates {effs}')
    check('payload failure answered by a full page load: a full_page_fallback note',
          any(n['kind'] == 'full_page_fallback' for n in c['notes']), f'notes {[n["kind"] for n in c["notes"]]}')
    c = _one_step(tmp, observed(), completed=True, endpoint='**/trails?_rsc=*', requests=reqs,
                  run_extra={'documents': [dict(docs[0], status=500)]})
    effs = [k['effect'] for k in c['candidates']]
    check('a full page load that failed too is not a fallback: still misleading_state', 'misleading_state' in effs, f'candidates {effs}')


def test_images_optional(tmp: Path) -> None:
    """Failed decorative images (resource type image or font) left the step usable: not misleading_state P0; the visible
    broken images are counted and noted."""
    reqs = [{'method': 'GET', 't_ms': 300 + i, 'url': f'http://127.0.0.1:9/_next/image?url=%2Fb{i}.jpg&w=640', 'injected': True,
             'fault_action': 'abort', 'status': None, 'ms': 2, 'phase': 'fault', 'navigation': False, 'resource_type': 'image'}
            for i in range(3)]
    c = _one_step(tmp, observed(broken_images=3), base_ob=observed(broken_images=0), completed=True, fault='asset_abort',
                  group='static_asset', endpoint='**/_next/image*', requests=reqs)
    effs = [k['effect'] for k in c['candidates']]
    check('failed images, step completed: no effect', not effs, f'candidates {effs}')
    check('failed images: a broken_images note with the count', any(n['kind'] == 'broken_images' and '3' in ' '.join(n['detail'])
                                                                   for n in c['notes']), f'notes {c["notes"]}')
    c = _one_step(tmp, observed(), completed=True, fault='http_500', group='server_error', endpoint='**/_next/image*',
                  requests=[dict(r, fault_action='fulfill', status=500) for r in reqs])
    effs = [k['effect'] for k in c['candidates']]
    check('a 500 on images only, step completed: not misleading_state (images are optional by default)',
          'misleading_state' not in effs, f'candidates {effs}')


def test_offline_counting(tmp: Path) -> None:
    """Offline counted every failed request in the context as the fault's: a prefetch the run blocked made a scenario
    'ran'. Only failed requests the scenario targets count."""
    import run_resilience as rr

    class Req:
        def __init__(self, url, method='GET', rtype='fetch', headers=None):
            self.url, self.method, self.resource_type, self.headers = url, method, rtype, headers or {}
    fn = getattr(rr, 'targeted', None)
    if fn is None:
        check('offline counts only requests the scenario targets', False, 'no targeted() helper')
        return
    scen = {'endpoint': '**/?_rsc=*', 'resource_type': 'fetch'}
    rx = rr.glob_rx(scen['endpoint'])
    check('offline counts only requests the scenario targets',
          fn(scen, rx, Req('http://127.0.0.1:9/?_rsc=1')) and not fn(scen, rx, Req('http://127.0.0.1:9/_next/static/a.js', rtype='script'))
          and not fn(scen, rx, Req('http://127.0.0.1:9/?_rsc=1', rtype='document')), 'filters not applied')


def test_back_action(tmp: Path) -> None:
    """A browser back step: the runner drives it with a bounded timeout, and repeating it in recovery would navigate
    further back, so its recovery is a reload (not tried)."""
    import run_resilience as rr
    check('back-only step: recovery kind is reload', rr.recovery_kind({'actions': [{'back': True}]}) == 'reload',
          rr.recovery_kind({'actions': [{'back': True}]}))

    class H:
        def throttle(self):
            pass

        def act(self, page, acts):
            page.calls.append('harness.act')
    page = FakePage()
    try:
        rr.act(H(), page, 'http://127.0.0.1:9/', [{'back': True}, {'forward': True}, {'reload': True}], nav_ms=4000)
        got = page.calls
    except TypeError as e:
        got = [f'{e}']
    check('back, forward and reload run with the navigation timeout', got[:3] == ['back:4000', 'forward:4000', 'reload:4000'], f'{got}')


def test_service_worker_option(tmp: Path) -> None:
    """Service workers can be allowed (returning visitors, offline navigation); the default stays block. The planner reads
    a Next.js project's workers and the report says when a run blocked a worker the app registers."""
    import run_resilience as rr
    fn = getattr(rr, 'sw_mode', None)
    check('service_workers: block by default, allow on request (global or per flow)', fn is not None and fn({}, {}) == 'block'
          and fn({'service_workers': 'allow'}, {}) == 'allow' and fn({'service_workers': 'allow'}, {'service_workers': 'block'}) == 'block',
          'no sw_mode helper')
    repo = tmp / 'site'
    (repo / 'app').mkdir(parents=True)
    (repo / 'public').mkdir()
    (repo / 'package.json').write_text('{"dependencies": {"next": "15.0.0", "react": "19.0.0"}}', encoding='utf-8')
    (repo / 'app' / 'page.tsx').write_text('export default function Page() { return <h1>Birds</h1>; }\n', encoding='utf-8')
    (repo / 'app' / 'sw.tsx').write_text("'use client';\nimport { useEffect } from 'react';\nexport function Sw() {\n"
                                         "  useEffect(() => { navigator.serviceWorker.register('/sw.js'); }, []);\n  return null;\n}\n", encoding='utf-8')
    (repo / 'public' / 'sw.js').write_text("self.addEventListener('fetch', (e) => {\n  if (e.request.mode === 'navigate') {\n"
                                           "    e.respondWith(fetch(e.request));\n  }\n});\n", encoding='utf-8')
    flows = {'site': 'http://127.0.0.1:9/', 'flows': [{'name': 'birds', 'critical_path': True, 'steps': [
        {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'}]}],
             'scenarios': [{'flow': 'birds', 'step': 'open', 'endpoint': '**/api/x', 'fault': 'http_500'}]}
    (tmp / 'flows.json').write_text(json.dumps(flows), encoding='utf-8')
    p = subprocess.run([sys.executable, str(HERE / 'plan_scenarios.py'), str(tmp / 'flows.json'), '--repo', str(repo), '--out', str(tmp / 'plan.json')],
                       capture_output=True, text=True)
    plan = json.loads((tmp / 'plan.json').read_text(encoding='utf-8')) if p.returncode == 0 else {}
    sws = plan.get('service_workers') or []
    check('the planner lists the app\'s service workers from the source', bool(sws) and sws[0].get('url') == '/sw.js',
          (p.stderr.strip().splitlines() or [''])[-1] if p.returncode else f'{plan.get("service_workers")}')
    check('the planner warns when the run will block a worker the app registers', 'service_workers' in p.stderr and 'allow' in p.stderr,
          p.stderr[-300:])
    import build_report as br
    lim = getattr(br, 'service_worker_limitations', None)
    got = lim({'safety': {}}, plan) if lim and plan else []
    check('the report says a worker the app registers was blocked', any('service worker' in x and 'blocked' in x for x in got), f'{got}')


def test_plan_estimate(tmp: Path) -> None:
    """--plan-only estimates the requests the plan will send, against request_budget."""
    flows = {'site': 'http://127.0.0.1:9/', 'request_budget': 100, 'requests_per_load': 30,
             'flows': [{'name': 'birds', 'critical_path': True, 'steps': [{'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                                                                       {'name': 'list', 'actions': [{'click': 'a'}], 'done': 'ul'}]}],
             'scenarios': [{'flow': 'birds', 'step': 'list', 'endpoint': '**/api/x', 'fault': f} for f in ('http_500', 'hang')]}
    (tmp / 'flows.json').write_text(json.dumps(flows), encoding='utf-8')
    p = subprocess.run([sys.executable, str(HERE / 'plan_scenarios.py'), str(tmp / 'flows.json'), '--plan-only'], capture_output=True, text=True)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = {}
    est = doc.get('estimate') or {}
    check('--plan-only estimates the requests against the budget', isinstance(est.get('requests'), int) and est.get('request_budget') == 100
          and est.get('over_budget') is True, f'{doc.get("estimate")}')


def _fake_rec(tag, *, ok=True, target=False, triggered=True, sent=3, steps=('open', 'list')):
    st = [{'name': n, 'completed': ok, 'error': None, 'observed': observed()} for n in steps]
    if target:
        st[-1] = dict(target_step(steps[-1], observed()), target=True)
    r = {'tag': tag, 'started_at': '2026-11-12T07:00:00Z', 'steps': st, 'ok': ok, 'requests': [], 'page_errors': [], 'console_errors': [],
         'console_filtered': 0, 'finished_at': '2026-11-12T07:00:20Z', 'requests_sent': sent}
    if target:
        r.update(fault_triggered=triggered, faulted_requests=1 if triggered else 0)
    return r


def _exec_setup(tmp: Path):
    flows = {'birds': {'name': 'birds', 'critical_path': True, 'steps': [{'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                                                                         {'name': 'list', 'actions': [{'click': 'a'}], 'done': 'ul'}]}}
    plan = {'planned_at': '2026-11-12T06:00:00Z', 'budgets': {}, 'unreachable': [],
            'scenarios': [scen(f'S0{i}', 'birds', 'list', '**/api/x', 'http_500', 'server_error') for i in (1, 2, 3)]}
    doc = {'site': 'http://127.0.0.1:9/', 'runs_per_scenario': 2, 'baselines': {}, 'scenarios': []}
    return flows, plan, doc


def test_incremental_and_resume(tmp: Path) -> None:
    """run.json is written after every scenario (run_status partial until the end), --resume keeps what is done, and a
    run the watchdog stopped is recorded errored and skipped, never run again."""
    import run_resilience as rr
    ex = getattr(rr, 'execute', None)
    if ex is None:
        check('run.json is written after each scenario', False, 'no execute()')
        return
    flows, plan, doc = _exec_setup(tmp)
    out = tmp / 'run'
    out.mkdir()
    seen_on_disk: list = []
    calls: list = []

    def runner(flow, upto, s, ind, tag):
        calls.append((s or {}).get('id', 'baseline') + ':' + tag)
        if s and s['id'] == 'S02' and tag == 'run1':
            d = json.loads((out / 'run.json').read_text(encoding='utf-8'))
            seen_on_disk.append((d.get('run_status'), [x['id'] for x in d['scenarios']]))
        return _fake_rec(tag, target=s is not None)
    with quiet():
        ex(doc, {}, flows, plan, {'birds': {}}, runner, lambda: 3 * len(calls), out, runs=2, budget=None, limit_s=60)
    check('run.json is written after each scenario', seen_on_disk == [('partial', ['S01'])], f'{seen_on_disk}')
    final = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    check('run.json says complete at the end', final.get('run_status') == 'complete' and len(final['scenarios']) == 3, f'{final.get("run_status")}')
    # resume: S01 done, S02 hung under the watchdog, S03 never started
    part = dict(final, run_status='partial', scenarios=final['scenarios'][:1])
    (out / 'run.json').write_text(json.dumps(part), encoding='utf-8')
    (out / 'hung.json').write_text(json.dumps([{'unit': 'scenario:S02:run2', 'limit_s': 60, 'at': '2026-11-12T07:10:00Z'}]), encoding='utf-8')
    calls.clear()
    doc2 = rr.resume_doc(out)
    with quiet():
        ex(doc2, {}, flows, plan, {'birds': {}}, runner, lambda: 3 * len(calls), out, runs=2, budget=None, limit_s=60)
    final = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    st = {s['id']: s for s in final['scenarios']}
    check('--resume runs only what is not done, and skips the hung scenario', calls == ['S03:run1', 'S03:run2'], f'{calls}')
    check('a hung scenario is errored with the watchdog\'s reason', st.get('S02', {}).get('status') == 'errored'
          and 'watchdog' in st['S02'].get('reason', ''), f'{st.get("S02")}')


def test_watchdog(tmp: Path) -> None:
    """A worker that stops answering (a page call that never returns) is killed after its unit's time limit, the unit is
    written to hung.json, and the worker is restarted with --resume: the run finishes."""
    import run_resilience as rr
    sup = getattr(rr, 'supervise', None)
    if sup is None:
        check('the watchdog kills a hung run and resumes', False, 'no supervise()')
        return
    out = tmp / 'run'
    out.mkdir()
    worker = tmp / 'worker.py'
    worker.write_text(
        'import json, sys, time\nfrom pathlib import Path\nout = Path(sys.argv[1])\n'
        'if "--resume" not in sys.argv:\n'
        '    (out / "progress.json").write_text(json.dumps({"unit": "scenario:S01:run1", "started": time.time(), "limit_s": 2}))\n'
        '    time.sleep(600)\n'
        'hung = json.loads((out / "hung.json").read_text())\n'
        '(out / "run.json").write_text(json.dumps({"run_status": "complete", "hung": hung}))\n', encoding='utf-8')
    t0 = time.monotonic()
    with quiet():
        code = sup([sys.executable, str(worker), str(out)], out, poll_s=0.2)
    took = time.monotonic() - t0
    hung = json.loads((out / 'hung.json').read_text(encoding='utf-8')) if (out / 'hung.json').is_file() else []
    check('the watchdog kills a hung run and resumes', code == 0 and took < 30 and [h['unit'] for h in hung] == ['scenario:S01:run1'],
          f'exit {code} after {took:.1f}s, hung {hung}')


def test_baseline_retry(tmp: Path) -> None:
    """A baseline that fails once under load is retried; one that passes on the retry is passed but recorded `retried`,
    and the headline confidence is at most medium."""
    import run_resilience as rr
    ex = getattr(rr, 'execute', None)
    if ex is None:
        check('a failed baseline run is retried once', False, 'no execute()')
        return
    flows, plan, doc = _exec_setup(tmp)
    plan['scenarios'] = plan['scenarios'][:1]
    out = tmp / 'run'
    out.mkdir()
    n = {'b': 0}

    def runner(flow, upto, s, ind, tag):
        if s is None:
            n['b'] += 1
            return _fake_rec(tag, ok=n['b'] != 1)
        return _fake_rec(tag, target=True)
    with quiet():
        ex(doc, {'baseline_retry_pause_s': 0}, flows, plan, {'birds': {}}, runner, lambda: 3, out, runs=2, budget=None, limit_s=60)
    b = doc['baselines']['birds']
    check('a failed baseline run is retried once', b['status'] == 'passed' and b.get('retried') == 1 and n['b'] == 3, f'{b.get("status")} {b.get("retried")} {n}')
    import classify as cl
    check('a retried baseline is recorded in the report\'s baseline section', cl.derive_baselines(doc)['birds'].get('retried') == 1,
          f'{cl.derive_baselines(doc)["birds"]}')
    import build_report as br
    statuses = {s['id']: 'ran' for s in plan['scenarios']}
    check('a retried baseline keeps the headline confidence at medium or below',
          br.confidence(doc, plan, statuses, [], []) in ('medium', 'low'), br.confidence(doc, plan, statuses, [], []))
    flows, plan, doc = _exec_setup(tmp)
    out2 = tmp / 'run2'
    out2.mkdir()
    with quiet():
        ex(doc, {'baseline_retry_pause_s': 0}, flows, plan, {'birds': {}}, lambda f, u, s, i, t: _fake_rec(t, ok=False), lambda: 3, out2,
           runs=2, budget=None, limit_s=60)
    b = doc['baselines']['birds']
    check('a baseline that fails on the retry too is failed, not `retried` (the attempts are kept)', b['status'] == 'failed'
          and 'retried' not in b and len(b.get('retried_runs') or []) == 2, f'{b.get("status")} {b.get("retried")} {len(b.get("retried_runs") or [])}')


def test_untriggered_reason(tmp: Path) -> None:
    """A fault that never fired says why, from the request log: matched before the fault was installed, only after it
    was removed, or never at all (served from a client cache, or the pattern does not match)."""
    import run_resilience as rr
    fn = getattr(rr, 'untriggered_reason', None)
    if fn is None:
        check('not_triggered names when the endpoint was requested', False, 'no untriggered_reason()')
        return
    before = [{'requests': [{'url': 'u', 'phase': 'before'}]}]
    later = [{'requests': [{'url': 'u', 'phase': 'recovery'}]}]
    never = [{'requests': []}]
    a, b, c = fn(before), fn(later), fn(never)
    check('not_triggered names when the endpoint was requested', 'before the fault' in a and 'after the fault was removed' in b
          and 'no request' in c and 'cache' in c, f'{a} | {b} | {c}')


def test_prefetch_note(tmp: Path) -> None:
    """prefetch: block makes a Next.js app fall back to full page loads; the baseline's console says so, and classify
    notes that the run changed the app."""
    flows = {'site': 'http://127.0.0.1:9/', 'prefetch': 'block', 'flows': [{'name': 'main', 'critical_path': True, 'steps': [
        {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'}, {'name': 'load', 'actions': [{'click': '#go'}], 'done': '.ready'}]}]}
    base = [{'name': 'open', 'observed': observed()}, {'name': 'load', 'observed': observed()}]
    budgets = json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))['budgets']
    plan = {'planned_at': '2026-11-12T06:00:00Z', 'budgets': budgets, 'unreachable': [], 'scenarios': []}
    brun = scenario_run('baseline1', [dict(s, completed=True) for s in base], [], 0)
    brun['console_errors'] = ['Failed to fetch RSC payload for http://127.0.0.1:9/a. Falling back to browser navigation.']
    run = {'site': 'http://127.0.0.1:9', 'started_at': '2026-11-12T06:30:00Z', 'runs_per_scenario': 2, 'indicators': {},
           'safety': {'prefetch': 'block'}, 'baselines': {'main': {'status': 'passed', 'critical_path': True, 'runs': [brun, brun]}},
           'scenarios': [], 'blocked_requests': [], 'requests_sent': 10, 'browser': {'name': 'chromium', 'version': '0'}}
    for n, d in (('run', run), ('scenarios', plan), ('flows', flows)):
        (tmp / f'{n}.json').write_text(json.dumps(d), encoding='utf-8')
    subprocess.run([sys.executable, str(HERE / 'classify.py'), str(tmp / 'run.json'), str(tmp / 'scenarios.json'), str(tmp / 'flows.json'),
                    '--out', str(tmp / 'c.json')], capture_output=True, text=True, check=True)
    c = json.loads((tmp / 'c.json').read_text(encoding='utf-8'))
    check('prefetch block that made the app fall back to full loads: a prefetch_changed_app note',
          any(n['kind'] == 'prefetch_changed_app' for n in c['notes']), f'notes {[n["kind"] for n in c["notes"]]}')
    import build_report as br
    fn = getattr(br, 'note_limitations', None)
    lim = fn(c['notes']) if fn else []
    check('the report states in limitations that blocking prefetch changed the app', any('prefetch' in x for x in lim), f'{lim}')


def test_partial_run_rejected(tmp: Path) -> None:
    """A run.json the runner never finished (run_status partial) is not evidence for a report."""
    ex = SKILL / 'examples'
    run = json.loads((ex / 'run' / 'run.json').read_text(encoding='utf-8'))
    run['run_status'] = 'partial'
    (tmp / 'run.json').write_text(json.dumps(run), encoding='utf-8')
    (tmp / 'screenshots').mkdir()
    p = subprocess.run([sys.executable, str(HERE / 'validate_resilience.py'), str(ex / 'resilience_report.example.json'), '--run', str(tmp / 'run.json'),
                        '--scenarios', str(ex / 'run' / 'scenarios.json'), '--candidates', str(ex / 'run' / 'candidates.json')],
                       capture_output=True, text=True, encoding='utf-8')
    check('a partial run.json is rejected', p.returncode == 1 and 'partial' in p.stdout, (p.stdout.strip().splitlines() or [''])[-1])


# ---------------------------------------------------------------- browser checks (--browser; needs Playwright with Chromium)
STUB_PAGE = b"""<!doctype html><html><head><title>Bike shop</title></head><body><main id="app"><h1>Bike shop</h1>
<a id="two" href="/two">Second page</a> <button id="load">Load bikes</button><div id="out"></div></main>
<script>document.getElementById('load').onclick = async () => { const o = document.getElementById('out');
  try { const r = await fetch('/api/bikes'); if (!r.ok) throw new Error(r.status); const j = await r.json();
    o.innerHTML = '<p class="ready">' + j.items.length + ' bikes</p>'; }
  catch (e) { o.innerHTML = '<p role="alert">Could not load the bikes</p>'; } };
if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js').catch(() => {});</script></body></html>"""
# keeps the second page for offline use, answers navigations from the network or, offline, from that cache; no
# clients.claim, so the first page is not controlled until a reload (the runner's warm-up reloads it)
STUB_SW = b"""self.addEventListener('install', (e) => { e.waitUntil(caches.open('pages-v1').then((c) => c.addAll(['/two']))); self.skipWaiting(); });
self.addEventListener('fetch', (e) => { if (e.request.mode === 'navigate') {
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request, {ignoreSearch: true}))); } });"""


def browser_check(port: int, keep: Path | None) -> None:
    """The runner against a loopback stub: the page's document hangs on the reload after S01's recovery (the reload's
    navigation times out), and S02 goes offline on a link to a full page. The run must finish, record the reload's
    failure, and record the browser's offline page as what the user saw."""
    import threading
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    state = {'api': 0, 'hung': False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            path = self.path.split('?')[0]
            if path == '/api/bikes':
                state['api'] += 1
                body, ctype = b'{"items": [1, 2, 3]}', 'application/json'
            elif path == '/sw.js':
                body, ctype = STUB_SW, 'application/javascript'
            elif path == '/two':
                body, ctype = b'<!doctype html><title>Two</title><h1>Second page</h1><p class="two">Rides</p>', 'text/html'
            elif path == '/':
                if state['api'] >= 3 and not state['hung']:   # the reload after S01's recovery: never answered
                    state['hung'] = True
                    time.sleep(400)
                    return
                body, ctype = STUB_PAGE, 'text/html'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = keep or Path(tempfile.mkdtemp(prefix='rp-browser-'))
    d.mkdir(parents=True, exist_ok=True)
    flows = {'site': f'http://127.0.0.1:{port}/', 'app_root': '#app', 'nav_timeout_ms': 3000, 'run_timeout_s': 90,
             'flows': [{'name': 'shop', 'critical_path': True, 'steps': [
                 {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                 {'name': 'load', 'actions': [{'click': '#load'}], 'done': '.ready', 'done_not': ['[role=alert]']}]},
                       {'name': 'pages', 'critical_path': False, 'steps': [
                 {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                 {'name': 'second', 'actions': [{'click': '#two'}], 'done': '.two'}]},
                       {'name': 'returning', 'critical_path': False, 'service_workers': 'allow', 'steps': [
                 {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                 {'name': 'second', 'actions': [{'click': '#two'}], 'done': '.two'}]},
                       {'name': 'history', 'critical_path': False, 'steps': [
                 {'name': 'open', 'actions': [{'goto': '/'}], 'done': 'h1'},
                 {'name': 'second', 'actions': [{'click': '#two'}], 'done': '.two'},
                 {'name': 'back', 'actions': [{'back': True}], 'done': '#load'}]}],
             'scenarios': [{'flow': 'shop', 'step': 'load', 'endpoint': '**/api/bikes', 'fault': 'http_500'},
                           {'flow': 'pages', 'step': 'second', 'endpoint': '**/two', 'fault': 'offline'},
                           {'flow': 'returning', 'step': 'second', 'endpoint': '**/two', 'fault': 'offline'},
                           {'flow': 'history', 'step': 'back', 'endpoint': '**/', 'fault': 'offline'}]}
    (d / 'flows.json').write_text(json.dumps(flows), encoding='utf-8')
    subprocess.run([sys.executable, str(HERE / 'plan_scenarios.py'), str(d / 'flows.json'), '--out', str(d / 'scenarios.json')], check=True,
                   capture_output=True)
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, str(HERE / 'run_resilience.py'), str(d / 'flows.json'), str(d / 'scenarios.json'), '--out', str(d),
                            '--runs', '1'], capture_output=True, text=True, timeout=240)
        code, err = p.returncode, p.stderr
    except subprocess.TimeoutExpired as e:
        code, err = None, f'still running after {e.timeout:.0f}s (hung)'
    took = time.monotonic() - t0
    srv.shutdown()
    run = json.loads((d / 'run.json').read_text(encoding='utf-8')) if (d / 'run.json').is_file() else {}
    check('browser: the run finishes although a reload never answers', code == 0 and run.get('run_status') == 'complete',
          f'exit {code} after {took:.0f}s: {err.strip().splitlines()[-1:] if err else ""}')
    sc = {s['id']: s for s in run.get('scenarios', [])}
    s1 = (sc.get('S01') or {}).get('runs') or [{}]
    rl = ((s1[0].get('steps') or [{}])[-1]).get('reload') or {}
    check('browser: the reload that timed out is recorded', 'Timeout' in str(rl.get('error')), f'{rl}')
    s2 = sc.get('S02') or {}
    ob = (((s2.get('runs') or [{}])[0].get('steps') or [{}])[-1]).get('observed') or {}
    check('browser: offline on a link records the browser\'s offline page', s2.get('status') == 'ran' and ob.get('browser_error_page') is True,
          f'{s2.get("status")} {s2.get("reason")} {ob.get("page_url")}')
    s3 = sc.get('S03') or {}
    r3 = (s3.get('runs') or [{}])[0]
    t3 = (r3.get('steps') or [{}])[-1]
    check('browser: service workers allowed for a flow: the worker controls the page (returning visitor)',
          (r3.get('service_worker') or {}).get('controlled') is True and (run.get('safety') or {}).get('service_workers') == 'allow',
          f'{r3.get("service_worker")} {(run.get("safety") or {}).get("service_workers")}')
    check('browser: offline navigation answered by the worker: ran, page shown from its cache', s3.get('status') == 'ran'
          and t3.get('completed_under_fault') is True and not (t3.get('observed') or {}).get('browser_error_page'),
          f'{s3.get("status")} {s3.get("reason")} completed {t3.get("completed_under_fault")}')
    s4 = sc.get('S04') or {}
    ob4 = (((s4.get('runs') or [{}])[0].get('steps') or [{}])[-1]).get('observed') or {}
    check('browser: a back step under offline ends on the browser\'s offline page', s4.get('status') == 'ran' and ob4.get('browser_error_page') is True,
          f'{s4.get("status")} {s4.get("reason")} {ob4.get("page_url")}')
    if run.get('run_status') == 'complete':
        subprocess.run([sys.executable, str(HERE / 'classify.py'), str(d / 'run.json'), str(d / 'scenarios.json'), str(d / 'flows.json'),
                        '--out', str(d / 'candidates.json')], capture_output=True, text=True)
        cands = json.loads((d / 'candidates.json').read_text(encoding='utf-8')) if (d / 'candidates.json').is_file() else {}
        effs = [(k['scenarios'], k['effect']) for k in cands.get('candidates', [])]
        kinds = {(n.get('scenario'), n['kind']) for n in cands.get('notes', [])}
        check('browser: classify rates nothing on the app for the offline pages and the worker\'s answer', not effs
              and {('S02', 'browser_error_page'), ('S03', 'served_by_service_worker'), ('S04', 'browser_error_page')} <= kinds,
              f'candidates {effs}, notes {sorted(kinds, key=str)}')
    if not keep:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if '--browser' in sys.argv:
        i = sys.argv.index('--browser')
        port = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 8651
        keep = Path(sys.argv[sys.argv.index('--keep') + 1]) if '--keep' in sys.argv else None
        browser_check(port, keep)
        print(f'{"FAILED" if FAILS else "passed"}: {len(FAILS)} failing check(s)')
        return 1 if FAILS else 0
    for t in (test_document_fault, test_storm, test_fill_only_recovery, test_surrogates, test_read_shown_as_done, test_done_not,
              test_persisted_failure, test_blank_baseline, test_static_asset_incomplete, test_goto_recovery, test_confidence_cap,
              test_bounded_reads, test_browser_error_page, test_full_page_fallback, test_images_optional, test_offline_counting,
              test_back_action, test_service_worker_option, test_plan_estimate, test_incremental_and_resume, test_watchdog,
              test_baseline_retry, test_untriggered_reason, test_prefetch_note, test_partial_run_rejected):
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
            except Exception as e:  # noqa: BLE001
                check(t.__name__, False, f'{type(e).__name__}: {e}')
    print(f'{"FAILED" if FAILS else "passed"}: {len(FAILS)} failing check(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
