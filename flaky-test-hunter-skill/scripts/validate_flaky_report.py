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
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import stats  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'flaky_report.schema.json'
NORMAL_LABELS = ('baseline', 'repeat')
NOT_WORST = ('bisect',)
GATE = {'required': 3, 'ci': 2, 'local': 1}
CLOCK_ENV = ('TDA_CLOCK_SHIFT', 'TDA_CLOCK_DATE', 'FAKETIME')
ALONE = 'cannot_run_alone'
SPAWN_LOAD_ENV = 'QA_SPAWN_LOAD'      # recorded by runs made under scripts/spawn_load.py (--record-env)
FOREIGN_BINARY = 'built from another checkout'      # run_tests.cargo_foreign_binaries' note
FOREIGN_LIMITATION = re.compile(r'another (?:checkout|copy)|shared target|target[- ]dir|CARGO_TARGET_DIR', re.I)
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


def single_selection(run: dict) -> str | None:
    """The one test id or test file a run was asked for (an --isolate run, or a file run on its own)."""
    sel = run.get('tests_selected') or []
    return sel[0].replace('\\', '/') if len(sel) == 1 and isinstance(sel[0], str) else None


def selects(sel: str, tid: str, file: str | None) -> bool:
    """A single selection names this test (its id, in either form) or the file that defines it."""
    if '::' in sel:
        return sel == tid or same_test(tid, sel)
    f = (file or '').replace('\\', '/')
    return bool(f) and (f == sel or f.endswith('/' + sel) or sel.endswith('/' + f))


def alone_info(runs_data: dict, code_hash: str, obs: dict, files: dict) -> dict[str, dict]:
    """test id -> {attempts, errored, error, run_ids} for tests with an errored run of their own: a run on this code
    that selected only the test (or only its file) and errored, so nothing was collected. `attempts` adds the
    counted runs that selected only the test or its file."""
    same = [r for r in runs_data['runs'] if r.get('code_hash') == code_hash]
    bad = [(r, single_selection(r)) for r in same if r.get('errored')]
    bad = [(r, s) for r, s in bad if s]
    if not bad:
        return {}
    ok_single = {r['id']: single_selection(r) for r in same if not r.get('errored') and single_selection(r)}
    out = {}
    for tid in obs:
        mine = [r for r, s in bad if selects(s, tid, files.get(tid))]
        if not mine:
            continue
        ran = sum(1 for rid, _, _ in obs[tid] if rid in ok_single and selects(ok_single[rid], tid, files.get(tid)))
        out[tid] = {'attempts': len(mine) + ran, 'errored': len(mine), 'error': mine[0].get('error') or 'errored',
                    'run_ids': [r['id'] for r in mine]}
    return out


def test_stats(tid: str, obs: list, file: str | None, plat: dict | None = None, alone: dict | None = None,
               clock_runs: set | None = None) -> dict:
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
    if alone:
        # errored (nothing collected) in runs of its own: every such run when it never ran alone
        extra['alone'] = {k: alone[k] for k in ('attempts', 'errored', 'error')}
        if status == 'not_observed_flaky' and alone['errored'] == alone['attempts']:
            status = ALONE
    if clock_runs is not None and status == 'flaky' and all(rid in clock_runs for rid, _, o in obs if o == 'fail') \
            and any(rid not in clock_runs for rid, _, o in obs if o == 'pass'):
        extra['probe_only'] = 'clock'     # failed only where the skill moved the clock
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
    if t and t.get('probe_only') and not f.get('probe_confirmation') and order.index(pr) < 3:
        pr = 'P3'
        notes.append(f"capped at P3: it failed only under the {t['probe_only']} shift this skill applied and never "
                     'without it, a probe artefact until control runs (file times left real) still fail')
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
    new = version_at_least(d, (1, 4, 0))
    alone = alone_info(runs_data, d['metadata']['code_hash'], obs, files) if new else {}
    clock = {rid for rid, r in runs.items() if moves_clock(r)} if new else None
    tests = [test_stats(tid, obs[tid], files.get(tid), plat, alone.get(tid), clock) for tid in sorted(obs)]
    return tests, runs


def static_not_run(d: dict, smells: dict | None, ran: set) -> list[dict]:
    """Rows for tests the scan knows a plain run leaves out (smells.json suites[].not_run_by_default: #[ignore],
    a feature or platform gate, required-features) that have no counted run: status not_run, with the gate."""
    if not smells or not version_at_least(d, (1, 4, 0)):
        return []
    rows = []
    for s in smells.get('suites') or []:
        for x in s.get('not_run_by_default') or []:
            if x['id'] in ran or any(r['id'] == x['id'] for r in rows):
                continue
            why = ': '.join(v for v in (x.get('gate'), x.get('detail')) if v) or 'not run by default'
            rows.append({'id': x['id'], 'file': x.get('file'), 'runs': 0, 'passes': 0, 'fails': 0, 'normal': rate(0, 0),
                         'worst': {'label': None, 'runs': 0, 'fails': 0, 'wilson_low': 0.0}, 'by_label': [],
                         'status': 'not_run', 'not_run_reason': why})
    return rows


def is_static_row(t: dict) -> bool:
    return t['status'] == 'not_run' and not t['runs']


