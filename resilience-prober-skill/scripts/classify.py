#!/usr/bin/env python3
"""Turn scenario runs into effects, merge scenarios with one root cause, and rate them (stdlib).

    python classify.py run/run.json run/scenarios.json flows.json --out run/candidates.json

Effects per run of a scenario (RUBRIC.md): crash_b1, crash_b2, blank_screen, chunk_failure, endless_loading,
silent_failure, misleading_state, lost_input, no_recovery, retry_storm; console_only is a note. Only runs
whose fault matched a request count (`fault_triggered`); a scenario with none is `not_triggered` and has no
effects. An effect is `reproduced` when every run of the scenario triggered the fault and showed it
(two runs or more), `observed` otherwise.

The indicators (spinner, loading, error, empty-state and boundary patterns) are the ones run.json recorded
for the flow, so a product's own wording set in flows.json is what classifies it.

Candidates merge scenarios with one root cause: the same effect at the same flow step and endpoint across
faults (one missing error handler behind a 500 and a truncated body), no_recovery across the endpoints of
one step (the screen has no way to retry), and retry_storm per endpoint. A retry storm counts requests per
distinct URL and query, with cache-busting parameters (`_`, `t`, `ts`, `cb`, a long number) dropped: a
wildcard pattern matching many different files, or type-ahead queries for different terms, is not a storm,
while the same request repeated (cache-busted or not) is. Requests the page aborted itself (an
AbortController cancelling a superseded query) are not counted.

A fault that answered the page's own HTML document (a navigation request, or a scenario narrowed to
`resource_type: document`) runs no app code: the page shows the fault catalogue's injected body, and in
production it would show the server's or edge's error page. Such a run yields a `document_fault` note
(the request and what was shown), never an effect rated on the app. A run whose recovery repeated a step
that had nothing left to repeat (its only actions typed input, which recovery doesn't re-type, from an
older runner) yields a `recovery_not_tried` note instead of no_recovery.

The output also carries `baselines`: per flow, the status and, for a failed or unstable flow, the step that
failed, what the page showed and the screenshot (validate_resilience.py compares the report with it).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

SKILL = Path(__file__).resolve().parent.parent
IMPACT = {'crash_b1': ('Fatal', 4), 'crash_b2': ('Fatal', 4), 'blank_screen': ('Fatal', 4), 'endless_loading': ('Stuck', 3),
          'lost_input': ('Stuck', 3), 'no_recovery': ('Stuck', 3), 'chunk_failure': ('Stuck', 3), 'misleading_state': ('Misleading', 3),
          'silent_failure_write': ('Misleading', 3), 'silent_failure': ('Friction', 2), 'retry_storm': ('Friction', 2)}
# fault groups where the request fails, so the user should be told; static assets have their own rule (chunk_failure,
# blank_screen) because a kept screen is the expected outcome for them
FAILING = {'server_error', 'rate_limit', 'auth_expiry', 'timeout', 'connection', 'offline', 'payload', 'partial'}


def default_indicators() -> dict:
    ind = json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))
    for k in ('loading_words', 'boundary_selectors'):
        ind.setdefault(k, [])
    return ind


def derive_baselines(run: dict) -> dict:
    """What the report's `baseline` section must say, from run.json."""
    out = {}
    for name, b in run['baselines'].items():
        e = {'status': b['status'], 'critical_path': bool(b.get('critical_path')), 'runs': len(b['runs'])}
        bad = [r for r in b['runs'] if not r['ok']]
        if bad:
            r = bad[0]
            st = r['steps'][-1] if r['steps'] else None
            ob = (st or {}).get('observed') or {}
            e.update({'failed_step': st['name'] if st else None, 'error': r.get('error') or (st or {}).get('error'),
                      'shown': {'fallback_words': ob.get('fallback_words', []), 'boundary_element': ob.get('boundary_element', False),
                                'error_words': ob.get('error_words', []), 'visible_chars': ob.get('body_len')},
                      'page_errors': r['page_errors'][:3],
                      'screenshots': sorted({x['screenshot'] for rr in bad for x in rr['steps'] if x.get('screenshot')})})
        out[name] = e
    return out


