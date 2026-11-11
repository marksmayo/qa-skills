#!/usr/bin/env python3
"""Generate the test design rows each technique calls for, from the agent's model (stdlib only).

    python design_rows.py model.json --out rows.json

Per criterion and technique in model.json:
  boundary          for each limit: the value just outside, on, and just inside it (step from the model),
                    so every limit is tested from both sides
  partition         one row per class (valid and invalid), with its representative value
  decision_table    every combination of the conditions' values (the full table); impossible
                    combinations are marked infeasible in the model, with a reason
  pairwise          rows covering every pair of values of every two parameters (greedy, deterministic),
                    honouring the model's constraints; strength 2
  state_transition  one row per valid transition, and one per event that is not allowed in a state
  scenario          one row per flow (the main flow and each alternative)
Rows are deterministic: the same model always gives the same rows and ids. Cases cover rows by id.
"""
from __future__ import annotations
import argparse, itertools, json, sys
from decimal import Decimal
from pathlib import Path

ABBR = {'boundary': 'BVA', 'partition': 'EP', 'decision_table': 'DT', 'pairwise': 'PW', 'state_transition': 'ST', 'scenario': 'SC'}


def q(v, step) -> float:
    d = Decimal(str(step))
    return float((Decimal(str(v))).quantize(d)) if d < 1 else int(v) if float(v).is_integer() else float(v)


def fmt(x, step) -> str:
    places = max(0, -Decimal(str(step)).as_tuple().exponent)
    return f'{x:.{places}f}'


def boundary(m: dict) -> list[dict]:
    step = m.get('step', 1)
    rows = []
    for lim in m['limits']:
        v, k = lim['value'], lim['kind']
        below, above = q(Decimal(str(v)) - Decimal(str(step)), step), q(Decimal(str(v)) + Decimal(str(step)), step)
        for side, x in (('below', below), ('on', q(v, step)), ('above', above)):
            if k.startswith('min'):
                valid = x >= v if k == 'min_inclusive' else x > v
            else:
                valid = x <= v if k == 'max_inclusive' else x < v
            rows.append({'values': {m['variable']: x}, 'limit': v, 'limit_kind': k, 'side': side, 'in_range': valid,
                         'label': f'{m["variable"]} = {m.get("unit", "")}{fmt(x, step)} ({side} the {k.replace("_", " ")} limit {m.get("unit", "")}{fmt(v, step)})'})
    return rows


def partition(m: dict) -> list[dict]:
    return [{'values': {m['variable']: c['representative']}, 'class': c['id'], 'valid': c.get('valid', True),
             'label': f'{c["description"]} (e.g. {m.get("unit", "")}{c["representative"]})'} for c in m['classes']]


def decision_table(m: dict) -> list[dict]:
    names = [c['name'] for c in m['conditions']]
    return [{'values': dict(zip(names, combo)), 'label': ', '.join(f'{n} = {v}' for n, v in zip(names, combo))}
            for combo in itertools.product(*[c['values'] for c in m['conditions']])]


def allowed(row: dict, constraints: list[dict]) -> bool:
    return not any(all(row.get(k) == v for k, v in c['not'].items()) for c in constraints)


def pairwise(m: dict) -> list[dict]:
    params = m['parameters']
    names = [p['name'] for p in params]
    cons = m.get('constraints', [])
    need = set()
    for (i, a), (j, b) in itertools.combinations(enumerate(params), 2):
        for va in a['values']:
            for vb in b['values']:
                if allowed({a['name']: va, b['name']: vb}, cons):
                    need.add((i, va, j, vb))

    def gain(row: dict) -> int:
        return sum(1 for (x, vx, y, vy) in need if row.get(names[x]) == vx and row.get(names[y]) == vy)

    space = 1
    for p in params:
        space *= len(p['values'])
    rows = []
    while need:
        if space <= 5000:
            # small spaces: the full combination that covers the most uncovered pairs (first in order on ties)
            best = max((dict(zip(names, combo)) for combo in itertools.product(*[p['values'] for p in params])
                        if allowed(dict(zip(names, combo)), cons)), key=gain)
            row = best
        else:
            i, va, j, vb = sorted(need, key=lambda t: (t[0], params[t[0]]['values'].index(t[1]), t[2], params[t[2]]['values'].index(t[3])))[0]
            row = {names[i]: va, names[j]: vb}
            for k, p in enumerate(params):
                if p['name'] in row:
                    continue
                cands = [v for v in p['values'] if allowed(dict(row, **{p['name']: v}), cons)] or [p['values'][0]]
                row[p['name']] = max(cands, key=lambda v: gain(dict(row, **{p['name']: v})))
        covered = {(x, vx, y, vy) for (x, vx, y, vy) in need if row.get(names[x]) == vx and row.get(names[y]) == vy}
        if not covered:
            break
        need -= covered
        rows.append({'values': {n: row[n] for n in names}, 'label': ', '.join(f'{n} = {row[n]}' for n in names)})
    return rows


def state_transition(m: dict) -> list[dict]:
    rows = []
    valid = {(t['from'], t['event']) for t in m['transitions']}
    for t in m['transitions']:
        rows.append({'values': {'state': t['from'], 'event': t['event']}, 'to': t['to'], 'valid': True,
                     'label': f'{t["from"]} --{t["event"]}--> {t["to"]}'})
    for s in m['states']:
        for ev in m['events']:
            if (s, ev) not in valid:
                rows.append({'values': {'state': s, 'event': ev}, 'to': s, 'valid': False, 'label': f'{ev} is not allowed when {s} (stays {s})'})
    return rows


def scenario(m: dict) -> list[dict]:
    return [{'values': {'flow': f['id']}, 'label': f['name'], 'steps': f.get('steps', [])} for f in m['flows']]


GEN = {'boundary': boundary, 'partition': partition, 'decision_table': decision_table, 'pairwise': pairwise,
       'state_transition': state_transition, 'scenario': scenario}


def generate(model: dict) -> list[dict]:
    out = []
    for c in model['criteria']:
        for t in c['techniques']:
            for n, r in enumerate(GEN[t['technique']](t['model']), 1):
                out.append(dict(id=f'{c["id"]}-{ABBR[t["technique"]]}-{n:02d}', criterion=c['id'], technique=t['technique'], **r))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('model', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    model = json.loads(a.model.read_text(encoding='utf-8'))
    rows = generate(model)
    a.out.write_text(json.dumps({'ticket': model['ticket'], 'rows': rows}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    by: dict = {}
    for r in rows:
        by.setdefault((r['criterion'], r['technique']), 0)
        by[(r['criterion'], r['technique'])] += 1
    print(f'{len(rows)} row(s) -> {a.out}: ' + ', '.join(f'{c} {t} {n}' for (c, t), n in by.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