def summary_numbers(d: dict, runs_data: dict, runs: dict) -> dict:
    tests = d['tests']
    count = lambda s: sum(t['status'] == s for t in tests)  # noqa: E731
    by_cause, by_pr = defaultdict(int), {'P0': 0, 'P1': 0, 'P2': 0, 'P3': 0}
    for f in d['flakes']:
        by_cause[f['cause']] += 1
        by_pr[f['priority']] += 1
    ran = sum(v['status'] == 'ran' for v in d['variations'])
    planned = sum(v['status'] in ('ran', 'not_run') for v in d['variations'])
    completeness = round(ran / planned * suite_share(d['metadata'].get('suites'), observed_counts(runs_data, runs),
                                                     version_at_least(d, (1, 4, 0))), 2) if planned else 0.0
    single = sum(bool((r.get('variation') or {}).get('isolate')) for r in runs.values())
    return {'tests_run': sum(t['status'] != 'not_run' for t in tests), 'flaky': count('flaky'),
            'consistently_failing': count('consistently_failing'), 'not_observed_flaky': count('not_observed_flaky'),
            'not_run': count('not_run'),
            **({'platform_dependent': count('platform_dependent')} if count('platform_dependent') else {}),
            **({ALONE: count(ALONE)} if count(ALONE) else {}),
            'by_cause': dict(sorted(by_cause.items())), 'by_priority': by_pr,
            'total_runs': len(runs),
            **({'single_test_runs': single} if single and version_at_least(d, (1, 3, 0)) else {}),
            'wall_minutes': round(sum(r.get('duration_s', 0) for r in runs.values()) / 60, 1),
            'evidence_completeness': completeness}


def version_at_least(d: dict, v: tuple) -> bool:
    try:
        return tuple(int(x) for x in str(d.get('schema_version', '0')).split('.')) >= v
    except ValueError:
        return False


def observed_counts(runs_data: dict | None, runs: dict | None) -> dict[str, int]:
    """preset -> the number of distinct test ids its counted runs recorded (skipped cases left out)."""
    if not runs_data or not runs:
        return {}
    ids: dict[str, set] = defaultdict(set)
    for c in runs_data.get('cases', []):
        r = runs.get(c['run_id'])
        if r and c['status'] != 'skipped':
            ids[r.get('preset') or ''].add(REPEAT_SUFFIX.sub('', c['id']))
    return {k: len(v) for k, v in ids.items()}


def suite_share(suites: list | None, observed: dict[str, int] | None = None, count_ran: bool = False) -> float:
    """The share of the repository's tests that had per-test runs: tests of suites that ran over all suites' tests
    (static counts), or suites that ran over all suites when a count is unknown. A suite that ran with no static
    count (script self-tests print their checks, they don't declare them) counts the distinct tests its presets'
    runs recorded. 1.0 when the report lists no suites (one language, as before schema 1.3.0). count_ran (schema
    1.4.0): a suite that ran counts the distinct tests its runs recorded, at most its static count, so tests a plain
    run leaves out (#[ignore], feature and platform gates, a sample of a large suite) lower the share."""
    if not suites:
        return 1.0
    observed = observed or {}
    counts = []
    for s in suites:
        n = s.get('tests')
        if not isinstance(n, int) and s['status'] == 'ran':
            n = sum(observed.get(p, 0) for p in s.get('presets') or []) or None
        counts.append(n)
    if all(isinstance(n, int) for n in counts) and sum(counts):
        def seen(s, n):
            got = sum(observed.get(p, 0) for p in s.get('presets') or [])
            return min(n, got) if count_ran and got else n
        return sum(seen(s, n) for s, n in zip(suites, counts) if s['status'] == 'ran') / sum(counts)
    return sum(s['status'] == 'ran' for s in suites) / len(suites)


def suite_errors(d: dict, runs_data: dict | None, runs: dict | None, smells: dict | None) -> tuple[list[str], list[str]]:
    """metadata.suites against the runs (a suite that ran has counted runs of its presets with test cases; one that
    did not, has none) and against scan_pitfalls.py's inventory (every language it found tests in is listed)."""
    errs, warns = [], []
    suites = d['metadata'].get('suites') or []
    langs = [s['language'] for s in suites]
    for l in sorted({l for l in langs if langs.count(l) > 1}):
        errs.append(f'metadata.suites lists {l} more than once')
    if runs is not None:
        with_cases = {c['run_id'] for c in runs_data['cases'] if c['status'] != 'skipped'}
        counted = [r for r in runs.values() if r['id'] in with_cases]
        for s in suites:
            mine = [r['id'] for r in counted if r.get('preset') in s['presets']]
            if s['status'] == 'ran' and not mine:
                errs.append(f"metadata.suites {s['language']} is recorded as ran, but no counted run with test results "
                            f"used its presets ({', '.join(s['presets']) or 'none named'}); record it as not_run with the reason")
            if s['status'] == 'not_run' and mine:
                errs.append(f"metadata.suites {s['language']} is recorded as not_run, but {len(mine)} counted run(s) used "
                            f"its presets ({', '.join(mine[:3])}); record it as ran")
        if suites:
            stray = sorted({r.get('preset') or '(no preset)' for r in counted} - {p for s in suites for p in s['presets']})
            if stray:
                warns.append(f"runs with preset(s) {', '.join(stray)} belong to no suite in metadata.suites")
    found = [s['language'] for s in (smells or {}).get('suites') or [] if s.get('tests') or s.get('test_files')]
    if len(found) > 1 and not suites:
        errs.append(f"metadata.suites: the scan found tests in {len(found)} languages ({', '.join(found)}); list each "
                    'as ran or not_run with the reason, so a language with no per-test runs counts against evidence '
                    'completeness')
    elif suites:
        for l in found:
            if l not in langs:
                errs.append(f'metadata.suites does not list {l}, which the scan found tests in (ran or not_run, with '
                            'the reason)')
    return errs, warns


def write_summary(d: dict, runs_data: dict, smells: dict | None = None) -> None:
    tests, runs = compute(d, runs_data)
    tests = sorted(tests + static_not_run(d, smells, {t['id'] for t in tests}), key=lambda t: t['id'])
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


def clock_control(run: dict) -> bool:
    """A clock run that left file times real: TDA_CLOCK_FILES=0 (clock_shift.py / .mjs), or NO_FAKE_STAT with
    libfaketime. The value of TDA_CLOCK_FILES is recorded only with run_tests.py --record-env TDA_CLOCK_FILES."""
    env = (run.get('variation') or {}).get('env') or {}
    return moves_clock(run) and (env.get('TDA_CLOCK_FILES') == '0' or bool(env.get('NO_FAKE_STAT')))


