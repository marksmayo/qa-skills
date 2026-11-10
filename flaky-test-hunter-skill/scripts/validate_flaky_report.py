#!/usr/bin/env python3
"""Validate a flaky test report against the schema and the rubric's honesty rules (stdlib only).

    python validate_flaky_report.py flaky_report.json --runs runs.json [--smells smells.json]
                                    [--bisect bisect.json] [--repo <repo>] [--write-summary]

--runs recomputes every test's counts, Wilson intervals and status from the real runs and checks
each flake's evidence against them. --smells checks every static smell is accounted for.
--bisect checks a polluter against the bisection's own record. --repo checks that every proposed
(unapplied) fix applies cleanly to the repository's files. --write-summary fills in the
computed fields (tests[], each flake's rate factor, gate, masking, score and priority, and the
summary counts) before validating; the headline stays yours.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stats  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'flaky_report.schema.json'
NORMAL_LABELS = ('baseline', 'repeat')
NOT_WORST = ('bisect',)
GATE = {'required': 3, 'ci': 2, 'local': 1}
TEST_PATH = re.compile(r'(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|(^|/)src/test/|'
                       r'(^|/)__tests__/|(^|/)e2e/|(^|/)conftest\.py$|Test\.(java|kt)$')
RETRY = re.compile(r'@pytest\.mark\.flaky|\bflaky\b\s*\(|rerunfailures|--reruns|\breruns\s*=|retryTimes|'
                   r'\bretries\s*[:=]\s*[1-9]|test-retry|@Retry|@RepeatedTest|rerunFailingTestsCount|--retry', re.I)
BANNED = re.compile(r'\bnot flaky\b|\bflake[- ]free\b|\bno flaky tests\b|\b(suite|tests?) (is|are) (now )?(stable|reliable|'
                    r'deterministic)\b|\bnever flak', re.I)


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


# ---------------------------------------------------------------- aggregation from runs
def outcomes(runs_data: dict, code_hash: str) -> tuple[dict, dict, dict]:
    """test id -> [(run_id, label, 'pass'|'fail')], test id -> file, run id -> run (same code only)."""
    runs = {r['id']: r for r in runs_data['runs'] if r.get('code_hash') == code_hash}
    out, files = defaultdict(list), {}
    for c in runs_data['cases']:
        r = runs.get(c['run_id'])
        if not r or c['status'] == 'skipped':
            continue
        tid = c['id'].split('#')[0]
        out[tid].append((c['run_id'], r.get('label', 'run'), 'pass' if c['status'] == 'passed' else 'fail'))
        files.setdefault(tid, c.get('file'))
    return out, files, runs


def rate(n: int, k: int) -> dict:
    low, high = stats.wilson(k, n)
    return {'runs': n, 'fails': k, 'rate': round(k / n, 4) if n else None, 'wilson_low': low, 'wilson_high': high}


def test_stats(tid: str, obs: list, file: str | None) -> dict:
    passes = sum(o == 'pass' for _, _, o in obs)
    fails = len(obs) - passes
    by = defaultdict(lambda: [0, 0])
    for _, label, o in obs:
        by[label][0] += 1
        by[label][1] += o == 'fail'
    normal = [o for _, label, o in obs if label in NORMAL_LABELS]
    worst = {'label': None, 'runs': 0, 'fails': 0, 'wilson_low': 0.0}
    for label, (n, k) in by.items():
        if label in NOT_WORST or not k:
            continue
        low = stats.wilson(k, n)[0]
        if low > worst['wilson_low'] or worst['label'] is None:
            worst = {'label': label, 'runs': n, 'fails': k, 'wilson_low': low}
    status = 'not_run' if not obs else 'flaky' if passes and fails else 'consistently_failing' if fails else 'not_observed_flaky'
    return {'id': tid, 'file': file, 'runs': len(obs), 'passes': passes, 'fails': fails,
            'normal': rate(len(normal), sum(o == 'fail' for o in normal)), 'worst': worst,
            'by_label': [{'label': l, 'runs': n, 'fails': k} for l, (n, k) in sorted(by.items())], 'status': status}


def band(score: int) -> str:
    return 'P0' if score >= 18 else 'P1' if score >= 9 else 'P2' if score >= 4 else 'P3'


def score_flake(f: dict, t: dict | None, meta: dict) -> dict:
    worst_low = t['worst']['wilson_low'] if t else 0.0
    rf = stats.rate_band(worst_low)
    gate = GATE[meta['gate']['level']]
    masking = 2 if f['test_id'] in meta['critical_tests'] or f['cause'] == 'product_race' else 1
    score = rf * gate * masking
    pr, notes = band(score), []
    order = ['P0', 'P1', 'P2', 'P3']
    if f['cause'] == 'product_race' and order.index(pr) > 1:
        pr = 'P1'
        notes.append('raised to P1: a product race')
    if f['cause_confidence'] == 'suspected' and order.index(pr) < 2:
        pr = 'P2'
        notes.append('capped at P2: the cause is only suspected')
    if f.get('history_only') and order.index(pr) < 2:
        pr = 'P2'
        notes.append('capped at P2: seen in CI history only, never reproduced here')
    note = f'{rf} (worst Wilson low {100 * worst_low:.1f}%) x gate {gate} x masking {masking} = {score}'
    return {'rate_factor': rf, 'gate': gate, 'masking': masking, 'score': score, 'priority': pr,
            'priority_note': note + ('; ' + '; '.join(notes) if notes else '')}


def compute(d: dict, runs_data: dict) -> tuple[list[dict], dict]:
    obs, files, runs = outcomes(runs_data, d['metadata']['code_hash'])
    tests = [test_stats(tid, obs[tid], files.get(tid)) for tid in sorted(obs)]
    return tests, runs


def summary_numbers(d: dict, runs_data: dict, runs: dict) -> dict:
    tests = d['tests']
    count = lambda s: sum(t['status'] == s for t in tests)  # noqa: E731
    by_cause, by_pr = defaultdict(int), {'P0': 0, 'P1': 0, 'P2': 0, 'P3': 0}
    for f in d['flakes']:
        by_cause[f['cause']] += 1
        by_pr[f['priority']] += 1
    ran = sum(v['status'] == 'ran' for v in d['variations'])
    planned = sum(v['status'] in ('ran', 'not_run') for v in d['variations'])
    return {'tests_run': sum(t['status'] != 'not_run' for t in tests), 'flaky': count('flaky'),
            'consistently_failing': count('consistently_failing'), 'not_observed_flaky': count('not_observed_flaky'),
            'not_run': count('not_run'), 'by_cause': dict(sorted(by_cause.items())), 'by_priority': by_pr,
            'total_runs': len(runs), 'wall_minutes': round(sum(r.get('duration_s', 0) for r in runs.values()) / 60, 1),
            'evidence_completeness': round(ran / planned, 2) if planned else 0.0}


def write_summary(d: dict, runs_data: dict) -> None:
    tests, runs = compute(d, runs_data)
    d['tests'] = tests
    by_id = {t['id']: t for t in tests}
    for f in d['flakes']:
        f.update(score_flake(f, by_id.get(f['test_id']), d['metadata']))
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    d['flakes'].sort(key=lambda f: (order[f['priority']], -f['score'], f['test_id']))
    d['summary'] = {'headline': d['summary'].get('headline', ''), **summary_numbers(d, runs_data, runs)}


# ---------------------------------------------------------------- checks
def walk_strings(x, path=''):
    if isinstance(x, dict):
        for k, v in x.items():
            if k in ('diff', 'failure_excerpts', 'failure_excerpt', 'excerpt'):
                continue
            yield from walk_strings(v, f'{path}.{k}')
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from walk_strings(v, f'{path}[{i}]')
    elif isinstance(x, str):
        yield path, x


def diff_paths(diff: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r'^\+\+\+ (?:b/)?(\S+)', diff, re.M) if m.group(1) != '/dev/null']


def apply_diff(repo: Path, diff: str) -> str | None:
    """Check a unified diff applies to the files under repo; return the first problem, or None."""
    files = re.split(r'(?m)^(?=--- )', diff)
    for block in files:
        if not block.startswith('--- '):
            continue
        m_old = re.match(r'--- (\S+)', block)
        m_new = re.search(r'(?m)^\+\+\+ (?:b/)?(\S+)', block)
        if not m_new:
            return 'a file header has no +++ line'
        rel, is_new = m_new.group(1), m_old.group(1) == '/dev/null'
        path = repo / rel
        if is_new and path.exists():
            return f'{rel} already exists but the diff creates it'
        if not is_new and not path.exists():
            return f'{rel} does not exist'
        lines = [] if is_new else path.read_text(encoding='utf-8').splitlines()
        for h in re.finditer(r'(?m)^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@.*\n((?:[ +\-\\].*(?:\n|$))*)', block):
            start = int(h.group(1)) - (0 if is_new else 1)
            old_count = int(h.group(2)) if h.group(2) is not None else 1
            body = [l for l in h.group(3).split('\n') if l and not l.startswith('\\')]
            expect = [l[1:] for l in body if l[0] in ' -']
            if len(expect) != old_count:
                return f'{rel}: hunk at line {h.group(1)} says {old_count} old lines but has {len(expect)}'
            if lines[max(start, 0):max(start, 0) + len(expect)] != expect:
                return f'{rel}: hunk at line {h.group(1)} does not match the file'
    return None


def check(d: dict, runs_data: dict | None, smells: dict | None, bisect: dict | None,
          repo: Path | None = None) -> tuple[list[str], list[str]]:
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(d, schema, schema)
    if errs:
        return errs, []
    warns = []
    meta = d['metadata']
    tests = {t['id']: t for t in d['tests']}
    flakes = d['flakes']

    for path, text in walk_strings(d):
        m = BANNED.search(text)
        if m:
            errs.append(f'{path}: claims more than the runs show: {m.group(0)!r} (say "not observed flaky in N runs")')

    # tools and variations
    tools = {t['name']: t for t in d['tools']}
    for t in d['tools']:
        if t['status'] != 'ran' and not t['reason']:
            errs.append(f"tool {t['name']} is {t['status']} with no reason")
    for v in d['variations']:
        if v['status'] == 'ran':
            if not v['labels']:
                errs.append(f"variation {v['kind']} ran but names no run labels")
            if v.get('tool') and tools.get(v['tool'], {}).get('status') != 'ran':
                errs.append(f"variation {v['kind']} ran with {v['tool']}, which is recorded as not run")
        elif not v['reason']:
            errs.append(f"variation {v['kind']} is {v['status']} with no reason")

    # flakes
    seen_tests = defaultdict(int)
    for f in flakes:
        seen_tests[f['test_id']] += 1
        at = f"{f['id']} ({f['test_id']})"
        if f['cause'] == 'unknown' and f['cause_confidence'] != 'suspected':
            errs.append(f'{at}: an unknown cause can only be suspected')
        if RETRY.search(f['fix']['diff']) or RETRY.search(f['fix']['rationale']):
            errs.append(f'{at}: a retry or rerun is proposed as the fix; it hides the flake instead of removing the cause')
        for p in diff_paths(f['fix']['diff']):
            if not TEST_PATH.search(p):
                errs.append(f'{at}: the fix edits {p}, which is not test code; product code is never changed')
        if not diff_paths(f['fix']['diff']):
            errs.append(f'{at}: the fix diff names no file (+++ b/<path>)')
        elif repo is not None and not f['fix']['applied']:
            problem = apply_diff(repo, f['fix']['diff'])
            if problem:
                errs.append(f'{at}: the proposed fix does not apply to the repo: {problem}')
        if f['fix']['applied']:
            if len(f['fix']['verification_run_ids']) < meta['budget']['repeats']:
                errs.append(f"{at}: an applied fix needs {meta['budget']['repeats']} verification runs")
            for p in diff_paths(f['fix']['diff']):
                if p not in {x['path'] for x in d['files_changed']}:
                    errs.append(f'{at}: applied fix changes {p}, which is missing from files_changed')
        elif f['fix']['verification_run_ids']:
            errs.append(f'{at}: a fix that was not applied cannot have verification runs')
        if f['polluter'] and f['cause'] not in ('shared_state', 'order_dependence'):
            errs.append(f'{at}: a polluter only belongs to shared_state or order_dependence flakes')
    for tid, n in seen_tests.items():
        if n > 1:
            errs.append(f'{tid}: more than one flake entry')

    # smells
    flake_ids = {f['id']: f for f in flakes}
    for s in d['static_smells']:
        if s['status'] == 'linked_to_flake':
            if s['flake_id'] not in flake_ids:
                errs.append(f"smell {s['smell_id']}: linked to unknown flake {s['flake_id']}")
            elif s['test'] and not flake_ids[s['flake_id']]['test_id'].endswith('::' + s['test']):
                errs.append(f"smell {s['smell_id']} is in {s['test']} but linked to {flake_ids[s['flake_id']]['test_id']}")
        elif s['flake_id'] is not None:
            errs.append(f"smell {s['smell_id']} is {s['status']} but names a flake")
    if smells is not None:
        listed = {s['smell_id'] for s in d['static_smells']}
        for s in smells['smells']:
            if s['id'] not in listed:
                errs.append(f"static smell {s['id']} ({s['smell']} at {s['file']}:{s['line']}) is not accounted for")

    # against the runs
    if runs_data is not None:
        recomputed, runs = compute(d, runs_data)
        rc = {t['id']: t for t in recomputed}
        if rc != tests:
            diff_ids = sorted(k for k in set(rc) | set(tests) if rc.get(k) != tests.get(k))
            errs.append(f'tests[] differs from the runs for: {", ".join(diff_ids[:6])} (run --write-summary)')
        other = [r['id'] for r in runs_data['runs'] if r.get('code_hash') != meta['code_hash']]
        if other:
            warns.append(f'{len(other)} run(s) have a different code_hash and were left out: {", ".join(other[:5])}')
        labels = {r.get('label') for r in runs.values()}
        for v in d['variations']:
            for l in v['labels']:
                if l not in labels:
                    errs.append(f"variation {v['kind']} names label {l!r}, which no run has")
        obs, _, _ = outcomes(runs_data, meta['code_hash'])
        for tid, t in rc.items():
            if t['status'] == 'flaky' and tid not in seen_tests:
                errs.append(f'{tid} passed and failed on the same code but has no flake entry')
            if t['status'] == 'consistently_failing' and tid not in {c['test_id'] for c in d['consistently_failing']}:
                errs.append(f'{tid} failed in every run but is not listed in consistently_failing')
        for c in d['consistently_failing']:
            if rc.get(c['test_id'], {}).get('status') != 'consistently_failing':
                errs.append(f"{c['test_id']} is listed as consistently failing but the runs say "
                            f"{rc.get(c['test_id'], {}).get('status', 'not run')}")
        for f in flakes:
            at = f"{f['id']} ({f['test_id']})"
            t = rc.get(f['test_id'])
            if not t or t['status'] != 'flaky':
                errs.append(f"{at}: needs a pass and a fail on the same code; the runs say {t['status'] if t else 'not run'}")
                continue
            res = {rid: o for rid, _, o in obs[f['test_id']]}
            lab = {rid: l for rid, l, _ in obs[f['test_id']]}
            for rid in f['evidence_runs']['passing']:
                if res.get(rid) != 'pass':
                    errs.append(f'{at}: evidence run {rid} is not a pass of this test')
            for rid in f['evidence_runs']['failing']:
                if res.get(rid) != 'fail':
                    errs.append(f'{at}: evidence run {rid} is not a fail of this test')
            for rid in f['experiment']['run_ids']:
                if rid not in res:
                    errs.append(f'{at}: experiment run {rid} did not run this test')
            if f['cause_confidence'] == 'confirmed':
                ok = False
                if f['polluter']:
                    pol = f['polluter']
                    ok = res.get(pol['pair_run_id']) == 'fail' and res.get(pol['alone_run_id']) == 'pass'
                    if not ok:
                        errs.append(f'{at}: polluter pair must fail and the victim alone must pass')
                else:
                    by = defaultdict(list)
                    for rid in f['experiment']['run_ids']:
                        if rid in res:
                            by[lab[rid]].append(res[rid])
                    off = [l for l, xs in by.items() if len(xs) >= 3 and all(x == 'pass' for x in xs)]
                    on = [l for l, xs in by.items() if sum(x == 'fail' for x in xs) >= 2]
                    ok = bool(off and on)
                    if not ok:
                        errs.append(f'{at}: confirmed needs an experiment that turns the failure off (one label, '
                                    f'3+ runs all passing) and on (another label, 2+ failures)')
            if t and f['rate_factor'] != stats.rate_band(t['worst']['wilson_low']):
                errs.append(f'{at}: rate factor does not match the worst observed rate')
            exp = score_flake(f, t, meta)
            for k in ('rate_factor', 'gate', 'masking', 'score', 'priority'):
                if f[k] != exp[k]:
                    errs.append(f'{at}: {k} is {f[k]!r} but the rubric gives {exp[k]!r}')
        exp_sum = summary_numbers(d, runs_data, runs)
        for k, v in exp_sum.items():
            if d['summary'][k] != v:
                errs.append(f'summary.{k} is {d["summary"][k]!r} but the runs give {v!r}')
    else:
        warns.append('no --runs given; counts, statuses and experiments were not re-checked')

    if bisect is not None:
        for f in flakes:
            if f['polluter'] and f['test_id'] == bisect['victim']:
                if bisect['outcome'] != 'minimal_pair':
                    errs.append(f"{f['id']}: names a polluter but the bisection's outcome is {bisect['outcome']}")
                else:
                    steps = {s['run_id'] for s in bisect['steps']}
                    if not set(f['polluter']['bisect_run_ids']) <= steps:
                        errs.append(f"{f['id']}: polluter bisect runs are not the bisection's steps")
    return errs, warns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--runs', type=Path)
    ap.add_argument('--smells', type=Path)
    ap.add_argument('--bisect', type=Path)
    ap.add_argument('--repo', type=Path, help='check every proposed fix applies to these files')
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    try:
        d = json.loads(args.report.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: cannot read report: {exc}')
        return 1
    runs_data = json.loads(args.runs.read_text(encoding='utf-8')) if args.runs else None
    if args.write_summary:
        if runs_data is None:
            print('ERROR: --write-summary needs --runs')
            return 1
        write_summary(d, runs_data)
        args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    smells = json.loads(args.smells.read_text(encoding='utf-8')) if args.smells else None
    bisect = json.loads(args.bisect.read_text(encoding='utf-8')) if args.bisect else None
    errs, warns = check(d, runs_data, smells, bisect, args.repo)
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
