#!/usr/bin/env python3
"""Validate designed test cases against the ticket, the model and the generated rows.

    python validate_cases.py test_cases.json --ticket ticket.json --model model.json --rows rows.json
        [--xray xray.json] [--csv cases.csv] [--write-summary]

Checks: the ticket fingerprint and criteria text are the ticket's; every criterion has a technique with a
reason, and cases, or a status saying why not; every case cites existing criteria and rows of its
criterion's technique; rows.json equals what design_rows.py generates from the model, and every row has a
case or is infeasible with a reason; each boundary limit is tested below, on and above; a boundary model
covers every limit the criterion's text states; the decision table is complete; the pairwise rows cover
every allowed pair; every valid transition and every disallowed event has a row; every step has an action
and an observable expected result (no "works", "correct", "as expected"); test data uses reserved email
domains and published test card numbers; vague terms found in a criterion are questions or dismissed with
a reason; cases that rely on a question say so; priorities follow the rubric; the exports equal the cases.
"""
from __future__ import annotations
import argparse, csv, itertools, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from design_rows import generate, allowed  # noqa: E402
from analyse_criteria import analyse  # noqa: E402

EXT = json.loads((HERE.parent / 'data' / 'expected-terms.json').read_text(encoding='utf-8'))
VAGUE = re.compile(r'(?i)(?<![\w-])(' + '|'.join(re.escape(t) for t in EXT['vague_expected'] + EXT['timing_without_number']) + r')(?![\w-])')
EMAIL = re.compile(r'[\w.+-]+@([\w-]+(?:\.[\w-]+)+)')
CARD = re.compile(r'\b(?:\d[ -]?){13,19}\b')
PRIO = ['P0', 'P1', 'P2', 'P3']
JIRA_PRIO = {'highest': 'P0', 'blocker': 'P0', 'critical': 'P0', 'high': 'P1', 'major': 'P1', 'medium': 'P2', 'low': 'P3', 'lowest': 'P3', 'minor': 'P3', 'trivial': 'P3'}


def luhn(d: str) -> bool:
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        s += n
        alt = not alt
    return s % 10 == 0


def expected_priority(case: dict, base: str, money: set) -> str:
    i = PRIO.index(base)
    if case['type'] in ('boundary', 'negative') and any(c in money for c in case['criterion_ids']):
        i -= 1
    if case.get('cosmetic'):
        i += 1
    return PRIO[max(0, min(3, i))]


