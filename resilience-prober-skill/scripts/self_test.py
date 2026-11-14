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
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))

FAILS: list[str] = []


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
              requests=None) -> dict:
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
    import run_resilience as rr
    check('the runner reloads after recovery for failing faults by default', getattr(rr, 'reload_check', None) is not None and
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


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    for t in (test_document_fault, test_storm, test_fill_only_recovery, test_surrogates, test_read_shown_as_done, test_done_not,
              test_persisted_failure, test_blank_baseline, test_static_asset_incomplete, test_goto_recovery, test_confidence_cap):
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
            except Exception as e:  # noqa: BLE001
                check(t.__name__, False, f'{type(e).__name__}: {e}')
    print(f'{"FAILED" if FAILS else "passed"}: {len(FAILS)} failing check(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
