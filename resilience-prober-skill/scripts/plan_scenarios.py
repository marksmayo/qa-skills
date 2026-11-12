#!/usr/bin/env python3
"""Plan fault scenarios and write each one's expected outcome before anything runs.

    python plan_scenarios.py flows.json --out run/scenarios.json [--plan-only]

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
--plan-only prints the count and the scenarios without writing.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('flows', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--plan-only', action='store_true')
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
    doc = {'planned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'budgets': budgets, 'scenarios': out, 'unreachable': unreachable}
    if a.plan_only or not a.out:
        print(json.dumps({'count': len(out), 'scenarios': [f'{s["id"]} {s["flow"]}/{s["step"]} {s["fault"]} {s["endpoint"]}' for s in out]}, indent=1))
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    print(f'{len(out)} scenario(s) planned -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
