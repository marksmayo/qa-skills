#!/usr/bin/env python3
"""Turn scenario runs into effects, merge scenarios with one root cause, and rate them (stdlib).

    python classify.py run/run.json run/scenarios.json flows.json --out run/candidates.json

Effects per run of a scenario (RUBRIC.md): crash_b1, crash_b2, blank_screen, chunk_failure, endless_loading,
silent_failure, misleading_state, lost_input, no_recovery, retry_storm; console_only is a note. An effect is
`reproduced` when every run of the scenario shows it, `observed` when one does.

Candidates merge scenarios with one root cause: the same effect at the same flow step and endpoint across
faults (one missing error handler behind a 500 and a truncated body), no_recovery across the endpoints of
one step (the screen has no way to retry), and retry_storm per endpoint.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
IMPACT = {'crash_b1': ('Fatal', 4), 'crash_b2': ('Fatal', 4), 'blank_screen': ('Fatal', 4), 'endless_loading': ('Stuck', 3),
          'lost_input': ('Stuck', 3), 'no_recovery': ('Stuck', 3), 'chunk_failure': ('Stuck', 3), 'misleading_state': ('Misleading', 3),
          'silent_failure_write': ('Misleading', 3), 'silent_failure': ('Friction', 2), 'retry_storm': ('Friction', 2)}
FAILING = {'server_error', 'rate_limit', 'auth_expiry', 'timeout', 'connection', 'offline', 'payload', 'partial', 'static_asset'}


def effects(scen: dict, run: dict, base_step: dict, ind: dict, fault: dict) -> tuple[list[dict], list[dict]]:
    st = run['steps'][-1]
    ob = st['observed']
    out, notes = [], []
    new_errors = [e for e in run['page_errors']]
    crash = bool(ob['fallback_words']) or (bool(new_errors) and ob['root_len'] == 0)
    if crash:
        tier = 'crash_b1' if ob['body_len'] < 20 else 'crash_b2'
        out.append({'effect': tier, 'measure': {'fallback_words': ob['fallback_words'], 'page_errors': new_errors[:3], 'root_len': ob['root_len']}})
    elif ob['body_len'] < 20:
        out.append({'effect': 'blank_screen', 'measure': {'visible_chars': ob['body_len']}})
    elif scen['group'] == 'static_asset' and ob['root_len'] < 20:
        out.append({'effect': 'chunk_failure', 'measure': {'app_root_chars': ob['root_len'], 'request': (run['requests'] or [{}])[0]}})
    has_error = bool(st['first_error_ms'] is not None or ob['error_element'] or set(ob['error_words']) - set(base_step['observed']['error_words']))
    if ob['spinner'] and st['watched_ms'] >= ind['budgets']['spinner_ms'] and not has_error:
        out.append({'effect': 'endless_loading', 'measure': {'spinner_visible_after_ms': st['watched_ms']}})
    base_text = base_step['observed']['text']
    new_empty = [w for w in ob['empty_words'] if w not in base_text.lower()]
    if not crash and scen['group'] in FAILING and not has_error:
        if new_empty:
            out.append({'effect': 'misleading_state', 'measure': {'empty_state_words': new_empty, 'baseline_excerpt': base_text[:120],
                                                                 'shown_excerpt': ob['text'][:120]}})
        elif scen['write'] and st['completed_under_fault']:
            out.append({'effect': 'misleading_state', 'measure': {'shown_as_done': True}})
        elif not any(e['effect'] in ('endless_loading', 'blank_screen', 'chunk_failure') for e in out):
            out.append({'effect': 'silent_failure_write' if scen['write'] else 'silent_failure',
                        'measure': {'message_budget_ms': ind['budgets']['message_ms'], 'watched_ms': st['watched_ms'], 'error_shown': False}})
    for field, before in (st['inputs'].get('before') or {}).items():
        after = (st['inputs'].get('after_fault') or {}).get(field) or {}
        if before.get('length') and (after.get('length') == 0 or after.get('sha256') != before.get('sha256')):
            out.append({'effect': 'lost_input', 'measure': {'field': field, 'before': before, 'after': after}})
    if not st['recovered'] and scen['group'] != 'latency':
        out.append({'effect': 'no_recovery', 'measure': {'recovery': st['recovery'], 'retry_controls': st['retry_controls']}})
    reqs = run['requests']
    if reqs:
        span = max(1, reqs[-1]['t_ms'] - reqs[0]['t_ms'])
        early = [(a, b) for a, b in zip(reqs, reqs[1:]) if a.get('retry_after') and (b['t_ms'] - a['t_ms']) < float(a['retry_after']) * 1000]
        gaps = [b['t_ms'] - a['t_ms'] for a, b in zip(reqs, reqs[1:])]
        growing = len(gaps) >= 3 and all(y > x for x, y in zip(gaps, gaps[1:]))
        if len(reqs) > ind['budgets']['retry_storm_requests'] or (early and len(reqs) > 2 and not growing):
            out.append({'effect': 'retry_storm', 'measure': {'requests': len(reqs), 'span_ms': span, 'gaps_ms': gaps[:12],
                                                             'retry_after_ignored': len(early), 'growing_gaps': growing}})
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
    ind = json.loads((SKILL / 'data' / 'indicators.json').read_text(encoding='utf-8'))
    faults = json.loads((SKILL / 'data' / 'faults.json').read_text(encoding='utf-8'))['faults']
    flows = {f['name']: f for f in cfg['flows']}
    by_id = {s['id']: s for s in plan['scenarios']}
    per_scenario, notes, groups = [], [], {}
    for entry in run['scenarios']:
        s = by_id[entry['id']]
        if entry['status'] != 'ran':
            per_scenario.append({'id': s['id'], 'status': entry['status'], 'effects': [], 'reason': entry.get('reason')})
            continue
        idx = next(i for i, st in enumerate(flows[s['flow']]['steps']) if st['name'] == s['step'])
        base_run = run['baselines'][s['flow']]['runs'][0]
        base_step = dict(base_run['steps'][idx], console_errors=base_run['console_errors'])
        seen: dict[str, list] = {}
        for r in entry['runs']:
            effs, ns = effects(s, r, base_step, ind, faults[s['fault']])
            notes += [dict(n, scenario=s['id']) for n in ns]
            for e in effs:
                seen.setdefault(e['effect'], []).append({'run': r['tag'], 'measure': e['measure'], 'screenshots': r['steps'][-1]['screenshots'],
                                                         'requests': len(r['requests'])})
        n_runs = len(entry['runs'])
        effs = [{'effect': k, 'confidence': 'reproduced' if len(v) == n_runs and n_runs >= 2 else 'observed', 'runs': v} for k, v in seen.items()]
        per_scenario.append({'id': s['id'], 'status': 'ran', 'effects': [e['effect'] for e in effs]})
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
    out = {'scenarios': per_scenario, 'candidates': cands, 'notes': notes}
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(cands)} candidate(s), {len(notes)} note(s) -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
