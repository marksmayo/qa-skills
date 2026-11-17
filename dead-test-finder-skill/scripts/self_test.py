#!/usr/bin/env python3
"""Self-test for the scanner and the collect step on a synthetic repository (stdlib only).

    python self_test.py

Builds a small repository in a temporary directory: a testing library ("quayside") whose own
source exports test() and describe(), Vitest tests of that library that call its test() to
build fixtures, a dogfood suite run by the library itself (*.e2e.ts), node:test scripts, and a
Python product package in a directory named e2e. Checks that:

  - the library's own test.only(...) inside a Vitest test is no .only candidate and no test;
  - a local helper named test hides the head only in its scope;
  - assigned and Promise.all expectations count as awaited, a bare one does not;
  - expect aliases and assertion helpers (same file and imported) count as assertions;
  - a typed array literal with arrow types is a literal, a guarded return is no early return;
  - object keys keep two tests apart, cleanup catches are not swallowed failures;
  - the dogfood suite is `custom`, node:test is `node-test`, describe.each<T> is parsed;
  - a directory named e2e holding a product manifest is not a test directory;
  - collect_check gives Vitest its JSON path with '=', and stops (exit 3) and restores the files
    when a list command overwrites a test file.
"""
from __future__ import annotations
import contextlib, io, json, os, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import collect_check, scan_tests  # noqa: E402

