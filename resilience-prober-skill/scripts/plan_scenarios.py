#!/usr/bin/env python3
"""Plan fault scenarios and write each one's expected outcome before anything runs.

    python plan_scenarios.py flows.json --out run/scenarios.json [--plan-only] [--runs 2] [--repo <app source>]

flows.json lists the flows (named steps with actions, a `done` selector, `input` fields, `write`) and the
scenarios to run, either explicitly ({"flow", "step", "endpoint", "fault"} plus optional "method",
"headers" ({name: value}, true for present, false for absent: e.g. {"RSC": "1"} for a Next.js server
component payload, {"Next-Router-Prefetch": false} for a non-prefetch request), "resource_type" (fetch,
document, script ...) and "from": "load" (install the fault before the page loads, for requests a framework
sends at load such as prefetches)) or as {"auto": {"endpoints": {step: [patterns]}, "faults": [...]}},
which crosses every listed fault with each step's endpoints. Duplicate scenarios are dropped.

`unreachable` in flows.json lists data the browser cannot reach ({"flow"?, "step"?, "what", "reason"}),
such as data a server component fetches on the server: browser-side faults reach only the payload that
carries its result. It is copied into the plan so the report can say what was not testable.

Each scenario gets an `expected` outcome written now, from the step (read or write) and the fault group,
with the budgets: the bar a run is held to can't move after the result is known. `planned_at` records when.
--plan-only prints the count, the scenarios and the estimate without writing.

`estimate` (in the plan and in --plan-only's output) counts the runs (two baselines per flow, --runs per
scenario), the page loads they make (the first load, each goto, back, forward and reload, and the reload
after recovery for reads under a failing fault) and the requests at `requests_per_load` per load (flows.json,
default 60; a modern app's page load is 50-80 requests), against `request_budget`: `over_budget` true means
the plan will not finish within it.

--repo reads the app's source with next_app.py (a Next.js project): the service workers it registers or
ships (`service_workers` in the plan: worker URL, file, registrations, and its fetch strategies such as
navigation: network-only), so a run that blocks them (the default) says so, and offline navigation with the
worker (`service_workers: "allow"` in flows.json) can be planned.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
SCENARIO_KEYS = {'flow', 'step', 'endpoint', 'fault', 'method', 'headers', 'resource_type', 'from', 'note'}


def expected(step: dict, fault: dict, budgets: dict) -> str:
    msg_s = budgets['message_ms'] // 1000
    spin_s = budgets['spinner_ms'] // 1000
    g = fault['group']
    if g == 'latency':
        return f'The step completes once the response arrives; any spinner stops, and no error is shown for a slow but successful response.'
    if g == 'static_asset':
        return f'The screen keeps its content or shows an error within {msg_s} s; no blank screen.'
    if g == 'flapping':
        return 'The app retries and the step completes after the failures, or it shows an error with a retry control that recovers.'
    base = (f'Within {msg_s} s the user sees an error message (role=alert or error wording) instead of a spinner running past {spin_s} s; '
            'no blank or crashed screen; data is never shown as empty because of the failure; ')
    if step.get('write'):
        base += 'the typed input is kept; the action is never shown as done; '
    if g in ('server_error', 'rate_limit') and fault.get('headers', {}).get('Retry-After'):
        base += f'no more than {budgets["retry_storm_requests"]} requests in {budgets["retry_window_ms"] // 1000} s and none before Retry-After; '
    return base + 'after the fault is removed the step completes without a full reload.'


def loads(flow: dict, upto: int | None) -> int:
    """Page loads one run of a flow makes, up to a step: the first load, then every goto (not a #hash) and history step."""
    n = 1
    for st in flow['steps'][: (upto + 1) if upto is not None else None]:
        n += sum(1 for a in st.get('actions', []) if ('goto' in a and not str(a['goto']).startswith('#'))
                 or any(a.get(k) for k in ('back', 'forward', 'reload')))
    return n


def estimate(cfg: dict, flows: dict, scenarios: list[dict], runs: int) -> dict:
    """Runs, page loads and requests the plan will make, against request_budget (rough: requests_per_load per load)."""
    per = int(cfg.get('requests_per_load') or 60)
    reload_after = cfg.get('reload_after_recovery', True) is not False
    n_runs = 2 * len(flows) + runs * len(scenarios)
    pl = sum(2 * loads(f, None) for f in flows.values())
    for s in scenarios:
        f = flows[s['flow']]
        upto = next(i for i, st in enumerate(f['steps']) if st['name'] == s['step'])
        one = loads(f, upto)
        if reload_after and s['group'] != 'latency' and not s['write']:
            one += loads(f, upto)        # the reload after recovery runs the flow to the step again
        pl += runs * one
    budget = cfg.get('request_budget')
    return {'runs': n_runs, 'page_loads': pl, 'requests_per_load': per, 'requests': pl * per, 'request_budget': budget,
            'over_budget': (pl * per > budget) if budget is not None else None,
            'note': 'rough: page loads times requests_per_load; a baseline run prints the real count per run'}


def service_workers(repo: Path) -> list[dict]:
    """The service workers a Next.js project registers or ships (next_app.py), for the plan."""
    from next_app import read_next_app
    out = []
    for pr in read_next_app(repo).get('projects') or []:
        for w in pr.get('service_workers') or []:
            out.append({'url': w.get('url'), 'worker_file': ((pr.get('root') + '/') if pr.get('root') else '') + (w.get('worker_file') or '')
                        if w.get('worker_file') else None, 'registered': bool(w.get('registrations')),
                        'registrations': [{'file': r.get('file'), 'line': r.get('line')} for r in w.get('registrations') or []],
                        'fetch_strategies': [{'when': x.get('when'), 'strategy': x.get('strategy'), 'line': x.get('line')}
                                             for x in w.get('fetch_strategies') or []],
                        'generated_by': w.get('generated_by')})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--plan-only', action='store_true')
    ap.add_argument('--runs', type=int, default=2, help='runs per scenario the run will use (for the estimate)')
    ap.add_argument('--repo', type=Path, help='the app\'s source: list the service workers a Next.js project registers')
    a = ap.parse_args()
    cfg = json.loads(a.flows.read_text(encoding='utf-8'))
    cat = json.loads((SKILL / 'data' / 'faults.json').read_text(encoding='utf-8'))
    budgets = (json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))['budgets'] | ((cfg.get('indicators') or {}).get('budgets') or {})
               | (cfg.get('budgets') or {}))
    flows = {f['name']: f for f in cfg['flows']}
    unreachable = []
    for u in cfg.get('unreachable') or []:
        if u.get('flow') and u['flow'] not in flows:
            raise SystemExit(f'unreachable: no flow {u["flow"]!r}')
        if len((u.get('reason') or '').strip()) < 10 or not (u.get('what') or '').strip():
            raise SystemExit('unreachable: each entry needs "what" and a "reason" (why a browser-side fault cannot reach it)')
        unreachable.append({'flow': u.get('flow'), 'step': u.get('step'), 'what': u['what'], 'reason': u['reason']})
    specs = cfg['scenarios']
    if isinstance(specs, dict) and 'auto' in specs:
        auto, specs = specs['auto'], []
        for f in cfg['flows']:
            for st in f['steps']:
                for ep in auto['endpoints'].get(f'{f["name"]}/{st["name"]}', []):
                    for fault in auto.get('faults') or cat['standard_set']:
                        specs.append({'flow': f['name'], 'step': st['name'], 'endpoint': ep, 'fault': fault})
    seen, out = set(), []
    for s in specs:
        k = (s['flow'], s['step'], s['endpoint'], s['fault'], s.get('method'), json.dumps(s.get('headers'), sort_keys=True), s.get('from'))
        if k in seen:
            continue
        seen.add(k)
        flow = flows.get(s['flow'])
        step = next((st for st in (flow or {}).get('steps', []) if st['name'] == s['step']), None)
        if step is None or s['fault'] not in cat['faults']:
            raise SystemExit(f'scenario {s}: unknown flow, step or fault')
        if set(s) - SCENARIO_KEYS:
            raise SystemExit(f'scenario {s}: unknown field(s) {sorted(set(s) - SCENARIO_KEYS)}')
        fault = cat['faults'][s['fault']]
        out.append({'id': f'S{len(out) + 1:02d}', **s, 'group': fault['group'], 'likelihood': cat['groups'][fault['group']]['likelihood'],
                    'critical_path': bool(flow.get('critical_path')), 'write': bool(step.get('write') or flow.get('write') and step.get('input')),
                    'expected': expected(dict(step, write=bool(step.get('write') or (flow.get('write') and step.get('input')))), fault, budgets)})
    doc = {'planned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'budgets': budgets, 'scenarios': out, 'unreachable': unreachable,
           'estimate': estimate(cfg, flows, out, a.runs)}
    if a.repo:
        sws = service_workers(a.repo)
        doc['service_workers'] = sws
        blocked = [n for n, f in flows.items() if (f.get('service_workers') or cfg.get('service_workers') or 'block') != 'allow']
        if sws and blocked:
            print(f'warning: the app ships {len(sws)} service worker(s) ({", ".join(str(w["url"]) for w in sws)}) and service_workers is '
                  f'block for {", ".join(blocked)}: returning visitors and offline navigation are not tested. Set "service_workers": '
                  '"allow" (in flows.json or on a flow) and add offline scenarios on navigations to test them', file=sys.stderr)
    est = doc['estimate']
    if est['over_budget']:
        print(f'warning: about {est["requests"]} requests ({est["page_loads"]} page loads) against a request_budget of '
              f'{est["request_budget"]}: the plan will not finish within it', file=sys.stderr)
    if a.plan_only or not a.out:
        print(json.dumps({'count': len(out), 'scenarios': [f'{s["id"]} {s["flow"]}/{s["step"]} {s["fault"]} {s["endpoint"]}' for s in out],
                          'estimate': est, **({'service_workers': doc['service_workers']} if 'service_workers' in doc else {})}, indent=1))
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'{len(out)} scenario(s) planned -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
