#!/usr/bin/env python3
"""Self-test for the test impact selector's Go support, on small synthetic fixtures (stdlib only).

    python scripts/self_test.py [--only NAME] [-v]

Each case builds a tiny Go module ("harbour": a fares package, an api package that imports it, a geo
package nobody imports or tests) beside a JavaScript tool with its own test, in a temporary directory.
Cases that need the Go toolchain or git are skipped (and say so) when those are not on PATH.
"""
from __future__ import annotations
import argparse, difflib, json, os, shutil, subprocess, sys, tempfile, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODULE = 'example.test/harbour'
FILES = {
    'go.mod': f'module {MODULE}\n\ngo 1.21\n',
    'pkg/fares/fares.go': 'package fares\n\n// Price is the fare for n passengers.\nfunc Price(n int) int {\n\tif n > 4 {\n\t\treturn n * 9\n\t}\n\treturn n * 10\n}\n',
    'pkg/fares/fares_test.go': 'package fares\n\nimport "testing"\n\nfunc TestPrice(t *testing.T) {\n\tif Price(2) != 20 {\n\t\tt.Fatal("two")\n\t}\n}\n\nfunc TestGroup(t *testing.T) {\n\tt.Run("five", func(t *testing.T) {\n\t\tif Price(5) != 45 {\n\t\t\tt.Fatal("five")\n\t\t}\n\t})\n}\n',
    'pkg/api/api.go': f'package api\n\nimport (\n\t"fmt"\n\n\t"{MODULE}/pkg/fares"\n)\n\n// Quote formats a fare.\nfunc Quote(n int) string {{\n\treturn fmt.Sprintf("%d", fares.Price(n))\n}}\n',
    'pkg/api/api_test.go': 'package api\n\nimport "testing"\n\nfunc TestQuote(t *testing.T) {\n\tif Quote(1) != "10" {\n\t\tt.Fatal(Quote(1))\n\t}\n}\n',
    'pkg/api/driver_test.go': 'package api\n\nimport "testing"\n\nfunc TestDriver(t *testing.T) {}\n',
    'pkg/clock/clock.go': 'package clock\n\n// Now is fixed in tests.\nfunc Now() int { return 1 }\n',
    'pkg/clock/clock_test.go': 'package clock\n\nimport "testing"\n\nfunc TestNow(t *testing.T) {\n\tif Now() != 1 {\n\t\tt.Fatal("now")\n\t}\n}\n',
    'pkg/geo/driver.go':'package geo\n\n// Hops counts legs.\nfunc Hops(n int) int {\n\treturn n - 1\n}\n',
    'tools/agent/package.json': '{"name": "agent", "scripts": {"test": "node --test"}}\n',
    'tools/agent/run.js': 'module.exports = () => 1;\n',
    'tools/agent/run.test.js': 'const run = require("./run");\nrequire("node:test")("runs", () => run());\n',
    '.github/workflows/ci.yml': 'jobs:\n  test:\n    steps:\n      - run: go test -race -timeout 25m -coverprofile=c.out ./...\n',
}


class Skip(Exception):
    pass


def make_repo(root: Path, files: dict[str, str] = FILES) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8', newline='\n')
    return root


def diff_of(rel: str, old: str, new: str) -> str:
    return ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f'a/{rel}', f'b/{rel}'))


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8', newline='\n')
    return p


FARES_CHANGED = FILES['pkg/fares/fares.go'].replace('n > 4', 'n >= 4')
GEO_CHANGED = FILES['pkg/geo/driver.go'].replace('n - 1', 'n - 2')


def select(repo: Path, tmp: Path, rel: str, new: str, **kw) -> dict:
    import select_tests
    d = write(tmp / f'{rel.replace("/", "_")}.diff', diff_of(rel, FILES[rel], new))
    return select_tests.select(repo, d, **kw)


# ---------------------------------------------------------------- cases
def case_import_graph_go(tmp: Path) -> None:
    """Go imports are edges: a test reaches the packages its package imports (by go.mod's module path)."""
    import import_graph
    repo = make_repo(tmp / 'repo')
    g = import_graph.build(repo)
    assert g.languages.get('go') == 'parsed', g.languages
    reach = sorted(g.tests_reaching('pkg/fares/fares.go'))
    assert reach == ['pkg/api/api_test.go', 'pkg/api/driver_test.go', 'pkg/fares/fares_test.go'], reach
    assert g.tests_reaching('pkg/geo/driver.go') == {}, g.tests_reaching('pkg/geo/driver.go')