def probe_errors(f: dict, t: dict, res: dict, runs: dict) -> list[str]:
    """A flake whose every failure came under the clock shift the skill applied (tests[].probe_only) is a probe
    artefact until control runs show otherwise: the same shift with file times left real, failing at least twice.
    Without them its cause cannot be `confirmed` (the on/off experiment shows the probe makes it fail, not what in
    the test does) and score_flake caps it at P3."""
    at = f"{f['id']} ({f['test_id']})"
    errs = []
    conf = f.get('probe_confirmation')
    if conf and not t.get('probe_only'):
        errs.append(f'{at}: probe_confirmation is for a flake that failed only under a probe; this one '
                    'failed without it too (drop the field)')
    elif conf:
        bad = [rid for rid in conf['run_ids'] if res.get(rid) != 'fail' or rid not in runs or not clock_control(runs[rid])]
        if bad or len(conf['run_ids']) < 2:
            redacted = [rid for rid in bad if rid in runs and (runs[rid].get('variation') or {}).get('env', {})
                        .get('TDA_CLOCK_FILES') == '<redacted>']
            errs.append(f'{at}: probe_confirmation needs 2 or more failing control runs of this test (the clock moved '
                        'with file times left real: TDA_CLOCK_FILES=0, or NO_FAKE_STAT with libfaketime)'
                        + (f"; not such runs: {', '.join(bad[:4])}" if bad else '')
                        + ('; record the value with --record-env TDA_CLOCK_FILES' if redacted else ''))
    elif t.get('probe_only') and f['cause_confidence'] == 'confirmed':
        errs.append(f"{at}: it failed only under the {t['probe_only']} shift this skill applied, so the on/off "
                    'experiment confirms the probe, not a cause in the test: a probe artefact is at most indicated '
                    'until probe_confirmation names control runs that still fail')
    return errs


