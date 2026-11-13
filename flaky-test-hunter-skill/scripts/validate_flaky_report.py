#!/usr/bin/env python3
"""Validate a flaky test report against the schema and the rubric's honesty rules (stdlib only).

    python validate_flaky_report.py flaky_report.json --runs runs.json [--smells smells.json]
                                    [--bisect bisect.json] [--repo <repo>] [--write-summary]

--runs recomputes every test's counts, Wilson intervals and status from the real runs and checks
each flake's evidence against them. --smells checks every static smell is accounted for.
--bisect (one or more files) checks a polluter or enabler against the bisection's own record. --repo checks that every proposed
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
CLOCK_ENV = ('TDA_CLOCK_SHIFT', 'TDA_CLOCK_DATE', 'FAKETIME')
MIN_LABEL_RUNS = 3      # a label needs this many runs to set the worst rate on its own
POOLED = 'pooled'       # the worst-rate candidate made of every non-bisect run
REPEAT_SUFFIX = re.compile(r'#\d+$')  # parse_results' suffix for a repeated name; a '#' inside a title stays
TEST_PATH = re.compile(r'(^|/)tests?/|(^|/)testdata/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)src/test/|'
                       r'(^|/)__tests__/|(^|/)e2e/|(^|/)conftest\.py$|Test\.(java|kt)$|'
                       # test runner config and setup files (RUBRIC: fixes may touch test config)
                       r'(^|/)(?:vitest|jest|playwright|cypress|karma)\.config\.[cm]?[jt]s$|'
                       r'(^|/)vitest\.workspace\.[cm]?[jt]s$|(^|/)(?:vitest|jest)\.setup\.[cm]?[jt]sx?$|'
                       r'(^|/)setupTests\.[jt]sx?$|(^|/)pytest\.ini$|(^|/)\.mocharc\.[a-z]+$')
# shared config files: a fix may touch only their test-runner section
SECTION_FILES = re.compile(r'(^|/)(?:pyproject\.toml|setup\.cfg|tox\.ini)$')
TEST_SECTION = re.compile(r'^\[(?:tool\.pytest\.ini_options|pytest|tool:pytest)\]\s*$')
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
    """test id -> [(run_id, label, 'pass'|'fail')], test id -> file, run id -> run (same code only). Errored runs
    (run_tests.py's `errored`: a crash, a timeout, a pytest usage or collection error, no JUnit) did not run the tests,
    so none of their cases count."""
    runs = {r['id']: r for r in runs_data['runs'] if r.get('code_hash') == code_hash and not r.get('errored')}
    out, files = defaultdict(list), {}
    for c in runs_data['cases']:
        r = runs.get(c['run_id'])
        if not r or c['status'] == 'skipped':
            continue
        tid = REPEAT_SUFFIX.sub('', c['id'])  # only the repeat suffix: a '#' in a title is part of the id
        out[tid].append((c['run_id'], r.get('label', 'run'), 'pass' if c['status'] == 'passed' else 'fail'))
        files.setdefault(tid, c.get('file'))
    return out, files, runs


def rate(n: int, k: int) -> dict:
    low, high = stats.wilson(k, n)
    return {'runs': n, 'fails': k, 'rate': round(k / n, 4) if n else None, 'wilson_low': low, 'wilson_high': high}


def platform_of(run: dict) -> str | None:
    """The platform a run recorded (`platform`, added to each run when a suite ran on more than one OS)."""
    return run.get('platform') or (run.get('variation') or {}).get('platform')


def test_stats(tid: str, obs: list, file: str | None, plat: dict | None = None) -> dict:
    passes = sum(o == 'pass' for _, _, o in obs)
    fails = len(obs) - passes
    by = defaultdict(lambda: [0, 0])
    for _, label, o in obs:
        by[label][0] += 1
        by[label][1] += o == 'fail'
    normal = [o for _, label, o in obs if label in NORMAL_LABELS]
    # Worst rate: the label with the highest Wilson lower bound, among labels with at least
    # MIN_LABEL_RUNS runs, and the pool of every non-bisect run. A 1/1 label (lower bound 20.6%)
    # cannot rank a flake like 3/3 (43.8%); its runs still count in the pool.
    cands = [(label, n, k) for label, (n, k) in sorted(by.items())
             if label not in NOT_WORST and k and n >= MIN_LABEL_RUNS]
    pn = sum(n for label, (n, _) in by.items() if label not in NOT_WORST)
    pk = sum(k for label, (_, k) in by.items() if label not in NOT_WORST)
    if pk:
        cands.append((POOLED, pn, pk))
    worst = {'label': None, 'runs': 0, 'fails': 0, 'wilson_low': 0.0}
    for label, n, k in cands:
        low = stats.wilson(k, n)[0]
        if low > worst['wilson_low'] or worst['label'] is None:
            worst = {'label': label, 'runs': n, 'fails': k, 'wilson_low': low}
    status = 'not_run' if not obs else 'flaky' if passes and fails else 'consistently_failing' if fails else 'not_observed_flaky'
    extra = {}
    platforms = sorted({(plat or {}).get(rid) or '(not recorded)' for rid, _, _ in obs})
    if len(platforms) > 1:
        # Runs on more than one platform (one code hash): a test that passes every run on one platform and fails every
        # run on another is a deterministic platform difference, not a flake. Flaky means mixed on one platform.
        per = {p: [0, 0] for p in platforms}
        for rid, _, o in obs:
            k = (plat or {}).get(rid) or '(not recorded)'
            per[k][0] += 1
            per[k][1] += o == 'fail'
        extra['by_platform'] = [{'platform': p, 'runs': n, 'fails': k} for p, (n, k) in per.items()]
        if status == 'flaky' and not any(0 < k < n for n, k in per.values()):
            status = 'platform_dependent'
    return {'id': tid, 'file': file, 'runs': len(obs), 'passes': passes, 'fails': fails,
            'normal': rate(len(normal), sum(o == 'fail' for o in normal)), 'worst': worst,
            'by_label': [{'label': l, 'runs': n, 'fails': k} for l, (n, k) in sorted(by.items())], 'status': status,
            **extra}


def failing_platforms(t: dict) -> list[str]:
    return sorted(p['platform'] for p in t.get('by_platform', []) if p['runs'] and p['fails'] == p['runs'])


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
    where = ''
    if t and t['worst']['label']:
        w = t['worst']
        where = f", {w['fails']}/{w['runs']} {'over all runs' if w['label'] == POOLED else 'under ' + w['label']}"
    note = f'{rf} (worst Wilson low {100 * worst_low:.1f}%{where}) x gate {gate} x masking {masking} = {score}'
    return {'rate_factor': rf, 'gate': gate, 'masking': masking, 'score': score, 'priority': pr,
            'priority_note': note + ('; ' + '; '.join(notes) if notes else '')}


def compute(d: dict, runs_data: dict) -> tuple[list[dict], dict]:
    obs, files, runs = outcomes(runs_data, d['metadata']['code_hash'])
    plat = {rid: platform_of(r) for rid, r in runs.items()}
    tests = [test_stats(tid, obs[tid], files.get(tid), plat) for tid in sorted(obs)]
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
            'not_run': count('not_run'),
            **({'platform_dependent': count('platform_dependent')} if count('platform_dependent') else {}),
            'by_cause': dict(sorted(by_cause.items())), 'by_priority': by_pr,
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
def history_errors(f: dict, t: dict | None, meta: dict) -> list[str]:
    """A history_only flake: seen to pass and fail in CI history, never reproduced in the local runs. Its evidence is
    the CI runs themselves (history_evidence), each with a reference and an excerpt; it is never confirmed and its
    priority is capped at P2 (score_flake)."""
    at = f"{f['id']} ({f['test_id']})"
    errs = []
    ev = f.get('history_evidence') or []
    if (meta.get('history') or {}).get('status') != 'used':
        errs.append(f'{at}: history_only needs metadata.history used, with its source')
    if not ev:
        errs.append(f'{at}: history_only needs history_evidence: the CI runs that passed and failed, with references')
        return errs
    out = {e['id']: e['outcome'] for e in ev}
    if 'pass' not in out.values() or 'fail' not in out.values():
        errs.append(f'{at}: history_evidence needs at least one passing and one failing CI run')
    for rid in f['evidence_runs']['passing']:
        if out.get(rid) != 'pass':
            errs.append(f'{at}: evidence run {rid} is not a passing run in history_evidence')
    for rid in f['evidence_runs']['failing']:
        if out.get(rid) != 'fail':
            errs.append(f'{at}: evidence run {rid} is not a failing run in history_evidence')
    if f['cause_confidence'] == 'confirmed':
        errs.append(f'{at}: a history-only flake was never reproduced here, so its cause cannot be confirmed')
    if f['polluter']:
        errs.append(f'{at}: a polluter needs a local bisection; a history-only flake cannot name one')
    if t and t['status'] == 'flaky':
        errs.append(f'{at}: the local runs show it flaky, so it is not history_only (drop the flag and cite the runs)')
    return errs


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


def moves_clock(run: dict) -> bool:
    """The run moved the clock: plugins/clock_shift.py (TDA_CLOCK_SHIFT / TDA_CLOCK_DATE) or libfaketime (FAKETIME)."""
    env = (run.get('variation') or {}).get('env') or {}
    return any(env.get(k) for k in CLOCK_ENV)


def base_test_id(test_id: str) -> str:
    """tests.test_x::test_y[case-1] -> tests.test_x::test_y: a smell names the function, a flake may name one case."""
    return re.sub(r'\[.*\]$', '', test_id)


GO_MODULES: list[tuple[str, str]] = []   # (module path, its directory relative to --repo), from every go.mod


def go_modules(repo: Path) -> list[tuple[str, str]]:
    """Every go.mod under the repo (vendored and dependency trees pruned), longest module path first."""
    import os
    out = []
    for dp, dns, fns in os.walk(repo):
        dns[:] = [d for d in dns if d not in ('.git', 'vendor', 'node_modules', 'testdata', 'third_party')]
        if 'go.mod' in fns:
            text = (Path(dp) / 'go.mod').read_text(encoding='utf-8', errors='replace')
            m = re.search(r'(?m)^\s*module\s+(\S+)', text)
            if m:
                rel = Path(dp).relative_to(repo).as_posix()
                out.append((m.group(1).strip('"'), '' if rel == '.' else rel))
    return sorted(out, key=lambda x: -len(x[0]))


def go_package_dir(head: str) -> str | None:
    """A Go test id's head is the package's import path (gotestsum / go test -json): map it to the package directory
    through the module paths of the repo's go.mod files. None when no module is known (no --repo)."""
    for mod, moddir in GO_MODULES:
        if head == mod or head.startswith(mod + '/'):
            rest = head[len(mod):].strip('/')
            return '/'.join(x for x in (moddir, rest) if x)
    return None


def same_file(flake_file: str | None, flake_id: str, smell_file: str) -> bool:
    """The flake's test file is the smell's file. Pytest ids name a dotted module, JUnit from
    Vitest/Jest the file path; when neither says, the name match alone decides. Go ids name the
    package's import path and no file: a smell in any _test.go file of that package's directory
    matches (the directory comes from go.mod with --repo, otherwise the import path must end in it)."""
    smell_file = smell_file.replace('\\', '/')
    cands = [flake_file.replace('\\', '/')] if flake_file else []
    head = flake_id.split('::')[0]
    if smell_file.endswith('_test.go') and not flake_file and '/' in head and not head.endswith('.go'):
        sdir = smell_file.rpartition('/')[0]
        pdir = go_package_dir(head)
        if pdir is not None:
            return pdir == sdir
        return head == sdir or (bool(sdir) and head.endswith('/' + sdir))
    cands.append(head.replace('\\', '/'))
    if '/' not in head and '.' in head and not re.search(r'\.[cm]?[jt]sx?$', head):
        cands.append(head.replace('.', '/') + '.py')
    known = [c for c in cands if '/' in c or re.search(r'\.\w+$', c)]
    if not known:
        return True
    return any(c == smell_file or c.endswith('/' + smell_file) or smell_file.endswith('/' + c) for c in known)


def smell_in_test(flake_id: str, flake_file: str | None, smell: dict) -> bool:
    """A smell sits in the flake's test: same file, and the smell's test name is the flake's name,
    or its trailing part. Pytest: tests.test_x::test_y[case] matches test_y. Vitest/Jest name tests
    'Describe > Inner > title'; the scanner records that chain, and a bare title still matches the
    last segment."""
    base = base_test_id(flake_id)
    name = base.split('::', 1)[1] if '::' in base else base
    want = base_test_id(smell['test'])
    names_match = (name == want or base.endswith('::' + want) or name.endswith(' > ' + want)
                   or (smell['file'].endswith('_test.go') and name.split('/')[0] == want))  # Go subtest TestX/case
    return names_match and same_file(flake_file, flake_id, smell['file'])


def diff_paths(diff: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r'^\+\+\+ (?:b/)?(\S+)', diff, re.M) if m.group(1) != '/dev/null']