def case_select_go_change(tmp: Path) -> None:
    """A Go change selects its package's tests and its importers' tests, and the command keeps CI's go test flags."""
    repo = make_repo(tmp / 'repo')
    sel = select(repo, tmp, 'pkg/fares/fares.go', FARES_CHANGED)
    files = sorted(s['file'] for s in sel['selected'])
    assert files == ['pkg/api/api_test.go', 'pkg/api/driver_test.go', 'pkg/fares/fares_test.go'], files
    assert not sel['blind_spots'], sel['blind_spots']
    go = [c for c in sel['command']['commands'] if c['framework'] == 'go-testing']
    assert len(go) == 1 and go[0]['argv'] == ['go', 'test', '-race', '-timeout', '25m', './pkg/api', './pkg/fares'], go


def case_unmapped_go_widens_within_go(tmp: Path) -> None:
    """An unmapped Go file widens to Go tests (its package, else the module), never to other languages' tests."""
    repo = make_repo(tmp / 'repo')
    sel = select(repo, tmp, 'pkg/geo/driver.go', GEO_CHANGED)
    spots = [b for b in sel['blind_spots'] if b['kind'] == 'unmapped_source']
    assert spots, sel['blind_spots']
    widened = spots[0]['widened_to']
    assert widened and all(t.endswith('_test.go') for t in widened), widened


def case_naming_same_package(tmp: Path) -> None:
    """Go naming matches <stem>_test.go only in the same directory (a Go test sits beside its package)."""
    repo = make_repo(tmp / 'repo')
    sel = select(repo, tmp, 'pkg/geo/driver.go', GEO_CHANGED)
    named = [x['file'] for x in sel['selected'] + sel['considered_not_selected']
             if any(r['source'] == 'naming' for r in x['reasons'])]
    assert named == [], named


def go_junit(tmp: Path) -> Path:
    return write(tmp / 'h1' / 'go.xml', f'''<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="{MODULE}/pkg/fares" tests="3" failures="2">
    <testcase classname="{MODULE}/pkg/fares" name="TestPrice" time="0.01"><failure message="Failed">two</failure></testcase>
    <testcase classname="{MODULE}/pkg/fares" name="TestGroup" time="0.01"><failure message="Failed">x</failure></testcase>
    <testcase classname="{MODULE}/pkg/fares" name="TestGroup/five" time="0.01"><failure message="Failed">five</failure></testcase>
  </testsuite>
  <testsuite name="{MODULE}/pkg/api" tests="1" failures="1">
    <testcase classname="" name="TestMain" time="0"><failure message="Failed">build failed</failure></testcase>
  </testsuite>
</testsuites>
''')


def case_history_gotestsum_files(tmp: Path) -> None:
    """gotestsum JUnit (classname = import path) maps to test files; a package build failure to its package."""
    import select_tests
    repo = make_repo(tmp / 'repo')
    go_junit(tmp / 'hist')
    write(tmp / 'hist' / 'H1.diff', diff_of('pkg/fares/fares.go', FILES['pkg/fares/fares.go'], FARES_CHANGED))
    hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': [
        {'id': 'H1', 'diff': 'H1.diff', 'full_run': True, 'runs': [{'package': '.', 'junit': 'h1/go.xml'}]}]}))
    h = select_tests.load_history(hist, repo)[0]
    assert h['failed'] == ['pkg/api/(package)', 'pkg/fares/fares_test.go'], h['failed']


