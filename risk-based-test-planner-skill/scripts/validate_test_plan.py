#!/usr/bin/env python3
"""Validate a risk-based test plan against the schema, the scored risk file and the rubric (stdlib only).

    python validate_test_plan.py test_plan.json --risk risk.json [--input security=security_audit.json ...] [--write-summary]

Checks: every factor with a score cites evidence and a not_assessed factor has none; a judged
factor keeps its computed score and gives a reason; likelihood, impact, risk, band and
confidence recompute; every area matches risk.json except judged factors; every in-scope area is
planned or not tested (never both, never neither); test types fit the band; effort recomputes
from the budget; a High area is left untested only when the budget is short; residual bands
match; open findings keep the confidence they had in their source report (--input); no "safe",
"fully tested" or "risk-free". --write-summary fills in each area's computed fields, the effort
and the summary; the headline stays yours.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score_risk  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'test_plan.schema.json'
BANNED = re.compile(r'\b(is|are|will be|looks?) safe\b|\bsafe to (release|ship|deploy)\b|\bfully tested\b|\brisk[- ]free\b|'
                    r'\bguarantee[sd]?\b|\bno risk\b|\bnothing (can|will) (break|go wrong)\b|\bcompletely (tested|covered)\b|'
                    r'\b100% (tested|covered|coverage)\b', re.I)
LOW_TYPES = {'smoke', 'regression'}


def schema_errors(value, schema: dict, path: str = '$') -> list[str]:
    errs = []
    if 'enum' in schema and value not in schema['enum']:
        return [f'{path}: {value!r} is not one of {schema["enum"]}']
    t = schema.get('type')
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any((tt == 'object' and isinstance(value, dict)) or (tt == 'array' and isinstance(value, list)) or
                 (tt == 'string' and isinstance(value, str)) or (tt == 'boolean' and isinstance(value, bool)) or
                 (tt == 'integer' and isinstance(value, int) and not isinstance(value, bool)) or
                 (tt == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                 (tt == 'null' and value is None) for tt in types)
        if not ok:
            return [f'{path}: expected {"/".join(types)}, got {type(value).__name__}']
    if isinstance(value, str) and len(value) < schema.get('minLength', 0):
        errs.append(f'{path}: shorter than {schema["minLength"]} characters')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errs.append(f'{path}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            errs.append(f'{path}: above {schema["maximum"]}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errs.append(f'{path}: needs at least {schema["minItems"]} item(s)')
        for i, v in enumerate(value):
            errs += schema_errors(v, schema.get('items', {}), f'{path}[{i}]')
    if isinstance(value, dict):
        for k in schema.get('required', []):
            if k not in value:
                errs.append(f'{path}: missing {k!r}')
        props = schema.get('properties', {})
        extra = schema.get('additionalProperties', True)
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], f'{path}.{k}')
            elif isinstance(extra, dict):
                errs += schema_errors(v, extra, f'{path}.{k}')
            elif extra is False:
                errs.append(f'{path}: unexpected field {k!r}')
    return errs


def fill(plan: dict) -> None:
    for a in plan['areas']:
        a.update(score_risk.recompute(a))
    by = {a['id']: a for a in plan['areas']}
    planned = [{'id': s['area_id'], 'risk': by[s['area_id']]['risk'], 'band': by[s['area_id']]['band']}
               for s in plan['strategy'] if s['area_id'] in by]
    alloc, short, shares = score_risk.allocate(planned, plan['metadata']['budget_sessions'],
                                               len([a for a in plan['areas'] if a['band'] == 'High']))
    for s in plan['strategy']:
        if s['area_id'] in alloc:
            s['effort_sessions'] = alloc[s['area_id']]
            s['effort_share'] = shares[s['area_id']]
    plan['_short'] = short


def summary(plan: dict, short: int) -> dict:
    bands = [a['band'] for a in plan['areas']]
    sessions = [s['effort_sessions'] for s in plan['strategy']]
    return {'areas': len(plan['areas']), 'high': bands.count('High'), 'medium': bands.count('Medium'), 'low': bands.count('Low'),
            'planned': len(plan['strategy']), 'not_tested': len(plan['not_tested']),
            'sessions_allocated': None if plan['metadata']['budget_sessions'] is None else sum(x or 0 for x in sessions),
            'sessions_short': short}


def check(plan: dict, risk: dict, inputs: dict) -> list[str]:
    errs = []
    by = {a['id']: a for a in plan['areas']}
    rby = {a['id']: a for a in risk['areas']}
    if set(by) != set(rby):
        errs.append(f'areas: must be exactly the areas in the risk file {sorted(rby)}')
    for a in plan['areas']:
        w = f'areas[{a["id"]}]'
        for name, f in a['factors'].items():
            if f['status'] == 'not_assessed' and f['score'] is not None:
                errs.append(f'{w}.{name}: not_assessed but scored')
            if f['status'] != 'not_assessed' and (f['score'] is None or not f['evidence']):
                errs.append(f'{w}.{name}: a score needs evidence')
            if f['status'] == 'judged':
                if len(f.get('judged_reason') or '') < 20:
                    errs.append(f'{w}.{name}: a judged score needs judged_reason (20+ characters)')
                rf = rby.get(a['id'], {}).get('factors', {}).get(name, {})
                if f.get('computed_score') != rf.get('score'):
                    errs.append(f'{w}.{name}: computed_score must keep the scored value {rf.get("score")}')
        r = rby.get(a['id'])
        if r:
            for name, f in a['factors'].items():
                if f['status'] != 'judged' and f != r['factors'].get(name):
                    errs.append(f'{w}.{name}: differs from the risk file (mark it judged, with a reason, to change it)')
            for k in ('paths', 'journeys', 'surfaces', 'flags', 'open_findings'):
                if a.get(k) != r.get(k):
                    errs.append(f'{w}.{k}: differs from the risk file')
        want = score_risk.recompute(a)
        for k, v in want.items():
            if a.get(k) != v:
                errs.append(f'{w}.{k}: {a.get(k)!r} but the factors give {v!r} (run --write-summary)')
        for f in a['open_findings']:
            src = inputs.get(f['skill'], {}).get(f['id'])
            if src is not None and src.get('confidence') != f['confidence']:
                errs.append(f'{w}: {f["skill"]} {f["id"]} has confidence {src.get("confidence")!r} in its report, not {f["confidence"]!r}')
    planned = [s['area_id'] for s in plan['strategy']]
    untested = [n['area_id'] for n in plan['not_tested']]
    for aid in by:
        n = planned.count(aid) + untested.count(aid)
        if n != 1:
            errs.append(f'areas[{aid}]: must be in exactly one of strategy and not_tested (found {n})')
    for aid in set(planned + untested) - set(by):
        errs.append(f'{aid}: not an area')
    want = json.loads(json.dumps(plan))
    fill(want)
    short = want.pop('_short')
    for s, ws in zip(plan['strategy'], want['strategy']):
        a = by.get(s['area_id'])
        if not a:
            continue
        types = {t['type'] for t in s['test_types']}
        if a['band'] == 'High' and (len(types) < 2 or 'exploratory' not in types):
            errs.append(f'strategy[{a["id"]}]: a High area needs at least two test types, one of them exploratory')
        if a['band'] == 'High' and not s.get('charters'):
            errs.append(f'strategy[{a["id"]}]: a High area needs exploratory charters')
        if a['band'] == 'Medium' and (len(types) < 2 or 'regression' not in types):
            errs.append(f'strategy[{a["id"]}]: a Medium area needs a test type plus regression')
        if 'novel' in a['flags'] and 'exploratory' not in types:
            errs.append(f'strategy[{a["id"]}]: novel code needs an exploratory charter')
        if (s['effort_sessions'], s['effort_share']) != (ws['effort_sessions'], ws['effort_share']):
            errs.append(f'strategy[{a["id"]}]: effort {s["effort_sessions"]} ({s["effort_share"]}) but the allocation gives '
                        f'{ws["effort_sessions"]} ({ws["effort_share"]}) (run --write-summary)')
        if a['band'] == 'High' and plan['metadata']['budget_sessions'] is not None and ws['effort_sessions'] == 0 and short == 0:
            errs.append(f'strategy[{a["id"]}]: a planned High area got no sessions')
    for n in plan['not_tested']:
        a = by.get(n['area_id'])
        if not a:
            continue
        if n['residual_band'] != a['band']:
            errs.append(f'not_tested[{a["id"]}]: residual_band must be the area\'s band ({a["band"]})')
        if n['accepted_by'] is not None and not n['accepted_by'].strip():
            errs.append(f'not_tested[{a["id"]}]: accepted_by is a person\'s name or null')
        if a['band'] == 'High' and not (short > 0 and re.search(r'budget|session', n['reason'], re.I)):
            errs.append(f'not_tested[{a["id"]}]: a High area is left untested only when the budget is short, and the reason must say so')
        if a['band'] == 'Medium' and 'novel' in a['flags']:
            errs.append(f'not_tested[{a["id"]}]: novel code is not left untested')
    texts = [('summary.headline', plan['summary'].get('headline', ''))] + \
        [(f'not_tested[{n["area_id"]}]', n['reason']) for n in plan['not_tested']] + \
        [(f'exit_criteria[{i}]', e['condition'] + ' ' + e['measure']) for i, e in enumerate(plan['exit_criteria'])] + \
        [(f'assumptions[{i}]', t) for i, t in enumerate(plan['assumptions'])] + \
        [(f'strategy[{s["area_id"]}]', ' '.join(t['why'] for t in s['test_types'])) for s in plan['strategy']]
    for where, t in texts:
        m = BANNED.search(t)
        if m:
            errs.append(f'{where}: "{m.group(0)}" - exit criteria are conditions to check, not a prediction')
    want_s = summary(want, short)
    for k, v in want_s.items():
        if plan['summary'].get(k) != v:
            errs.append(f'summary.{k}: {plan["summary"].get(k)!r} but computed {v!r} (run --write-summary)')
    if short and not re.search(r'\bshort\b', plan['summary'].get('headline', ''), re.I):
        errs.append('summary.headline: the budget is short; say so')
    return errs


def load_inputs(specs: list[str]) -> dict:
    out = {}
    for spec in specs:
        kind, path = spec.split('=', 1)
        data = score_risk.load_input(kind, Path(path))
        out[kind] = {i['id']: i for i in data['items']}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('plan', type=Path)
    ap.add_argument('--risk', type=Path, required=True)
    ap.add_argument('--input', action='append', default=[])
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    risk = json.loads(args.risk.read_text(encoding='utf-8'))
    if args.write_summary:
        try:
            fill(plan)
            short = plan.pop('_short')
            plan['summary'] = {'headline': (plan.get('summary') or {}).get('headline', ''), **summary(plan, short)}
        except (KeyError, TypeError) as exc:
            print(f'INVALID: cannot compute ({exc!r}); fix the plan structure first')
            return 1
        args.plan.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errs = schema_errors(plan, json.loads(SCHEMA.read_text(encoding='utf-8')))
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    errs = check(plan, risk, load_inputs(args.input))
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    s = plan['summary']
    print(f'VALID: {s["areas"]} areas ({s["high"]} High, {s["medium"]} Medium, {s["low"]} Low), {s["planned"]} planned, '
          f'{s["not_tested"]} not tested' + (f', {s["sessions_allocated"]} sessions' if s['sessions_allocated'] is not None else '') +
          (f', {s["sessions_short"]} short' if s['sessions_short'] else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
