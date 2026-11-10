#!/usr/bin/env python3
"""Validate a ticket readiness review against the schema and the rubric's honesty rules (stdlib only).

    python validate_readiness.py readiness.json [--tickets tickets.json|ticket.json] [--lint lint.json]
                                                [--write-summary]

--tickets re-checks each review against the normalised ticket: the fingerprint, that every
criterion and flag excerpt is verbatim ticket text, and that every instruction-like passage
fetch_ticket.py found is listed. --lint checks that every lint flag was kept as a flag or
dismissed with a reason. --write-summary recomputes each readiness score, verdict, blocking
count and the batch ranking, and writes them back before validating.
"""
from __future__ import annotations
import argparse, json, math, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_ticket  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'readiness.schema.json'

STORY_DIMS = {'testability': 3, 'clarity': 2, 'completeness': 2, 'nfr': 1, 'dependencies': 1, 'size': 1}
BUG_DIMS = {'reproduction_steps': 3, 'expected_vs_actual': 2, 'environment': 2, 'frequency': 1, 'evidence': 1}
PRIMARY = {'story': 'testability', 'bug': 'reproduction_steps', 'epic': 'size'}
VERDICT_ORDER = {'not_ready': 0, 'not_assessed': 1, 'ready_with_questions': 2, 'ready': 3}
# flag kinds -> the dimension a kept flag counts against
FLAG_DIM = {'vague_term': 'clarity', 'weak_modal': 'clarity', 'undefined_reference': 'clarity',
            'ambiguous_pronoun': 'clarity', 'conflict': 'clarity', 'unquantified_nfr': 'nfr',
            'open_ended': 'completeness', 'missing_case': 'completeness', 'compound': 'testability',
            'subjective_outcome': 'testability', 'missing_outcome': 'testability', 'unknown_reference': 'dependencies'}
BANNED = re.compile(r'\b(ticket|story|bug|criteria|criterion|requirements?|it|this)\s+(is|are|looks|seems|now)\s+'
                    r'(fully\s+|now\s+)?(correct|complete|approved|compliant|right|perfect|signed off)\b|\bapproved\b', re.I)


# ---------------------------------------------------------------- schema (subset of JSON Schema)
def schema_errors(value, schema: dict, root: dict, path: str = '$') -> list[str]:
    if '$ref' in schema:
        node = root
        for part in schema['$ref'].lstrip('#/').split('/'):
            node = node[part]
        return schema_errors(value, node, root, path)
    errs = []
    if 'const' in schema and value != schema['const']:
        return [f'{path}: must be {schema["const"]!r}']
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
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            errs.append(f'{path}: shorter than {schema["minLength"]} characters')
        if schema.get('pattern') and not re.search(schema['pattern'], value):
            errs.append(f'{path}: {value!r} does not match {schema["pattern"]}')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errs.append(f'{path}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            errs.append(f'{path}: above {schema["maximum"]}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errs.append(f'{path}: needs at least {schema["minItems"]} item(s)')
        if 'items' in schema:
            for i, v in enumerate(value):
                errs += schema_errors(v, schema['items'], root, f'{path}[{i}]')
    if isinstance(value, dict):
        for k in schema.get('required', []):
            if k not in value:
                errs.append(f'{path}: missing {k!r}')
        props = schema.get('properties', {})
        extra = schema.get('additionalProperties', True)
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], root, f'{path}.{k}')
            elif extra is False:
                errs.append(f'{path}: unexpected field {k!r}')
    return errs


# ---------------------------------------------------------------- rubric
def expected_dims(kind: str) -> dict[str, int]:
    return BUG_DIMS if kind == 'bug' else STORY_DIMS


def readiness_score(r: dict) -> float | None:
    """Weighted mean of assessed dimensions, truncated (never rounded up) to two decimals."""
    used = [(d['score'], d['weight']) for d in r['dimensions'] if d['status'] == 'assessed' and d['score'] is not None]
    if not used:
        return None
    mean = sum(s * w for s, w in used) / sum(w for _, w in used)
    return math.floor(mean * 100 + 1e-9) / 100