def alone_errors(d: dict, runs_data: dict, rc: dict, new: bool) -> list[str]:
    """cannot_run_alone[] against the runs: every test with that status is in exactly one entry, every listed test
    has the status, alone_run_ids are errored runs on this code that selected only one of the entry's tests (or its
    file), and needs_run_ids are counted runs in which a listed test passed."""
    errs = []
    entries = d.get('cannot_run_alone') or []
    if entries and not new:
        return ['cannot_run_alone needs schema_version 1.4.0']
    listed: dict[str, int] = defaultdict(int)
    same = {r['id']: r for r in runs_data['runs'] if r.get('code_hash') == d['metadata']['code_hash']}
    passed = defaultdict(set)
    for c in runs_data['cases']:
        if c['status'] == 'passed':
            passed[c['run_id']].add(REPEAT_SUFFIX.sub('', c['id']))
    for i, e in enumerate(entries):
        at = f"cannot_run_alone[{i}] ({e.get('file') or e['test_ids'][0]})"
        for tid in e['test_ids']:
            listed[tid] += 1
            st = rc.get(tid, {}).get('status')
            if st != ALONE:
                errs.append(f"{at}: {tid} is listed as cannot_run_alone but the runs say {st or 'not run'} (it needs "
                            'an errored run of its own and no failure in the runs that did run it)')
        for rid in e['alone_run_ids']:
            r = same.get(rid)
            sel = single_selection(r) if r else None
            if not r or not r.get('errored') or not sel or not any(
                    selects(sel, tid, rc.get(tid, {}).get('file')) for tid in e['test_ids']):
                errs.append(f'{at}: alone_run_ids names {rid}, which is not an errored run of one of these tests (or '
                            'their file) alone')
        if e.get('needs') and not e.get('needs_run_ids'):
            errs.append(f"{at}: names what the tests need ({e['needs']}) but no needs_run_ids: the runs that put it "
                        'first and passed')
        for rid in e.get('needs_run_ids') or []:
            r = same.get(rid)
            if not r or r.get('errored') or not (passed[rid] & set(e['test_ids'])):
                errs.append(f'{at}: needs_run_ids names {rid}, in which none of these tests passed')
    for tid, trow in rc.items():
        if trow['status'] == ALONE and not listed.get(tid):
            a = trow.get('alone') or {}
            errs.append(f"{tid} errored in every run of its own ({a.get('errored')} of {a.get('attempts')}: "
                        f"{a.get('error')}) and passed in the suite: list it in cannot_run_alone with the error and "
                        'what it depends on (an order or import dependence, not a flake and not unobserved)')
    for tid, n in listed.items():
        if n > 1:
            errs.append(f'{tid}: in more than one cannot_run_alone entry')
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
    if smell_file.endswith('_test.go') and cands and cands[0].endswith('_test.go'):
        # one Go package: test names are unique across its _test.go files, so any file in the directory matches
        return cands[0].rpartition('/')[0] == smell_file.rpartition('/')[0]
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
        new = version_at_least(d, (1, 4, 0))
        # schema 1.4.0: rows for tests known statically that no counted run reached (status not_run, 0 runs)
        static_rows = {k: t for k, t in tests.items() if new and is_static_row(t) and k not in rc}
        from_runs = {k: t for k, t in tests.items() if k not in static_rows}
        if rc != from_runs:
            diff_ids = sorted(k for k in set(rc) | set(from_runs) if rc.get(k) != from_runs.get(k))
            errs.append(f'tests[] differs from the runs for: {", ".join(diff_ids[:6])} (run --write-summary)')
        for x in static_not_run(d, smells, set(rc)):
            if x['id'] not in static_rows:
                errs.append(f"{x['id']} never ran ({x['not_run_reason']}) but has no not_run row in tests[]: a plain "
                            'run leaves it out, so it is not run, not unobserved (run --write-summary with --smells)')
        for k, trow in static_rows.items():
            if not trow.get('not_run_reason'):
                errs.append(f'{k}: a not_run row needs not_run_reason (why no run reached it)')
        errs += alone_errors(d, runs_data, rc, new)
        foreign = [r['id'] for r in runs.values() if any(FOREIGN_BINARY in n for n in r.get('notes') or [])]
        if foreign and not any(FOREIGN_LIMITATION.search(l) for l in d['limitations']):
            errs.append(f"{len(foreign)} counted run(s) ({', '.join(foreign[:4])}) ran test binaries built from another "
                        'checkout (a target directory shared between copies of the repository: see the run notes): '
                        'name it in limitations, because code_hash does not describe what those runs ran')
        other = [r['id'] for r in runs_data['runs'] if r.get('code_hash') != meta['code_hash']]
        if other:
            warns.append(f'{len(other)} run(s) have a different code_hash and were left out: {", ".join(other[:5])}')
        errored = [r for r in runs_data['runs'] if r.get('code_hash') == meta['code_hash'] and r.get('errored')]
        if errored:
            warns.append(f'{len(errored)} errored run(s) did not run the tests and were left out of every count: '
                         + ', '.join(f"{r['id']} ({r.get('error') or 'errored'})" for r in errored[:4]))
            if not new:
                obs0, files0, _ = outcomes(runs_data, meta['code_hash'])
                only = sorted(k for k, a in alone_info(runs_data, meta['code_hash'], obs0, files0).items()
                              if a['errored'] == a['attempts'] and rc.get(k, {}).get('status') == 'not_observed_flaky')
                if only:
                    warns.append(f"{len(only)} test(s) errored in every run alone and passed in the suite "
                                 f"({', '.join(only[:4])}): an order or import dependence the counts above leave out; "
                                 'schema 1.4.0 reports them as cannot_run_alone')
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
            if v['kind'] == 'suite_load' and v['status'] == 'ran' and not any(
                    (r.get('variation') or {}).get('env', {}).get(SPAWN_LOAD_ENV)
                    or (r.get('totals') or {}).get('tests', 0) > 1
                    for r in runs.values() if r.get('label') in v['labels']):
                errs.append(f"variation suite_load ran, but none of its runs ({', '.join(v['labels'])}) records "
                            f"process-creation load ({SPAWN_LOAD_ENV} in the run's variation.env: run under "
                            'scripts/spawn_load.py with --record-env) or ran the test beside others (more than one '
                            'test in the run)')
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
            errs += probe_errors(f, t, res, runs)
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
        # a smell's test with no counted run: nothing was observed, so it is not_run, never not_observed. A script
        # self-test (selftest preset) names its checks by message, not by function: a smell in the script ran when
        # any of its checks did, since the script runs whole.
        selftest_ids: dict[str, list[str]] = defaultdict(list)
        for c in runs_data['cases']:
            r = runs.get(c['run_id'])
            if r and r.get('preset') == 'selftest' and c['status'] != 'skipped' and c.get('file'):
                tid = REPEAT_SUFFIX.sub('', c['id'])
                if tid not in selftest_ids[c['file'].replace('\\', '/')]:
                    selftest_ids[c['file'].replace('\\', '/')].append(tid)
        for s in d['static_smells']:
            if not s['test'] or s['status'] not in ('not_observed', 'not_run'):
                continue
            ran_as = [tid for tid, t in rc.items() if t['runs'] and smell_in_test(tid, t['file'], s)]
            if not ran_as:
                ran_as = [t for t in selftest_ids.get(s['file'].replace('\\', '/'), []) if rc.get(t, {}).get('runs')]
            if s['status'] == 'not_observed' and not ran_as:
                errs.append(f"smell {s['smell_id']} ({s['file']} :: {s['test']}) is not_observed, but its test never ran "
                            'in the counted runs: record it as not_run')
            elif s['status'] == 'not_run' and ran_as:
                errs.append(f"smell {s['smell_id']} is not_run, but its test ran in {sum(rc[t]['runs'] for t in ran_as)} "
                            f"counted run(s) ({ran_as[0]}): record it as not_observed with the run count, or link it")
        exp_sum = summary_numbers(d, runs_data, runs)
        for k, v in exp_sum.items():
            if d['summary'].get(k) != v:
                errs.append(f'summary.{k} is {d["summary"].get(k)!r} but the runs give {v!r}')
        for k in ('single_test_runs', 'platform_dependent'):
            if k in d['summary'] and k not in exp_sum:
                errs.append(f'summary.{k} is {d["summary"][k]!r} but the runs give none')
    else:
        warns.append('no --runs given; counts, statuses and experiments were not re-checked')
    se, sw = suite_errors(d, runs_data, runs if runs_data is not None else None, smells)
    errs += se
    warns += sw

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
    errs += headline_priority_errors(d['summary'].get('headline', ''), d['summary'].get('by_priority') or {})
    return errs, warns