FILES = {
    'package.json': '{"name": "quayside-repo", "private": true, "devDependencies": {"vitest": "3.2.4"}}\n',
    'packages/quayside/package.json': '{"name": "quayside", "main": "dist/index.js", "exports": {".": "./dist/index.js"}}\n',
    'packages/quayside/src/registry.ts': (
        "export function test(title: string, fn: () => void) { return { title, fn }; }\n"
        "test.only = (title: string, fn: () => void) => ({ title, fn, only: true });\n"
        "export function describe(title: string, fn: () => void) { fn(); }\n"),
    'packages/quayside/src/queue.ts': (
        "export async function drain(n: number): Promise<number> { return n; }\n"
        "export function lookup(o: { has?: string; hasNot?: string }) { return o; }\n"),
    'packages/quayside/tests/helpers/checks.ts': (
        "import { expect } from 'vitest';\n"
        "export function expectRow(row: unknown) {\n  expect(row).toBeDefined();\n}\n"),
    'packages/quayside/tests/registry.test.ts': """import { describe, expect, it, vi } from 'vitest';
import { expect as vexpect } from 'vitest';
import { test } from '../src/registry';
import { drain, lookup } from '../src/queue';
import { expectRow } from './helpers/checks';

const noop = () => {};

function failsWith(p: Promise<unknown>, code: string) {
  return expect(p).rejects.toThrow(code);
}

describe('registration', () => {
  it('records a focused test', () => {
    const t = test.only('focused', noop);
    expect(t.title).toBe('focused');
  });

  it('settles a held expectation', async () => {
    const assertion = expect(drain(1)).rejects.toThrow('x');
    await vi.advanceTimersByTimeAsync(10);
    await assertion;
  });

  it('settles a batch', async () => {
    await Promise.all([
      expect(drain(1)).rejects.toThrow('a'),
      expect(drain(2)).rejects.toThrow('b'),
    ]);
  });

  it('forgets to await', async () => {
    expect(drain(3)).resolves.toBe(3);
  });

  it('checks with an alias', () => {
    vexpect(lookup({ has: 'a' })).toEqual({ has: 'a' });
  });

  it('checks with a local helper', async () => {
    await failsWith(drain(4), 'E_DRAIN');
  });

  it('checks with an imported helper', () => {
    expectRow(lookup({}));
  });

  it('runs every case', () => {
    const cases: [() => void, () => void][] = [[noop, noop]];
    for (const [a, b] of cases) {
      expect(a).not.toBe(b);
    }
  });

  it('stops early on a guard', () => {
    const row = lookup({ has: 'x' });
    expect(row).toBeDefined();
    if (row.has !== 'x') return;
    expect(row.has).toBe('x');
  });

  it('cleans up quietly', async () => {
    try {
      expect(await drain(5)).toBe(5);
    } finally {
      try {
        await drain(0);
      } catch {}
    }
  });

  it('filters by has', () => {
    const row = lookup({ has: 'needle-in-the-first-stack-of-hay' });
    expect(row).toEqual({ has: 'needle-in-the-first-stack-of-hay' });
    expect(Object.keys(row)).toHaveLength(1);
    expect(row.has).toBeTruthy();
  });

  it('filters by has not', () => {
    const row = lookup({ hasNot: 'needle-in-the-first-stack-of-hay' });
    expect(row).toEqual({ hasNot: 'needle-in-the-first-stack-of-hay' });
    expect(Object.keys(row)).toHaveLength(1);
    expect(row.has).toBeTruthy();
  });

  it.only('is really focused', () => {
    expect(1).toBe(1);
  });
});

describe('tiers', () => {
  const test = (id: string) => ({ id });
  it('builds rows with a local helper named test', () => {
    expect([test('a'), test('b')]).toHaveLength(2);
  });
});

describe.each<string>(['fast', 'slow'])('mode %s', (mode) => {
  it('drains', async () => {
    expect(await drain(mode.length)).toBeGreaterThan(0);
  });
});
""",
    'apps/dogfood/package.json': '{"name": "dogfood", "private": true, "dependencies": {"quayside": "workspace:*"}}\n',
    'apps/dogfood/suites/fixtures.ts': (
        "import { test as base } from 'quayside';\n"
        "export const test = base.extend<{ shop: string }>({ shop: 'demo' });\n"
        "export { expect } from 'quayside';\n"),
    'apps/dogfood/suites/checkout.e2e.ts': (
        "import { expect, test } from './fixtures';\n"
        "test.describe('checkout', () => {\n"
        "  test('pays for a basket', async ({ app }) => {\n"
        "    await expect(app.heading()).toHaveText('Paid');\n"
        "  });\n"
        "});\n"),
    'scripts/tools.test.ts': (
        "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "test('splits a line', () => {\n  assert.equal('a b'.split(' ').length, 2);\n});\n"),
    'python/e2e/pyproject.toml': '[project]\nname = "harbour-e2e"\n',
    'python/e2e/harbour_e2e/__init__.py': '',
    'python/e2e/harbour_e2e/runner.py': 'def test_mode(flag):\n    return flag\n',
    'python/tests/e2e/test_flow.py': 'def test_flow():\n    assert 1 + 1 == 2\n',
}


GO_FILES = {
    'go.mod': 'module example.test/tidegauge\n\ngo 1.26\n',
    'gauge/gauge.go': ('package gauge\n\nimport (\n\t"errors"\n\t"strings"\n)\n\n// Level reads a level.\n'
                       'func Level(raw string) (float64, error) {\n\tif strings.TrimSpace(raw) == "" {\n'
                       '\t\treturn 0, errors.New("empty")\n\t}\n\treturn 1.5, nil\n}\n\n'
                       'type Station struct{ Name string }\n\n'
                       'func (s *Station) Label() string { return strings.ToUpper(s.Name) }\n'),
    'gauge/gauge_test.go': """package gauge

import "testing"

func TestLevelParses(t *testing.T) {
\tgot, err := Level("1.5")
\tif err != nil || got != 1.5 {
\t\tt.Fatalf("got %v, %v", got, err)
\t}
}

func TestLevelRuns(t *testing.T) {
\t_, _ = Level("2.0") // t.Fatal("in a comment is no failure")
\t_ = fmt.Errorf("a product-style error is no failure either")
}

func TestLevelLogs(t *testing.T) {
\tv, _ := Level("3")
\tt.Logf("level %v", v)
}

func TestLevelSkipped(t *testing.T) {
\tt.Skip("unreliable on the old gauge, see GAUGE-12")
\t_, _ = Level("x")
}

func TestLevelTable(t *testing.T) {
\tcases := []struct{ in string }{}
\tfor _, c := range cases {
\t\tif _, err := Level(c.in); err != nil {
\t\t\tt.Errorf("%v", err)
\t\t}
\t}
}

func TestLevelTautology(t *testing.T) {
\tv, _ := Level("1")
\tif v != v {
\t\tt.Fatal("never")
\t}
}

func TestLevelHelper(t *testing.T) {
\tcheckLevel(t, "1.5")
}

func TestLevelQuietHelper(t *testing.T) {
\tlogLevel(t, "1.5")
}

func checkLevel(t *testing.T, raw string) {
\tt.Helper()
\tif _, err := Level(raw); err != nil {
\t\tt.Fatal(err)
\t}
}

func logLevel(tb testing.TB, raw string) {
\tv, _ := Level(raw)
\ttb.Log(v)
}

func TestLevelSubtests(t *testing.T) {
\tt.Run("empty", func(st *testing.T) {
\t\tif _, err := Level(""); err == nil {
\t\t\tst.Error("want an error")
\t\t}
\t})
}

func TestLabel(t *testing.T) {
\ts := &Station{Name: "wharf"}
\tif s.Label() != "WHARF" {
\t\tt.Error("label")
\t}
}

func Testlowercase(t *testing.T) {
\tt.Fatal("never runs")
}
""",
    'vendor/example.test/rope/rope_test.go': 'package rope\n\nimport "testing"\n\nfunc TestRope(t *testing.T) {}\n',
}


def go_checks(tmp: Path, failures: list[str]) -> None:
    """Go: the scanner's definitions and rules, the language guard's exit, neutralising a Go function (zero values,
    unused imports blanked) and recording go test -json runs."""
    import go_tests, neutralise  # noqa: E402
    root = tmp / 'go'
    for rel, text in GO_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    res = scan_tests.scan(root)
    defs = {d['id']: d for d in res['definitions']}
    f = 'gauge/gauge_test.go'
    names = {i.split('::')[1] for i in defs if i.startswith(f + '::')}
    want = {'TestLevelParses', 'TestLevelRuns', 'TestLevelLogs', 'TestLevelSkipped', 'TestLevelTable', 'TestLevelTautology',
            'TestLevelHelper', 'TestLevelQuietHelper', 'TestLevelSubtests', 'TestLabel', 'Testlowercase'}
    check(names == want, f'every Go test in the file is a definition (got {sorted(names)})', failures)
    check(not any(i.startswith('vendor/') for i in defs), 'vendored Go tests are not definitions', failures)
    got = {(c['category'], c['rule'], c['test_ids'][0].split('::')[1]) for c in res['candidates']}
    want_c = {('no_assertion', 'no_assertion', 'TestLevelRuns'), ('no_assertion', 'log_only', 'TestLevelLogs'),
              ('no_assertion', 'no_assertion', 'TestLevelQuietHelper'), ('skipped', 'skip', 'TestLevelSkipped'),
              ('unreached_assertion', 'empty_table', 'TestLevelTable'), ('tautology', 'compare_to_self', 'TestLevelTautology'),
              ('never_runs', 'name_pattern', 'Testlowercase')}
    check(got == want_c, f'the Go rules find exactly the planted problems (missing {sorted(want_c - got)}, '
          f'extra {sorted(got - want_c)})', failures)
    skip = next((c for c in res['candidates'] if c['rule'] == 'skip' and c['file'] == f), None)
    check(bool(skip) and skip['details'].get('ticket') == 'GAUGE-12' and skip['details'].get('conditional') is False,
          'a whole-body t.Skip carries its ticket for check_skips.py', failures)
    runs_def = defs.get(f'{f}::TestLevelRuns') or {}
    check(runs_def.get('framework') == 'go-test' and 'gauge:Level' in runs_def.get('calls', [])
          and 'gauge:Station.Label' in (defs.get(f'{f}::TestLabel') or {}).get('calls', []),
          f'Go definitions name the product functions they call (got {runs_def.get("calls")})', failures)
    check('language_coverage' not in res, 'a Go repository raises no language warning', failures)

    # the language guard: a repository dominated by a language nobody reads exits 3 unless --allow-minority
    rb = tmp / 'rb'
    (rb / 'lib').mkdir(parents=True)
    for i in range(4):
        (rb / 'lib' / f'gauge{i}.rb').write_text('class Gauge; end\n', encoding='utf-8')
    (rb / 'tests').mkdir()
    (rb / 'tests' / 'test_x.py').write_text('def test_x():\n    pass\n', encoding='utf-8')
    code = quiet(scan_tests.main, [str(rb), '--out', str(tmp / 'rb.json')])
    out = json.loads((tmp / 'rb.json').read_text(encoding='utf-8'))
    check(code == 3 and out['summary'].get('language_warning'), f'an unsupported dominant language exits 3 with the '
          f'warning in the summary (exit {code})', failures)
    check(quiet(scan_tests.main, [str(rb), '--out', str(tmp / 'rb.json'), '--allow-minority']) == 0,
          '--allow-minority accepts a minority scan (exit 0)', failures)

    # neutralise a Go function: the file the run sees has zero values and blank imports for what it no longer uses
    cand = tmp / 'go-candidates.json'
    cand.write_text(json.dumps(res), encoding='utf-8')
    seen: list[tuple[str, str]] = []
    real = go_tests.run_go

    def fake_run_go(cwd, package, pattern, runs_file, label, prefix, env=None, timeout=600):
        text = (Path(cwd) / 'gauge' / 'gauge.go').read_text(encoding='utf-8')
        seen.append((label, text))
        events = json.dumps({'Action': 'pass', 'Package': 'example.test/tidegauge/gauge', 'Test': 'TestLevelRuns'})
        return go_tests.record(runs_file, events, label, prefix, f'go test -run {pattern} {package}', 0)
    go_tests.run_go = fake_run_go
    try:
        code = quiet(neutralise.main, ['--repo', str(root), '--candidates', str(cand), '--test', f'{f}::TestLevelRuns',
                                       '--run-dir', str(tmp / 'go-run')])
    finally:
        go_tests.run_go = real
    ret = next((t for lab, t in seen if 'return' in lab), '')
    check(code == 0 and 'return *new(float64), *new(error)' in ret and '_ "errors"' in ret and '\t"strings"' in ret,
          f'a Go target is neutralised with zero values and its unused import blanked (exit {code})', failures)
    if code == 0:
        nres = json.loads((tmp / 'go-run' / 'neutralisations.json').read_text(encoding='utf-8'))['results'][0]
        check(nres['outcome'] == 'passed_neutralised', f'the Go neutralisation outcome comes from its runs '
              f'(got {nres["outcome"]})', failures)
    check(go_tests.patch(GO_FILES['gauge/gauge.go'], 'Station.Label', 'return') is not None and
          'return *new(string)' in go_tests.patch(GO_FILES['gauge/gauge.go'], 'Station.Label', 'return'),
          'a method is neutralised by Type.Method', failures)
    # collect_check lists Go tests with go test -list; a package that does not build leaves its tests not assessed
    (root / 'tide').mkdir(exist_ok=True)
    (root / 'tide' / 'tide_test.go').write_text('package tide\n\nimport "testing"\n\nfunc TestTide(t *testing.T) {\n'
                                                '\tt.Fatal("x")\n}\n', encoding='utf-8')
    res2 = scan_tests.scan(root)
    cand.write_text(json.dumps(res2), encoding='utf-8')
    real_run = collect_check.run

    def fake_list(cmd, cwd, env):
        if cmd[:3] != ['go', 'test', '-list']:
            return 1, ''
        names = [d['name'] for d in res2['definitions'] if d['file'].startswith('gauge/') and d['name'] != 'Testlowercase']
        return 1, '\n'.join(names) + '\nok  \texample.test/tidegauge/gauge\t0.01s\n' \
            'FAIL\texample.test/tidegauge/tide [build failed]\n'
    collect_check.run = fake_list
    try:
        code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'go-col')])
    finally:
        collect_check.run = real_run
    col = json.loads((tmp / 'go-col' / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
    check(code == 0 and col.get('collected') == 10 and col.get('not_collected') == [f'{f}::Testlowercase']
          and col.get('not_assessed') == ['tide/tide_test.go::TestTide'],
          f'go test -list collects Go tests, names the malformed one and leaves an unbuilt package not assessed '
          f'(exit {code}, collected {col.get("collected")}, not collected {col.get("not_collected")}, '
          f'not assessed {col.get("not_assessed")})', failures)
    built = '\n'.join(json.dumps(e) for e in (
        {'Action': 'start', 'Package': 'example.test/tidegauge/gauge'},
        {'Action': 'output', 'Package': 'example.test/tidegauge/gauge', 'Output': 'FAIL\texample.test/tidegauge/gauge [build failed]\n'},
        {'Action': 'fail', 'Package': 'example.test/tidegauge/gauge'}))
    run = go_tests.record(tmp / 'go-run' / 'b.json', built, 'neutralise', 'B', 'go test', 1)
    check(run['errored'] and run['totals']['tests'] == 0, 'a package that does not build is an errored run, never a failure',
          failures)


# A small tool repository: scripts with their own self-tests (a self_test.py whose check() records a failure instead of
# raising, and a product script with a --self-test flag), a benchmark suite that pytest finds only through its own
# pytest.ini (python_files = bench_*.py), product code named like a test module, a test file nothing runs, and a
# sample project under examples/ with a planted dead test.
TOOL_FILES = {
    'README.md': '# fare tool\n',
    'pyproject.toml': '[project]\nname = "fare-tool"\nversion = "0.1.0"\n',
    'scripts/pricing.py': ('"""Fare arithmetic."""\n\n\ndef fare(stops):\n    return stops * 2\n\n\n'
                           'def surcharge(fare_value,\n              rate):\n'
                           '    """A surcharge (its signature spans two lines)."""\n    return fare_value * rate\n\n\n'
                           'def zones():\n    return ["inner", "outer"]\n'),
    'scripts/test_paths.py': ('"""Decides which paths hold tests (product code named like a test module)."""\n\n\n'
                              'class Classifier:\n    def test_dir(self, rel):\n'
                              '        return rel.split("/")[0] in ("tests", "test")\n'),
    'scripts/tidy.py': ('import test_paths\n\n\ndef tidy(paths):\n    c = test_paths.Classifier()\n'
                        '    return [p for p in paths if not c.test_dir(p)]\n'),
    'scripts/lint.py': ('import argparse, sys\n\n\ndef trailing(line):\n    return line.rstrip() != line\n\n\n'
                        'def self_test():\n    cases = [("x ", True), ("x", False)]\n'
                        '    bad = [c for c, want in cases if trailing(c) != want]\n    for c in bad:\n'
                        '        print(f"FAIL {c!r}")\n    return 1 if bad else 0\n\n\n'
                        'def main(argv=None):\n    ap = argparse.ArgumentParser()\n'
                        '    ap.add_argument("--self-test", action="store_true")\n    args = ap.parse_args(argv)\n'
                        '    if args.self_test:\n        return self_test()\n    return 0\n\n\n'
                        'if __name__ == "__main__":\n    sys.exit(main())\n'),
    'scripts/report.py': ('import sys\n\n\ndef test_ids(rows):\n    return [r["id"] for r in rows]\n\n\n'
                          'def format_checks():\n    fails = []\n    if test_ids([{"id": 1}]) != [1]:\n'
                          '        fails.append("ids are read")\n    return fails\n\n\n'
                          'def self_test():\n    fails = format_checks()\n    for f in fails:\n        print("FAIL", f)\n'
                          '    return 1 if fails else 0\n\n\n'
                          'if __name__ == "__main__":\n    sys.exit(self_test() if "--self-test" in sys.argv else 0)\n'),
    'scripts/self_test.py': ('"""Self-test for the scripts: python scripts/self_test.py"""\nimport sys\n'
                             'from pathlib import Path\n\nHERE = Path(__file__).resolve().parent\n'
                             'sys.path.insert(0, str(HERE))\nimport pricing  # noqa: E402\n\nFAILS = []\n\n\n'
                             'def check(cond, what):\n    print(("ok    " if cond else "FAIL  ") + what)\n'
                             '    if not cond:\n        FAILS.append(what)\n\n\n'
                             'def test_fares():\n    check(pricing.fare(3) == 6, "a fare is two per stop")\n\n\n'
                             'def surcharges():\n    check(pricing.surcharge(10, 2) == 20, "a surcharge multiplies the fare")\n\n\n'
                             'def quiet_case():\n    pricing.fare(1)\n\n\n'
                             'def zone_count():\n    check(len(pricing.zones()) == 2, "two zones")\n\n\n'
                             'def write(path, text):\n    Path(path).write_text(text)\n\n\n'
                             'def main():\n    for t in (test_fares, surcharges, quiet_case, zone_count):\n        t()\n'
                             '    print(f"{len(FAILS)} failure(s)" if FAILS else "all checks passed")\n'
                             '    return 1 if FAILS else 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'),
    'benchmarks/pytest.ini': '[pytest]\npython_files = bench_*.py\n',
    'benchmarks/bench_pricing.py': ('import sys\nfrom pathlib import Path\n\n'
                                    'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))\n'
                                    'import pricing  # noqa: E402\n\n\n'
                                    'def test_fare_speed():\n    assert pricing.fare(4) == 8\n\n\n'
                                    'def test_surcharge_speed():\n    pricing.surcharge(1, 2)\n'),
    'tests/cases_extra.py': 'def test_extra():\n    assert 1 + 1 == 2\n',
    'examples/shop/requirements.txt': 'flask\n',
    'examples/shop/shop.py': 'def cart():\n    return []\n',
    'examples/shop/tests/test_cart.py': 'from shop import cart\n\n\ndef test_cart():\n    cart()\n',
}


def tool_repo_checks(tmp: Path, failures: list[str]) -> None:
    """A tool repository: configured pytest suites, script self-tests and their runner, product code named test_*.py,
    fixture projects, copies kept identical in two folders, and neutralisation through the suite's own runner."""
    import neutralise  # noqa: E402
    root = tmp / 'tool'
    for rel, text in {**TOOL_FILES, 'tool_copies/lint.py': TOOL_FILES['scripts/lint.py']}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    res = scan_tests.scan(root)
    defs = {d['id']: d for d in res['definitions']}
    cands = res['candidates']
    bench = 'benchmarks/bench_pricing.py'
    # pytest.ini in benchmarks/ sets python_files = bench_*.py: its tests are defined and run with pytest
    check(f'{bench}::test_fare_speed' in defs and defs[f'{bench}::test_fare_speed'].get('runner') == 'pytest'
          and not [c for c in cands if c['file'] == bench and c['category'] == 'never_runs'],
          f'a pytest.ini python_files suite is scanned and collectable (got {sorted(i for i in defs if "bench" in i)})',
          failures)
    check(any(c['rule'] == 'no_assertion' and c['test_ids'] == [f'{bench}::test_surcharge_speed'] for c in cands),
          'a benchmark with no assertion is a no_assertion candidate', failures)
    # product code named like a test module, imported by product scripts, is not a test file
    check(not any(d['file'] == 'scripts/test_paths.py' for d in defs.values())
          and any(p['file'] == 'scripts/test_paths.py' for p in res.get('product_files', [])),
          f'a test_*.py imported by product code is product code (got {res.get("product_files")})', failures)
    # script self-tests: the functions the runner calls are tests of the selftest runner, with its command
    st = 'scripts/self_test.py'
    units = {i.split('::')[1]: d for i, d in defs.items() if d['file'] == st}
    check(set(units) == {'test_fares', 'surcharges', 'quiet_case', 'zone_count'} and all(
        d.get('framework') == 'selftest' and d.get('runner') == 'selftest' and d.get('selftest_command') == f'python {st}'
        for d in units.values()), f'self-test units are the functions its runner calls (got {sorted(units)})', failures)
    lint = defs.get('scripts/lint.py::self_test') or {}
    check(lint.get('selftest_command') == 'python scripts/lint.py --self-test' and 'lint.trailing' in lint.get('calls', []),
          f'a --self-test flag makes self_test() a test that targets its own module (got {lint})', failures)
    # a script kept identical in another folder runs the same tests: scanned once, on the canonical copy
    check(not any(d['file'].startswith('tool_copies/') for d in defs.values()) and
          {'file': 'tool_copies/lint.py', 'canonical': 'scripts/lint.py'} in res.get('copies_skipped', []),
          f'an identical copy of a test script is scanned once (got {res.get("copies_skipped")})', failures)
    rep_units = sorted(i for i in defs if i.startswith('scripts/report.py::'))
    check(rep_units == ['scripts/report.py::format_checks'] and 'report.test_ids' in defs[rep_units[0]]['calls'],
          f'a product function named test_ids is not a self-test; the check group returning its failures is '
          f'(got {rep_units})', failures)
    dead = {c['test_ids'][0].split('::')[1] for c in cands if c['category'] in ('no_assertion', 'unreached_assertion')
            and c['file'] in (st, 'scripts/lint.py', 'scripts/report.py')}
    check(dead == {'quiet_case'}, f'a check() that records a failure is an assertion; a unit with none is not '
          f'(no_assertion: {sorted(dead)})', failures)
    # fixture projects under examples/ are left out by default and listed; --include-fixtures scans them
    check(not any(i.startswith('examples/') for i in defs) and
          [f['dir'] for f in res.get('fixtures_excluded', [])] == ['examples/shop'],
          f'a sample project under examples/ is excluded and listed (got {res.get("fixtures_excluded")})', failures)
    check(any(i.startswith('examples/shop/') for i in (d['id'] for d in scan_tests.scan(root, include_fixtures=True)['definitions'])),
          'include_fixtures scans the sample project', failures)
    check((defs.get('tests/cases_extra.py::test_extra') or {}).get('runner') == 'unknown',
          'a test file no configured runner collects has an unknown runner', failures)

    # collect_check: self-tests have no list command (not_assessed, never not_collected); the benchmarks are listed by
    # pytest from their own config directory
    cand = tmp / 'tool-candidates.json'
    cand.write_text(json.dumps(res), encoding='utf-8')
    real_run, seen = collect_check.run, []

    def fake_list(cmd, cwd, env):
        seen.append((cmd, Path(cwd)))
        if 'pytest' in cmd and Path(cwd).name == 'benchmarks':
            return 0, 'bench_pricing.py::test_fare_speed\nbench_pricing.py::test_surcharge_speed\n'
        return 0, ''
    collect_check.run = fake_list
    try:
        code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'tool-col')])
    finally:
        collect_check.run = real_run
    col = json.loads((tmp / 'tool-col' / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
    check(code == 0 and f'{bench}::test_fare_speed' not in col.get('not_collected', [])
          and f'{st}::test_fares' in col.get('not_assessed', []) and f'{st}::test_fares' not in col.get('not_collected', []),
          f'benchmarks are listed from their config directory and self-tests are not assessed (exit {code}, '
          f'not collected {col.get("not_collected")})', failures)

    # neutralise through the suite's own runner: under pytest test_fares passes whatever pricing.fare returns (check()
    # never raises); the script's runner prints FAIL and exits 1
    run_dir = tmp / 'tool-run'
    code = quiet(neutralise.main, ['--repo', str(root), '--candidates', str(cand), '--run-dir', str(run_dir),
                                   '--test', f'{st}::test_fares', '--test', f'{st}::surcharges', '--test', f'{st}::zone_count',
                                   '--test', 'tests/cases_extra.py::test_extra', '--copy-ignore', '*.png', '*.apk'])
    got = {r['test_id']: r for r in json.loads((run_dir / 'neutralisations.json').read_text(encoding='utf-8'))['results']} \
        if (run_dir / 'neutralisations.json').is_file() else {}
    fares = got.get(f'{st}::test_fares') or {}
    check(code == 0 and fares.get('runner') == 'selftest' and fares.get('outcome') == 'failed_neutralised'
          and [t['target'] for t in fares.get('targets', [])] == ['pricing.fare'],
          f'a self-test is neutralised with its own runner and its sys.path import resolved (exit {code}, '
          f'{fares.get("runner")}, {fares.get("outcome")}, {[t.get("note") or t["target"] for t in fares.get("targets", [])]})',
          failures)
    sur = got.get(f'{st}::surcharges') or {}
    check(sur.get('outcome') == 'failed_neutralised', f'a function whose signature spans two lines is neutralised '
          f'(got {sur.get("outcome")}, {[t.get("note") for t in sur.get("targets", [])]})', failures)
    zc = got.get(f'{st}::zone_count') or {}
    check(zc.get('outcome') == 'failed_neutralised' and zc.get('checks_no_exception') is True,
          f'a self-test that crashes on the neutral body (exit 1 before any check line) failed, not errored '
          f'(got {zc.get("outcome")}, {[v for t in zc.get("targets", []) for v in t["variants"]]})', failures)
    import validate_dead_test_report as v  # noqa: E402
    runs = json.loads((run_dir / 'runs.json').read_text(encoding='utf-8')) if (run_dir / 'runs.json').is_file() else \
        {'runs': [], 'cases': []}
    verr = [e for r in got.values() for e in v.neutralisation_errors(r, runs, {x['id'] for x in runs['runs']}, None)]
    check(got and not verr, f'the validator reads a self-test neutralisation by its script\'s checks (got {verr[:2]})',
          failures)
    extra = got.get('tests/cases_extra.py::test_extra') or {}
    check(extra.get('outcome') == 'not_run' and 'runner' in (extra.get('not_run_reason') or ''),
          f'a test no known runner runs is not neutralised (got {extra.get("outcome")}, {extra.get("not_run_reason")})',
          failures)
    # the other units are silenced while one runs, so quiet_case (no check) passes with fare neutralised
    code = quiet(neutralise.main, ['--repo', str(root), '--candidates', str(cand), '--run-dir', str(run_dir),
                                   '--test', f'{st}::quiet_case', '--target', f'{st}::quiet_case=pricing.fare'])
    got = {r['test_id']: r for r in json.loads((run_dir / 'neutralisations.json').read_text(encoding='utf-8'))['results']}
    quiet_r = got.get(f'{st}::quiet_case') or {}
    check(code == 0 and quiet_r.get('outcome') == 'passed_neutralised' and 'test_fares' in quiet_r.get('silenced', []),
          f'one self-test unit runs at a time (got {quiet_r.get("outcome")}, silenced {quiet_r.get("silenced")})', failures)


def report_checks(tmp: Path, failures: list[str]) -> None:
    """The validator: a stale skip found by review (no skips.json verdict) validates with its git age, and is refused
    under the age threshold or without the age's source."""
    import copy
    import validate_dead_test_report as v  # noqa: E402
    ex = HERE.parent / 'examples'
    rep = json.loads((ex / 'dead_test_report.example.json').read_text(encoding='utf-8'))
    ev = v.load_evidence(rep, ex)

    def with_skip(skip: dict) -> list[str]:
        r = copy.deepcopy(rep)
        r['findings'].append({
            'id': 'F99', 'category': 'stale_skip', 'source': 'agent', 'candidates': [],
            'basis': [{'file': 'core/tests/test_x.go', 'line': 4, 'excerpt': 't.Skip("waiting on the fare service")'}],
            'test_ids': ['core/tests/test_x.go::TestFare'], 'location': {'file': 'core/tests/test_x.go', 'line': 4},
            'confidence': 'static', 'why': 'The whole body is a t.Skip added long ago and never revisited.',
            'criticality': 1, 'criticality_reason': 'Covers a minor reporting path.', 'evidence': {'skip': skip},
            'proposal': {'action': 'investigate', 'patch_path': None, 'rationale': 'Decide whether to unskip or delete it.'},
            'applied': False, 'weight': 0, 'score': 0, 'priority': 'P3'})
        v.fill(r)
        r['summary'] = {'headline': r['summary']['headline'], **v.summary(r, ev)}
        return v.check(r, ev, None, ex)
    ok = with_skip({'verdict': 'stale', 'age_days': 200, 'age_source': 'git blame (2026-04-28)', 'ticket': None})
    check(not ok, f'an agent stale skip with its git age validates (got {ok[:2]})', failures)
    young = with_skip({'verdict': 'stale', 'age_days': 90, 'age_source': 'git blame (2026-08-16)', 'ticket': None})
    check(any('F99' in e and 'stale' in e for e in young), 'an agent stale skip under the age threshold is refused', failures)
    nosrc = with_skip({'verdict': 'stale', 'age_days': 200, 'ticket': None})
    check(any('F99' in e for e in nosrc), 'an agent stale skip needs the source of its age', failures)

    # a candidate whose test passed with its code neutralised is dismissed as a scanner error only with the
    # counter-evidence (a run of the suite's own runner) as an evidence file, never on a source line alone
    def dismiss_f2(basis: list[dict]) -> list[str]:
        r = copy.deepcopy(rep)
        f2 = next(f for f in r['findings'] if f['id'] == 'F2')
        r['findings'] = [f for f in r['findings'] if f['id'] != 'F2']
        r['dismissed'].append({'candidate_id': f2['candidates'][0], 'scanner_error': True, 'basis': basis,
                               'reason': 'The checks are recorded by the script runner, not raised.'})
        v.fill(r)
        r['summary'] = {'headline': r['summary']['headline'], **v.summary(r, ev)}
        return v.check(r, ev, None, ex)
    line_only = dismiss_f2([{'file': 'core/tests/test_tax.py', 'line': 1, 'excerpt': 'import'}])
    check(any('C3' in e and 'counter-evidence' in e for e in line_only),
          f'a passed-neutralised candidate needs counter-evidence to be a scanner error (got {line_only[:3]})', failures)
    with_ev = dismiss_f2([{'evidence': 'run/runs.json'}])
    check(not any('C3' in e and 'counter-evidence' in e for e in with_ev),
          'an evidence file is counter-evidence for the dismissal', failures)


# A tool repository whose tests are unittest.TestCase classes run by `python -m unittest discover` in CI (no pytest
# anywhere): class names that pytest's python_classes would not match, a module-level test function and a plain
# class unittest never runs, a sub-folder that is no package, a test that checks only that its mock was not called,
# scripts found through `.parents[1]` on sys.path and loaded by file path (a hyphenated name).
UNIT_CI = ('name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n'
           '      - uses: actions/checkout@v4\n      - run: {cmd}\n')
UNIT_FILES = {
    'README.md': '# rate tools\n',
    '.github/workflows/ci.yml': UNIT_CI.format(cmd='python -m unittest discover -s tests'),
    'scripts/tally.py': '"""Totals."""\n\n\ndef total(rows):\n    return sum(rows)\n',
    'scripts/fetch-rates.py': (
        '"""Fetch the rate archive, or take one already on disk."""\nimport argparse, shutil, urllib.request\n\n'
        'URL = "https://rates.example.com/rates.zip"\n\n\ndef main(argv=None):\n    ap = argparse.ArgumentParser()\n'
        '    ap.add_argument("--offline")\n    ap.add_argument("dest")\n    args = ap.parse_args(argv)\n'
        '    if args.offline:\n        shutil.copyfile(args.offline, args.dest)\n    else:\n'
        '        urllib.request.urlretrieve(URL, args.dest)\n    return 0\n'),
    'tests/test_tally.py': (
        'import sys, unittest\nfrom pathlib import Path\n\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))\nimport tally  # noqa: E402\n\n\n'
        'class TallySuite(unittest.TestCase):\n    def sample(self):\n        return tally.total([1, 2])\n\n'
        '    def test_total(self):\n        self.assertEqual(tally.total([2, 3]), 5)\n\n'
        '    def test_through_helper(self):\n        self.assertEqual(self.sample(), 3)\n\n\n'
        'class Plain:\n    def test_plain(self):\n        assert tally.total([]) == 0\n\n\n'
        'def test_orphan():\n    assert tally.total([4]) == 4\n'),
    'tests/test_rates.py': (
        'import importlib.util, tempfile, unittest\nfrom pathlib import Path\nfrom unittest.mock import patch\n\n'
        'ROOT = Path(__file__).resolve().parents[1]\n'
        'SPEC = importlib.util.spec_from_file_location("fetch_rates", ROOT / "scripts" / "fetch-rates.py")\n'
        'rates = importlib.util.module_from_spec(SPEC)\nSPEC.loader.exec_module(rates)\n\n\n'
        'class RatesCli(unittest.TestCase):\n    def test_offline_archive_is_not_downloaded(self):\n'
        '        with tempfile.TemporaryDirectory() as tmp:\n            src = Path(tmp, "a.zip")\n'
        '            src.write_bytes(b"x")\n'
        '            with patch.object(rates.urllib.request, "urlretrieve") as download:\n'
        '                rates.main(["--offline", str(src), str(Path(tmp, "b.zip"))])\n'
        '            download.assert_not_called()\n\n'
        '    def test_online_downloads(self):\n'
        '        with patch.object(rates.urllib.request, "urlretrieve") as download:\n'
        '            rates.main(["out.zip"])\n        download.assert_called_once()\n\n'
        '    def test_offline_copies(self):\n        with tempfile.TemporaryDirectory() as tmp:\n'
        '            src, dest = Path(tmp, "a.zip"), Path(tmp, "b.zip")\n            src.write_bytes(b"x")\n'
        '            self.assertEqual(rates.main(["--offline", str(src), str(dest)]), 0)\n'
        '            self.assertEqual(dest.read_bytes(), b"x")\n'),
    'tests/test_cwd.py': (
        'import os, sys, unittest\n\nsys.path.insert(0, os.path.join(os.getcwd(), "scripts"))\n'
        'import tally  # noqa: E402\n\n\nclass CwdSuite(unittest.TestCase):\n    def test_cwd(self):\n'
        '        self.assertEqual(tally.total([1]), 1)\n'),
    'tests/sub/test_deep.py': ('import unittest\n\n\nclass Deep(unittest.TestCase):\n    def test_deep(self):\n'
                               '        self.assertEqual(len("ab"), 2)\n'),
}


def unittest_repo_checks(tmp: Path, failures: list[str]) -> None:
    """unittest suites: TestCase classes are tests whatever they are called, the runner comes from CI, what unittest
    never runs is a candidate, mock-only checks are candidates, and neutralisation resolves the scripts and runs
    with unittest."""
    import neutralise  # noqa: E402
    root = tmp / 'unit'
    for rel, text in UNIT_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    res = scan_tests.scan(root)
    defs = {d['id']: d for d in res['definitions']}
    cands = res['candidates']
    rules = {(c['rule'], c['test_ids'][0].split('::', 1)[1]) for c in cands}
    tt, tr = 'tests/test_tally.py', 'tests/test_rates.py'
    check(not any(c['rule'] == 'class_pattern' for c in cands),
          f'a unittest.TestCase class is never a class_pattern candidate (got '
          f'{sorted(c["test_ids"][0] for c in cands if c["rule"] == "class_pattern")})', failures)
    run = (res.get('python_runners') or {}).get('.') or {}
    check(run.get('runner') == 'unittest' and run.get('start') == 'tests' and 'ci.yml' in ' '.join(run.get('evidence', []))
          and (defs.get(f'{tt}::TallySuite::test_total') or {}).get('runner') == 'unittest',
          f'the runner is the one CI runs, recorded per definition (got {run}, '
          f'{(defs.get(tt + "::TallySuite::test_total") or {}).get("runner")})', failures)
    never = {n for r, n in rules if r in ('not_a_testcase', 'not_a_package')}
    check(never == {'test_orphan', 'Plain::test_plain', 'Deep::test_deep'}
          and all(c['category'] == 'never_runs' for c in cands if c['rule'] in ('not_a_testcase', 'not_a_package')),
          f'under unittest a module-level test function, a plain class and a folder that is no package never run '
          f'(got {sorted(never)})', failures)
    check(('only_negative_mock_checks', 'RatesCli::test_offline_archive_is_not_downloaded') in rules
          and ('only_mock_call_checks', 'RatesCli::test_online_downloads') in rules
          and not any(n == 'RatesCli::test_offline_copies' for _, n in rules)
          and all(c['category'] == 'mock_only' for c in cands if c['rule'].startswith('only_')),
          f'a test whose only checks are on its mock is a mock_only candidate (got {sorted(rules)})', failures)
    off = defs.get(f'{tr}::RatesCli::test_offline_archive_is_not_downloaded') or {}
    check('scripts/fetch-rates.py:main' in off.get('calls', []),
          f'a script loaded with spec_from_file_location gives file-form targets (got {off.get("calls")})', failures)
    check('tally.total' in (defs.get(f'{tt}::TallySuite::test_through_helper') or {}).get('calls', []),
          'a helper method that calls the product is followed for targets', failures)

    # the same tests under pytest (CI runs pytest): TestCase classes are collected whatever python_classes says, and
    # a module-level test function runs
    proot = tmp / 'unit-pytest'
    for rel, text in {**UNIT_FILES, 'pytest.ini': '[pytest]\n',
                      '.github/workflows/ci.yml': UNIT_CI.format(cmd='python -m pytest')}.items():
        (proot / rel).parent.mkdir(parents=True, exist_ok=True)
        (proot / rel).write_text(text, encoding='utf-8')
    pres = scan_tests.scan(proot)
    prules = {(c['rule'], c['test_ids'][0].split('::', 1)[1]) for c in pres['candidates'] if c['category'] == 'never_runs'}
    check(prules == {('class_pattern', 'Plain::test_plain')}
          and all(d.get('runner') == 'pytest' for d in pres['definitions']),
          f'under pytest only the plain class outside python_classes never runs (got {sorted(prules)})', failures)

    # collect_check lists a unittest suite with unittest's own loader
    cand = tmp / 'unit-candidates.json'
    cand.write_text(json.dumps(res), encoding='utf-8')
    code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'unit-col')])
    col = json.loads((tmp / 'unit-col' / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
    pk = next((p for p in col.get('packages', []) if p['framework'] == 'unittest'), {})
    check(code == 0 and pk.get('status') == 'ran' and 'unittest' in (pk.get('command') or '')
          and set(col.get('not_collected', [])) == {f'{tt}::test_orphan', f'{tt}::Plain::test_plain',
                                                    'tests/sub/test_deep.py::Deep::test_deep'}
          and col.get('collected') == 6,
          f'a unittest suite is listed with the unittest loader (exit {code}, {pk.get("status")}, collected '
          f'{col.get("collected")}, not collected {col.get("not_collected")}, {pk.get("note")})', failures)

    # neutralise: the suite's own runner (unittest), scripts found through .parents[1] and by file path
    run_dir = tmp / 'unit-run'
    ids = [f'{tt}::TallySuite::test_total', f'{tr}::RatesCli::test_offline_archive_is_not_downloaded',
           f'{tr}::RatesCli::test_offline_copies', 'tests/test_cwd.py::CwdSuite::test_cwd']
    code = quiet(neutralise.main, ['--repo', str(root), '--candidates', str(cand), '--run-dir', str(run_dir)]
                 + [x for i in ids for x in ('--test', i)])
    got = {r['test_id']: r for r in json.loads((run_dir / 'neutralisations.json').read_text(encoding='utf-8'))['results']} \
        if (run_dir / 'neutralisations.json').is_file() else {}
    total = got.get(ids[0]) or {}
    check(code == 0 and total.get('runner') == 'unittest' and total.get('outcome') == 'failed_neutralised'
          and [t.get('file') for t in total.get('targets', [])] == ['scripts/tally.py'],
          f'a sys.path folder written with .parents[1] is resolved and the test runs with unittest (exit {code}, '
          f'{total.get("runner")}, {total.get("outcome")}, {[t.get("note") or t.get("file") for t in total.get("targets", [])]}, '
          f'{total.get("error_reason")})', failures)
    off_r, cop = got.get(ids[1]) or {}, got.get(ids[2]) or {}
    check(off_r.get('outcome') == 'passed_neutralised' and cop.get('outcome') == 'failed_neutralised'
          and [t.get('file') for t in off_r.get('targets', [])] == ['scripts/fetch-rates.py'],
          f'a script loaded by file path is neutralised: the mock-only test passes, the real one fails (got '
          f'{off_r.get("outcome")}, {cop.get("outcome")}, {[t.get("note") for t in off_r.get("targets", [])]})', failures)
    cwd_r = got.get(ids[3]) or {}
    check(cwd_r.get('outcome') == 'not_run' and 'sys.path' in (cwd_r.get('not_run_reason') or '')
          and 'line 3' in (cwd_r.get('not_run_reason') or ''),
          f'a target behind a sys.path entry that cannot be worked out is not_run with that reason (got '
          f'{cwd_r.get("outcome")}, {cwd_r.get("not_run_reason")})', failures)
    import validate_dead_test_report as v  # noqa: E402
    runs = json.loads((run_dir / 'runs.json').read_text(encoding='utf-8')) if (run_dir / 'runs.json').is_file() else \
        {'runs': [], 'cases': []}
    verr = [e for r in got.values() for e in v.neutralisation_errors(r, runs, {x['id'] for x in runs['runs']}, None)]
    check(got and not verr, f'the validator reads unittest neutralisation runs (got {verr[:2]})', failures)


# A Cargo crate: Rust tests are listed and scanned statically (never listed by a runner, never neutralised).
RUST_FILES = {
    'Cargo.toml': '[package]\nname = "gauge"\nversion = "0.1.0"\nedition = "2021"\n\n[features]\nextra = []\n',
    '.github/workflows/ci.yml': UNIT_CI.format(cmd='cargo test --workspace'),
    'src/lib.rs': (
        'pub fn level(x: i32) -> i32 {\n    x * 2\n}\n\npub fn label(x: i32) -> String {\n    format!("level {}", level(x))\n}\n\n'
        'pub fn clamp(x: i32) -> i32 {\n    x.max(0).min(100)\n}\n\npub fn scale(x: i32, by: i32) -> i32 {\n    x * by\n}\n\n'
        '#[cfg(test)]\nmod tests {\n    use super::*;\n\n'
        '    #[test]\n    fn doubles() {\n        assert_eq!(level(2), 4);\n    }\n\n'
        '    #[test]\n    fn smoke() {\n        let _ = label(1);\n    }\n\n'
        '    #[test]\n    #[ignore]\n    fn slow() {\n        assert_eq!(level(3), 6);\n    }\n\n'
        '    #[test]\n    fn gated_body() {\n        #[cfg(target_os = "plan9")]\n        {\n'
        '            assert_eq!(clamp(101), 100);\n        }\n    }\n\n'
        '    #[cfg(feature = "extra")]\n    #[test]\n    fn extra() {\n        assert_eq!(scale(2, 3), 6);\n    }\n\n'
        '    #[test]\n    fn through_helper() {\n        expect_level(4, 8);\n    }\n\n'
        '    fn expect_level(x: i32, want: i32) {\n        assert_eq!(level(x), want);\n    }\n}\n'),
    'src/main.rs': 'fn main() {\n    println!("{}", gauge::label(1));\n}\n',
}


def rust_checks(tmp: Path, failures: list[str]) -> None:
    """Rust: #[test] functions are definitions scanned statically; what no CI command runs is a candidate; nothing is
    listed by cargo or neutralised, and the output says so."""
    root = tmp / 'rust'
    for rel, text in RUST_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    out_file = tmp / 'rust.json'
    code = quiet(scan_tests.main, [str(root), '--out', str(out_file)])
    res = json.loads(out_file.read_text(encoding='utf-8')) if out_file.is_file() else {}
    defs = {d['id']: d for d in res.get('definitions', [])}
    names = {d['name'] for d in defs.values() if d.get('lang') == 'rust'}
    check(code == 0 and names == {'doubles', 'smoke', 'slow', 'gated_body', 'extra', 'through_helper'}
          and all(d.get('framework') == 'cargo-test' and d.get('cargo_id') for d in defs.values()),
          f'Rust #[test] functions are definitions, read with cargo_project (exit {code}, got {sorted(names)})', failures)
    got = {(c['category'], c['rule'], c['test_ids'][0].rsplit('::', 1)[-1]) for c in res.get('candidates', [])}
    want = {('no_assertion', 'no_assertion', 'smoke'), ('never_runs', 'ignored_never_run', 'slow'),
            ('unreached_assertion', 'cfg_body', 'gated_body'), ('never_runs', 'feature_never_enabled', 'extra')}
    check(got == want, f'the Rust rules find exactly the planted problems (missing {sorted(want - got)}, extra '
          f'{sorted(got - want)})', failures)
    so = res.get('static_only') or []
    check(len(so) == 1 and so[0].get('language') == 'Rust' and so[0].get('tests') == 6
          and 'neutralis' in (res.get('summary', {}).get('static_only_warning') or '')
          and 'language_coverage' not in res,
          f'Rust tests are recorded as scanned statically only, loudly (got {so}, '
          f'{res.get("summary", {}).get("static_only_warning")})', failures)
    # CI that runs the ignored tests and enables the feature: neither is a never-runs candidate
    (root / '.github/workflows/ci.yml').write_text(
        UNIT_CI.format(cmd='cargo test --workspace --all-features -- --include-ignored'), encoding='utf-8')
    res2 = scan_tests.scan(root)
    check(not [c for c in res2['candidates'] if c['category'] == 'never_runs'],
          f'an ignored or feature-gated test that a CI command runs is no candidate (got '
          f'{[c["rule"] for c in res2["candidates"] if c["category"] == "never_runs"]})', failures)
    cand = tmp / 'rust-candidates.json'
    cand.write_text(json.dumps(res), encoding='utf-8')
    code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'rust-col')])
    col = json.loads((tmp / 'rust-col' / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
    check(code == 0 and len(col.get('not_assessed', [])) == 6 and not col.get('not_collected'),
          f'Rust tests have no list command here: not assessed, never not collected (exit {code}, '
          f'{col.get("not_collected")})', failures)
    import neutralise  # noqa: E402
    smoke = next((i for i in defs if i.endswith('::smoke')), 'none')
    code = quiet(neutralise.main, ['--repo', str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'rust-run'),
                                   '--test', smoke])
    nfile = tmp / 'rust-run' / 'neutralisations.json'
    nres = json.loads(nfile.read_text(encoding='utf-8'))['results'][0] if nfile.is_file() else {}
    check(code == 0 and nres.get('outcome') == 'not_run' and 'Rust' in (nres.get('not_run_reason') or ''),
          f'a Rust test is not neutralised, with the reason (exit {code}, {nres.get("outcome")}, '
          f'{nres.get("not_run_reason")})', failures)


def honesty_checks(tmp: Path, failures: list[str]) -> None:
    """The validator needs the scan's language warnings in the limitations and lowers evidence completeness for
    what was not read or cannot be run; the renderer keeps limitations in --brief, groups dismissals and never
    crashes on a schema-valid report."""
    import copy
    import render_report  # noqa: E402
    import validate_dead_test_report as v  # noqa: E402
    ex = HERE.parent / 'examples'
    rep = json.loads((ex / 'dead_test_report.example.json').read_text(encoding='utf-8'))
    ev = v.load_evidence(rep, ex)

    def errors(ev2: dict, limitations: list[str]) -> list[str]:
        r = copy.deepcopy(rep)
        r['limitations'] = r['limitations'] + limitations
        v.fill(r)
        r['summary'] = {'headline': r['summary']['headline'], **v.summary(r, ev2)}
        return v.check(r, ev2, None, ex)
    warned = copy.deepcopy(ev)
    warned['candidates']['language_coverage'] = {
        'status': 'unsupported_dominant', 'dominant_unsupported_language': 'Kotlin', 'decided_by': 'lines',
        'supported_lines': 100, 'unsupported_lines': 300, 'warning': 'This repository is predominantly Kotlin.'}
    warned['candidates']['summary']['language_warning'] = 'This repository is predominantly Kotlin.'
    e1 = errors(warned, [])
    check(any('Kotlin' in e and 'limitations' in e for e in e1),
          f'a scan that warned about the main language needs a limitation naming it (got {e1[:3]})', failures)
    e2 = errors(warned, ['Kotlin, the main language of the repository, was not analysed: its tests are not assessed.'])
    check(not e2, f'the limitation naming the unread language satisfies the validator (got {e2[:3]})', failures)
    base_c = v.completeness(rep, ev)
    check(abs(v.completeness(rep, warned) - round(base_c * 0.25, 4)) < 1e-9,
          f'evidence completeness is multiplied by the share of code in languages the scan read (got '
          f'{v.completeness(rep, warned)} from {base_c})', failures)
    static = copy.deepcopy(ev)
    extra = [{'id': f'src/lib.rs::tests::t{i}', 'file': 'src/lib.rs', 'line': i + 1, 'name': f't{i}', 'lang': 'rust',
              'package': '.', 'framework': 'cargo-test', 'runner': 'cargo', 'calls': []} for i in range(5)]
    static['candidates']['definitions'] = static['candidates']['definitions'] + extra
    static['candidates']['static_only'] = [{'language': 'Rust', 'tests': 5, 'reason': 'not listed or neutralised'}]
    static['collection'] = {**static['collection'], 'defined': static['collection']['defined'] + 5,
                            'not_assessed': static['collection']['not_assessed'] + [d['id'] for d in extra]}
    n = len(ev['candidates']['definitions'])
    check(abs(v.completeness(rep, static) - round(base_c * n / (n + 5), 4)) < 1e-9,
          f'evidence completeness is multiplied by the share of tests the skill can list and run (got '
          f'{v.completeness(rep, static)}, want {round(base_c * n / (n + 5), 4)})', failures)
    r3 = copy.deepcopy(rep)
    r3['tests'] = r3['tests'] + [{'id': d['id'], 'file': d['file'], 'line': d['line'], 'status': 'not_assessed',
                                  'reason': 'Rust tests are scanned statically only'} for d in extra]
    r3['collection'] = {k: static['collection'][k] for k in ('defined', 'collected', 'not_collected', 'not_assessed')}
    v.fill(r3)
    r3['summary'] = {'headline': r3['summary']['headline'], **v.summary(r3, static)}
    e3 = v.check(r3, static, None, ex)
    check(any('Rust' in e and 'limitations' in e for e in e3) and len(e3) == 1,
          f'statically scanned tests need a limitation saying they were not neutralised (got {e3[:3]})', failures)
    r3['limitations'].append('The 5 Rust tests were scanned statically only: none was listed or neutralised.')
    e4 = v.check(r3, static, None, ex)
    check(not e4, f'the static-only limitation satisfies the validator (got {e4[:3]})', failures)

    # schema: neutralisation targets are typed, so a list of names is refused before the renderer sees it
    bad = copy.deepcopy(rep)
    f_neut = next(f for f in bad['findings'] if f['evidence'].get('neutralisation'))
    f_neut['evidence']['neutralisation']['targets'] = ['ledger.tax.gst']
    schema = json.loads(v.SCHEMA.read_text(encoding='utf-8'))
    check(any('targets' in e for e in v.schema_errors(bad, schema, schema)),
          'the schema types evidence.neutralisation.targets (a list of names is invalid)', failures)
    check(not v.schema_errors(rep, schema, schema), 'the example still conforms to the schema', failures)
    # renderer
    try:
        line = render_report.evidence_line({'neutralisation': {'outcome': 'passed_neutralised', 'targets': [
            {'target': 'scripts/fetch-rates.py:main', 'variants': [{'variant': 'return', 'test_status': 'passed'}]},
            'tally.total']}})
    except (TypeError, KeyError) as exc:
        line = f'crashed: {exc!r}'
    check('fetch-rates.py:main: return -> passed' in line and 'total' in line,
          f'file-form targets render with their file, and a bare name does not crash (got {line!r})', failures)
    many = copy.deepcopy(rep)
    many['dismissed'] = [{'candidate_id': f'C{i}', 'reason': 'The class is a unittest.TestCase, which pytest collects.',
                          'scanner_error': True} for i in range(100, 140)] + \
        [{'candidate_id': 'C7', 'reason': 'A conditional skip with a stated reason.'}]
    render = getattr(render_report, 'render', None)
    brief = render(many, brief=True, color=False) if render else ''
    full = render(many, brief=False, color=False) if render else ''
    check('## Limitations' in brief and many['limitations'][0] in brief, '--brief keeps the limitations', failures)
    dis = brief.split('## Dismissed', 1)[-1].split('## ', 1)[0]
    check(bool(brief) and len(dis.strip().splitlines()) <= 4 and '40' in dis and 'C100' in dis,
          f'--brief summarises dismissals that share a reason (got {len(dis.strip().splitlines())} lines)', failures)
    check('C139' in full, 'the full report still names every dismissed candidate', failures)


PYTEST_FILES = {
    'pytest.ini': '[pytest]\naddopts = -v\ntestpaths = tests\n',
    'tests/test_fares.py': ('import pytest\n\n\n@pytest.mark.parametrize("n", [1, 2])\ndef test_fare(n):\n    assert n\n\n\n'
                            'class TestZones:\n    def test_zone(self):\n        assert 1\n'),
    'tests/test_berths.py': 'def test_berth():\n    assert 2\n',
    'tests/test_map.py': 'import not_installed_driver\n\n\ndef test_map():\n    assert 3\n',
}
PYTEST_IDS = ['tests/test_fares.py::test_fare[1]', 'tests/test_fares.py::test_fare[2]', 'tests/test_fares.py::TestZones::test_zone',
              'tests/test_berths.py::test_berth']


def fake_pytest(addopts: list[str], errors: list[str]):
    """A stand-in for `pytest --collect-only` whose output depends on the verbosity, as pytest's does: the addopts of
    the repository's ini file count with the command line (-v raises it, -q lowers it). Verbosity 0 or more prints a
    tree of <Module>/<Function> nodes, -1 the node ids, -2 or less one count per file. errors are files that fail to
    import (pytest lists the others, then exits 2 with an ERROR line per file)."""
    def run(cmd, cwd, env):
        if 'pytest' not in cmd:
            return 1, ''
        args = addopts + cmd
        v = args.count('-v') - args.count('-q') - 2 * args.count('-qq')
        files = sorted({i.split('::')[0] for i in PYTEST_IDS})
        if v >= 0:
            body = ['<Dir repo>', '  <Dir tests>']
            for f in files:
                body.append(f'    <Module {f.split("/")[-1]}>')
                body += [f'      <Function {i.split("::")[-1]}>' for i in PYTEST_IDS if i.startswith(f + '::')]
        elif v == -1:
            body = list(PYTEST_IDS)
        else:
            body = [f'{f}: {sum(1 for i in PYTEST_IDS if i.startswith(f + "::"))}' for f in files]
        if errors:
            body += ['', '=' * 20 + ' short test summary info ' + '=' * 20] + [f'ERROR {e}' for e in errors] + \
                ['!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!']
            return 2, '\n'.join(body) + '\n'
        return 0, '\n'.join(body) + '\n'
    return run


def cmd_shim_checks(tmp: Path, failures: list[str]) -> None:
    """A test file name with cmd.exe metacharacters (a&qa_marker&.test.ts, from the repository being audited) never
    reaches cmd.exe through an npx .cmd shim: the stand-in shim on PATH only echoes, and qa_marker.cmd beside it
    writes marker.txt if cmd.exe ever runs it as a second command. Windows only: elsewhere no shell re-parses."""
    if os.name != 'nt':
        print('  skipped cmd-shim check: the .cmd shim re-parsing only happens on Windows (cmd.exe)')
        return
    shim, pkg = tmp / 'cmdshim', tmp / 'cmdshim-pkg'
    shim.mkdir()
    pkg.mkdir()
    (shim / 'npx.cmd').write_text('@echo off\r\necho shim got: %*\r\n', encoding='ascii')
    (shim / 'qa_marker.cmd').write_text('@echo ran> "%~dp0marker.txt"\r\n', encoding='ascii')
    name = 'a&qa_marker&.test.ts'
    (pkg / name).write_text('test("x", () => {})\n', encoding='utf-8')
    old = os.environ.get('PATH', '')
    os.environ['PATH'] = str(shim) + os.pathsep + old
    try:
        for cmd in (['npx', 'vitest', 'list', '--json=out.json', name], ['npx', 'jest', '--listTests', '--json', name],
                    ['npx', 'playwright', 'test', '--list', '--reporter=json', name]):
            code, out = collect_check.run(cmd, pkg, {'PATH': os.environ['PATH']})
            check(not (shim / 'marker.txt').exists(),
                  f'{cmd[1]}: cmd.exe ran a second command taken from the test file name {name!r}', failures)
            check(code != 0 and 'cmd.exe' in out, f'{cmd[1]}: the shim run is refused with a reason (got {code}: {out[-200:]})',
                  failures)
            (shim / 'marker.txt').unlink(missing_ok=True)
    finally:
        os.environ['PATH'] = old


def pytest_checks(tmp: Path, failures: list[str]) -> None:
    """pytest's listing whatever verbosity the repository's addopts set, and one file that cannot be imported."""
    root = tmp / 'py'
    for rel, text in PYTEST_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding='utf-8')
    res = scan_tests.scan(root)
    cand = tmp / 'py-candidates.json'
    cand.write_text(json.dumps(res), encoding='utf-8')
    real_run = collect_check.run

    def collect(addopts, errors, tag):
        collect_check.run = fake_pytest(addopts, errors)
        try:
            code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / tag)])
        finally:
            collect_check.run = real_run
        col = json.loads((tmp / tag / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
        return code, col, next((p for p in col.get('packages', []) if p['framework'] == 'pytest'), {})

    # test_map.py imports a missing module, but these runs list it like the others: only the verbosity is under test
    PYTEST_IDS.append('tests/test_map.py::test_map')
    for addopts, tag, what in ((['-v'], 'py-v', 'addopts = -v (a tree)'), (['-v', '-v'], 'py-vv', 'addopts = -vv'),
                               (['-q'], 'py-q', 'addopts = -q (counts per file)')):
        code, col, pk = collect(addopts, [], tag)
        check(code == 0 and pk.get('status') == 'ran' and col.get('collected') == 4 and not col.get('not_collected')
              and 'verbosity' in (pk.get('note') or ''),
              f'{what}: the listing is read and the verbosity change recorded (exit {code}, status {pk.get("status")}, '
              f'collected {col.get("collected")}, note {pk.get("note")!r})', failures)
    PYTEST_IDS.pop()
    code, col, pk = collect([], ['tests/test_map.py'], 'py-err')
    check(code == 0 and pk.get('status') == 'partial' and pk.get('errored_files') == ['tests/test_map.py']
          and col.get('collected') == 3 and col.get('not_assessed') == ['tests/test_map.py::test_map'],
          f'one file that fails to import leaves the others listed and only its tests not assessed (status '
          f'{pk.get("status")}, errored {pk.get("errored_files")}, collected {col.get("collected")}, '
          f'not assessed {col.get("not_assessed")})', failures)
    code, col, pk = collect([], ['tests/conftest.py'], 'py-conftest')
    check(code == 0 and pk.get('status') == 'errored' and col.get('collected') == 0,
          f'an error in a conftest.py (or anything but a listed test file) still errors the whole list (status '
          f'{pk.get("status")}, collected {col.get("collected")})', failures)
    # the real pytest, when it is installed here: addopts = -v and a module that cannot be imported
    import importlib.util
    if importlib.util.find_spec('pytest') is None:
        return
    code = quiet(collect_check.main, [str(root), '--candidates', str(cand), '--run-dir', str(tmp / 'py-real')])
    col = json.loads((tmp / 'py-real' / 'collection.json').read_text(encoding='utf-8')) if code == 0 else {}
    pk = next((p for p in col.get('packages', []) if p['framework'] == 'pytest'), {})
    check(code == 0 and pk.get('status') == 'partial' and col.get('collected') == 3
          and col.get('not_assessed') == ['tests/test_map.py::test_map'] and not col.get('not_collected'),
          f'real pytest with addopts = -v and one unimportable module: three tests collected, its test not assessed '
          f'(exit {code}, status {pk.get("status")}, collected {col.get("collected")}, not assessed {col.get("not_assessed")}, '
          f'not collected {col.get("not_collected")})', failures)


def quiet(fn, argv: list[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return fn(argv)
        except SystemExit as exc:                    # an argparse refusal is a failed check, not a crash
            return exc.code if isinstance(exc.code, int) else 1


def check(cond: bool, what: str, failures: list[str]) -> None:
    if not cond:
        failures.append(what)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix='dtf-self-'))
    failures: list[str] = []
    try:
        for rel, text in FILES.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        res = scan_tests.scan(tmp, ['*.e2e.ts'])
        defs = {d['id']: d for d in res['definitions']}
        cands = res['candidates']
        reg = 'packages/quayside/tests/registry.test.ts'

        def cand(rule: str, name: str) -> list[dict]:
            return [c for c in cands if c['rule'] == rule and any(t.endswith('::' + name) or t.endswith(' > ' + name)
                                                                   for t in c['test_ids'])]
        only = [c for c in cands if c['rule'] == 'only']
        check(len(only) == 1 and only[0]['test_ids'][0].endswith('is really focused'),
              f'only the real it.only is a .only candidate (got {[c["test_ids"] for c in only]})', failures)
        check(not any(i.endswith('::focused') or i.endswith('> focused') for i in defs),
              "the library's own test.only('focused') is not a test definition", failures)
        check(not any('> a' == i[-4:] or i.endswith('::a') for i in defs), "a local helper named test is not a test", failures)
        check(f'{reg}::tiers > builds rows with a local helper named test' in defs, 'the test beside the local helper is found',
              failures)
        awaited = {c['test_ids'][0].split(' > ')[-1] for c in cands if c['rule'] == 'resolves_not_awaited'}
        check(awaited == {'forgets to await'}, f'only the bare expectation is not awaited (got {sorted(awaited)})', failures)
        no_assert = {c['test_ids'][0].split(' > ')[-1] for c in cands if c['category'] == 'no_assertion'}
        check(not no_assert & {'checks with an alias', 'checks with a local helper', 'checks with an imported helper'},
              f'aliases and helpers are assertions (no_assertion: {sorted(no_assert)})', failures)
        check(not cand('assert_in_loop', 'runs every case'), 'a typed array literal is a literal', failures)
        check(not cand('assert_after_return', 'stops early on a guard'), 'a guarded return is not an early return', failures)
        check(not cand('empty_catch', 'cleans up quietly'), 'a cleanup catch in finally is not a swallowed failure', failures)
        check(not [c for c in cands if c['category'] == 'duplicate' and any('filters by has' in t for t in c['test_ids'])],
              'object keys keep two tests apart', failures)
        check(f'{reg}::mode %s > drains' in defs and defs[f'{reg}::mode %s > drains']['name_pattern'],
              'describe.each<T>(...) is a parametrised block', failures)
        dog = [d for d in res['definitions'] if d['file'].endswith('checkout.e2e.ts')]
        check(len(dog) == 1 and dog[0]['framework'] == 'custom' and dog[0].get('runner_module') == 'quayside',
              f'the dogfood suite is the custom runner\'s (got {dog})', failures)
        node = [d for d in res['definitions'] if d['file'] == 'scripts/tools.test.ts']
        check(len(node) == 1 and node[0]['framework'] == 'node-test', f'node:test is node-test (got {node})', failures)
        check(not any(d['file'].startswith('python/e2e/') for d in res['definitions']),
              'a directory named e2e with a product manifest is not a test directory', failures)
        check(any(d['file'] == 'python/tests/e2e/test_flow.py' for d in res['definitions']),
              'a plain tests/e2e directory still holds tests', failures)
        alias_def = defs.get(f'{reg}::registration > checks with an imported helper')
        check(alias_def is not None and 'expectRow' not in alias_def['calls'],
              'an imported assertion helper is never a neutralisation target', failures)

        # ---- collect_check: the Vitest JSON path, and the guard around every list command
        cand_file = tmp / 'candidates.json'
        cand_file.write_text(json.dumps(res), encoding='utf-8')
        seen: list[list[str]] = []
        real_run = collect_check.run

        def fake_run(cmd, cwd, env):
            seen.append(cmd)
            if cmd[:3] == ['npx', 'vitest', 'list'] and '--json' in cmd:
                i = cmd.index('--json')                   # the old mistake: the next argument is the output file
                (Path(cwd) / cmd[i + 1]).write_text('[]', encoding='utf-8')
            if cmd[:3] == ['npx', 'vitest', 'list']:
                out = next(a for a in cmd if a.startswith('--json='))[len('--json='):]
                Path(out).write_text(json.dumps([{'name': 'registration > records a focused test',
                                                  'file': str(Path(cwd) / cmd[-1])}]), encoding='utf-8')
            return 0, ''
        collect_check.run = fake_run
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run'),
                                       '--files', reg])
        finally:
            collect_check.run = real_run
        vit = [c for c in seen if c[:3] == ['npx', 'vitest', 'list']]
        check(code == 0 and vit and any(a.startswith('--json=') for a in vit[0]) and '--json' not in vit[0],
              f'vitest list gets --json=<path> (got {vit})', failures)
        col = json.loads((tmp / 'run' / 'collection.json').read_text(encoding='utf-8'))
        check(col['collected'] == 1, f'the listed test is collected (got {col["collected"]})', failures)
        original = (tmp / reg).read_bytes()

        def clobber(cmd, cwd, env):
            (Path(cwd) / 'tests' / 'registry.test.ts').write_text('[]', encoding='utf-8')
            return 0, '[]'
        collect_check.run = clobber
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run2'),
                                       '--files', reg])
        finally:
            collect_check.run = real_run
        check(code == 3 and (tmp / reg).read_bytes() == original and not (tmp / 'run2' / 'collection.json').exists(),
              'a list command that overwrites a test file stops the script and the file is restored', failures)
        nt = json.loads((tmp / 'run' / 'collection.json').read_text(encoding='utf-8'))
        check(all(i not in nt['not_collected'] for i in [d['id'] for d in node]), 'node:test is never not_collected', failures)

        # ---- --split-on-error: one file the runner cannot list no longer costs the package's other files
        bad = 'packages/quayside/tests/tags.test.ts'

        def picky(cmd, cwd, env):
            files = [a for a in cmd[4:] if not a.startswith('-')]
            if cmd[:3] != ['npx', 'vitest', 'list']:
                return 0, ''
            if not files or any(f.endswith('tags.test.ts') for f in files):
                return 1, 'Error: the config does not define the tag "slow"'
            out = next(a for a in cmd if a.startswith('--json='))[len('--json='):]
            Path(out).write_text(json.dumps([{'name': d['name'], 'file': str(tmp / d['file'])} for d in res['definitions']
                                             if d['file'] == reg]), encoding='utf-8')
            return 0, ''
        (tmp / bad).write_text("import { it, expect } from 'vitest';\nit('tags a job', { tags: ['slow'] }, () => {\n"
                               "  expect(1).toBe(1);\n});\n", encoding='utf-8')
        res = scan_tests.scan(tmp, ['*.e2e.ts'])
        cand_file.write_text(json.dumps(res), encoding='utf-8')
        collect_check.run = picky
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run3'),
                                       '--split-on-error', '10'])
        finally:
            collect_check.run = real_run
        sp = json.loads((tmp / 'run3' / 'collection.json').read_text(encoding='utf-8'))
        pk = next(p for p in sp['packages'] if p['framework'] == 'vitest' and p['package'] == 'packages/quayside')
        check(code == 0 and pk['status'] == 'partial' and pk['errored_files'] == [bad],
              f'a split list leaves the bad file errored and the rest listed (got {pk.get("status")}, '
              f'{pk.get("errored_files")})', failures)
        check(f'{bad}::tags a job' in sp['not_assessed'] and sp['collected'] >= 15,
              'tests in the failing file are not_assessed, the others collected', failures)
        cmd_shim_checks(tmp, failures)
        pytest_checks(tmp, failures)
        go_checks(tmp, failures)
        tool_repo_checks(tmp, failures)
        report_checks(tmp, failures)
        unittest_repo_checks(tmp, failures)
        rust_checks(tmp, failures)
        honesty_checks(tmp, failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print('self-test FAILED:\n  - ' + '\n  - '.join(failures))
        return 1
    print('dead-test-finder self-test: ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