def summarise(d: dict, rows: list[dict], model: dict) -> dict:
    infeasible = {x['row_id'] for x in model.get('infeasible', [])}
    covered = {r for c in d['cases'] for r in c['row_ids']}
    return {'criteria': len(d['criteria']), 'criteria_with_cases': sum(1 for c in d['criteria'] if c['case_ids']),
            'cases': len(d['cases']), 'rows_total': len(rows), 'rows_covered': sum(1 for r in rows if r['id'] in covered),
            'rows_infeasible': sum(1 for r in rows if r['id'] in infeasible), 'questions': len(d['questions']),
            'by_technique': {t: sum(1 for c in d['cases'] if c['technique'] == t) for t in sorted({c['technique'] for c in d['cases']})},
            'by_priority': {p: sum(1 for c in d['cases'] if c['priority'] == p) for p in PRIO}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cases', type=Path)
    ap.add_argument('--ticket', type=Path, required=True)
    ap.add_argument('--model', type=Path, required=True)
    ap.add_argument('--rows', type=Path, required=True)
    ap.add_argument('--xray', type=Path)
    ap.add_argument('--csv', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.cases.read_text(encoding='utf-8'))
    t = json.loads(a.ticket.read_text(encoding='utf-8'))
    model = json.loads(a.model.read_text(encoding='utf-8'))
    rows = json.loads(a.rows.read_text(encoding='utf-8'))['rows']
    E: list[str] = []
    e = E.append
    for k in ('schema_version', 'skill', 'ticket', 'criteria', 'cases', 'questions', 'summary', 'limitations'):
        if k not in d:
            e(f'missing {k}')
    if E:
        print('\n'.join(E)); return 1
    if d['ticket'].get('content_sha256') != t.get('content_sha256') or model.get('content_sha256') != t.get('content_sha256'):
        e('ticket fingerprint differs from ticket.json (cases or model made from another version of the ticket)')
    acs = {ac['id']: ac for ac in t['acceptance_criteria']}
    mcrit = {c['id']: c for c in model['criteria']}
    gen = generate(model)
    if gen != rows:
        e('rows.json differs from what design_rows.py generates from the model')
    row_by = {r['id']: r for r in gen}
    infeasible = {x['row_id']: x for x in model.get('infeasible', [])}
    for x in infeasible.values():
        if len(x.get('reason', '')) < 15:
            e(f'infeasible row {x["row_id"]} needs a reason')
    cases = {c['id']: c for c in d['cases']}
    if len(cases) != len(d['cases']):
        e('case ids are not unique')
    qids = {q['id'] for q in d['questions']}

    # criteria ----------------------------------------------------------------------------------------
    seen = set()
    for c in d['criteria']:
        seen.add(c['id'])
        ac = acs.get(c['id'])
        if not ac:
            e(f'{c["id"]} is not a criterion of {t["key"]}'); continue
        if c.get('text') != ac['text']:
            e(f'{c["id"]}: text differs from the ticket')
        m = mcrit.get(c['id'])
        if not m or not m['techniques'] or any(len(x.get('reason', '')) < 15 for x in m['techniques']):
            e(f'{c["id"]}: needs at least one technique with a reason in the model')
        if not c['case_ids'] and c.get('status') not in ('handed_off', 'blocked'):
            e(f'{c["id"]}: no cases and no reason (status handed_off or blocked, with why)')
        if c.get('status') in ('handed_off', 'blocked') and len(c.get('why', '')) < 15:
            e(f'{c["id"]}: {c["status"]} needs a reason')
        if sorted(c['case_ids']) != sorted(k for k, v in cases.items() if c['id'] in v['criterion_ids']):
            e(f'{c["id"]}: case_ids differ from the cases that cite it')
        # analysis signals the model must answer
        an = analyse(ac)
        dismissed = {(x['criterion_id'], x.get('term')) for x in model.get('dismissed_signals', []) if len(x.get('reason', '')) >= 15}
        asked = {(q['criterion_id'], q['term']) for q in model.get('questions', [])}
        for v in an['vague']:
            if (c['id'], v['term']) not in dismissed | asked:
                e(f'{c["id"]}: vague term "{v["term"]}" is neither a question nor dismissed with a reason')
        techs = {x['technique']: x['model'] for x in m['techniques']} if m else {}
        if an['signals']['limits']:
            if 'boundary' not in techs:
                if (c['id'], None) not in {(x['criterion_id'], x.get('term')) for x in model.get('dismissed_signals', []) if x.get('signal') == 'boundary'}:
                    e(f'{c["id"]}: the text states limits ({"; ".join(l["text"].strip() for l in an["signals"]["limits"])}) but no boundary model')
            else:
                have = {round(float(l['value']), 6) for l in techs['boundary']['limits']}
                for l in an['signals']['limits']:
                    for v in ([l['low'], l['high']] if l['kind'] == 'range' else [l['value']]):
                        if round(float(v), 6) not in have:
                            e(f'{c["id"]}: the limit {v} in the text is not in the boundary model')
    for k in acs:
        if k not in seen:
            e(f'{k} of the ticket is not in criteria')

    # technique completeness ----------------------------------------------------------------------------
    for c in model['criteria']:
        for x in c['techniques']:
            mm, mine = x['model'], [r for r in gen if r['criterion'] == c['id'] and r['technique'] == x['technique']]
            if x['technique'] == 'boundary':
                for lim in mm['limits']:
                    sides = {r['side'] for r in mine if r['limit'] == lim['value']}
                    if sides != {'below', 'on', 'above'}:
                        e(f'{c["id"]}: the limit {lim["value"]} is not tested below, on and above')
            if x['technique'] == 'decision_table':
                n = 1
                for cond in mm['conditions']:
                    n *= len(cond['values'])
                if len(mine) != n:
                    e(f'{c["id"]}: decision table has {len(mine)} rows, not the full {n}')
            if x['technique'] == 'pairwise':
                ps = mm['parameters']
                for (i, pa), (j, pb) in itertools.combinations(enumerate(ps), 2):
                    for va in pa['values']:
                        for vb in pb['values']:
                            if allowed({pa['name']: va, pb['name']: vb}, mm.get('constraints', [])) and not any(
                                    r['values'][pa['name']] == va and r['values'][pb['name']] == vb for r in mine):
                                e(f'{c["id"]}: the pair {pa["name"]}={va}, {pb["name"]}={vb} is not covered')
            if x['technique'] == 'state_transition':
                into = {tr['to'] for tr in mm['transitions']}
                for st in mm['states']:
                    if st != mm.get('initial', mm['states'][0]) and st not in into:
                        e(f'{c["id"]}: the state "{st}" has no transition into it: a transition is missing from the model')
                for tr in mm['transitions']:
                    if not any(r['valid'] and r['values'] == {'state': tr['from'], 'event': tr['event']} for r in mine):
                        e(f'{c["id"]}: transition {tr["from"]} --{tr["event"]}--> {tr["to"]} has no row')

    # cases -----------------------------------------------------------------------------------------------
    covered = set()
    base = JIRA_PRIO.get((t.get('priority') or 'medium').lower(), 'P2')
    money = {c['id'] for c in model['criteria'] if c.get('money')}
    for c in d['cases']:
        w = c['id']
        if not c['criterion_ids'] or any(x not in acs for x in c['criterion_ids']):
            e(f'{w}: cites no criterion or one that does not exist')
        for r in c['row_ids']:
            rr = row_by.get(r)
            if not rr:
                e(f'{w}: row {r} was not generated'); continue
            if rr['criterion'] not in c['criterion_ids'] or rr['technique'] != c['technique']:
                e(f'{w}: row {r} belongs to {rr["criterion"]} {rr["technique"]}')
            covered.add(r)
        if not c['steps']:
            e(f'{w}: no steps')
        for i, s in enumerate(c['steps'], 1):
            if not (s.get('action') or '').strip():
                e(f'{w} step {i}: no action')
            exp = (s.get('expected') or '').strip()
            if not exp:
                e(f'{w} step {i}: no expected result')
            m = VAGUE.search(exp)
            if m:
                e(f'{w} step {i}: expected result says "{m.group(0)}": name what the tester observes')
        if not any((s.get('expected') or '').strip() for s in c['steps']):
            e(f'{w}: no step has an expected result')
        blob = json.dumps({k: c[k] for k in ('steps', 'test_data', 'preconditions')}, ensure_ascii=False)
        for m in EMAIL.finditer(blob):
            dom = m.group(1).lower()
            if not any(dom == r or dom.endswith('.' + r) for r in EXT['reserved_email_domains']):
                e(f'{w}: test data email at {dom} is not a reserved domain (use example.test)')
        for m in CARD.finditer(blob):
            digits = re.sub(r'\D', '', m.group(0))
            if 13 <= len(digits) <= 19 and luhn(digits) and digits not in EXT['test_card_numbers']:
                e(f'{w}: a card number that passes Luhn and is not a published test number')
        for q in c.get('depends_on_questions', []):
            if q not in qids:
                e(f'{w}: depends on unknown question {q}')
        if c['priority'] != expected_priority(c, base, money):
            e(f'{w}: priority {c["priority"]} should be {expected_priority(c, base, money)}')
    for r in gen:
        if r['id'] not in covered and r['id'] not in infeasible:
            e(f'row {r["id"]} ({r["label"]}) has no case and is not marked infeasible')
    for q in d['questions']:
        if q['id'] not in {x['id'] for x in model.get('questions', [])}:
            e(f'question {q["id"]} is not in the model')
        for b in q.get('blocks_cases', []):
            if q['id'] not in cases.get(b, {}).get('depends_on_questions', []):
                e(f'{q["id"]} blocks {b}, but {b} does not say it depends on it')
    for q in model.get('questions', []):
        if q['id'] not in qids:
            e(f'model question {q["id"]} is not in questions')

    # exports -------------------------------------------------------------------------------------------
    if a.xray or a.csv:
        from export_cases import to_xray, to_csv_rows
        if a.xray and json.loads(a.xray.read_text(encoding='utf-8')) != to_xray(d):
            e(f'{a.xray} differs from the cases (re-export)')
        if a.csv:
            with a.csv.open(encoding='utf-8', newline='') as fh:
                if [r for r in csv.reader(fh)] != to_csv_rows(d):
                    e(f'{a.csv} differs from the cases (re-export)')

    want = summarise(d, gen, model)
    if a.write_summary:
        d['summary'].update(want)
        a.cases.write_text(json.dumps(d, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    for k, v in want.items():
        if d['summary'].get(k) != v:
            e(f'summary.{k} {d["summary"].get(k)!r} should be {v!r}')
    if E:
        print(f'{a.cases}: {len(E)} problem(s)')
        for x in E:
            print('  -', x)
        return 1
    s = want
    print(f'{a.cases}: valid. {s["cases"]} case(s) for {s["criteria_with_cases"]} of {s["criteria"]} criteria; rows {s["rows_covered"]}/{s["rows_total"]} covered, '
          f'{s["rows_infeasible"]} infeasible; {s["questions"]} question(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
