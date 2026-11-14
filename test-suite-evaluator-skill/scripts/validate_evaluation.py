#!/usr/bin/env python3
"""Validate a Test Suite Evaluator evaluation.json against the schema and the rubric (stdlib only).

    python validate_evaluation.py evaluation.json

Beyond the schema it checks what SKILL.md and RUBRIC.md require: all fifteen dimensions with the
rubric's weights (re-weighted only for dimensions marked not applicable), the raw weighted score
and the group scores recomputed from the dimension scores, every triggered cap listed and applied,
the rating band matching the overall score, evidence and confidence for every score, evidence
completeness recomputed from the inventory, the assessment mode supported by the inventory, the
reliability score consistent with the failures the runs recorded, every untested interface accounted
for, and no code-coverage claim that rests on test-derived inference.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE.parent / 'schemas' / 'evaluation.schema.json'

# id, name, default weight (RUBRIC.md order)
DIMENSIONS = [
    ('effectiveness', 'Test effectiveness', 16),
    ('trust', 'Suite trustworthiness', 14),
    ('reliability', 'Reliability and determinism', 10),
    ('design', 'Test design and level selection', 8),
    ('assertions', 'Assertion and oracle quality', 8),
    ('coverage', 'Test scope and coverage', 8),
    ('isolation', 'Isolation and state management', 7),
    ('maintainability', 'Maintainability and appropriate reuse', 7),
    ('ci', 'CI/CD integration', 6),
    ('debuggability', 'Debuggability and reporting', 4),
    ('performance', 'Performance and feedback speed', 3),
    ('test_data', 'Test data management', 3),
    ('portability', 'Environment portability and realism', 2),
    ('security_a11y', 'Security, privacy, accessibility, compatibility', 2),
    ('ownership', 'Ownership and operational discipline', 2),
]
DEFAULT_WEIGHT = {i: w for i, _, w in DIMENSIONS}
GROUPS = {
    'testing_effectiveness': ['effectiveness'],
    'suite_trustworthiness': ['trust'],
    'engineering_quality': ['design', 'maintainability', 'debuggability', 'test_data', 'portability'],
    'operational_health': ['ci', 'performance', 'ownership'],
}
EVIDENCE_POINTS = {'application_source': 15, 'test_source': 15, 'ci_configuration': 10, 'recent_execution': 15,
                   'historical_ci': 15, 'runtime_coverage': 10, 'mutation_report': 5, 'requirements': 10, 'defect_history': 5}
BANDS = [(9.0, 'Exceptional'), (8.0, 'Excellent'), (6.5, 'Strong'), (5.0, 'Serviceable'), (3.0, 'Weak'), (0.0, 'Dysfunctional')]
STATIC = ('static_only', 'not_executable')
TOL = 0.05 + 1e-9

# A code-coverage claim: a named implementation-coverage measure, a coverage percentage, or a share of the code,
# lines, branches... said to be covered or executed. Matches inside a file name or identifier
# (service-form-branch-coverage.test.tsx, branch_coverage) and text in backticks are not claims.
_NOT_IN_NAME_BEFORE = r'(?<![\w./\\-])'
_NOT_IN_NAME_AFTER = r'(?![\w/\\-]|\.\w)'
# Interface coverage names what it counts right before the word: route, endpoint, interface, operation, requirement,
# specification or acceptance-criteria coverage is not code coverage. Fixed-width lookbehinds, one per word (singular,
# plural, and hyphenated "API-operation coverage").
_INTERFACE_WORDS = ['route', 'routes', 'endpoint', 'endpoints', 'interface', 'interfaces', 'operation', 'operations',
                    'requirement', 'requirements', 'spec', 'specification', 'criteria', 'handler', 'handlers',
                    'command', 'commands', 'export', 'exports', 'tool', 'tools']
_NOT_INTERFACE = ''.join(f'(?<!{w}[ -])' for w in _INTERFACE_WORDS)
COVERAGE_CLAIM = re.compile(
    _NOT_IN_NAME_BEFORE + r'(code|line|branch|statement|condition|function|instrumented|implementation|source)[- ]coverage'
    + _NOT_IN_NAME_AFTER +
    r'|\b\d+(\.\d+)?\s*%\s*(of\s+)?(the\s+)?(code\s+|line\s+|branch\s+|statement\s+|test\s+)?coverage\b'
    # "route coverage is 100%" counts interfaces (interface_coverage), not code; "coverage is 80%" alone is a claim
    r'|' + _NOT_INTERFACE + r'\bcoverage\s+(of|is|at|was|reached|reaches|=|:)\s*\d+(\.\d+)?\s*%'
    r'|\b\d+(\.\d+)?\s*%\s+of\s+(the\s+|all\s+)?(application\s+|app\s+|production\s+|source\s+)?'
    r'(code(base)?|lines|branches|statements|functions|conditions)\b'
    r'|\b(lines|branches|statements|functions|conditions)\s*[:=]?\s*\d+(\.\d+)?\s*%', re.I)
NUMBER = re.compile(r'\d+(\.\d+)?\s*%')
NEGATION = re.compile(r"\b(not|no|never|without|unavailable|unmeasured|unmeasurable|unknown|cannot|missing|absent|lacks?|"
                      r"lacking|none|nor)\b|n't\b", re.I)
# A configured threshold or target is not a claim that it was reached.
TARGET = re.compile(r'\b(thresholds?|targets?|goals?|minimums?|floors?|gates?|required|requires?)\b', re.I)
# Clauses: a negation in one clause ("No tests for 2FA,") does not excuse a claim in the next ("so branch coverage is low").
CLAUSE_SPLIT = re.compile(r'[,;:()–—]|\s-\s|\b(?:so|but|while|whereas|because|although|though|yet|hence|thus|therefore)\b', re.I)
CODE_SPAN = re.compile(r'`[^`\n]*`')
OLD_DIMENSION_IDS = {'trustworthiness': 'trust', 'oracles': 'assertions', 'scope': 'coverage', 'data': 'test_data',
                     'environment': 'portability', 'crosscutting': 'security_a11y'}
PLACEHOLDER = re.compile(r'\b(placeholder|todo|tbd|lorem ipsum|fixme)\b|^\W*$|^\s*(n/?a|-+|\.\.\.)\s*$', re.I)


PLACEHOLDER_LEAD = re.compile(r'^\W*(placeholder|todo|tbd|lorem ipsum|fixme)\b\s*(?:[:\-–—(]|$)', re.I)
PLACEHOLDER_SHORT = 40


def is_placeholder(text: str) -> bool:
    """A finding that stands in for one: empty, n/a, a marker on its own or leading the text ("TODO: ..."), or a short
    text holding a marker. A full sentence that uses the word (a suite whose rules check "placeholder text", a test
    file with a TODO comment) is domain vocabulary, not a placeholder."""
    text = (text or '').strip()
    if not PLACEHOLDER.search(text):
        return False
    return len(text) < PLACEHOLDER_SHORT or bool(PLACEHOLDER_LEAD.search(text)) or not re.search(r'\w', text)
REC_ORDER = ['critical_risk', 'top_improvement', 'quick_win', 'structural', 'evidence_needed']
REF = re.compile(r'\[([A-Za-z][\w-]*)\]')


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


# ---------------------------------------------------------------- computed values
def band(score: float) -> str:
    for low, name in BANDS:
        if score >= low - 1e-9:
            return name
    return 'Dysfunctional'


def weighted(dims: dict, ids) -> float | None:
    scored = [(dims[i]['score'], dims[i]['weight']) for i in ids
              if i in dims and dims[i].get('score') is not None and dims[i]['weight'] > 0]
    total = sum(w for _, w in scored)
    return sum(s * w for s, w in scored) / total if total else None


def expected_caps(rep: dict, dims: dict) -> dict[str, float]:
    caps = {}
    score = lambda i: dims.get(i, {}).get('score')  # noqa: E731
    if score('effectiveness') is not None and score('effectiveness') < 4:
        caps['effectiveness_below_4'] = 4
    if score('trust') is not None and score('trust') < 4:
        caps['trust_below_4'] = 4
    if score('reliability') is not None and score('reliability') < 4:
        caps['reliability_below_4'] = 5
    if score('assertions') is not None and score('assertions') < 3:
        caps['assertions_below_3'] = 3
    if score('coverage') is not None and score('coverage') < 4:
        caps['coverage_below_4'] = 4
    execution = rep['metadata']['execution']
    if execution == 'not_executable':
        caps['not_executable'] = 5
    if execution in STATIC:
        caps['static_only'] = 8
    for f in rep['critical_findings']:
        cat = f.get('category', 'other')
        if cat in ('secret_exposure', 'production_data_exposure') and not f.get('remediated'):
            caps['secret_or_production_data'] = 4
        if cat == 'systematic_false_pass':
            caps['systematic_false_pass'] = 4
    return caps


def ev_text(e) -> str:
    return e.get('text', '') if isinstance(e, dict) else str(e)


def ev_level(e, d: dict) -> str:
    """An evidence item's level: its own when it states one, otherwise its dimension's."""
    return e['level'] if isinstance(e, dict) else d['evidence_level']


def texts(rep: dict):
    """Yield (path, dimension or None, evidence item or None, text) for every free-text field a reader sees."""
    m, h = rep['metadata'], rep['headline']
    for i, n in enumerate(m.get('notes', [])):
        yield f'metadata.notes[{i}]', None, None, n
    for i, c in enumerate(h.get('applied_caps', [])):
        yield f'headline.applied_caps[{i}].explanation', None, None, c.get('explanation', '')
    er = m.get('execution_result') or {}
    for i, l in enumerate(er.get('layers', [])):
        yield f'metadata.execution_result.layers[{i}].reason', None, None, l.get('reason', '')
    for i, r in enumerate(er.get('runs', [])):
        yield f'metadata.execution_result.runs[{i}].notes', None, None, r.get('notes', '')
    for i, f in enumerate(er.get('failures', [])):
        yield f'metadata.execution_result.failures[{i}]', None, None, f.get('evidence', '')
    for d in rep['dimensions']:
        yield f'dimensions[{d["id"]}].finding', d, None, d.get('finding', '')
        for i, e in enumerate(d.get('evidence', [])):
            yield f'dimensions[{d["id"]}].evidence[{i}]', d, e, ev_text(e)
        for key in ('risks', 'recommendations'):
            for i, t in enumerate(d.get(key, [])):
                yield f'dimensions[{d["id"]}].{key}[{i}]', d, None, t
    for i, f in enumerate(rep['critical_findings']):
        for t in [f.get('title', ''), f.get('impact', '')] + list(f.get('evidence', [])):
            yield f'critical_findings[{i}]', None, None, t
    for i, r in enumerate(rep['recommendations']):
        for t in [r.get('title', ''), r.get('action', '')] + list(r.get('acceptance_criteria', [])):
            yield f'recommendations[{i}]', None, None, t
    for i, e in enumerate(rep['evidence_inventory']):
        yield f'evidence_inventory[{i}].details', None, None, e.get('details', '')
    for i, b in enumerate(rep.get('behavioural_coverage', [])):
        yield f'behavioural_coverage[{i}].notes', None, None, b.get('notes', '')
    for i, b in enumerate(rep.get('interface_coverage', [])):
        for t in [b.get('method', ''), b.get('notes', '')]:
            yield f'interface_coverage[{i}]', None, None, t
    for i, c in enumerate(rep.get('commands', [])):
        yield f'commands[{i}].summary', None, None, c.get('summary', '')


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.;!?])\s+|\n', text or '') if s.strip()]


def coverage_claims(text: str) -> list[str]:
    """Sentences that claim code coverage.

    A stated percentage is always a claim, whatever else the sentence says, unless its clause is about a configured
    threshold or target. A named measure without a number ("branch coverage") is a claim unless its own clause
    negates it ("Instrumented code coverage was not measurable"). Backticked text, file names and identifiers are
    never claims.
    """
    out = []
    for sentence in sentences(text):
        plain = CODE_SPAN.sub(' ', sentence)
        if not COVERAGE_CLAIM.search(plain):
            continue
        for clause in CLAUSE_SPLIT.split(plain):
            clause = clause or ''
            m = COVERAGE_CLAIM.search(clause)
            if not m:
                continue
            numeric = any(NUMBER.search(x.group(0)) for x in COVERAGE_CLAIM.finditer(clause))
            if numeric and not TARGET.search(clause):
                out.append(sentence)
                break
            if not numeric and not NEGATION.search(clause):
                out.append(sentence)
                break
        else:
            # the claim straddles a clause boundary ("coverage: 83%"): judge the whole sentence
            if not any(COVERAGE_CLAIM.search(c or '') for c in CLAUSE_SPLIT.split(plain)):
                numeric = any(NUMBER.search(x.group(0)) for x in COVERAGE_CLAIM.finditer(plain))
                if (numeric and not TARGET.search(plain)) or (not numeric and not NEGATION.search(plain)):
                    out.append(sentence)
    return out


# ---------------------------------------------------------------- checks
def check(rep: dict) -> tuple[list[str], list[str]]:
    errs, warns = [], []
    meta, head = rep['metadata'], rep['headline']

    # dimensions: all fifteen, once each, with the rubric's weights
    dims = {}
    for d in rep['dimensions']:
        if d['id'] in dims:
            errs.append(f'dimension {d["id"]} appears twice')
        dims[d['id']] = d
    missing = [i for i, _, _ in DIMENSIONS if i not in dims]
    if missing:
        errs.append(f'dimensions missing: {", ".join(missing)} (list every dimension; unassessed ones get score null)')
        return errs, warns
    na = [i for i, d in dims.items() if d.get('applicable') is False]
    for i, d in dims.items():
        if d.get('applicable') is False:
            if d['score'] is not None or d['weight'] != 0:
                errs.append(f'{i}: a not-applicable dimension has score null and weight 0')
            if not (d.get('not_applicable_reason') or '').strip():
                errs.append(f'{i}: not applicable needs not_applicable_reason')
    applicable_default = sum(DEFAULT_WEIGHT[i] for i in dims if i not in na)
    for i, d in dims.items():
        if i in na:
            continue
        want = DEFAULT_WEIGHT[i] * 100 / applicable_default
        if abs(d['weight'] - want) > 0.051:
            why = 'the rubric default' if not na else 'the rubric default re-scaled over the applicable dimensions'
            errs.append(f'{i}: weight {d["weight"]} should be {round(want, 2)} ({why}; do not re-weight for missing evidence)')
    total = sum(d['weight'] for d in dims.values())
    if abs(total - 100) > 0.5:
        errs.append(f'dimension weights total {total}, not 100')

    # every score has evidence, confidence and reasoning; evidence levels are per item when they differ
    for i, d in dims.items():
        evidence = [e for e in d.get('evidence', []) if ev_text(e).strip()]
        level = d['evidence_level']
        item_levels = {e['level'] for e in evidence if isinstance(e, dict)}
        ids = [e['id'] for e in evidence if isinstance(e, dict) and e.get('id')]
        if len(ids) != len(set(ids)):
            errs.append(f'{i}: evidence ids repeat')
        if level == 'mixed':
            if any(not isinstance(e, dict) for e in evidence):
                errs.append(f'{i}: a mixed dimension gives every evidence item its own level ({{"text", "level"}})')
            if len(item_levels) < 2:
                errs.append(f'{i}: evidence_level mixed needs evidence items of at least two levels; '
                            f'otherwise use their one level')
        elif item_levels - {level}:
            errs.append(f'{i}: evidence items at {", ".join(sorted(item_levels - {level}))} in a dimension recorded as '
                        f'{level}; use mixed (the dimension level must hold for all its evidence)')
        if is_placeholder(d.get('finding', '')):
            errs.append(f'{i}: finding is a placeholder ("{d["finding"][:40]}")')
        if d['score'] is None:
            if d.get('applicable') is not False:
                if level != 'unknown':
                    errs.append(f'{i}: not assessed (score null) but evidence_level is {level}; an unassessed dimension '
                                f'is unknown, or score it from its evidence')
                if 'not assessed' not in d.get('finding', '').lower():
                    errs.append(f'{i}: score null, so the finding must say "Not assessed" and why')
            continue
        if not evidence:
            errs.append(f'{i}: scored {d["score"]} without evidence')
        if level == 'unknown':
            errs.append(f'{i}: scored {d["score"]} with evidence_level unknown; an unsupported dimension is not assessed (score null)')
        if d['confidence'] == 'high':
            if level == 'test_derived':
                errs.append(f'{i}: confidence high needs direct evidence; test_derived inference is medium at most')
            if len(evidence) < 2:
                errs.append(f'{i}: confidence high needs multiple consistent evidence sources (at least two evidence items)')
    # thin suites: these dimensions grade the few tests that exist, so they can't be held with high confidence when the
    # suite reaches little of the product
    cov = dims['coverage']['score']
    if cov is not None and cov < 4:
        for i in ('reliability', 'assertions', 'isolation', 'performance'):
            if dims[i]['score'] is not None and dims[i]['confidence'] == 'high':
                errs.append(f'{i}: coverage is {cov}, so the {i} score describes a small sample of the product; its '
                            f'confidence is medium at most')
    scored = [i for i, d in dims.items() if d['score'] is not None]
    if not scored:
        errs.append('no dimension is scored, so there is no weighted score to report')
        return errs, warns

    # inventory and evidence completeness
    inv = {}
    for e in rep['evidence_inventory']:
        if e['kind'] != 'other':
            if e['kind'] in inv:
                errs.append(f'evidence_inventory lists {e["kind"]} twice')
            inv[e['kind']] = e['status']
        if e['kind'] == 'historical_ci' and e.get('origin') == 'assessor' and e['status'] in ('available', 'partial'):
            errs.append('historical_ci cannot come from the assessor\'s own runs: they are recent execution evidence '
                        '(and may support a flakiness observation), not CI history')
        if e['status'] in ('missing', 'partial') and e['kind'] != 'other' and not (e.get('details') or '').strip():
            warns.append(f'evidence_inventory {e["kind"]} is {e["status"]} with no details saying what is lacking')
    absent = [k for k in EVIDENCE_POINTS if k not in inv]
    if absent:
        errs.append(f'evidence_inventory lacks {", ".join(absent)} (list every RUBRIC item, as missing if absent)')
    has = lambda k: inv.get(k) in ('available', 'partial')  # noqa: E731
    points = sum(EVIDENCE_POINTS[k] * {'available': 1, 'partial': 0.5}.get(s, 0) for k, s in inv.items())
    if abs(head['evidence_completeness'] - points) > 1.0:
        errs.append(f'evidence_completeness {head["evidence_completeness"]} should be {points:g} from the inventory '
                    f'(RUBRIC points; partial counts half)')

    # assessment mode supported by the inventory
    mode = meta['assessment_mode']
    if mode == 'white_box' and not has('application_source'):
        errs.append('white_box mode needs application source in the evidence inventory')
    if mode == 'grey_box' and not has('requirements'):
        errs.append('grey_box mode needs requirements or specifications in the evidence inventory')
    strongest = 'white_box' if inv.get('application_source') == 'available' else \
        'grey_box' if inv.get('requirements') == 'available' else 'black_box'
    order = ['black_box', 'grey_box', 'white_box']
    if order.index(strongest) > order.index(mode):
        warns.append(f'the inventory supports {strongest} mode but the evaluation records {mode}')
    if mode == 'white_box' and not (has('runtime_coverage') or has('mutation_report')):
        warns.append('white_box mode from application source only: no runtime coverage or mutation report, so say in '
                     'metadata.notes that implementation-linked claims rest on reading source, not instrumentation')
    for i, d in dims.items():
        levels = {d['evidence_level']} | {e['level'] for e in d.get('evidence', []) if isinstance(e, dict)}
        if 'specification_derived' in levels and not has('requirements'):
            errs.append(f'{i}: specification_derived evidence needs requirements or specifications in the inventory')
    for i, b in enumerate(rep.get('interface_coverage', [])):
        if b['tested'] > b['total']:
            errs.append(f'interface_coverage[{i}]: tested {b["tested"]} exceeds total {b["total"]}')
        listed = sum(u.get('count', 1) if isinstance(u, dict) else 1 for u in b.get('untested', []))
        if listed != b['total'] - b['tested']:
            errs.append(f'interface_coverage[{i}]: untested accounts for {listed} interface(s) but {b["total"]} - '
                        f'{b["tested"]} = {b["total"] - b["tested"]} are untested; list each one, or group them as '
                        f'{{"group": ..., "count": n}}')
        if b['evidence_level'] == 'specification_derived' and not has('requirements'):
            errs.append(f'interface_coverage[{i}]: specification_derived needs requirements or specifications in the inventory')

    # execution evidence
    execution = meta['execution']
    if execution in ('full_run', 'partial_run', 'reports_only') and not has('recent_execution'):
        errs.append(f'execution {execution} needs recent execution results in the evidence inventory')
    if execution in STATIC and has('recent_execution'):
        errs.append(f'execution {execution} contradicts recent execution results marked {inv["recent_execution"]}')
    if execution in STATIC and meta.get('provisional') is not True:
        errs.append(f'execution {execution}: no execution evidence supports the scores, so the evaluation is '
                    f'provisional (metadata.provisional true)')
    if execution not in STATIC and meta.get('provisional'):
        errs.append(f'provisional is true only when nothing was executed (static_only or not_executable), not for {execution}')
    if execution in STATIC and head['overall_confidence'] == 'high':
        errs.append('overall confidence cannot be high without execution evidence')
    errs += execution_errors(meta)
    errs += reliability_errors(meta, dims)

    # raw weighted score and group scores
    raw = weighted(dims, [i for i, _, _ in DIMENSIONS])
    if abs(head['raw_weighted_score'] - raw) > TOL:
        errs.append(f'raw_weighted_score {head["raw_weighted_score"]} should be {raw:.1f} '
                    f'(weighted mean of the scored dimensions; unassessed ones are excluded, never zero)')
    for key, ids in GROUPS.items():
        want = weighted(dims, ids)
        if key == 'operational_health' and not has('historical_ci'):
            if head[key] is not None:
                errs.append('operational_health needs historical CI outcomes in the inventory; without them it is not assessed (null)')
            continue
        got = head[key]
        if want is None and got is not None:
            errs.append(f'{key} is {got} but none of {", ".join(ids)} is scored; it must be null (not assessed)')
        elif want is not None and (got is None or abs(got - want) > TOL):
            errs.append(f'{key} should be {want:.1f} (weighted mean of {", ".join(ids)})')

    # caps
    caps = expected_caps(rep, dims)
    listed = {}
    for c in head['applied_caps']:
        if c['rule'] in listed:
            errs.append(f'cap {c["rule"]} listed twice')
        listed[c['rule']] = c
    for rule, limit in caps.items():
        if rule not in listed:
            errs.append(f'cap {rule} (maximum {limit}) applies but is not listed in headline.applied_caps')
        elif listed[rule]['limit'] != limit:
            errs.append(f'cap {rule} has limit {listed[rule]["limit"]}, the rubric says {limit}')
    for rule in listed:
        if rule not in caps:
            errs.append(f'cap {rule} is listed but its condition does not hold')
    for f in rep['critical_findings']:
        if f.get('category') in ('secret_exposure', 'production_data_exposure') and f['severity'] != 'critical':
            errs.append(f'critical finding "{f["title"]}": secret or production-data exposure is severity critical')
    want_overall = min([raw] + list(caps.values()))
    if abs(head['overall_score'] - want_overall) > TOL:
        errs.append(f'overall_score {head["overall_score"]} should be {want_overall:.1f} '
                    f'(raw weighted score with the lowest applicable cap applied)')
    if head['overall_score'] > head['raw_weighted_score'] + TOL:
        errs.append('overall_score is higher than raw_weighted_score; caps only lower it')

    # headline band
    want_band = band(head['overall_score'])
    if head['rating_band'] != want_band:
        errs.append(f'rating_band {head["rating_band"]!r} does not match overall {head["overall_score"]} ({want_band!r})')
    if execution in STATIC and head['rating_band'] == 'Exceptional':
        errs.append('a static-only assessment cannot be labelled Exceptional')

    # recommendations: the Step 9 groups, ranked in their order
    recs = rep['recommendations']
    prios = [r['priority'] for r in recs]
    if len(prios) != len(set(prios)):
        errs.append('recommendation priorities repeat; rank them')
    cats = [r['category'] for r in sorted(recs, key=lambda r: r['priority'])]
    if cats != sorted(cats, key=REC_ORDER.index):
        errs.append('recommendation priorities must rank the groups in Step 9 order: critical_risk, top_improvement, '
                    'quick_win, structural, evidence_needed')
    n_top = cats.count('top_improvement')
    if not 1 <= n_top <= 3:
        errs.append(f'{n_top} top_improvement recommendations; Step 9 asks for the three highest-leverage ones (1 to 3)')
    serious = [f for f in rep['critical_findings']
               if f['severity'] == 'critical' or f.get('category', 'other') != 'other'] or \
        [r for r in caps if r not in ('static_only', 'not_executable')]
    if serious and 'critical_risk' not in cats:
        errs.append('a finding of severity critical, a finding in a capping category, or a score cap applies, so at '
                    'least one recommendation is a critical_risk')
    lacking = {k for k, s in inv.items() if s in ('missing', 'partial')}
    if lacking and 'evidence_needed' not in cats:
        errs.append(f'evidence is missing or partial ({", ".join(sorted(lacking))}); add an evidence_needed '
                    f'recommendation saying what would strengthen a reassessment')
    for r in recs:
        if r['category'] == 'quick_win' and r['effort'] != 'small':
            errs.append(f'recommendation "{r["title"]}": a quick win is small effort, not {r["effort"]}')
        if r['category'] == 'evidence_needed':
            kinds = r.get('evidence_kinds') or []
            if not kinds:
                errs.append(f'recommendation "{r["title"]}": evidence_needed names the evidence_kinds it would supply')
            for k in kinds:
                if k not in lacking:
                    errs.append(f'recommendation "{r["title"]}": evidence kind {k} is not missing or partial in the inventory')
        elif not r.get('dimensions_affected'):
            errs.append(f'recommendation "{r["title"]}": name the dimensions_affected')
    for c in ('quick_win', 'structural'):
        if c not in cats:
            warns.append(f'no {c} recommendation; Step 9 asks for one unless none exists (say so in metadata.notes)')

    # code-coverage claims: need a runtime coverage report, and inside a dimension, verified evidence for that sentence
    cov_evidence = has('runtime_coverage')
    for path, d, item, text in texts(rep):
        for claim in coverage_claims(text):
            if not cov_evidence:
                errs.append(f'{path}: claims code coverage without a runtime coverage report in the inventory: "{claim[:100]}"')
                continue
            if d is None:
                continue
            if item is not None:
                lvl = ev_level(item, d)
                if lvl != 'verified':
                    errs.append(f'{path}: claims code coverage from {lvl} evidence: "{claim[:100]}"')
            elif d['evidence_level'] == 'mixed':
                verified = {e['id'] for e in d.get('evidence', []) if isinstance(e, dict) and e.get('id')
                            and e['level'] == 'verified'}
                if not set(REF.findall(claim)) & verified:
                    errs.append(f'{path}: claims code coverage in a mixed dimension without citing a verified evidence '
                                f'item as [id]: "{claim[:100]}"')
            elif d['evidence_level'] != 'verified':
                errs.append(f'{path}: claims code coverage from {d["evidence_level"]} evidence: "{claim[:100]}"')
    for d in dims.values():
        known = {e['id'] for e in d.get('evidence', []) if isinstance(e, dict) and e.get('id')}
        for key in ('finding', 'risks', 'recommendations'):
            vals = [d.get(key, '')] if key == 'finding' else d.get(key, [])
            for t in vals:
                for ref in REF.findall(t):
                    if known and ref not in known and re.fullmatch(r'[A-Za-z]{1,3}\d+', ref):
                        errs.append(f'{d["id"]}.{key}: cites [{ref}], which is not an evidence id of this dimension')
    return errs, warns


def reliability_errors(meta: dict, dims: dict) -> list[str]:
    """A test-class failure that appears in some recorded runs and not in others is a flaky or order-dependent test.
    Reliability 7 and above needs repeated runs that agree (RUBRIC 3), so it can't stand beside one."""
    er = meta.get('execution_result') or {}
    score = dims['reliability']['score']
    if score is None or score < 7:
        return []
    runs = {r['id']: r for r in er.get('runs', [])}
    flaky = []
    for f in er.get('failures', []):
        failed_in = [r for r in f.get('runs_failed', []) if r in runs]
        if f['class'] != 'test' or not failed_in:
            continue
        layers = {name for r in failed_in for name in runs[r]['layers']}
        others = [rid for rid, r in runs.items() if rid not in failed_in and layers & set(r['layers'])]
        if others:
            flaky.append(f'{f["test"]!r} (failed in {", ".join(failed_in)}, passed in {", ".join(others)})')
    if flaky:
        return [f'reliability {score}: the runs disagree on test-class failures ({"; ".join(flaky[:3])}); repeated runs '
                f'that disagree are at most 6 (RUBRIC 3), or classify the failure if it is not the test\'s']
    return []