def section_problem(diff: str, path: str, repo: Path | None) -> str | None:
    """For pyproject.toml / setup.cfg / tox.ini: every hunk must sit in the test runner's section."""
    blocks = [b for b in re.split(r'(?m)^(?=--- )', diff) if re.search(r'(?m)^\+\+\+ (?:b/)?' + re.escape(path) + r'\s*$', b)]
    file_lines = None
    if repo is not None and (repo / path).is_file():
        file_lines = (repo / path).read_text(encoding='utf-8').splitlines()
    for block in blocks:
        for h in re.finditer(r'(?m)^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@.*\n((?:[ +\-\\].*(?:\n|$))*)', block):
            section = None
            if file_lines is not None:
                for line in reversed(file_lines[:max(int(h.group(1)) - 1, 0)]):
                    if re.match(r'^\[.*\]\s*$', line):
                        section = line.strip()
                        break
            for line in h.group(2).split('\n'):
                if not line or line[0] == '\\':
                    continue
                if re.match(r'^\[.*\]\s*$', line[1:]) and line[0] in ' +':
                    section = line[1:].strip()
                if line[0] in '+-' and not (section and TEST_SECTION.match(section)):
                    if section is None and file_lines is None:
                        return f'{path}: cannot tell which section the hunk at line {h.group(1)} edits (pass --repo)'
                    return f'{path}: the hunk at line {h.group(1)} edits {section or "the top of the file"}, not the test runner section'
    return None


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
    GO_MODULES[:] = go_modules(repo) if repo is not None else []
    meta = d['metadata']
    tests = {t['id']: t for t in d['tests']}
    flakes = d['flakes']

    for path, text in walk_strings(d):
        m = BANNED.search(text)
        if m:
            errs.append(f'{path}: claims more than the runs show: {m.group(0)!r} (say "not observed flaky in N runs")')
    headline = d['summary']['headline'].strip()
    if PLACEHOLDER.search(headline) or len(headline.split()) < 6 or not re.search(r'\d', headline):
        errs.append('summary.headline: write the result in a sentence with its numbers (tests, runs, flakes), '
                    f'not a placeholder: {headline[:60]!r}')

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
            if SECTION_FILES.search(p):
                problem = section_problem(f['fix']['diff'], p, repo)
                if problem:
                    errs.append(f'{at}: the fix edits {problem}; only test runner config may change')
            elif not TEST_PATH.search(p):
                errs.append(f'{at}: the fix edits {p}, which is not test code or test config; product code is never changed')
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
            elif s['test']:
                fid = flake_ids[s['flake_id']]['test_id']
                if not smell_in_test(fid, (tests.get(fid) or {}).get('file'), s):
                    errs.append(f"smell {s['smell_id']} is in {s['file']} :: {s['test']} but linked to {fid}")
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
        errored = [r for r in runs_data['runs'] if r.get('code_hash') == meta['code_hash'] and r.get('errored')]
        if errored:
            warns.append(f'{len(errored)} errored run(s) did not run the tests and were left out of every count: '
                         + ', '.join(f"{r['id']} ({r.get('error') or 'errored'})" for r in errored[:4]))
        labels = {r.get('label') for r in runs.values()}
        all_labels = {r.get('label') for r in runs_data['runs'] if r.get('code_hash') == meta['code_hash']}
        for v in d['variations']:
            for l in v['labels']:
                if l in all_labels and l not in labels:
                    errs.append(f"variation {v['kind']} names label {l!r}, whose runs all errored (crashed, timed out "
                                'or ran no tests); record the variation as not_run with the reason')
                elif l not in labels:
                    errs.append(f"variation {v['kind']} names label {l!r}, which no run has")
            if v['status'] == 'ran' and v['labels'] and not any(
                    (r.get('totals') or {}).get('tests', 1) for r in runs.values() if r.get('label') in v['labels']):
                errs.append(f"variation {v['kind']} ran, but none of its runs ({', '.join(v['labels'])}) ran a test; "
                            'record it as not_run with the reason')
            if v['kind'] == 'clock' and v['status'] == 'ran' and not any(
                    moves_clock(r) for r in runs.values() if r.get('label') in v['labels']):
                errs.append(f"variation clock ran, but none of its runs ({', '.join(v['labels'])}) records a clock change "
                            f"({', '.join(CLOCK_ENV)} in the run's variation.env)")
        obs, _, _ = outcomes(runs_data, meta['code_hash'])
        for tid, t in rc.items():
            if t['status'] == 'flaky' and tid not in seen_tests:
                errs.append(f'{tid} passed and failed on the same code but has no flake entry')
            if t['status'] == 'consistently_failing' and tid not in {c['test_id'] for c in d['consistently_failing']}:
                errs.append(f'{tid} failed in every run but is not listed in consistently_failing')
            if t['status'] == 'platform_dependent':
                listed = next((c for c in d['consistently_failing'] if c['test_id'] == tid), None)
                if not listed:
                    errs.append(f'{tid} failed every run on {", ".join(failing_platforms(t))} and passed on the other '
                                'platform(s): list it in consistently_failing with its platforms (a platform difference, '
                                'not a flake)')
                elif sorted(listed.get('platforms') or []) != failing_platforms(t):
                    errs.append(f'{tid}: consistently_failing platforms {listed.get("platforms")} but the runs fail on '
                                f'{failing_platforms(t)}')
        for c in d['consistently_failing']:
            st = rc.get(c['test_id'], {}).get('status')
            if st not in ('consistently_failing', 'platform_dependent'):
                errs.append(f"{c['test_id']} is listed as consistently failing but the runs say "
                            f"{st or 'not run'}")
        for f in flakes:
            at = f"{f['id']} ({f['test_id']})"
            t = rc.get(f['test_id'])
            if f.get('history_only'):
                errs += history_errors(f, t, meta)
                exp = score_flake(f, t, meta)
                for k in ('rate_factor', 'gate', 'masking', 'score', 'priority'):
                    if f[k] != exp[k]:
                        errs.append(f'{at}: {k} is {f[k]!r} but the rubric gives {exp[k]!r}')
                continue
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
                    if pol.get('relation', 'polluter') == 'enabler':
                        ok = res.get(pol['pair_run_id']) == 'pass' and res.get(pol['alone_run_id']) == 'fail'
                        if not ok:
                            errs.append(f'{at}: an enabler pair must pass and the test alone must fail')
                    else:
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
        for b in (bisect if isinstance(bisect, list) else [bisect]):
            for f in flakes:
                if f['polluter'] and f['test_id'] == b['victim']:
                    rel = f['polluter'].get('relation', 'polluter')
                    if b['outcome'] != 'minimal_pair':
                        errs.append(f"{f['id']}: names a {rel} but the bisection's outcome is {b['outcome']}")
                        continue
                    if b.get('relation', 'polluter') != rel:
                        errs.append(f"{f['id']}: names a {rel} but the bisection looked for a {b.get('relation', 'polluter')}")
                    steps = {s['run_id'] for s in b['steps']}
                    if not set(f['polluter']['bisect_run_ids']) <= steps:
                        errs.append(f"{f['id']}: {rel} bisect runs are not the bisection's steps")
                    named = {b.get('polluter'), b.get('polluter_node_id')} - {None}
                    if named and f['polluter']['test_id'] not in named and not any(
                            same_test(f['polluter']['test_id'], n) for n in named):
                        errs.append(f"{f['id']}: {rel} {f['polluter']['test_id']} is not the one the bisection named "
                                    f"({b.get('polluter')})")

    # numbers in the headline
    if d['tests']:
        bounds = set()
        for t in d['tests']:
            for n, k in ((t['runs'], t['fails']), (t['normal']['runs'], t['normal']['fails']),
                         (t['worst']['runs'], t['worst']['fails']), (t['runs'], 0)):
                if n:
                    bounds.add(stats.wilson(k, n)[1] * 100)
        for m in UPPER_BOUND.finditer(d['summary'].get('headline', '')):
            claimed, places = float(m.group(1)), len(m.group(1).partition('.')[2])
            if not any(abs(round(b, places) - claimed) < 1e-9 for b in bounds):
                errs.append(f"summary.headline: upper bound {m.group(1)}% matches no Wilson upper bound of the runs "
                            '(compute it with stats.py; 0 of n has upper bound stats.py 0 n)')
    return errs, warns