def case_replay_skips_and_counts_packages(tmp: Path) -> None:
    """replay skips a diff that does not apply (instead of aborting) and counts a package-level failure as
    selected when a test file of that package was selected."""
    import replay
    repo = make_repo(tmp / 'repo')
    go_junit(tmp / 'hist')
    write(tmp / 'hist' / 'H1.diff', diff_of('pkg/fares/fares.go', FILES['pkg/fares/fares.go'], FARES_CHANGED))
    bad = diff_of('pkg/geo/driver.go', FILES['pkg/geo/driver.go'].replace('n - 1', 'n + 7'), GEO_CHANGED)
    write(tmp / 'hist' / 'H2.diff', bad)
    hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': [
        {'id': 'H1', 'diff': 'H1.diff', 'full_run': True, 'runs': [{'package': '.', 'junit': 'h1/go.xml'}]},
        {'id': 'H2', 'diff': 'H2.diff', 'full_run': True, 'runs': [{'package': '.', 'junit': 'h1/go.xml'}]}]}))
    res = replay.replay(repo, hist, 'strict', [], None, None, None)
    assert res['changes_replayed'] == 1, res['changes_replayed']
    assert [s['change'] for s in res['changes_skipped']] == ['H2'], res['changes_skipped']
    assert res['failures_total'] == 2 and res['failures_missed'] == 0, (res['failures_total'], res['misses'])


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.test',
                           '-c', 'commit.gpgsign=false', *args], capture_output=True, text=True, check=True).stdout


def case_replay_from_git_commits(tmp: Path) -> None:
    """A history change named by its commit is replayed on a checkout of that commit's parent, so changes
    already in HEAD can be replayed."""
    if shutil.which('git') is None:
        raise Skip('git is not on PATH')
    import replay
    repo = make_repo(tmp / 'repo')
    git(repo, 'init', '-q')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'base')
    write(repo / 'pkg/fares/fares.go', FARES_CHANGED)
    git(repo, 'commit', '-q', '-am', 'change')
    sha = git(repo, 'rev-parse', 'HEAD').strip()
    go_junit(tmp / 'hist')
    hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': [
        {'id': 'H1', 'commit': sha, 'full_run': True, 'runs': [{'package': '.', 'junit': 'h1/go.xml'}]}]}))
    res = replay.replay(repo, hist, 'strict', [], None, None, None)
    assert res['changes_replayed'] == 1 and not res['changes_skipped'], res['changes_skipped']
    assert res['changes'][0]['changed_paths'] == ['pkg/fares/fares.go'], res['changes'][0]['changed_paths']
    assert res['failures_missed'] == 0, res['misses']


def case_baseline_failures(tmp: Path) -> None:
    """Tests that already fail at the base are left out of co-failure evidence and of replay's recall."""
    import replay, select_tests
    repo = make_repo(tmp / 'repo')
    go_junit(tmp / 'hist')
    changes = []
    for i in (1, 2, 3):
        write(tmp / 'hist' / f'H{i}.diff', diff_of('pkg/fares/fares.go', FILES['pkg/fares/fares.go'], FARES_CHANGED))
        changes.append({'id': f'H{i}', 'diff': f'H{i}.diff', 'full_run': True,
                        'runs': [{'package': '.', 'junit': 'h1/go.xml'}]})
    hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': changes}))
    base = write(tmp / 'baseline.json', json.dumps({'failures': ['pkg/fares/fares_test.go']}))
    d = write(tmp / 'c.diff', diff_of('pkg/fares/fares.go', FILES['pkg/fares/fares.go'], FARES_CHANGED))
    sel = select_tests.select(repo, d, history=hist, baseline_failures=base)
    fares = next(s for s in sel['selected'] if s['file'] == 'pkg/fares/fares_test.go')
    assert not any(r['source'] == 'co_failure' for r in fares['reasons']), fares['reasons']
    assert sel['inputs'].get('baseline_failures'), sel['inputs']
    res = replay.replay(repo, hist, 'strict', [], None, None, None, baseline_failures=base)
    assert res['failures_baseline'] == 3 and res['failures_total'] == 3, (res['failures_baseline'], res['failures_total'])