def compute_verdict(r: dict) -> tuple[str, list[str]]:
    kind = r['ticket']['kind']
    dims = {d['name']: d for d in r['dimensions']}
    blocking = [q for q in r['questions'] if q['blocking']]
    reasons = []
    if kind == 'epic':
        return 'not_ready', ['Epics are split into stories before they are built; see the split proposal.']
    primary = dims.get(PRIMARY[kind])
    if kind == 'story' and not r['criteria']:
        return 'not_assessed', ['No acceptance criteria were found or could be extracted from the description.']
    if primary is None or primary['status'] != 'assessed':
        return 'not_assessed', [f'{PRIMARY[kind].replace("_", " ").capitalize()} could not be assessed.']
    if blocking:
        reasons.append(f'{len(blocking)} blocking question(s): ' + '; '.join(q['id'] for q in blocking))
    if primary['score'] < 2:
        reasons.append(f'{PRIMARY[kind].replace("_", " ").capitalize()} scores {primary["score"]} (needs 2 or more).')
    zero = [d['name'] for d in r['dimensions'] if d['status'] == 'assessed' and d['score'] == 0]
    if zero:
        reasons.append('Scored 0: ' + ', '.join(zero) + '.')
    if reasons:
        return 'not_ready', reasons
    low = [d['name'] for d in r['dimensions'] if d['status'] == 'assessed' and d['score'] < 2]
    unmet = [i['id'] for i in r['definition_of_ready']['extra_items'] if i['status'] == 'not_met']
    if low:
        reasons.append('Scored 1: ' + ', '.join(low) + '.')
    if unmet:
        reasons.append('Definition of Ready items not met: ' + ', '.join(unmet) + '.')
    if reasons:
        return 'ready_with_questions', reasons
    return 'ready', ['No blocking questions, and every assessed dimension scores 2 or more.']


def oracle_cap(criteria: list[dict]) -> int:
    if not criteria:
        return 0
    frac = sum(1.0 if c['oracle'] == 'present' else 0.5 if c['oracle'] == 'partial' else 0 for c in criteria) / len(criteria)
    return 3 if frac == 1 else 2 if frac >= 0.75 else 1 if frac >= 0.25 else 0


def rank(reviews: list[dict]) -> list[dict]:
    order = sorted(reviews, key=lambda r: (VERDICT_ORDER[r['verdict']['value']],
                                           -r['verdict']['blocking_questions'],
                                           r['verdict']['readiness_score'] if r['verdict']['readiness_score'] is not None else -1,
                                           r['ticket']['key'] or ''))
    return [{'rank': i, 'key': r['ticket']['key'], 'verdict': r['verdict']['value'],
             'readiness_score': r['verdict']['readiness_score'], 'blocking_questions': r['verdict']['blocking_questions']}
            for i, r in enumerate(order, 1)]


def write_summary(d: dict) -> None:
    for r in d['reviews']:
        value, reasons = compute_verdict(r)
        r['verdict'] = {'value': value, 'readiness_score': readiness_score(r),
                        'blocking_questions': sum(q['blocking'] for q in r['questions']), 'reasons': reasons}
    if len(d['reviews']) > 1 or 'batch' in d:
        b = d.setdefault('batch', {'query': None, 'source': 'connector'})
        b['ranking'] = rank(d['reviews'])
        b['counts'] = {k: sum(r['verdict']['value'] == k for r in d['reviews'])
                       for k in ('ready', 'ready_with_questions', 'not_ready', 'not_assessed')}
        d['batch'] = {'query': b.get('query'), 'source': b.get('source', 'connector'),
                      'ranking': b['ranking'], 'counts': b['counts']}


# ---------------------------------------------------------------- checks
def norm(s: str) -> str:
    return ' '.join((s or '').split())


def field_text(t: dict, field: str) -> str | None:
    if field == 'summary':
        return t.get('summary') or ''
    if field == 'description':
        return t.get('description_md') or ''
    m = re.fullmatch(r'acceptance_criteria\.(AC-\d+)', field)
    if m:
        return next((a['text'] for a in t.get('acceptance_criteria') or [] if a['id'] == m.group(1)), None)
    m = re.fullmatch(r'comment\[(\d+)\]', field)
    if m:
        cs = t.get('comments') or []
        i = int(m.group(1))
        return cs[i].get('body_md') if i < len(cs) else None
    if field.startswith('bug.'):
        b = t.get('bug') or {}
        part = field[4:]
        v = b.get(part)
        return '\n'.join(v) if isinstance(v, list) else v
    return None