def execution_errors(meta: dict) -> list[str]:
    """What the runs showed: layers run or not, counts, and classified failures."""
    errs = []
    execution = meta['execution']
    er = meta.get('execution_result')
    if execution in ('full_run', 'partial_run') and not er:
        return [f'execution {execution} needs metadata.execution_result: the layers, each run\'s pass/fail/skip/error '
                f'counts, and the failures classified']
    if not er:
        return errs
    layers = {l['name']: l for l in er['layers']}
    if len(layers) != len(er['layers']):
        errs.append('execution_result.layers repeat a name')
    for l in er['layers']:
        if l['status'] != 'run' and not (l.get('reason') or '').strip():
            errs.append(f'execution_result layer {l["name"]} is {l["status"]}; give the reason')
    statuses = {l['status'] for l in er['layers']}
    if execution == 'full_run' and statuses != {'run'}:
        not_run = [l['name'] for l in er['layers'] if l['status'] != 'run']
        errs.append(f'execution full_run but layer(s) {", ".join(not_run)} were not fully run; that is partial_run')
    if execution == 'partial_run' and statuses == {'run'}:
        errs.append('execution partial_run but every layer is recorded as run; record the layer or tests left out')
    runs = er['runs']
    run_ids = [r['id'] for r in runs]
    if len(run_ids) != len(set(run_ids)):
        errs.append('execution_result run ids repeat')
    # a run stopped before it finished: the tests it never reached are counted, and a layer that no complete run covered
    # was not fully run
    for r in runs:
        if r.get('stopped') and not isinstance(r.get('not_reached'), int):
            errs.append(f'run {r["id"]} was stopped before it finished; record not_reached (the tests it never reached, '
                        f'from the collected count minus those reported)')
    for name, l in layers.items():
        covering = [r for r in runs if name in r['layers']]
        if l['status'] == 'run' and covering and all(r.get('stopped') for r in covering):
            errs.append(f'layer {name} is recorded as run but every run of it was stopped before it finished; '
                        f'it is partial, and the execution partial_run')
    assessor = [r for r in runs if r['origin'] == 'assessor']
    if execution in ('full_run', 'partial_run') and not assessor:
        errs.append(f'execution {execution} means the assessor ran the suite; record at least one run with origin assessor')
    if execution == 'reports_only' and assessor:
        errs.append('execution reports_only but runs with origin assessor are recorded; that is full_run or partial_run')
    if execution in STATIC and any(r['passed'] + r['failed'] for r in runs):
        errs.append(f'execution {execution} but runs with test results are recorded')
    for r in runs:
        for name in r['layers']:
            if name not in layers:
                errs.append(f'run {r["id"]} names layer {name!r}, which is not in execution_result.layers')
            elif layers[name]['status'] == 'not_run' and r['origin'] == 'assessor':
                errs.append(f'run {r["id"]} ran layer {name!r}, which is recorded as not_run')
    for name, l in layers.items():
        if l['status'] in ('run', 'partial') and execution in ('full_run', 'partial_run') and \
                not any(name in r['layers'] for r in assessor):
            errs.append(f'layer {name!r} is recorded as {l["status"]} but no assessor run covers it')
    failures = er.get('failures', [])
    failing = max([r['failed'] + r['errored'] for r in runs] or [0])
    classified = sum(f.get('count', 1) for f in failures)
    if failing and classified < failing:
        errs.append(f'a run had {failing} failing or erroring tests but execution_result.failures classifies only '
                    f'{classified}; classify each as product, test, environment or unknown')
    # per run: the failures classified as failing in it add up to no more than it recorded, and, when every failure
    # names its runs, to exactly what it recorded (a miscount in either direction is caught, not just a shortfall)
    every_named = bool(failures) and all(f.get('runs_failed') for f in failures)
    for r in runs:
        recorded = r['failed'] + r['errored']
        attributed = sum(f.get('count', 1) for f in failures if r['id'] in f.get('runs_failed', []))
        if attributed > recorded:
            errs.append(f'run {r["id"]} recorded {recorded} failing or erroring tests but execution_result.failures '
                        f'classifies {attributed} as failing in it; make the counts add up to the run')
        elif every_named and attributed < recorded:
            errs.append(f'run {r["id"]} recorded {recorded} failing or erroring tests but the failures naming it add up '
                        f'to {attributed}; classify the other {recorded - attributed}')
    for f in failures:
        for rid in f.get('runs_failed', []):
            if rid not in run_ids:
                errs.append(f'failure {f["test"]!r} names run {rid!r}, which is not recorded')
        for rid in f.get('runs_failed', []):
            r = next((x for x in runs if x['id'] == rid), None)
            if r is not None and r['failed'] + r['errored'] == 0:
                errs.append(f'failure {f["test"]!r} says it failed in run {rid}, which had no failures')
    return errs


