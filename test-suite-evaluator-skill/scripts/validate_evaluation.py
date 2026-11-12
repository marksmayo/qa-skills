#!/usr/bin/env python3
"""Validate a Test Suite Evaluator evaluation.json against the schema and the rubric (stdlib only).

    python validate_evaluation.py evaluation.json

Beyond the schema it checks what SKILL.md and RUBRIC.md require: all fifteen dimensions with the
rubric's weights (re-weighted only for dimensions marked not applicable), the raw weighted score
and the group scores recomputed from the dimension scores, every triggered cap listed and applied,
the rating band matching the overall score, evidence and confidence for every score, evidence
completeness recomputed from the inventory, the assessment mode supported by the inventory, and
no code-coverage claim that rests on test-derived inference.
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

COVERAGE_CLAIM = re.compile(
    r'\b(code|line|branch|statement|condition|function|instrumented|implementation|source)[- ]coverage\b'
    r'|\b\d+(\.\d+)?\s*%\s*(of\s+)?(the\s+)?(code\s+|line\s+|branch\s+|statement\s+|test\s+)?coverage\b'
    r'|\bcoverage\s+(of|is|at|was|reached|reaches|=|:)\s*\d+(\.\d+)?\s*%', re.I)
NEGATION = re.compile(r"\b(not|no|never|without|unavailable|unmeasured|unmeasurable|unknown|cannot|missing|absent|lacks?|"
                      r"lacking|none|nor)\b|n't\b", re.I)


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


def texts(rep: dict):
    """Yield (path, dimension or None, text) for every free-text field a reader sees."""
    m, h = rep['metadata'], rep['headline']
    for i, n in enumerate(m.get('notes', [])):
        yield f'metadata.notes[{i}]', None, n
    for i, c in enumerate(h.get('applied_caps', [])):
        yield f'headline.applied_caps[{i}].explanation', None, c.get('explanation', '')
    for d in rep['dimensions']:
        yield f'dimensions[{d["id"]}].finding', d, d.get('finding', '')
        for key in ('evidence', 'risks', 'recommendations'):
            for i, t in enumerate(d.get(key, [])):
                yield f'dimensions[{d["id"]}].{key}[{i}]', d, t
    for i, f in enumerate(rep['critical_findings']):
        for t in [f.get('title', ''), f.get('impact', '')] + list(f.get('evidence', [])):
            yield f'critical_findings[{i}]', None, t
    for i, r in enumerate(rep['recommendations']):
        for t in [r.get('title', ''), r.get('action', '')] + list(r.get('acceptance_criteria', [])):
            yield f'recommendations[{i}]', None, t
    for i, e in enumerate(rep['evidence_inventory']):
        yield f'evidence_inventory[{i}].details', None, e.get('details', '')
    for i, b in enumerate(rep.get('behavioural_coverage', [])):
        yield f'behavioural_coverage[{i}].notes', None, b.get('notes', '')
    for i, c in enumerate(rep.get('commands', [])):
        yield f'commands[{i}].summary', None, c.get('summary', '')


def coverage_claims(text: str) -> list[str]:
    out = []
    for sentence in re.split(r'(?<=[.;!?])\s+|\n', text or ''):
        if COVERAGE_CLAIM.search(sentence) and not NEGATION.search(sentence):
            out.append(sentence.strip())
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

    # every score has evidence, confidence and reasoning
    for i, d in dims.items():
        if d['score'] is None:
            continue
        if not [e for e in d.get('evidence', []) if str(e).strip()]:
            errs.append(f'{i}: scored {d["score"]} without evidence')
        if d['evidence_level'] == 'unknown':
            errs.append(f'{i}: scored {d["score"]} with evidence_level unknown; an unsupported dimension is not assessed (score null)')
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
    for i, d in dims.items():
        if d['evidence_level'] == 'specification_derived' and not has('requirements'):
            errs.append(f'{i}: specification_derived evidence needs requirements or specifications in the inventory')

    # execution evidence
    execution = meta['execution']
    if execution in ('full_run', 'partial_run', 'reports_only') and not has('recent_execution'):
        errs.append(f'execution {execution} needs recent execution results in the evidence inventory')
    if execution in STATIC and has('recent_execution'):
        errs.append(f'execution {execution} contradicts recent execution results marked {inv["recent_execution"]}')
    if execution == 'not_executable' and meta.get('provisional') is not True:
        errs.append('a suite that could not be discovered or executed makes the evaluation provisional (metadata.provisional true)')
    if execution in STATIC and head['overall_confidence'] == 'high':
        errs.append('overall confidence cannot be high without execution evidence')

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

    # recommendations
    prios = [r['priority'] for r in rep['recommendations']]
    if len(prios) != len(set(prios)):
        errs.append('recommendation priorities repeat; rank them')

    # code-coverage claims
    cov_evidence = has('runtime_coverage')
    for path, d, text in texts(rep):
        for claim in coverage_claims(text):
            if not cov_evidence:
                errs.append(f'{path}: claims code coverage without a runtime coverage report in the inventory: "{claim[:100]}"')
            elif d is not None and d['evidence_level'] in ('test_derived', 'unknown'):
                errs.append(f'{path}: claims code coverage from {d["evidence_level"]} evidence: "{claim[:100]}"')
    return errs, warns


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # UTF-8 output whatever the locale; never crash on a character
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
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
        return 1
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
          f'evidence {h["evidence_completeness"]:g}%')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