PLACEHOLDER = re.compile(r'\b(?:placeholder|todo|tbd|tbc|fixme|lorem ipsum|xxx)\b|^\W*$', re.I)
UPPER_BOUND = re.compile(r'upper (?:bound|limit|end)[^0-9%.]{0,40}?(\d+(?:\.\d+)?)\s*%', re.I)


def same_test(a: str, b: str) -> bool:
    """tests.test_x::name (a runs.json case id) and tests/test_x.py::name (a pytest node id) name one test."""
    ma, _, na = a.partition('::')
    mb, _, nb = b.partition('::')
    if na != nb:
        return False
    norm = lambda m: re.sub(r'\.py$', '', m).replace('/', '.').replace('\\', '.')  # noqa: E731
    x, y = norm(ma), norm(mb)
    return x == y or x.endswith('.' + y) or y.endswith('.' + x)


# ---------------------------------------------------------------- self-test
SELF_HASH = 'a' * 64
SELF_MOD = 'example.test/harbour/pkg/cart'


def _self_fixture() -> tuple[dict, dict]:
    """A small Go suite: TestTotal passed, failed, passed (a flake); TestTax passed every run."""
    runs = {'runs': [], 'cases': []}
    for i, label in enumerate(('baseline', 'repeat', 'repeat'), 1):
        runs['runs'].append({'id': f'R-{i}', 'label': label, 'code_hash': SELF_HASH, 'duration_s': 6.0,
                             'totals': {'tests': 2}, 'variation': {'env': {}}, 'platform': 'linux'})
        runs['cases'].append({'run_id': f'R-{i}', 'id': f'{SELF_MOD}::TestTotal', 'file': None,
                              'status': 'failed' if i == 2 else 'passed'})
        runs['cases'].append({'run_id': f'R-{i}', 'id': f'{SELF_MOD}::TestTax', 'file': None, 'status': 'passed'})
    diff = ('--- a/pkg/cart/cart_test.go\n+++ b/pkg/cart/cart_test.go\n@@ -10,1 +10,1 @@\n'
            '-\ttime.Sleep(10 * time.Millisecond)\n+\t<-done\n')
    flake = {'id': 'FL-1', 'test_id': f'{SELF_MOD}::TestTotal', 'cause': 'async_wait', 'cause_confidence': 'indicated',
             'experiment': {'run_ids': ['R-1', 'R-2', 'R-3'], 'what_changed': 'Repeated the package three times.',
                            'result': 'Failed once when the goroutine finished after the fixed sleep.'},
             'evidence_runs': {'passing': ['R-1'], 'failing': ['R-2']}, 'polluter': None,
             'failure_excerpts': ['cart_test.go:12: total = 0, want 30'], 'rate_factor': 1, 'gate': 1, 'masking': 1,
             'score': 1, 'priority': 'P3', 'priority_note': '',
             'fix': {'diff': diff, 'rationale': 'Wait on the done channel instead of a fixed sleep.', 'applied': False,
                     'verification_run_ids': []}, 'quarantine': None}
    rep = {'schema_version': '1.2.0', 'skill': {'name': 'flaky-test-hunter', 'version': 'self-test'},
           'generated_at': '2026-11-14T00:00:00Z',
           'metadata': {'repo': 'harbour', 'commit': None, 'code_hash': SELF_HASH, 'framework': 'go test',
                        'run_command': 'go test -json ./...', 'runs_file': 'runs.json', 'platform': 'linux',
                        'budget': {'repeats': 3, 'minutes': 10, 'stopped_early': False},
                        'gate': {'level': 'local', 'evidence': 'self-test'}, 'critical_tests': [],
                        'history': {'status': 'used', 'source': 'CI run history (read-only)'}},
           'tools': [{'name': 'go', 'version': '1.26', 'status': 'ran', 'reason': "The repo's runner."}],
           'variations': [{'kind': 'repeat', 'status': 'ran', 'labels': ['baseline', 'repeat'], 'reason': 'Three runs.'}],
           'tests': [], 'flakes': [flake], 'consistently_failing': [], 'static_smells': [],
           'summary': {'headline': '1 of 2 tests passed and failed on the same code across 3 runs.'},
           'limitations': [], 'files_changed': []}
    return rep, runs