CACHE_BUSTERS = {'_', 't', 'ts', 'cb', 'nocache', 'cachebust', 'cache_bust', 'rand', 'r', '_t', 'timestamp', 'v'}
LONG_NUMBER = re.compile(r'^\d{8,}$')


def storm_key(url: str) -> str:
    """The URL a retry would repeat: path and query, without cache-busting parameters (so a query with other terms is another request)."""
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in CACHE_BUSTERS and not LONG_NUMBER.match(v)]
    return p._replace(query=urlencode(sorted(q)), fragment='').geturl()


def recovery_actions(step: dict) -> list[dict]:
    """What recovery repeats: the step's actions without re-typing input; a step that only types gets its input re-sent as typed."""
    rest = [a for a in step.get('actions', []) if 'fill' not in a]
    return rest or [{'refill': a['fill']} for a in step.get('actions', []) if 'fill' in a]


def self_aborted(q: dict) -> bool:
    return not q.get('injected') and 'ABORTED' in str(q.get('failure') or '').upper()


def storm(reqs: list[dict], budgets: dict) -> dict | None:
    by_url: dict[str, list[dict]] = {}
    for q in reqs:
        if not self_aborted(q):
            by_url.setdefault(storm_key(q['url']), []).append(q)
    if not by_url:
        return None
    url, same = max(by_url.items(), key=lambda kv: len(kv[1]))
    span = max(1, same[-1]['t_ms'] - same[0]['t_ms'])
    early = [(a, b) for a, b in zip(same, same[1:]) if a.get('retry_after') and (b['t_ms'] - a['t_ms']) < float(a['retry_after']) * 1000]
    gaps = [b['t_ms'] - a['t_ms'] for a, b in zip(same, same[1:])]
    growing = len(gaps) >= 3 and all(y > x for x, y in zip(gaps, gaps[1:]))
    if len(same) > budgets['retry_storm_requests'] or (early and len(same) > 2 and not growing):
        return {'url': url, 'requests': len(same), 'distinct_urls': len(by_url), 'span_ms': span, 'gaps_ms': gaps[:12],
                'retry_after_ignored': len(early), 'growing_gaps': growing}
    return None