def check_review(r: dict, i: int, ticket: dict | None, lint: dict | None) -> tuple[list[str], list[str]]:
    errs, warns = [], []
    at = f"reviews[{i}] ({r['ticket'].get('key')})"
    kind = r['ticket']['kind']
    E = lambda m: errs.append(f'{at}: {m}')  # noqa: E731

    # dimensions
    want = expected_dims(kind)
    got = {d['name']: d for d in r['dimensions']}
    if set(got) != set(want):
        E(f'dimensions must be exactly {sorted(want)} for a {kind}, got {sorted(got)}')
    for d in r['dimensions']:
        if d['name'] in want and d['weight'] != want[d['name']]:
            E(f"dimension {d['name']} weight must be {want[d['name']]}")
        if d['status'] == 'assessed' and d['score'] is None:
            E(f"dimension {d['name']} is assessed but has no score")
        if d['status'] != 'assessed' and d['score'] is not None:
            E(f"dimension {d['name']} is {d['status']} and must not have a score")
        if d['status'] == 'assessed' and not d['evidence']:
            E(f"dimension {d['name']} has a score but no evidence")
    if kind == 'epic':
        for name in ('testability', 'clarity', 'completeness', 'nfr'):
            if name in got and got[name]['status'] == 'assessed':
                E(f'epics are assessed for size and dependencies only; {name} must be not_applicable')
        if not r['split_proposal']:
            E('an epic needs a split proposal')

    # criteria and flags
    crit_ids = [c['id'] for c in r['criteria']]
    if len(crit_ids) != len(set(crit_ids)):
        E('criterion ids are not unique')
    flag_ids, kept_lint = [], set()
    for c in r['criteria']:
        if c['oracle'] != 'present' and not c.get('oracle_note'):
            E(f"{c['id']}: oracle is {c['oracle']} but there is no oracle_note saying what's missing")
        if c.get('split_from') and not any(x['id'] == c['split_from'] for x in r['criteria']) and ticket is None:
            pass  # split_from refers to the ticket's criterion id, checked against the ticket below
        for f in c['flags']:
            flag_ids.append(f['id'])
            if f['evidence_type'] == 'agent_judgment' and f['confidence'] == 'high':
                E(f"{c['id']} {f['id']}: agent_judgment can never be high confidence")
            if f['evidence_type'] == 'tool' and not f.get('lint_id'):
                E(f"{c['id']} {f['id']}: a tool flag must carry the lint_id it came from")
            if f.get('lint_id'):
                kept_lint.add(f['lint_id'])
                if f['evidence_type'] != 'tool':
                    E(f"{c['id']} {f['id']}: a flag with a lint_id is tool evidence")
            if f['field'] != 'summary' and not f['field'].startswith('acceptance_criteria.') and f['field'] != 'description' \
                    and not f['field'].startswith(('comment[', 'bug.')):
                E(f"{c['id']} {f['id']}: unknown field {f['field']!r}")
    if len(flag_ids) != len(set(flag_ids)):
        E('flag ids are not unique')
    dismissed = {x['lint_id'] for x in r['dismissed_flags']}
    if kept_lint & dismissed:
        E(f'lint flags both kept and dismissed: {sorted(kept_lint & dismissed)}')
    for x in r['dismissed_flags']:
        if re.fullmatch(r'\s*(false positive|fp|not an issue|n/?a)\.?\s*', x['reason'], re.I):
            E(f"dismissal of {x['lint_id']} needs a reason someone can check, not {x['reason']!r}")

    # dimension consistency with flags and criteria
    for c in r['criteria']:
        for f in c['flags']:
            dim = FLAG_DIM.get(f['kind'])
            if dim in got and got[dim]['status'] == 'assessed' and got[dim]['score'] == 3:
                E(f"{dim} scores 3 but {c['id']} keeps a {f['kind']} flag ({f['id']}) against it")
    if kind == 'story' and 'testability' in got and got['testability']['status'] == 'assessed':
        cap = oracle_cap(r['criteria'])
        if got['testability']['score'] > cap:
            E(f"testability scores {got['testability']['score']} but the criteria's oracles allow at most {cap}")
    if kind == 'story' and 'size' in got and got['size']['status'] == 'assessed':
        n = len(r['criteria'])
        if n > 12 and not r['split_proposal']:
            E(f'{n} criteria: more than 12 needs a split proposal')
        if n > 12 and got['size']['score'] > 1:
            E(f'{n} criteria: size can score at most 1')
        if n > 8 and got['size']['score'] == 3:
            E(f'{n} criteria: size 3 needs 8 or fewer')
    if kind == 'story' and 'nfr' in got and got['nfr']['status'] == 'assessed':
        if any(f['kind'] == 'unquantified_nfr' for c in r['criteria'] for f in c['flags']) and got['nfr']['score'] > 1:
            E('a kept unquantified_nfr flag means nfr scores at most 1')

    # questions and suggestions
    q_ids = {q['id'] for q in r['questions']}
    if len(q_ids) != len(r['questions']):
        E('question ids are not unique')
    for q in r['questions']:
        for cid in q['related_criteria']:
            if cid not in crit_ids:
                E(f"{q['id']}: related criterion {cid} doesn't exist")
        if q['blocking'] and norm(q['question']) not in norm(r['comment_text']):
            E(f"{q['id']}: blocking question is missing from comment_text")
    crit_text = {c['id']: c['text'] for c in r['criteria']}
    for s in r['suggestions']:
        if s['criterion_id'] is not None:
            if s['criterion_id'] not in crit_text:
                E(f"{s['id']}: criterion {s['criterion_id']} doesn't exist")
            elif s['original'] is None or norm(s['original']) != norm(crit_text[s['criterion_id']]):
                E(f"{s['id']}: original must be the criterion's text, verbatim")
        elif s['original'] is not None:
            E(f"{s['id']}: a new criterion (no criterion_id) has no original")
        if s.get('depends_on_answer') and s['depends_on_answer'] not in q_ids:
            E(f"{s['id']}: depends on unknown question {s['depends_on_answer']}")
        if norm(s['suggested']) == norm(s['original'] or ''):
            E(f"{s['id']}: suggestion is identical to the original")
    for qb in (q for q in r['questions'] if q['blocking']):
        for s in r['suggestions']:
            if s['criterion_id'] in qb['related_criteria'] and not s.get('depends_on_answer'):
                warns.append(f"{at}: {s['id']} rewrites {s['criterion_id']}, which has blocking question {qb['id']}; "
                             f"set depends_on_answer if the rewrite assumes an answer")

    # code checks
    cc = r['code_checks']
    if cc['status'] == 'not_run' and not cc.get('reason'):
        E('code_checks not_run needs a reason')
    if cc['status'] == 'not_run' and cc['references']:
        E('code_checks not_run must not list references')

    # verdict
    v = r['verdict']
    exp_value, _ = compute_verdict(r)
    if v['value'] != exp_value:
        E(f"verdict is {v['value']} but the rubric gives {exp_value}")
    exp_score = readiness_score(r)
    if v['readiness_score'] != exp_score:
        E(f"readiness_score is {v['readiness_score']} but the dimensions give {exp_score}")
    nb = sum(q['blocking'] for q in r['questions'])
    if v['blocking_questions'] != nb:
        E(f"blocking_questions is {v['blocking_questions']} but {nb} question(s) are blocking")
    if v['value'] != 'ready' and not v['reasons']:
        E('a verdict other than ready needs reasons')
    texts = [r['comment_text']] + v['reasons'] + [d['rationale'] for d in r['dimensions']]
    for tx in texts:
        m = BANNED.search(tx)
        if m:
            E(f'report text claims more than testability: {m.group(0)!r}')
    if v['value'] == 'not_assessed' and kind == 'story' and not r['questions']:
        E('not_assessed needs a question asking for the criteria')

    # against the ticket
    if ticket is not None:
        if fetch_ticket.content_sha256(ticket) != r['ticket']['content_sha256']:
            E('ticket content_sha256 does not match the ticket (it changed, or a different ticket was reviewed)')
        if ticket.get('key') != r['ticket']['key']:
            E(f"ticket key {r['ticket']['key']} doesn't match {ticket.get('key')}")
        tk_crit = {a['id']: a['text'] for a in ticket.get('acceptance_criteria') or []}
        covered = {c.get('split_from') or c['id'] for c in r['criteria']}
        for ac_id in tk_crit:
            if ac_id not in covered:
                E(f'ticket criterion {ac_id} is not reviewed')
        all_text = norm(fetch_ticket.ticket_text(ticket))
        for c in r['criteria']:
            src = c.get('split_from') or c['id']
            if c.get('split_from'):
                if src not in tk_crit:
                    E(f"{c['id']}: split_from {src} isn't a criterion of the ticket")
                elif norm(c['text']) not in norm(tk_crit[src]):
                    E(f"{c['id']}: text of a split criterion must be a verbatim part of {src}")
            elif c['id'] in tk_crit:
                if norm(c['text']) != norm(tk_crit[c['id']]):
                    E(f"{c['id']}: text differs from the ticket's criterion")
            elif norm(c['text']) not in all_text:
                E(f"{c['id']}: text is not in the ticket")
            for f in c['flags']:
                ft = field_text(ticket, f['field'])
                if ft is None:
                    E(f"{c['id']} {f['id']}: field {f['field']!r} isn't in the ticket")
                elif norm(f['excerpt']) not in norm(ft):
                    E(f"{c['id']} {f['id']}: excerpt is not verbatim text of {f['field']}")
        listed = {norm(x['excerpt']) for x in r['suspicious_instructions_ignored']}
        for s in ticket.get('suspicious_instructions') or []:
            if norm(s['excerpt']) not in listed:
                E(f"instruction-like passage in {s['field']} is not listed in suspicious_instructions_ignored")
        if (ticket.get('type') or '').lower() in ('bug', 'defect', 'incident') and kind != 'bug':
            E(f"ticket type {ticket.get('type')} must be reviewed with the bug rubric")
    else:
        warns.append(f'{at}: no --tickets given; excerpts and the fingerprint were not re-checked')

    # against the lint
    if lint is not None:
        lt = next((x for x in lint['tickets'] if x['key'] == r['ticket']['key']), None)
        if lt is None:
            E('ticket is missing from the lint output')
        else:
            lint_flags = {f['id']: f for f in lt['flags']}
            for lid in sorted(set(lint_flags) - kept_lint - dismissed):
                E(f'lint flag {lid} ({lint_flags[lid]["kind"]} {lint_flags[lid]["term"]!r}) was neither kept nor dismissed')
            for lid in sorted((kept_lint | dismissed) - set(lint_flags)):
                E(f'lint id {lid} is not in the lint output')
            for c in r['criteria']:
                for f in c['flags']:
                    lf = lint_flags.get(f.get('lint_id'))
                    if lf and (lf['kind'] != f['kind'] or norm(lf['excerpt']) != norm(f['excerpt'])):
                        E(f"{f['id']}: kind or excerpt differs from lint flag {lf['id']}")
    return errs, warns