def self_test() -> int:
    import copy
    fails: list[str] = []

    def run(name: str, mutate, want_valid: bool, want_text: str = '') -> None:
        rep, runs = _self_fixture()
        mutate(rep, runs)
        write_summary(rep, runs)
        errs, _ = check(rep, runs, None, None)
        ok = (not errs) if want_valid else any(want_text in e for e in errs)
        if not ok:
            fails.append(f'{name}: expected {"VALID" if want_valid else "an error naming " + repr(want_text)}, got '
                         + ('; '.join(errs[:3]) or 'VALID'))

    run('baseline fixture', lambda r, x: None, True)

    def go_smell(rep, runs):
        rep['static_smells'].append({'smell_id': 'SM-1', 'smell': 'fixed-sleep-go', 'file': 'pkg/cart/cart_test.go',
                                     'line': 10, 'test': 'TestTotal', 'cause': 'async_wait', 'status': 'linked_to_flake',
                                     'flake_id': 'FL-1', 'note': 'The sleep the flake waits on.'})
    run('a Go smell links to a Go flake by its package directory', go_smell, True)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:          # with --repo the module path comes from go.mod
        (Path(tmp) / 'go.mod').write_text('module example.test/harbour\n\ngo 1.26\n', encoding='utf-8')
        rep, runs = _self_fixture()
        go_smell(rep, runs)
        write_summary(rep, runs)
        errs, _ = check(rep, runs, None, None, Path(tmp))
        errs = [e for e in errs if 'does not apply' not in e]     # the fixture repo holds only go.mod
        if errs:
            fails.append('a Go smell links through go.mod with --repo: ' + '; '.join(errs[:2]))
        GO_MODULES.clear()

    def go_smell_wrong_dir(rep, runs):
        go_smell(rep, runs)
        rep['static_smells'][0]['file'] = 'pkg/tax/tax_test.go'
    run('a Go smell in another package does not link', go_smell_wrong_dir, False, 'but linked to')

    def go_subtest(rep, runs):
        go_smell(rep, runs)
        for c in runs['cases']:
            c['id'] = c['id'].replace('::TestTotal', '::TestTotal/with_discount')
        rep['flakes'][0]['test_id'] += '/with_discount'
    run('a smell in a test links to a flake in one of its subtests', go_subtest, True)

    def history(rep, runs):
        for i in (1, 2, 3):
            runs['cases'].append({'run_id': f'R-{i}', 'id': f'{SELF_MOD}::TestRetry', 'file': None, 'status': 'passed'})
        rep['flakes'].append({**copy.deepcopy(rep['flakes'][0]), 'id': 'FL-2', 'test_id': f'{SELF_MOD}::TestRetry',
                              'cause': 'unknown', 'cause_confidence': 'suspected', 'history_only': True,
                              'experiment': {'run_ids': ['R-1', 'R-2', 'R-3'], 'what_changed': 'Repeated three times.',
                                             'result': 'Passed every local run; failed once in CI.'},
                              'history_evidence': [
                                  {'id': 'CI-101', 'source': 'ci', 'ref': 'workflow run 101, job test', 'outcome': 'fail',
                                   'excerpt': '--- FAIL: TestRetry (0.10s)'},
                                  {'id': 'CI-102', 'source': 'ci', 'ref': 'workflow run 102, job test', 'outcome': 'pass',
                                   'excerpt': '--- PASS: TestRetry (0.01s)'}],
                              'evidence_runs': {'passing': ['CI-102'], 'failing': ['CI-101']}})
    run('a history-only flake with CI evidence is valid', history, True)

    def history_no_evidence(rep, runs):
        history(rep, runs)
        del rep['flakes'][1]['history_evidence']
    run('a history-only flake needs its history evidence', history_no_evidence, False, 'history_evidence')

    def history_confirmed(rep, runs):
        history(rep, runs)
        rep['flakes'][1]['cause_confidence'] = 'confirmed'
    run('a history-only flake cannot be confirmed', history_confirmed, False, 'never reproduced')

    def history_not_used(rep, runs):
        history(rep, runs)
        rep['metadata']['history'] = {'status': 'not_given', 'source': None}
    run('a history-only flake needs metadata.history used', history_not_used, False, 'metadata.history')

    def race(rep, runs):
        runs['runs'].append({'id': 'RC-1', 'label': 'race', 'code_hash': SELF_HASH, 'duration_s': 9.0,
                             'totals': {'tests': 2}, 'variation': {'env': {}}, 'platform': 'linux'})
        runs['cases'] += [{'run_id': 'RC-1', 'id': f'{SELF_MOD}::TestTotal', 'file': None, 'status': 'passed'},
                          {'run_id': 'RC-1', 'id': f'{SELF_MOD}::TestTax', 'file': None, 'status': 'passed'}]
        rep['variations'].append({'kind': 'race', 'status': 'ran', 'labels': ['race'], 'reason': 'go test -race.'})
    run('a race variation is a kind of its own', race, True)

    def platform(rep, runs):
        for i in (1, 2):
            runs['runs'].append({'id': f'W-{i}', 'label': 'repeat', 'code_hash': SELF_HASH, 'duration_s': 7.0,
                                 'totals': {'tests': 2}, 'variation': {'env': {}}, 'platform': 'windows'})
            runs['cases'] += [{'run_id': f'W-{i}', 'id': f'{SELF_MOD}::TestTax', 'file': None, 'status': 'failed'}]
        rep['consistently_failing'].append({'test_id': f'{SELF_MOD}::TestTax', 'runs': 2, 'platforms': ['windows'],
                                            'failure_excerpt': 'tax_test.go:9: path separator', 'note':
                                            'Fails on every Windows run and passes on every Linux run: a portability bug.'})
        rep['variations'].append({'kind': 'platform', 'status': 'ran', 'labels': ['repeat'], 'reason': 'Linux and Windows.'})
    run('a test that fails on one platform and passes on another is not a flake', platform, True)

    def platform_unlisted(rep, runs):
        platform(rep, runs)
        rep['consistently_failing'] = []
    run('a platform-dependent failure must be listed', platform_unlisted, False, 'platform')

    if fails:
        print('validate_flaky_report self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('validate_flaky_report self-test: ok')
    return 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--runs', type=Path)
    ap.add_argument('--smells', type=Path)
    ap.add_argument('--bisect', type=Path, nargs='+', help='one or more bisect_polluter.py outputs')
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
    smells = json.loads(args.smells.read_text(encoding='utf-8')) if args.smells else None
    bisect = [json.loads(p.read_text(encoding='utf-8')) for p in args.bisect] if args.bisect else None
    errs, warns = check(d, runs_data, smells, bisect, args.repo)
    if args.write_summary:
        if errs:
            print('NOTE: the report was not rewritten because it is INVALID (fix the errors and run again)')
        else:  # only a valid report is rewritten; flakes come back ordered by priority
            args.report.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
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