def effects(scen: dict, run: dict, base_step: dict, ind: dict, budgets: dict, step_def: dict | None = None) -> tuple[list[dict], list[dict]]:
    st = run['steps'][-1]
    ob = st['observed']
    out, notes = [], []
    docs = [q for q in run['requests'] if q.get('injected') and (q.get('navigation') or q.get('resource_type') == 'document')]
    if docs or scen.get('resource_type') == 'document':
        where = docs[0]['url'] if docs else scen['endpoint']
        notes.append({'kind': 'document_fault',
                      'detail': [f'the fault answered the page\'s own document ({where}), so no app code ran: the page showed the injected '
                                 f'{scen["fault"]} body ({ob["body_len"]} visible characters); users would see the server\'s or edge\'s error '
                                 f'page, which this run does not test']})
        return out, notes
    new_errors = [e for e in run['page_errors']]
    boundary = bool(ob.get('boundary_element')) and not base_step['observed'].get('boundary_element')
    crash = bool(ob['fallback_words']) or boundary or (bool(new_errors) and ob['root_len'] == 0)
    if crash:
        tier = 'crash_b1' if ob['body_len'] < 20 else 'crash_b2'
        out.append({'effect': tier, 'measure': {'fallback_words': ob['fallback_words'], 'boundary_element': bool(ob.get('boundary_element')),
                                                'page_errors': new_errors[:3], 'root_len': ob['root_len']}})
    elif ob['body_len'] < 20:
        out.append({'effect': 'blank_screen', 'measure': {'visible_chars': ob['body_len']}})
    elif scen['group'] == 'static_asset' and ob['root_len'] < 20:
        out.append({'effect': 'chunk_failure', 'measure': {'app_root_chars': ob['root_len'], 'request': (run['requests'] or [{}])[0]}})
    has_error = bool(st['first_error_ms'] is not None or ob['error_element'] or set(ob['error_words']) - set(base_step['observed']['error_words']))
    if ob['spinner'] and st['watched_ms'] >= budgets['spinner_ms'] and not has_error:
        out.append({'effect': 'endless_loading', 'measure': {'spinner_visible_after_ms': st['watched_ms'], 'loading_words': ob.get('loading_words', [])}})
    base_text = base_step['observed']['text']
    new_empty = [w for w in ob['empty_words'] if w not in base_text.lower()]
    if not crash and scen['group'] in FAILING and not has_error:
        gone = [x for x in base_step['observed'].get('controls', []) if x not in ob.get('controls', [])]
        if new_empty:
            out.append({'effect': 'misleading_state', 'measure': {'empty_state_words': new_empty, 'baseline_excerpt': base_text[:120],
                                                                 'shown_excerpt': ob['text'][:120]}})
        elif scen['write'] and st['completed_under_fault']:
            out.append({'effect': 'misleading_state', 'measure': {'shown_as_done': True}})
        elif (not any(e['effect'] in ('endless_loading', 'blank_screen', 'chunk_failure') for e in out)
              and (scen['write'] or not st['completed_under_fault'] or gone)):
            # a read whose step still completed, with every control the baseline showed, reached the user: no silent failure
            out.append({'effect': 'silent_failure_write' if scen['write'] else 'silent_failure',
                        'measure': {'message_budget_ms': budgets['message_ms'], 'watched_ms': st['watched_ms'], 'error_shown': False,
                                    'controls_gone': gone[:10]}})
    for field, before in (st['inputs'].get('before') or {}).items():
        after = (st['inputs'].get('after_fault') or {}).get(field) or {}
        if before.get('length') and (after.get('length') == 0 or after.get('sha256') != before.get('sha256')):
            out.append({'effect': 'lost_input', 'measure': {'field': field, 'before': before, 'after': after}})
    if not st['recovered'] and scen['group'] != 'latency' and st.get('recovery') == 'repeat_step' and step_def is not None \
            and not [a for a in step_def.get('actions', []) if 'fill' not in a]:
        notes.append({'kind': 'recovery_not_tried', 'detail': ['the step only types input, and recovery repeated it without re-typing, '
                                                                'so nothing was sent again: whether the screen recovers is unknown']})
    elif not st['recovered'] and scen['group'] != 'latency':
        out.append({'effect': 'no_recovery', 'measure': {'recovery': st['recovery'], 'retry_controls': st['retry_controls']}})
    if run['requests']:
        m = storm(run['requests'], budgets)
        if m:
            out.append({'effect': 'retry_storm', 'measure': m})
    base_console = set(base_step.get('console_errors') or [])
    if not out and set(run['console_errors']) - base_console:
        notes.append({'kind': 'console_only', 'detail': sorted(set(run['console_errors']) - base_console)[:3]})
    return out, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', type=Path)
    ap.add_argument('scenarios', type=Path)
    ap.add_argument('flows', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run, plan, cfg = (json.loads(p.read_text(encoding='utf-8')) for p in (a.run, a.scenarios, a.flows))
    fallback = default_indicators()
    budgets = {**fallback['budgets'], **plan.get('budgets', {})}
    flows = {f['name']: f for f in cfg['flows']}
    by_id = {s['id']: s for s in plan['scenarios']}
    per_scenario, notes, groups = [], [], {}
    for entry in run['scenarios']:
        s = by_id[entry['id']]
        if entry['status'] != 'ran':
            per_scenario.append({'id': s['id'], 'status': entry['status'], 'effects': [], 'reason': entry.get('reason')})
            continue
        ind = (run.get('indicators') or {}).get(s['flow']) or fallback
        idx = next(i for i, st in enumerate(flows[s['flow']]['steps']) if st['name'] == s['step'])
        base_run = run['baselines'][s['flow']]['runs'][0]
        base_step = dict(base_run['steps'][idx], console_errors=base_run['console_errors'])
        seen: dict[str, list] = {}
        live = [r for r in entry['runs'] if r.get('fault_triggered', True)]
        for r in live:
            effs, ns = effects(s, r, base_step, ind, budgets, flows[s['flow']]['steps'][idx])
            notes += [dict(n, scenario=s['id']) for n in ns
                      if n['kind'] == 'console_only' or not any(m['kind'] == n['kind'] and m.get('scenario') == s['id'] for m in notes)]
            for e in effs:
                seen.setdefault(e['effect'], []).append({'run': r['tag'], 'measure': e['measure'], 'screenshots': r['steps'][-1]['screenshots'],
                                                         'requests': len(r['requests']), 'faulted_requests': r.get('faulted_requests')})
        n_runs = len(entry['runs'])
        effs = [{'effect': k, 'confidence': 'reproduced' if len(v) == n_runs and n_runs >= 2 else 'observed', 'runs': v} for k, v in seen.items()]
        per_scenario.append({'id': s['id'], 'status': 'ran', 'effects': [e['effect'] for e in effs],
                             'runs_triggered': len(live), 'runs': n_runs})
        for e in effs:
            eff = e['effect']
            if eff == 'no_recovery':
                key = (eff, s['flow'], s['step'])
            elif eff == 'retry_storm':
                key = (eff, s['endpoint'])
            else:
                key = (eff, s['flow'], s['step'], s['endpoint'])
            g = groups.setdefault(key, {'effect': eff, 'flow': s['flow'], 'step': s['step'], 'endpoints': [], 'faults': [], 'scenarios': [],
                                        'write': s['write'], 'critical_path': s['critical_path'], 'likelihood': 0, 'evidence': [], 'confidence': []})
            for k, v in (('endpoints', s['endpoint']), ('faults', s['fault']), ('scenarios', s['id'])):
                if v not in g[k]:
                    g[k].append(v)
            g['likelihood'] = max(g['likelihood'], s['likelihood'])
            g['evidence'].append({'scenario': s['id'], **e['runs'][0]})
            g['confidence'].append(e['confidence'])
    for name, b in run['baselines'].items():
        for msg in sorted({m for r in b['runs'] for m in r['console_errors']}):
            notes.append({'kind': 'console_only', 'detail': [msg], 'scenario': None, 'flow': name, 'in_baseline': True})
    cands = []
    for g in groups.values():
        name, weight = IMPACT[g['effect']]
        reach = 3 if g['critical_path'] else 2
        score = weight * g['likelihood'] * reach
        prio = 'P0' if score >= 27 else 'P1' if score >= 18 else 'P2' if score >= 9 else 'P3'
        conf = 'reproduced' if 'reproduced' in g['confidence'] else 'observed'
        floors = []
        if g['effect'] in ('lost_input', 'misleading_state') and g['write'] and prio in ('P2', 'P3'):
            prio = 'P1'
            floors.append('lost input or a misleading result on a write is at least P1')
        if g['effect'] == 'retry_storm' and any(f in ('http_503_retry_after', 'http_429_retry_after', 'http_429') for f in g['faults']) and prio == 'P3':
            prio = 'P2'
            floors.append('a retry storm against a 429 or 503 is at least P2')
        if conf != 'reproduced' and prio in ('P0', 'P1'):
            prio = 'P2'
            floors.append('not reproduced on a second run: capped at P2')
        cands.append({'effect': g['effect'].replace('silent_failure_write', 'silent_failure'), 'on_write': g['write'],
                      'impact': name, 'impact_weight': weight, 'likelihood': g['likelihood'], 'reach': reach, 'score': score, 'priority': prio,
                      'floors': floors, 'confidence': conf, 'flow': g['flow'], 'step': g['step'], 'endpoints': g['endpoints'], 'faults': g['faults'],
                      'scenarios': g['scenarios'], 'critical_path': g['critical_path'], 'evidence': g['evidence']})
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    cands.sort(key=lambda c: (order[c['priority']], -c['score'], c['flow'], c['effect']))
    for i, c in enumerate(cands, 1):
        c['id'] = f'K{i}'
    cands = [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id'}} for c in cands]
    out = {'baselines': derive_baselines(run), 'scenarios': per_scenario, 'candidates': cands, 'notes': notes}
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(cands)} candidate(s), {len(notes)} note(s) -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