def check(d: dict, tickets: dict[str, dict] | None, lint: dict | None) -> tuple[list[str], list[str]]:
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(d, schema, schema)
    if errs:
        return errs, []
    warns = []
    keys = [r['ticket']['key'] for r in d['reviews']]
    if len(keys) != len(set(keys)):
        errs.append('a ticket is reviewed twice')
    for i, r in enumerate(d['reviews']):
        tk = tickets.get(r['ticket']['key']) if tickets is not None else None
        if tickets is not None and tk is None:
            errs.append(f"reviews[{i}]: ticket {r['ticket']['key']} is not in --tickets")
        e, w = check_review(r, i, tk if tickets is not None else None, lint)
        errs += e
        warns += w
    if len(d['reviews']) > 1 and 'batch' not in d:
        errs.append('several reviews need a batch section with the worst-first ranking')
    if 'batch' in d:
        b = d['batch']
        if b['ranking'] != rank(d['reviews']):
            errs.append('batch ranking is not worst-first by verdict, blocking questions and score')
        counts = {k: sum(r['verdict']['value'] == k for r in d['reviews'])
                  for k in ('ready', 'ready_with_questions', 'not_ready', 'not_assessed')}
        if b['counts'] != counts:
            errs.append(f'batch counts {b["counts"]} do not match the reviews {counts}')
        if tickets is not None and len(tickets) != len(d['reviews']):
            errs.append(f'{len(tickets)} ticket(s) given but {len(d["reviews"])} reviewed')
    return errs, warns


def load_tickets(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    items = data['tickets'] if isinstance(data, dict) and 'tickets' in data else [data]
    return {t.get('key'): t for t in items}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--tickets', type=Path, help='ticket.json or tickets.json from fetch_ticket.py')
    ap.add_argument('--lint', type=Path, help='lint.json from lint_criteria.py')
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    try:
        d = json.loads(args.report.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: cannot read report: {exc}')
        return 1
    if args.write_summary:
        schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
        pre = [e for e in schema_errors(d, schema, schema) if '.verdict' not in e and 'batch' not in e]
        if pre:
            print('\n'.join(f'ERROR: {e}' for e in pre))
            print('INVALID')
            return 1
        write_summary(d)
        args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    tickets = load_tickets(args.tickets) if args.tickets else None
    lint = json.loads(args.lint.read_text(encoding='utf-8')) if args.lint else None
    errs, warns = check(d, tickets, lint)
    for w in warns:
        print(f'WARN: {w}')
    for e in errs:
        print(f'ERROR: {e}')
    print('INVALID' if errs else 'VALID')
    return 1 if errs else 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
