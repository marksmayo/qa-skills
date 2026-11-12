#!/usr/bin/env python3
"""Validate a mutation testing report against the schema and the rubric's honesty rules (stdlib only).

    python validate_mutation_report.py mutation_report.json [--mutants mutants.json] [--runs runs.json]
        [--proofs proofs.json] [--repo <repo>] [--write-summary]

--mutants checks the report's mutants and engines are exactly the normalised results.
--runs checks the green baseline against the real runs. --proofs checks every cited proof
passed on the original code and failed on each mutant. --repo checks the code hash, each
mutant's original text and enclosing function, and computes each finding's proximity (whether a
covering test calls the function directly). --write-summary fills in the computed fields: each
ranked finding's kind factor, proximity, score and priority, and every summary count, score and
score text (needs --repo). The headline stays yours, and any percentage in it must be one of the
computed scores, shown with its counts.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests  # noqa: E402
import ts_scan  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'mutation_report.schema.json'
OPS = json.loads((HERE.parent / 'data' / 'operators.json').read_text(encoding='utf-8'))
KIND_FACTOR = {k: v['factor'] for k, v in OPS['kinds'].items()}
BANDS = [(12, 'P0'), (8, 'P1'), (4, 'P2'), (0, 'P3')]
RANKED = ('weak_assertion', 'missing_case')
STATUSES = ('killed', 'survived', 'no_coverage', 'timeout', 'runtime_error', 'compile_error', 'not_run')
BANNED = re.compile(r'\bwell[- ]tested\b|\bstrong(ly)?[- ]tested\b|\bstrong tests\b|\bthoroughly tested\b|\bfully tested\b|'
                    r'\bbullet-?proof\b|\btests are (good|solid|strong|robust|sufficient)\b|\bno weak (tests|assertions)\b', re.I)
EXTRAPOLATE = re.compile(r'\b(whole|entire|overall|total) (code ?base|repo(sitory)?|project|suite|application)\b|'
                         r'\bcode ?base[- ]wide\b|\bacross the (code ?base|repo(sitory)?)\b', re.I)
PCT = re.compile(r'(\d+(?:\.\d+)?)\s?%')
GENERIC_REASON = re.compile(r'^\s*(equivalent|same( behaviou?r)?|no difference|cannot be killed|unkillable)\W*$', re.I)


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


# ---------------------------------------------------------------- computed fields
def priority(score: int) -> str:
    return next(p for floor, p in BANDS if score >= floor)


def calls_in(func: ast.AST, name: str) -> bool:
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                return True
    return False


def test_calls(repo: Path, test_id: str, name: str, cache: dict) -> bool | None:
    """Does the test's own body (or its class's) call `name` directly? None if the test can't be found."""
    path, _, rest = test_id.partition('::')
    if ts_scan.is_js(path):
        if path not in cache:
            p = repo / path
            cache[path] = p.read_text(encoding='utf-8') if p.is_file() else None
        if cache[path] is None or not rest:
            return None
        t = ts_scan.find_test(cache[path], rest)
        return None if t is None else ts_scan.calls(cache[path], t['start'], t['end'], name)
    parts = [p.split('[')[0] for p in rest.split('::') if p]
    if path not in cache:
        p = repo / path
        try:
            cache[path] = ast.parse(p.read_text(encoding='utf-8')) if p.is_file() else None
        except SyntaxError:
            cache[path] = None
    tree = cache[path]
    if tree is None or not parts:
        return None
    node = tree
    for part in parts:
        node = next((n for n in getattr(node, 'body', []) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            return None
    return calls_in(node, name)


def proximity(f: dict, mutants: dict, repo: Path | None, cache: dict) -> int | None:
    if not f['function']:
        return 1
    if repo is None:
        return None
    name = f['function'].split('.')[-1]
    tests = sorted({t for mid in f['mutant_ids'] if mid in mutants for t in mutants[mid]['covering_tests']})
    return 2 if any(test_calls(repo, t, name, cache) for t in tests) else 1


def engine_summary(engine: str, mutants: list[dict], findings: list[dict], n_files: int, scope: dict) -> dict:
    ms = [m for m in mutants if m['engine'] == engine]
    c = {s: sum(1 for m in ms if m['status'] == s) for s in STATUSES}
    ids = {m['id'] for m in ms}
    eq = sum(1 for f in findings if f['triage'] == 'equivalent' for mid in f['mutant_ids'] if mid in ids)
    acc = sum(1 for f in findings if f['triage'] == 'accepted' for mid in f['mutant_ids'] if mid in ids)
    of = c['killed'] + c['survived'] - eq + c['no_coverage']
    of_incl = of + c['timeout'] + c['runtime_error']
    k_incl = c['killed'] + c['timeout'] + c['runtime_error']
    v = round(c['killed'] / of, 4) if of else None
    vi = round(k_incl / of_incl, 4) if of_incl else None
    if scope['mode'] == 'changed':
        where = f'in {n_files} changed file{"s" if n_files != 1 else ""}'
    elif scope['mode'] == 'path':
        where = 'in ' + ', '.join(scope.get('paths') or [x['path'] for x in scope['files']])
    else:
        where = f'in the whole repository ({n_files} files)'
    if not of:
        text = f'{engine}: no scoreable mutants {where}'
    else:
        text = f'{engine}: {c["killed"]} of {of} mutants killed {where} ({100 * v:.1f}%)'
        extra = []
        if eq:
            extra.append(f'{eq} equivalent excluded')
        if c['timeout']:
            extra.append(f'{c["timeout"]} timed out')
        if c['runtime_error']:
            extra.append(f'{c["runtime_error"]} runtime error{"s" if c["runtime_error"] != 1 else ""}')
        if c['compile_error']:
            extra.append(f'{c["compile_error"]} not compiling (dropped)')
        if c['not_run']:
            extra.append(f'{c["not_run"]} not run')
        if extra:
            text += '; ' + ', '.join(extra)
        if c['timeout'] or c['runtime_error']:
            text += f' ({k_incl} of {of_incl}, {100 * vi:.1f}%, counting timeouts and runtime errors as kills)'
    return {'engine': engine, 'total': len(ms), **c, 'equivalent': eq, 'accepted': acc,
            'score_excluding_timeouts': {'killed': c['killed'], 'of': of, 'value': v},
            'score_including_timeouts': {'killed': k_incl, 'of': of_incl, 'value': vi}, 'score_text': text}


def completeness(d: dict) -> float:
    items = [1.0 if d['baseline']['passed'] and len(d['baseline']['run_ids']) >= 2 else 0.0,
             1.0 if d['metadata']['coverage'] else 0.0]
    engines = d['metadata']['engines']
    items.append(sum(1 for e in engines if e['status'] == 'ran') / len(engines) if engines else 0.0)
    ran = [m for m in d['mutants'] if m['status'] not in ('no_coverage', 'compile_error')]
    items.append(sum(1 for m in ran if m['status'] != 'not_run') / len(ran) if ran else 0.0)
    return round(sum(items) / len(items), 2)


def write_summary(d: dict, repo: Path) -> None:
    mutants = {m['id']: m for m in d['mutants']}
    cache: dict = {}
    for f in d['findings']:
        if f['triage'] in RANKED:
            f['kind_factor'] = max(KIND_FACTOR[mutants[m]['kind']] for m in f['mutant_ids'] if m in mutants)
            f['proximity'] = proximity(f, mutants, repo, cache)
            if f['criticality'] in (1, 2, 3):
                f['score'] = f['criticality'] * f['kind_factor'] * f['proximity']
                f['priority'] = priority(f['score'])
        else:
            f['kind_factor'] = f['proximity'] = f['score'] = f['priority'] = None
    scope = d['metadata']['scope']
    n_files = len(scope['files'])
    s = d['summary']
    s['by_engine'] = [engine_summary(e['name'], d['mutants'], d['findings'], n_files, scope)
                      for e in d['metadata']['engines'] if e['status'] == 'ran']
    s['findings'] = {t: sum(1 for f in d['findings'] if f['triage'] == t) for t in ('weak_assertion', 'missing_case', 'equivalent', 'accepted')}
    s['by_priority'] = {p: sum(1 for f in d['findings'] if f.get('priority') == p) for p in ('P0', 'P1', 'P2', 'P3')}
    s['coverage_gaps'] = len(d['coverage_gaps'])
    s['evidence_completeness'] = completeness(d)
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3, None: 4}
    d['findings'].sort(key=lambda f: (order[f.get('priority')], -(f.get('score') or 0), int(f['id'][1:])))


# ---------------------------------------------------------------- checks
def texts(d: dict):
    yield 'summary.headline', d['summary']['headline']
    for i, l in enumerate(d['limitations']):
        yield f'limitations[{i}]', l
    for f in d['findings']:
        yield f'{f["id"]}.summary', f['summary']
        yield f'{f["id"]}.reason', f['reason']
    for g in d['coverage_gaps']:
        yield f'{g["id"]}.note', g['note']
    yield 'baseline.note', d['baseline']['note']


def check(d: dict, mutants_file: dict | None, runs: dict | None, proofs: dict | None, repo: Path | None) -> tuple[list[str], list[str]]:
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(d, schema, schema)
    warns: list[str] = []
    if errs:
        return errs, warns
    md = d['metadata']
    mutants = {m['id']: m for m in d['mutants']}
    engines = {e['name']: e for e in md['engines']}
    if len(mutants) != len(d['mutants']):
        errs.append('mutant ids are not unique')
    for m in d['mutants']:
        if m['engine'] not in engines:
            errs.append(f'{m["id"]}: engine {m["engine"]!r} is not in metadata.engines')
        if m['kind'] != OPS['operators'].get(m['operator'], OPS['operators']['other'])['kind']:
            errs.append(f'{m["id"]}: kind {m["kind"]!r} does not match operator {m["operator"]!r} in data/operators.json')
        if m['status'] == 'killed' and m.get('engine_status') not in (None, 'killed') and m['engine'] != 'stryker':
            errs.append(f'{m["id"]}: killed but the engine said {m["engine_status"]!r}')
        if m['status'] == 'no_coverage' and m.get('killed_by'):
            errs.append(f'{m["id"]}: no_coverage mutant with killed_by')
    for e in md['engines']:
        if e['status'] != 'ran' and not e['reason'].strip():
            errs.append(f'engine {e["name"]}: {e["status"]} needs a reason')
        if e['status'] == 'ran' and any(m['engine'] == e['name'] for m in d['mutants']) is False and e['in_scope']:
            errs.append(f'engine {e["name"]}: reports {e["in_scope"]} mutants in scope but none are listed')
        if e['status'] != 'ran' and any(m['engine'] == e['name'] for m in d['mutants']):
            errs.append(f'engine {e["name"]} is {e["status"]} but has mutants')
        counts = {}
        for m in d['mutants']:
            if m['engine'] == e['name']:
                counts[m['status']] = counts.get(m['status'], 0) + 1
        if counts != {k: v for k, v in e['counts'].items() if v}:
            errs.append(f'engine {e["name"]}: counts {e["counts"]} do not match its mutants {counts}')
        if e['tree_changed'] is None and e['status'] == 'ran':
            errs.append(f'engine {e["name"]}: the working tree was not checked before and after the run')
        if e.get('code_hash') and e['code_hash'] != md['code_hash']:
            errs.append(f'engine {e["name"]}: ran on other code (its code_hash differs from the baseline\'s), so its '
                        'mutants are not tied to the green baseline')
        for path in e['tree_changed'] or []:
            if path not in d['files_changed']:
                errs.append(f'engine {e["name"]}: the run changed {path}, which is not in files_changed')
    for t in d['tools']:
        if t['status'] != 'ran' and not t['reason'].strip():
            errs.append(f'tool {t["name"]}: {t["status"]} needs a reason')
    # baseline
    b = d['baseline']
    if not b['passed'] and d['mutants']:
        errs.append('baseline did not pass, so no mutant results can be trusted; the run must stop before mutating')
    if b['passed'] and len(b['run_ids']) < 2:
        errs.append('baseline.passed needs two green runs')
    if b['flaky_tests'] and b['passed']:
        errs.append('baseline lists flaky tests but is marked passed; a flaky baseline stops the run')
    # findings: every survivor in exactly one finding, nothing else in findings
    seen: dict[str, str] = {}
    fids = set()
    for f in d['findings']:
        if f['id'] in fids:
            errs.append(f'{f["id"]}: duplicate finding id')
        fids.add(f['id'])
        fmuts = [mutants.get(mid) for mid in f['mutant_ids']]
        for mid, m in zip(f['mutant_ids'], fmuts):
            if m is None:
                errs.append(f'{f["id"]}: unknown mutant {mid}')
                continue
            if m['status'] != 'survived':
                what = {'no_coverage': 'it belongs in a coverage gap', 'killed': 'a killed mutant needs no triage'}.get(
                    m['status'], 'it is listed with the timeouts and errors, never as a survivor')
                errs.append(f'{f["id"]}: {mid} is {m["status"]}, not survived; {what}')
            if mid in seen:
                errs.append(f'{mid}: triaged in both {seen[mid]} and {f["id"]}')
            seen[mid] = f['id']
            if m['file'] != f['file']:
                errs.append(f'{f["id"]}: {mid} is in {m["file"]}, not {f["file"]}')
        fmuts = [m for m in fmuts if m]
        if fmuts:
            funcs = {m['function'] for m in fmuts}
            if f['function'] not in funcs:
                errs.append(f'{f["id"]}: function {f["function"]!r} is not the function of any of its mutants ({sorted(map(str, funcs))})')
        if f['triage'] == 'equivalent':
            if len(f['reason']) < 40 or GENERIC_REASON.match(f['reason']):
                errs.append(f'{f["id"]}: an equivalent mutant needs a reason someone can check: why both versions behave the same for every input')
        if f['triage'] == 'accepted' and len(f['reason']) < 20:
            errs.append(f'{f["id"]}: accepted needs a reason the behaviour does not matter')
        if f['triage'] in ('equivalent', 'accepted'):
            for k in ('criticality', 'kind_factor', 'proximity', 'score', 'priority'):
                if f[k] is not None:
                    errs.append(f'{f["id"]}: {f["triage"]} mutants are never ranked ({k} must be null)')
            if f['proof_ids']:
                errs.append(f'{f["id"]}: {f["triage"]} finding cites proofs that a test kills it')
        if f['triage'] in RANKED:
            if not f['suggestion']:
                errs.append(f'{f["id"]}: {f["triage"]} needs a suggestion (the assertion or the input)')
            if f['criticality'] not in (1, 2, 3) or not (f['criticality_reason'] or '').strip():
                errs.append(f'{f["id"]}: criticality 1-3 with a reason is required')
        if f['triage'] == 'weak_assertion':
            t = f['should_have_caught']
            if not t:
                errs.append(f'{f["id"]}: weak_assertion must name the covering test that should have caught it')
            else:
                for m in fmuts:
                    if t not in m['covering_tests']:
                        errs.append(f'{f["id"]}: {t} is not a covering test of {m["id"]}; a weak assertion is in a test that ran the line')
        if f['triage'] == 'missing_case' and f['should_have_caught']:
            if not any(f['should_have_caught'] in m['covering_tests'] for m in fmuts):
                errs.append(f'{f["id"]}: {f["should_have_caught"]} covers none of its mutants')
        if f['triage'] in RANKED and fmuts:
            kf = max(KIND_FACTOR[m['kind']] for m in fmuts)
            if f['kind_factor'] != kf:
                errs.append(f'{f["id"]}: kind_factor {f["kind_factor"]} should be {kf} (rerun --write-summary)')
            if f['proximity'] not in (1, 2):
                errs.append(f'{f["id"]}: proximity not computed (run --write-summary with --repo)')
            elif not f['function'] and f['proximity'] != 1:
                errs.append(f'{f["id"]}: a module-level mutant is only reached indirectly; proximity is 1')
            if f['criticality'] in (1, 2, 3) and f['proximity'] in (1, 2):
                sc = f['criticality'] * kf * f['proximity']
                if f['score'] != sc or f['priority'] != priority(sc):
                    errs.append(f'{f["id"]}: score/priority should be {sc}/{priority(sc)} (rerun --write-summary)')
    for m in d['mutants']:
        if m['status'] == 'survived' and m['id'] not in seen:
            errs.append(f'{m["id"]}: survived mutant has no triage')
    # coverage gaps: every no_coverage mutant in exactly one gap
    gseen: dict[str, str] = {}
    for g in d['coverage_gaps']:
        for mid in g['mutant_ids']:
            m = mutants.get(mid)
            if m is None:
                errs.append(f'{g["id"]}: unknown mutant {mid}')
                continue
            if m['status'] != 'no_coverage':
                errs.append(f'{g["id"]}: {mid} is {m["status"]}, not no_coverage')
            if mid in gseen:
                errs.append(f'{mid}: in both {gseen[mid]} and {g["id"]}')
            gseen[mid] = g['id']
            if m['file'] != g['file'] or not (min(g['lines']) <= m['line'] <= max(g['lines'])):
                errs.append(f'{g["id"]}: {mid} ({m["file"]}:{m["line"]}) is outside the gap\'s lines')
    for m in d['mutants']:
        if m['status'] == 'no_coverage' and m['id'] not in gseen:
            errs.append(f'{m["id"]}: no_coverage mutant is in no coverage gap')
    # summary
    scope = md['scope']
    n_files = len(scope['files'])
    s = d['summary']
    want = [engine_summary(e['name'], d['mutants'], d['findings'], n_files, scope) for e in md['engines'] if e['status'] == 'ran']
    if s['by_engine'] != want:
        errs.append('summary.by_engine does not match the mutants and triage (rerun --write-summary)')
    if s['findings'] != {t: sum(1 for f in d['findings'] if f['triage'] == t) for t in ('weak_assertion', 'missing_case', 'equivalent', 'accepted')}:
        errs.append('summary.findings counts are wrong')
    if s['by_priority'] != {p: sum(1 for f in d['findings'] if f.get('priority') == p) for p in ('P0', 'P1', 'P2', 'P3')}:
        errs.append('summary.by_priority is wrong')
    if s['coverage_gaps'] != len(d['coverage_gaps']):
        errs.append('summary.coverage_gaps is wrong')
    if s['evidence_completeness'] != completeness(d):
        errs.append(f'summary.evidence_completeness should be {completeness(d)}')
    allowed = set()
    for e in want:
        for key in ('score_excluding_timeouts', 'score_including_timeouts'):
            if e[key]['value'] is not None:
                allowed.add((round(100 * e[key]['value'], 1), e[key]['killed'], e[key]['of']))
    head = s['headline']
    for m in PCT.finditer(head):
        val = float(m.group(1))
        match = [a for a in allowed if abs(a[0] - val) < 0.051 or (abs(round(a[0]) - val) < 0.01 and '.' not in m.group(1))]
        if not match:
            errs.append(f'headline: {m.group(0)} is not one of the computed scores')
        elif not any(re.search(rf'\b{k}\s+of\s+{n}\b', head) for _, k, n in match):
            errs.append(f'headline: {m.group(0)} is shown without its counts ("{match[0][1]} of {match[0][2]}")')
    if scope['mode'] != 'all':
        for where, text in texts(d):
            if EXTRAPOLATE.search(text) and PCT.search(text):
                errs.append(f'{where}: a score from a {scope["mode"]} scope is never a score for the whole codebase')
    for where, text in texts(d):
        if BANNED.search(text):
            errs.append(f'{where}: "{BANNED.search(text).group(0)}" claims more than the mutants show')
    # files changed
    if not md['write_tests'] and d['files_changed']:
        errs.append('files_changed must be empty unless write_tests is true')
    # mutants file
    if mutants_file is not None:
        keyset = ('id', 'engine', 'file', 'line', 'end_line', 'operator', 'original', 'mutated', 'status', 'covering_tests')
        mine = {m['id']: tuple(m.get(k) for k in keyset) for m in d['mutants']}
        theirs = {m['id']: tuple(m.get(k) for k in keyset) for m in mutants_file['mutants']}
        for mid in sorted(set(mine) | set(theirs), key=lambda x: (x[0], int(x[1:]))):
            if mid not in theirs:
                errs.append(f'{mid}: not in the normalised results')
            elif mid not in mine:
                errs.append(f'{mid}: in the normalised results but missing from the report')
            elif mine[mid] != theirs[mid]:
                diff = [k for k, a, b in zip(keyset, mine[mid], theirs[mid]) if a != b]
                errs.append(f'{mid}: {", ".join(diff)} differ from the normalised results')
        te = {e['name']: e for e in mutants_file['engines']}
        for e in md['engines']:
            o = te.get(e['name'])
            if o is None:
                errs.append(f'engine {e["name"]}: not in the normalised results')
                continue
            for k in ('version', 'config_source', 'status', 'generated', 'in_scope', 'out_of_scope', 'tree_changed', 'counts',
                      'code_hash'):
                if e.get(k) != o.get(k):
                    errs.append(f'engine {e["name"]}: {k} differs from the normalised results')
        if mutants_file.get('problems'):
            errs.append('the normalised results list mapping problems: ' + '; '.join(mutants_file['problems'][:3]))
        if [x['path'] for x in mutants_file['scope']['files']] != [x['path'] for x in scope['files']]:
            errs.append('metadata.scope.files differ from the normalised scope')
    # runs
    if runs is not None:
        rmap = {r['id']: r for r in runs['runs']}
        ok = len(b['run_ids']) >= 2
        for rid in b['run_ids']:
            r = rmap.get(rid)
            if r is None:
                errs.append(f'baseline: run {rid} is not in runs.json')
                ok = False
                continue
            t = r['totals']
            green = r['exit_code'] == 0 and not t.get('failed') and not t.get('error') and t.get('passed', 0) > 0
            ok = ok and green
            if r.get('code_hash') != md['code_hash']:
                errs.append(f'baseline: run {rid} was on different code (code_hash)')
        if ok != b['passed']:
            errs.append(f'baseline.passed is {b["passed"]} but the runs say {ok}')
    # proofs
    cited = {pid for f in d['findings'] for pid in f['proof_ids']} | {pid for g in d['coverage_gaps'] for pid in g['proof_ids']}
    if cited and proofs is None:
        warns.append('proofs are cited but --proofs was not given; they were not checked')
    if proofs is not None:
        pmap = {p['id']: p for p in proofs['proofs']}
        owners = {pid: f for f in d['findings'] for pid in f['proof_ids']}
        owners.update({pid: g for g in d['coverage_gaps'] for pid in g['proof_ids']})
        for pid, owner in owners.items():
            p = pmap.get(pid)
            if p is None:
                errs.append(f'{owner["id"]}: proof {pid} is not in proofs.json')
                continue
            if p['original_run']['test_status'] != 'passed':
                errs.append(f'{pid}: the new test did not pass on the original code')
            if not p['mutants']:
                errs.append(f'{pid}: no mutant run')
            for r in p['mutants']:
                if r['mutant_id'] not in owner['mutant_ids']:
                    errs.append(f'{pid}: {r["mutant_id"]} is not one of {owner["id"]}\'s mutants')
                if r['verdict'] != 'kills' or r['run']['test_status'] not in ('failed', 'error'):
                    errs.append(f'{pid}: the new test did not fail on {r["mutant_id"]} ({r["verdict"]})')
            if owner.get('suggestion') and isinstance(owner['suggestion'], dict) and p['test_file'] != owner['suggestion']['test_file']:
                errs.append(f'{pid}: proved in {p["test_file"]}, but {owner["id"]} suggests {owner["suggestion"]["test_file"]}')
            if p['applied_to'] == 'repo' and p['test_file'] not in d['files_changed']:
                errs.append(f'{pid}: the test was written to the repo, so {p["test_file"]} belongs in files_changed')
            if p['applied_to'] == 'repo' and not md['write_tests']:
                errs.append(f'{pid}: written to the repo without write_tests')
            if [c for c in p['repo_files_changed'] if c != p['test_file'] or p['applied_to'] != 'repo']:
                errs.append(f'{pid}: the proof changed repository files')
    # repo
    if repo is not None:
        ch = run_tests.code_hash(repo)
        if ch != md['code_hash']:
            errs.append('metadata.code_hash does not match the repository; the mutants are from other code')
        cache: dict[str, list[str] | None] = {}
        for m in d['mutants']:
            if m['file'] not in cache:
                p = repo / m['file']
                cache[m['file']] = p.read_text(encoding='utf-8').splitlines() if p.is_file() else None
            lines = cache[m['file']]
            if lines is None:
                errs.append(f'{m["id"]}: {m["file"]} is not in the repository')
                continue
            if '\n'.join(lines[m['line'] - 1:m['end_line']]) != m['original']:
                errs.append(f'{m["id"]}: {m["file"]}:{m["line"]} does not hold the mutant\'s original text')
        tcache: dict = {}
        for f in d['findings']:
            if f['triage'] in RANKED and f['proximity'] in (1, 2):
                want_p = proximity(f, mutants, repo, tcache)
                if want_p != f['proximity']:
                    errs.append(f'{f["id"]}: proximity should be {want_p}')
    elif any(f['triage'] in RANKED for f in d['findings']):
        warns.append('proximity was not re-checked (no --repo)')
    return errs, warns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--mutants', type=Path)
    ap.add_argument('--runs', type=Path)
    ap.add_argument('--proofs', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    try:
        d = json.loads(args.report.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: cannot read report: {exc}')
        return 1
    if args.write_summary:
        if args.repo is None:
            print('ERROR: --write-summary needs --repo (proximity reads the tests)')
            return 1
        write_summary(d, args.repo.resolve())
        args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    load = lambda p: json.loads(p.read_text(encoding='utf-8')) if p else None  # noqa: E731
    errs, warns = check(d, load(args.mutants), load(args.runs), load(args.proofs), args.repo.resolve() if args.repo else None)
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
