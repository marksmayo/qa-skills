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


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    for t in (test_document_fault, test_storm, test_fill_only_recovery, test_surrogates):
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
            except Exception as e:  # noqa: BLE001
                check(t.__name__, False, f'{type(e).__name__}: {e}')
    print(f'{"FAILED" if FAILS else "passed"}: {len(FAILS)} failing check(s)')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
