#!/usr/bin/env python3
"""Validate a risk-based test plan against the schema, the scored risk file and the rubric (stdlib only).

    python validate_test_plan.py test_plan.json --risk risk.json [--input security=security_audit.json ...] [--write-summary]

Checks: every factor with a score cites evidence and a not_assessed factor has none; a judged
factor keeps its computed score and gives a reason; likelihood, impact, risk, band and
confidence recompute; every area matches risk.json except judged factors; every in-scope area is
planned or not tested (never both, never neither); test types fit the band; effort recomputes
from the budget; a High area is left untested only when the budget is short; residual bands
match; open findings keep the severity (as the adapter reads it, with its source_rating) and the
confidence they had in their source report (--input, which may name several reports of one kind;
findings are matched by kind, id and source report), the open_findings flag is set exactly when
one is critical or high at a confidence its report stands by (unconfirmed_findings when they are
only assumed or possible), a finding moved by --finding-area is explained in an assumption, and
a risk file warning that UI impact rests on surfaces alone is acknowledged, and when no area has
journey criticality or reach assessed the headline says the ranking rests on surface patterns; a
breaking_change area is planned with regression or contract testing (or left untested with a reason
that names the breaking change); the headline doesn't contradict the register (no "no area reaches
High" when one does, band counts and quoted risks and bands match); no "safe", "fully tested" or
"risk-free". A judged factor may keep the computed score with a new reason (a reviewed surface). --write-summary fills in each area's computed fields, the effort and the summary;
the headline stays yours.
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
            cands = [i for i in inputs.get(f['skill'], []) if i['id'] == f['id']
                     and (f.get('source') is None or i['_source'] == f['source'])]
            src = cands[0] if len(cands) == 1 else None
            if f['skill'] in inputs and not cands:
                errs.append(f'{w}: {f["skill"]} {f["id"]} is not in the {f["skill"]} report'
                            + (f' {f["source"]}' if f.get('source') else '') + ' given with --input')
            elif len(cands) > 1:
                errs.append(f'{w}: {f["skill"]} {f["id"]} is in more than one {f["skill"]} report given with --input '
                            f'({", ".join(i["_source"] for i in cands)}); re-run score_risk.py so it records its source')
            if src is not None and src.get('confidence') != f['confidence']:
                errs.append(f'{w}: {f["skill"]} {f["id"]} has confidence {src.get("confidence")!r} in its report, not {f["confidence"]!r}')
            if src is not None and (src.get('severity'), src.get('source_rating')) != (f['severity'], f.get('source_rating')):
                errs.append(f'{w}: {f["skill"]} {f["id"]} reads as severity {src.get("severity")!r} ({src.get("source_rating")}) from '
                            f'its report, not {f["severity"]!r} ({f.get("source_rating")}); re-run score_risk.py')
        want_flags = score_risk.finding_flags(a['open_findings'])
        for flag, what in (('open_findings', 'an open finding is critical or high at a confidence its report stands by'),
                           ('unconfirmed_findings', 'every critical or high open finding is only assumed or possible')):
            if (flag in want_flags) != (flag in a['flags']):
                errs.append(f'{w}.flags: {flag} must be set exactly when {what}')
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
        if 'breaking_change' in a['flags'] and not types & {'regression', 'contract'}:
            errs.append(f'strategy[{a["id"]}]: a breaking change needs regression or contract testing of what callers rely on')
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
        if 'breaking_change' in a['flags'] and not re.search(r'\bbreak', n['reason'], re.I):
            errs.append(f'not_tested[{a["id"]}]: the area carries a breaking change; the reason must say why it is left untested')
        for m in re.finditer(r'\b(High|Medium|Low) risk \((\d+(?:\.\d+)?)\)', n['reason']):
            if m.group(1) != a['band'] or a['risk'] is None or abs(float(m.group(2)) - a['risk']) > 0.05:
                errs.append(f'not_tested[{a["id"]}]: reason says "{m.group(0)}", but the area computes to {a["band"]} risk ({a["risk"]})')
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
    errs += headline_errors(plan['summary'].get('headline', ''), plan['areas'], want_s)
    for a in plan['areas']:
        for f in a['open_findings']:
            if 'reassigned_from' in f:
                if not any(re.search(rf'(?<![\w-]){re.escape(f["id"])}(?![\w-])', t) for t in plan['assumptions']):
                    errs.append(f'areas[{a["id"]}]: {f["skill"]} {f["id"]} was moved here by --finding-area; an assumption '
                                'must name it and say why its report\'s file is wrong')
    if any(str(x).startswith('no journey map: the impact of') for x in risk.get('warnings') or []):
        said = json.dumps([plan['summary'].get('headline', ''), plan['assumptions'], plan['open_questions']])
        if not re.search(r'journey', said, re.I):
            errs.append('the risk file warns that UI impact rests on surface patterns alone (no journey map); the headline, '
                        'an assumption or an open question must say so')
    # When no area has journey criticality or reach assessed, impact is the surface factor alone and the ranking is a
    # ranking by surface patterns: the headline itself must say so (an assumption is not enough).
    surface_only = plan['areas'] and all(a['factors'][k]['status'] == 'not_assessed'
                                         for a in plan['areas'] for k in ('journey_criticality', 'reach'))
    if surface_only and not re.search(r'\bsurface', plan['summary'].get('headline', ''), re.I):
        errs.append('summary.headline: no area has journey criticality or reach assessed, so impact is the surface factor '
                    'alone; the headline must say the ranking rests on surface patterns')
    return errs


NO_HIGH = re.compile(r'\bno (?:area|areas|part)\b[^.;,]{0,25}\b(?:reaches|is|are|scores?|rated|in)\b[^.;,]{0,10}\bhigh\b|'
                     r'\bno high[- ](?:risk )?areas?\b(?!\s+(?:is|are)\s+(?:left|not))|\bnothing (?:is|reaches|scores) high\b', re.I)
COUNT_WORDS = {'no': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
               'ten': 10}
BAND_COUNT = re.compile(r'\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(High|Medium|Low)(?:[- ]risk)?\s+areas?\b', re.I)


def headline_errors(headline: str, areas: list[dict], counts: dict) -> list[str]:
    """The headline must not contradict the computed register: no "no area reaches High" when one does, band counts
    that match, and an area's risk or band quoted as computed (a stale headline after re-scoring is caught here)."""
    errs = []
    if counts['high'] and NO_HIGH.search(headline):
        errs.append(f'summary.headline: says no area is High, but {counts["high"]} area(s) are')
    for m in BAND_COUNT.finditer(headline):
        n = int(m.group(1)) if m.group(1).isdigit() else COUNT_WORDS[m.group(1).lower()]
        band = m.group(2).lower()
        if n != counts[band]:
            errs.append(f'summary.headline: "{m.group(0)}" but the register has {counts[band]} {m.group(2).capitalize()} area(s)')
    for a in areas:
        name = re.escape(a['id'])
        for m in re.finditer(rf'(?<![\w/-]){name}(?![\w/-])\s*\(\s*risk\s+(\d+(?:\.\d+)?)', headline, re.I):
            if a['risk'] is None or abs(float(m.group(1)) - a['risk']) > 0.05:
                errs.append(f'summary.headline: quotes {a["id"]} at risk {m.group(1)}, but it computes to {a["risk"]}')
        for m in re.finditer(rf'(?<![\w/-]){name}(?![\w/-])\s*(?:\(\s*|is\s+|:\s*)(?:risk\s+[\d.]+,?\s*)?(High|Medium|Low)\b', headline, re.I):
            if m.group(1).capitalize() != a['band']:
                errs.append(f'summary.headline: calls {a["id"]} {m.group(1)}, but its band is {a["band"]}')
    return errs


def load_inputs(specs: list[str]) -> dict:
    """{kind: [items, each with _source = the report's file name]}: two reports of one kind (two migration reviews)
    both count, and a finding id that appears in both is told apart by the source the carried finding records."""
    out: dict[str, list[dict]] = {}
    for spec in specs:
        kind, path = spec.split('=', 1)
        data = score_risk.load_input(kind, Path(path))
        out.setdefault(kind, []).extend({**i, '_source': data['path']} for i in data['items'])
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