NUMBER_WORDS = {'no': 0, 'zero': 0, 'none': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
PRIORITY_IN_TEXT = re.compile(r'\b(?:(\d+|no|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?P([0-3])s?\b',
                              re.I)
LIST_JOIN = re.compile(r'^\s*(?:,|/|or|and|nor|,\s*or|,\s*and)\s*$', re.I)


def headline_priority_errors(headline: str, by_priority: dict) -> list[str]:
    """Priority labels the headline names must agree with the computed ranking: a bare label (\"ranks P2\") needs at
    least one flake of that priority, a counted one (\"2 P1 flakes\", \"no P0\") needs exactly that many. A count
    carries across a list (\"no P0 or P1\")."""
    errs, prev_end, prev_n = [], None, None
    for m in PRIORITY_IN_TEXT.finditer(headline):
        label = 'P' + m.group(2)
        q = m.group(1)
        if q is not None:
            n = int(q) if q.isdigit() else NUMBER_WORDS[q.lower()]
        elif prev_end is not None and LIST_JOIN.match(headline[prev_end:m.start()]):
            n = prev_n
        else:
            n = None
        have = int(by_priority.get(label, 0))
        if n is None and not have:
            errs.append(f'summary.headline: names {label}, but no flake is ranked {label} (computed: '
                        + ', '.join(f'{k} {v}' for k, v in sorted(by_priority.items())) + ')')
        elif n is not None and n != have:
            errs.append(f'summary.headline: says {n} {label}, but {have} flake(s) are ranked {label}')
        prev_end, prev_n = m.end(), n
    return errs


PLACEHOLDER =re.compile(r'\b(?:placeholder|todo|tbd|tbc|fixme|lorem ipsum|xxx)\b|^\W*$', re.I)
UPPER_BOUND = re.compile(r'upper (?:bound|limit|end)[^0-9%.]{0,40}?(\d+(?:\.\d+)?)\s*%', re.I)


def split_test_id(tid: str) -> tuple[str, str]:
    """(dotted module and classes, test name) of a runs.json case id (tests.test_x.Scale::test_y) or a pytest node
    id (tests/test_x.py::Scale::test_y[case]); a '::' inside a parameter id stays in the name."""
    head, br, tail = tid.partition('[')
    parts = head.split('::')
    mod = re.sub(r'\.py$', '', parts[0]).replace('/', '.').replace('\\', '.')
    return '.'.join([mod] + parts[1:-1]) if len(parts) > 1 else '', parts[-1] + br + tail


def same_test(a: str, b: str) -> bool:
    """tests.test_x.Scale::name (a runs.json case id) and tests/test_x.py::Scale::name (a pytest node id) name one
    test, classes included, even when one is relative to pytest's rootdir and the other to --cwd."""
    (x, na), (y, nb) = split_test_id(a), split_test_id(b)
    if na != nb:
        return False
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

    def run(name: str, mutate, want_valid: bool, want_text: str = '', smells: dict | None = None, after=None,
            want_warn: str = '') -> None:
        rep, runs = _self_fixture()
        mutate(rep, runs)
        write_summary(rep, runs, smells)
        if after:
            after(rep)
        errs, warns = check(rep, runs, smells, None)
        ok = (not errs) if want_valid else any(want_text in e for e in errs)
        if not ok:
            fails.append(f'{name}: expected {"VALID" if want_valid else "an error naming " + repr(want_text)}, got '
                         + ('; '.join(errs[:3]) or 'VALID'))
        elif want_warn and not any(want_warn in w for w in warns):
            fails.append(f'{name}: expected a warning naming {want_warn!r}, got {warns}')

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

    def go_file_set(rep, runs):              # parse_results names the _test.go defining the test; the smell is in a
        go_smell(rep, runs)                  # helper file of the same package
        rep['static_smells'][0]['file'] = 'pkg/cart/helpers_test.go'
        for c in runs['cases']:
            c['file'] = 'pkg/cart/cart_test.go'
    run('a Go smell in another _test.go file of the flake\'s package links', go_file_set, True)

    def go_file_other_pkg(rep, runs):
        go_file_set(rep, runs)
        rep['static_smells'][0]['file'] = 'pkg/tax/helpers_test.go'
    run('a Go smell in another package does not link when the flake names its file', go_file_other_pkg, False,
        'but linked to')

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

    def headline_priority(text):
        def mutate(rep, runs):
            rep['summary']['headline'] = text
        return mutate
    run('a headline naming the computed priority is valid',
        headline_priority('1 of 2 tests passed and failed on the same code across 3 runs; the flake ranks P3.'), True)
    run('a headline counting priorities as computed is valid',
        headline_priority('1 of 2 tests flaked across 3 runs: 1 P3 flake, no P0 or P1.'), True)
    run('a headline naming a priority no flake has is an error',
        headline_priority('1 of 2 tests passed and failed on the same code across 3 runs; the flake ranks P2.'),
        False, 'P2')
    run('a headline miscounting a priority is an error',
        headline_priority('1 of 2 tests flaked across 3 runs: 2 P3 flakes.'), False, 'P3')
    run('a headline saying no flake has a priority that one has is an error',
        headline_priority('1 of 2 tests flaked across 3 runs, no P3 flakes.'), False, 'P3')

    # Smells whose test never ran: not_observed would claim runs that never happened.
    def unrun_smell(status):
        def mutate(rep, runs):
            rep['static_smells'].append({'smell_id': 'SM-1', 'smell': 'fixed-sleep-rust', 'file': 'tests/cli.rs',
                                         'line': 12, 'test': 'logs_samples', 'cause': 'async_wait', 'status': status,
                                         'flake_id': None, 'note': 'cargo test was not run in this review.'})
        return mutate
    run('a smell whose test never ran cannot be not_observed', unrun_smell('not_observed'), False, 'never ran')
    run('a smell whose test never ran is not_run', unrun_smell('not_run'), True)

    def ran_smell_not_run(rep, runs):
        go_smell(rep, runs)
        rep['static_smells'][0].update({'test': 'TestTax', 'status': 'not_run', 'flake_id': None})
    run('a smell whose test ran is not not_run', ran_smell_not_run, False, 'ran in')

    # Suites: a language half of the suite with no per-test runs lowers evidence completeness.
    def suites(rep, runs):
        rep['schema_version'] = '1.3.0'
        for r in runs['runs']:
            r['preset'] = 'go-test'
        rep['metadata']['suites'] = [
            {'language': 'go', 'tests': 2, 'presets': ['go-test'], 'status': 'ran', 'reason': 'go test -json ./...'},
            {'language': 'rust', 'tests': 6, 'presets': ['cargo'], 'status': 'not_run',
             'reason': 'cargo test was not run: no toolchain on this host.'}]
    run('suites: a not-run language half is valid when completeness counts it', suites, True)

    def completeness_is(want):
        def after(rep):
            if rep['summary']['evidence_completeness'] != want:
                fails.append(f"suites: evidence completeness {rep['summary']['evidence_completeness']}, want {want}")
        return after
    run('suites: completeness is variations x tests run / tests', suites, True, after=completeness_is(0.25))
    run('suites: a report claiming full completeness with a language not run is an error', suites, False,
        'evidence_completeness', after=lambda rep: rep['summary'].update(evidence_completeness=1.0))

    def suite_without_runs(rep, runs):
        suites(rep, runs)
        rep['metadata']['suites'][1]['status'] = 'ran'
    run('suites: a suite recorded as ran needs runs of its presets', suite_without_runs, False, 'rust')

    def suite_runs_not_recorded(rep, runs):
        suites(rep, runs)
        rep['metadata']['suites'][0]['status'] = 'not_run'
    run('suites: a suite whose presets ran is not not_run', suite_runs_not_recorded, False, 'go')

    inventory = {'smells': [], 'suites': [{'language': 'go', 'tests': 2}, {'language': 'rust', 'tests': 6}]}

    def suites_missing(rep, runs):
        suites(rep, runs)
        del rep['metadata']['suites'][1]
    run('suites: every language the scan found tests in is in metadata.suites', suites_missing, False, 'rust',
        smells=inventory)
    run('suites: a one-language report needs no suites when the scan found one language', lambda r, x: None, True,
        smells={'smells': [], 'suites': [{'language': 'go', 'tests': 2}]})
    run('suites: a two-language scan needs metadata.suites', lambda r, x: None, False, 'metadata.suites',
        smells=inventory)

    # Script self-tests (run_tests.py --preset selftest): ids <script>::<check>, no static count of checks.
    def selftest_runs(rep, runs, checks=('render: joins rows', 'parse: one function', 'build: sorts in under 2 s')):
        for i in (1, 2):
            runs['runs'].append({'id': f'S-{i}', 'label': 'baseline' if i == 1 else 'repeat', 'code_hash': SELF_HASH,
                                 'duration_s': 2.0, 'totals': {'tests': len(checks)}, 'variation': {'env': {}},
                                 'platform': 'linux', 'preset': 'selftest'})
            runs['cases'] += [{'run_id': f'S-{i}', 'id': f'tools/self_test.py::{c}', 'file': 'tools/self_test.py',
                               'status': 'passed'} for c in checks]

    def selftest_suite(rep, runs):
        suites(rep, runs)
        selftest_runs(rep, runs)
        rep['metadata']['suites'].append({'language': 'self-test', 'tests': None, 'presets': ['selftest'],
                                          'status': 'ran', 'reason': 'python tools/self_test.py'})
    # go 2 tests ran, the self-test's 3 checks ran, rust 6 tests not run: (2 + 3) / (2 + 3 + 6)
    run('suites: a self-test suite with no static count counts the checks its runs recorded', selftest_suite, True,
        after=completeness_is(0.45))

    def selftest_smell(status):
        def mutate(rep, runs):
            selftest_runs(rep, runs)
            rep['static_smells'].append({'smell_id': 'SM-1', 'smell': 'wall-time-assertion', 'file': 'tools/self_test.py',
                                         'line': 30, 'test': 'test_render', 'cause': 'resource_contention',
                                         'status': status, 'flake_id': None, 'note': 'Ran in both self-test runs.'})
        return mutate
    run('a smell in a self-test script whose checks ran is not_observed', selftest_smell('not_observed'), True)
    run('a smell in a self-test script whose checks ran is not not_run', selftest_smell('not_run'), False, 'ran in')

    # Single-test runs (--isolate) are counted apart from suite runs in the summary.
    def isolated(rep, runs):
        rep['schema_version'] = '1.3.0'
        for k, tid in enumerate(('TestTotal', 'TestTax'), 1):
            runs['runs'].append({'id': f'I-{k}', 'label': 'isolate', 'code_hash': SELF_HASH, 'duration_s': 1.0,
                                 'totals': {'tests': 1}, 'variation': {'env': {}, 'isolate': True}, 'platform': 'linux'})
            runs['cases'].append({'run_id': f'I-{k}', 'id': f'{SELF_MOD}::{tid}', 'file': None, 'status': 'passed'})
        rep['variations'].append({'kind': 'isolate', 'status': 'ran', 'labels': ['isolate'], 'reason': 'Each alone.'})

    def single_runs_are(want):
        def after(rep):
            if rep['summary'].get('single_test_runs') != want:
                fails.append(f"isolate: single_test_runs {rep['summary'].get('single_test_runs')}, want {want}")
        return after
    run('isolate runs are counted apart in the summary', isolated, True, after=single_runs_are(2))

    # --- schema 1.4.0 ---------------------------------------------------------------------------------------
    def status_is(tid, want):
        def after(rep):
            got = next((t['status'] for t in rep['tests'] if t['id'] == tid), None)
            if got != want:
                fails.append(f'status of {tid}: {got}, want {want}')
        return after

    # A test that errors (cannot be collected) in every run alone and passes in the suite: a status of its own.
    PY_ID = 'tests.test_report.ReportTest::test_rows'

    def alone(version='1.4.0', entry=True, by_file=False, alone_pass=False):
        def mutate(rep, runs):
            rep['schema_version'] = version
            for i in (1, 2, 3):
                runs['cases'].append({'run_id': f'R-{i}', 'id': PY_ID, 'file': 'tests/test_report.py', 'status': 'passed'})
            sel = 'tests/test_report.py' if by_file else 'tests/test_report.py::ReportTest::test_rows'
            for i in (1, 2):
                runs['runs'].append({'id': f'I-{i}', 'label': 'isolate', 'code_hash': SELF_HASH, 'duration_s': 1.0,
                                     'totals': {'tests': 1}, 'variation': {'env': {}, 'isolate': not by_file},
                                     'platform': 'linux', 'tests_selected': [sel], 'errored': not alone_pass,
                                     'error': None if alone_pass else 'pytest exit 4: collection error (a test module '
                                     "could not be imported: ModuleNotFoundError: No module named 'publication')"})
                if alone_pass:
                    runs['cases'].append({'run_id': f'I-{i}', 'id': PY_ID, 'file': 'tests/test_report.py',
                                          'status': 'passed'})
            rep['variations'].append({'kind': 'isolate', 'status': 'not_run' if not alone_pass else 'ran',
                                      'labels': ['isolate'] if alone_pass else [],
                                      'reason': 'Each test alone; the runs of tests/test_report.py errored.'})
            if entry:
                rep['cannot_run_alone'] = [{
                    'test_ids': [PY_ID], 'file': 'tests/test_report.py', 'alone_run_ids': ['I-1', 'I-2'],
                    'error_excerpt': "ModuleNotFoundError: No module named 'publication'", 'cause': 'import_order',
                    'needs': None, 'needs_run_ids': [],
                    'note': 'The file loads a script whose sibling import resolves only after another test file '
                            'has put scripts/ on sys.path.'}]
        return mutate
    run('a test that errors alone and passes in the suite is cannot_run_alone', alone(), True,
        after=status_is(PY_ID, 'cannot_run_alone'))
    run('cannot_run_alone is counted in the summary', alone(), True,
        after=lambda rep: rep['summary'].get('cannot_run_alone') == 1 and rep['summary']['not_observed_flaky'] == 1
        or fails.append(f"cannot_run_alone summary: {rep['summary']}"))
    run('a file that errors when run alone makes its tests cannot_run_alone', alone(by_file=True), True,
        after=status_is(PY_ID, 'cannot_run_alone'))
    run('a cannot_run_alone test needs its entry', alone(entry=False), False, 'cannot_run_alone')
    run('a test that ran alone is not cannot_run_alone', alone(alone_pass=True), False, 'the runs say')
    run('before schema 1.4.0 the same runs only warn', alone(version='1.3.0', entry=False), True,
        after=status_is(PY_ID, 'not_observed_flaky'), want_warn='errored in every run alone')

    def alone_bad_run(rep, runs):
        alone()(rep, runs)
        rep['cannot_run_alone'][0]['alone_run_ids'] = ['R-1']
    run('cannot_run_alone evidence must be errored runs of the test alone', alone_bad_run, False, 'alone_run_ids')

    # A failure only under the clock shift the skill applied: a probe artefact unless a control run confirms it.
    def probe(confirmation=False, confidence='indicated', version='1.4.0', control_fails=True):
        def mutate(rep, runs):
            rep['schema_version'] = version
            rep['metadata']['gate']['level'] = 'required'
            env = {'TDA_CLOCK_SHIFT': '-1d'}
            ids = []
            for i in (1, 2, 3):
                ids.append(f'C-{i}')
                runs['runs'].append({'id': f'C-{i}', 'label': 'clock_minus_1d', 'code_hash': SELF_HASH, 'duration_s': 2.0,
                                     'totals': {'tests': 1}, 'variation': {'env': dict(env)}, 'platform': 'linux'})
                runs['cases'].append({'run_id': f'C-{i}', 'id': f'{SELF_MOD}::TestTax', 'file': None, 'status': 'failed'})
            for i in (1, 2):
                runs['runs'].append({'id': f'CF-{i}', 'label': 'clock_files_real', 'code_hash': SELF_HASH,
                                     'duration_s': 2.0, 'totals': {'tests': 1}, 'platform': 'linux',
                                     'variation': {'env': {**env, 'TDA_CLOCK_FILES': '0'}}})
                runs['cases'].append({'run_id': f'CF-{i}', 'id': f'{SELF_MOD}::TestTax', 'file': None,
                                      'status': 'failed' if control_fails else 'passed'})
            rep['variations'].append({'kind': 'clock', 'status': 'ran', 'labels': ['clock_minus_1d', 'clock_files_real'],
                                      'reason': 'clock_shift.py -1d, and again with file times left real.'})
            f = {**copy.deepcopy(rep['flakes'][0]), 'id': 'FL-2', 'test_id': f'{SELF_MOD}::TestTax',
                 'cause': 'time_and_locale', 'cause_confidence': confidence,
                 'experiment': {'run_ids': ['R-1', 'R-2', 'R-3'] + ids, 'what_changed': 'Moved the clock back a day.',
                                'result': 'Failed 3/3 under the shift, 0/3 without it.'},
                 'evidence_runs': {'passing': ['R-1'], 'failing': ['C-1']}}
            if confirmation:
                f['probe_confirmation'] = {'run_ids': ['CF-1', 'CF-2'],
                                           'note': 'Still fails with file times left real: the date the code reads.'}
            rep['flakes'].append(f)
        return mutate

    def flake_priority(fid, want, note=''):
        def after(rep):
            f = next(f for f in rep['flakes'] if f['id'] == fid)
            if f['priority'] != want or note not in f['priority_note']:
                fails.append(f"priority of {fid}: {f['priority']} ({f['priority_note']}), want {want} naming {note!r}")
        return after
    run('a failure only under the clock shift is ranked as a probe artefact', probe(), True,
        after=flake_priority('FL-2', 'P3', 'probe'))
    run('a probe-only failure is marked on its test row', probe(), True,
        after=lambda rep: next(t for t in rep['tests'] if t['id'].endswith('TestTax')).get('probe_only') == 'clock'
        or fails.append('probe_only missing on the test row'))
    run('a probe-only failure cannot be a confirmed cause without a control', probe(confidence='confirmed'), False,
        'probe')
    run('a probe-only failure confirmed by control runs ranks by the rubric', probe(confirmation=True), True,
        after=flake_priority('FL-2', 'P1'))
    run('a probe confirmation needs failing control runs', probe(confirmation=True, control_fails=False), False,
        'probe_confirmation')
    run('before schema 1.4.0 a clock-only failure ranks as before', probe(version='1.3.0'), True,
        after=flake_priority('FL-2', 'P1'))

    # A cargo run whose test binaries another checkout built must be named in limitations.
    FOREIGN = ('cargo reused a test binary built from another checkout: test target cli of quay reads '
               'CARGO_MANIFEST_DIR at compile time (tests/cli.rs:7) but its binary does not hold this checkout\'s '
               'manifest folder.')

    def foreign(limit):
        def mutate(rep, runs):
            rep['schema_version'] = '1.4.0'
            runs['runs'][0]['notes'] = [FOREIGN]
            if limit:
                rep['limitations'].append('R-1 ran test binaries that another checkout built in a shared target '
                                          'directory; its results describe that copy.')
        return mutate
    run('a run that reused another checkout\'s test binary needs a limitation', foreign(False), False,
        'another checkout')
    run('a limitation naming the shared target directory satisfies it', foreign(True), True)

    # Rust tests a plain cargo test leaves out are not_run rows and lower evidence completeness.
    gated_inv = {'smells': [], 'suites': [{'language': 'rust', 'tests': 4, 'not_run_by_default': [
        {'id': 'quay::tests::slow', 'file': 'src/lib.rs', 'line': 16, 'gate': 'ignored', 'detail': '#[ignore]'},
        {'id': 'quay::tests::needs_desktop', 'file': 'src/lib.rs', 'line': 21, 'gate': 'feature',
         'detail': 'needs --features desktop'}]}]}

    def gated(version='1.4.0'):
        def mutate(rep, runs):
            rep['schema_version'] = version
            for r in runs['runs']:
                r['preset'] = 'cargo'
            rep['metadata']['suites'] = [{'language': 'rust', 'tests': 4, 'presets': ['cargo'], 'status': 'ran',
                                          'reason': 'cargo test --workspace'}]
        return mutate
    run('tests cargo leaves out by default are not_run rows', gated(), True, smells=gated_inv,
        after=lambda rep: (sorted(t['id'] for t in rep['tests'] if t['status'] == 'not_run')
                           == ['quay::tests::needs_desktop', 'quay::tests::slow'] and rep['summary']['not_run'] == 2)
        or fails.append(f"not_run rows: {[t['id'] for t in rep['tests'] if t['status'] == 'not_run']}"))
    run('completeness counts the tests that ran, not the suite\'s static count', gated(), True, smells=gated_inv,
        after=completeness_is(0.5))

    def drop_not_run(rep):
        rep['tests'] = [t for t in rep['tests'] if t['status'] != 'not_run']
        rep['summary']['not_run'] = 0
    run('a report without rows for the tests that never ran is an error', gated(), False, 'never ran',
        smells=gated_inv, after=drop_not_run)
    run('before schema 1.4.0 a ran suite counts whole', gated('1.3.0'), True, smells=gated_inv,
        after=completeness_is(1.0))

    # Suite load: a variation of its own, whose runs record the load or run the test beside others.
    def suite_load(recorded):
        def mutate(rep, runs):
            rep['schema_version'] = '1.4.0'
            for i in (1, 2, 3):
                env = {'QA_SPAWN_LOAD': '4'} if recorded else {}
                runs['runs'].append({'id': f'L-{i}', 'label': 'suite_load', 'code_hash': SELF_HASH, 'duration_s': 2.0,
                                     'totals': {'tests': 1}, 'variation': {'env': env}, 'platform': 'linux'})
                runs['cases'].append({'run_id': f'L-{i}', 'id': f'{SELF_MOD}::TestTax', 'file': None, 'status': 'passed'})
            rep['variations'].append({'kind': 'suite_load', 'status': 'ran', 'labels': ['suite_load'],
                                      'reason': 'spawn_load.py with 4 workers.'})
        return mutate
    run('a suite_load variation with recorded process-creation load is valid', suite_load(True), True)
    run('a suite_load variation needs runs that record the load or run more than one test', suite_load(False), False,
        'suite_load')

    for a, b, want in (('tests.test_x.Scale::test_y', 'tests/test_x.py::Scale::test_y', True),
                       ('tests.test_x::test_y', 'tests/test_x.py::test_y', True),
                       ('src.pkg.tests.test_x.Scale::test_y', 'pkg_dir/src/pkg/tests/test_x.py::Scale::test_y', True),
                       ('tests.test_x.Scale::test_y', 'tests/test_x.py::Other::test_y', False),
                       ('tests.test_x.Scale::test_y', 'tests/test_x.py::test_y', False)):
        if same_test(a, b) != want:
            fails.append(f'same_test({a!r}, {b!r}) is {not want}, want {want}')

    # SKILL.md spells out the schema values that otherwise cost validation rounds: the newest schema_version, every
    # tools[].status and variations[].status value, that `skill` is an object and that `quarantine` is required.
    skill_md = (Path(__file__).resolve().parent.parent / 'SKILL.md').read_text(encoding='utf-8')
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    props = schema['properties']
    want = [f"`\"schema_version\": \"{props['schema_version']['enum'][-1]}\"`",
            *[f'`{v}`' for v in props['tools']['items']['properties']['status']['enum']],
            *[f'`{v}`' for v in props['variations']['items']['properties']['status']['enum']],
            '`"skill": {"name": "flaky-test-hunter", "version":', '`quarantine`']
    section = skill_md.split('## Report fields', 1)[-1]
    missing = [w for w in want if w not in section]
    if missing:
        fails.append('SKILL.md Report fields does not spell out ' + ', '.join(missing))

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
    ap.add_argument('--smells', type=Path, help='scan_pitfalls.py output: every smell accounted for, every suite '
                    'listed, and (schema 1.4.0) a not_run row for each test a plain run leaves out')
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
    if args.write_summary and runs_data is None:
        print('ERROR: --write-summary needs --runs')
        return 1
    smells = json.loads(args.smells.read_text(encoding='utf-8')) if args.smells else None
    if args.write_summary:
        write_summary(d, runs_data, smells)
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