def case_coverage_map_go_profiles(tmp: Path) -> None:
    """coverage_map.py normalise reads per-test Go coverprofiles (block ranges to lines, import paths to files)."""
    import coverage_map
    repo = make_repo(tmp / 'repo')
    prof = tmp / 'cov'
    write(prof / 'p1.out', f'mode: set\n{MODULE}/pkg/fares/fares.go:4.25,5.10 1 1\n{MODULE}/pkg/fares/fares.go:5.10,7.3 1 0\n'
                           f'{MODULE}/pkg/fares/fares.go:8.2,8.14 1 1\n')
    write(prof / 'p2.out', f'mode: set\n{MODULE}/pkg/fares/fares.go:4.25,5.10 1 1\n{MODULE}/pkg/fares/fares.go:5.10,7.3 1 1\n'
                           f'{MODULE}/pkg/api/api.go:10.25,12.2 1 1\n')
    manifest = {'format': 'go-coverprofile-per-test', 'tool_version': 'go1.26', 'base': str(prof),
                'runs': {'pkg/fares/fares_test.go::TestPrice': {'profile': 'p1.out', 'exit_code': 0},
                         'pkg/api/api_test.go::TestQuote': {'profile': 'p2.out', 'exit_code': 0}}}
    out = coverage_map.normalise(manifest, repo, '.', 'normalisation', None, None)
    t = out['tests']
    files = out['files']
    assert set(files) == {'pkg/fares/fares.go', 'pkg/api/api.go'}, set(files)
    line6 = [t[i] for i in files['pkg/fares/fares.go']['6']]
    assert line6 == ['pkg/api/api_test.go::TestQuote'], line6
    line8 = [t[i] for i in files['pkg/fares/fares.go']['8']]
    assert line8 == ['pkg/fares/fares_test.go::TestPrice'], line8


def case_coverage_map_go_run(tmp: Path) -> None:
    """coverage_map.py go runs each test file's tests with its own coverprofile and maps them."""
    if shutil.which('go') is None:
        raise Skip('go is not on PATH')
    repo = make_repo(tmp / 'repo')
    r = subprocess.run([sys.executable, str(HERE / 'coverage_map.py'), 'go', '--repo', str(repo), '--keep',
                        str(tmp / 'keep'), '--out', str(tmp / 'map.json')], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-600:] + r.stderr[-600:]
    out = json.loads((tmp / 'map.json').read_text(encoding='utf-8'))
    assert 'pkg/fares/fares.go' in out['files'], list(out['files'])
    tests = {out['tests'][i].split('::')[0] for i in out['files']['pkg/fares/fares.go']['8']}
    assert {'pkg/fares/fares_test.go', 'pkg/api/api_test.go'} <= tests, tests
    assert not (repo / 'c.out').exists() and not list(repo.rglob('*.out')), 'wrote into the repository'


def case_render_go_run_line(tmp: Path) -> None:
    """--brief shows a go test command's package arguments, not "<N test files>" in their place."""
    rep = {'metadata': {'repo': 'harbour', 'mode': 'strict', 'change': {'diff': 'c.diff'}},
           'summary': {'selected': 30, 'total_tests': 40, 'blind_spots': 0, 'evidence_completeness': 1.0,
                       'headline': 'Thirty of forty test files.', 'recall_status': 'not_assessed'},
           'command': {'commands': [{'package': '.', 'cwd': '.', 'framework': 'go-testing', 'full_suite': False,
                                     'argv': ['go', 'test', '-race', './pkg/api', './pkg/fares'], 'tests': 30,
                                     'args': 2, 'part': 1, 'parts': 1}], 'split_into': 1},
           'selected': [], 'blind_spots': [], 'sources': [], 'changes': [], 'recommendations': {'full_suite': 'no'}}
    p = write(tmp / 'r.json', json.dumps(rep))
    r = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--brief', '--color', 'never'],
                       capture_output=True, text=True)
    assert '(cd . && go test -race ./pkg/api ./pkg/fares)' in r.stdout, r.stdout + r.stderr


CASES = [case_import_graph_go, case_select_go_change, case_unmapped_go_widens_within_go, case_naming_same_package,
         case_history_gotestsum_files, case_replay_skips_and_counts_packages, case_replay_from_git_commits,
         case_baseline_failures, case_coverage_map_go_profiles, case_coverage_map_go_run, case_render_go_run_line]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--only')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)
    failed = skipped = 0
    for case in CASES:
        name = case.__name__[5:]
        if args.only and args.only not in name:
            continue
        with tempfile.TemporaryDirectory(prefix='tis-self-') as d:
            try:
                case(Path(d))
                print(f'ok    {name}')
            except Skip as exc:
                skipped += 1
                print(f'SKIP  {name}: {exc}')
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f'FAIL  {name}: {type(exc).__name__}: {str(exc)[:400]}')
                if args.verbose:
                    traceback.print_exc()
    print(f'{"FAILED" if failed else "passed"}: {failed} failed, {skipped} skipped')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