def self_test() -> int:
    """Failure counts against run totals, on the partial-run example."""
    import copy
    base = json.loads((HERE.parent / 'examples' / 'evaluation.partial-run.example.json').read_text(encoding='utf-8'))
    fails = []

    def errs_for(mutate) -> list[str]:
        rep = copy.deepcopy(base)
        mutate(rep['metadata']['execution_result'])
        return execution_errors(rep['metadata'])

    if errs_for(lambda er: None):
        fails.append('the example as shipped: ' + '; '.join(errs_for(lambda er: None)))

    def over(er):                     # two more failures classified in r1 than r1 recorded
        er['failures'][2]['count'] += 2
    if not any('r1' in e and 'classif' in e for e in errs_for(over)):
        fails.append('classifying more failures in a run than the run recorded is an error')

    def under(er):                    # every failure names its runs, and r2's add up to less than r2 recorded
        er['runs'][1]['failed'] += 1
        er['runs'][1]['passed'] -= 1
    if not any('r2' in e for e in errs_for(under)):
        fails.append('a run whose recorded failures exceed the failures attributed to it is an error')
    # a run stopped before it finished (a killed xdist run writes no JUnit): its unreached tests are counted, and a layer
    # that only stopped runs covered is partial, never run
    def stop_first(er):
        er['runs'][0]['stopped'] = True
    if not any('r1' in e and 'not_reached' in e for e in errs_for(stop_first)):
        fails.append('a stopped run without not_reached is an error')

    def stop_all(er):
        for r in er['runs']:
            r.update(stopped=True, not_reached=120)
    if not any('unit' in e and 'stopped' in e for e in errs_for(stop_all)):
        fails.append('a layer recorded as run when every run of it was stopped is an error')

    def stop_one(er):
        er['runs'][0].update(stopped=True, not_reached=120)
    if errs_for(stop_one):
        fails.append('a stopped run beside complete runs of the same layer is fine: ' + '; '.join(errs_for(stop_one)))

    # a finding is a placeholder when it is one, not when a sentence uses the word as the suite's own vocabulary
    for text, want in (('TODO', True), ('TBD: fill in after the runs', True), ('placeholder', True), ('n/a', True),
                       ('...', True), ('FIXME - write this up once the second run finishes', True),
                       ('The data rules check spelling, tip length and placeholder text in every content file, but no test '
                        'reads the rendered page, so a template change that drops a section passes.', False),
                       ('Two tests carry a TODO comment about a missing teardown (tests/test_cart.py:12, :40); the '
                        'shared fixture still cleans up, so the suite is reliable.', False)):
        if is_placeholder(text) != want:
            fails.append(f'is_placeholder({text[:40]!r}) should be {want}')
    if fails:
        print('validate_evaluation self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('validate_evaluation self-test: ok')
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # UTF-8 output whatever the locale; never crash on a character
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('evaluation', type=Path)
    args = ap.parse_args(argv)
    try:
        rep = json.loads(args.evaluation.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        print(f'INVALID: cannot read {args.evaluation}: {exc}')
        return 1
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(rep, schema, schema)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        old = sorted({d.get('id') for d in rep.get('dimensions', []) if isinstance(d, dict)} & set(OLD_DIMENSION_IDS))
        if old:
            print('  hint: this looks like an evaluation written for a contract before 1.1; rename dimension ids '
                  + ', '.join(f'{o} -> {OLD_DIMENSION_IDS[o]}' for o in old)
                  + ', then add the fields SKILL.md "Migrating older evaluations" lists')
        written = (rep.get('metadata') or {}).get('skill_version') if isinstance(rep.get('metadata'), dict) else None
        current = skill_version()
        if written and current and str(written).split('.')[:2] != current.split('.')[:2]:
            print(f'  hint: written for skill {written}; this validator checks the {current} contract. '
                  f'See SKILL.md "Migrating older evaluations".')
        return 1
    current = skill_version()
    written = rep['metadata']['skill_version']
    if current and written.split('.')[:2] != current.split('.')[:2]:
        print(f'WARNING: written for skill {written}, this validator checks the {current} contract; '
              f'see SKILL.md "Migrating older evaluations"')
    errs, warns = check(rep)
    for w in warns:
        print('WARNING: ' + w)
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    h = rep['headline']
    scored = sum(1 for d in rep['dimensions'] if d['score'] is not None)
    print(f'VALID: overall {h["overall_score"]:.1f} ({h["rating_band"]}), raw {h["raw_weighted_score"]:.1f}, '
          f'{len(h["applied_caps"])} cap(s), {scored}/{len(rep["dimensions"])} dimensions scored, '
          f'evidence {h["evidence_completeness"]:.1f}%')
    return 0


def skill_version() -> str | None:
    """The version in SKILL.md's frontmatter, which names the contract this validator checks."""
    try:
        m = re.search(r'^version:\s*([\d.]+)\s*$', (HERE.parent / 'SKILL.md').read_text(encoding='utf-8'), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


if __name__ == '__main__':
    raise SystemExit(main())
