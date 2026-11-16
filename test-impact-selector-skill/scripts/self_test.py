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


def case_history_commit_is_never_an_option(tmp: Path) -> None:
    """A history change whose commit or base is a git option (`--output=<file>`) is refused before git runs,
    and nothing is written: commit ids reach git only as revisions."""
    if shutil.which('git') is None:
        raise Skip('git is not on PATH')
    import select_tests
    repo = make_repo(tmp / 'repo')
    git(repo, 'init', '-q')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'base')
    marker = tmp / 'qa_marker_written_by_git.txt'
    for field, value in (('commit', f'--output={marker.as_posix()}'), ('base', f'--output={marker.as_posix()}')):
        ch = {'id': 'H1', 'commit': 'HEAD', 'runs': []}
        ch[field] = value
        hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': [ch]}))
        try:
            select_tests.load_history(hist, repo)
            refused = False
        except SystemExit as e:
            refused = 'not a commit' in str(e)
        assert not marker.exists(), f'{field} {value!r} reached git as an option and wrote {marker.name}'
        assert refused, f'a history {field} that is a git option should be refused'
    # a real commit (full hash, short hash, or a ref) still works
    write(repo / 'pkg/fares/fares.go', FARES_CHANGED)
    git(repo, 'commit', '-q', '-am', 'change')
    for c in (git(repo, 'rev-parse', 'HEAD').strip(), git(repo, 'rev-parse', '--short', 'HEAD').strip(), 'HEAD'):
        hist = write(tmp / 'hist' / 'history.json', json.dumps({'changes': [{'id': 'H1', 'commit': c, 'runs': []}]}))
        got = select_tests.load_history(hist, repo)
        assert got[0]['changed'] == ['pkg/fares/fares.go'], (c, got[0]['changed'])


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


# A server-rendered Python web app ("ferry timetable"): views render Jinja templates by name, templates
# extend and include each other and link a stylesheet; one test file never touches the app.
WEB = {
    'app.py': ('from flask import Flask, render_template\n\napp = Flask(__name__)\n\n\n@app.route("/")\n'
               'def timetable():\n    rows = [1, 2]\n    return render_template("timetable.html", rows=rows)\n\n\n'
               '@app.route("/about")\ndef about():\n    return render_template("about.html")\n'),
    'templates/base.html': ('<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'site.css\') }}">\n'
                            '{% block body %}{% endblock %}\n'),
    'templates/timetable.html': '{% extends "base.html" %}\n{% block body %}{% include "partials/row.html" %}{% endblock %}\n',
    'templates/partials/row.html': '<tr><td>{{ row }}</td></tr>\n',
    'templates/about.html': '<p>About the ferries</p>\n',
    'static/site.css': 'body { margin: 0; }\n',
    'sailings/route-1.json': '{"from": "A", "to": "B"}\n',
    'sailings/route-2.json': '{"from": "B", "to": "C"}\n',
    'tests/test_timetable.py': 'from app import app\n\n\ndef test_timetable():\n    assert app.test_client().get("/").status_code == 200\n',
    'tests/test_about.py': 'from app import app\n\n\ndef test_about():\n    assert app.test_client().get("/about").status_code == 200\n',
    'tests/test_maths.py': 'def test_add():\n    assert 1 + 1 == 2\n',
}


def web_select(tmp: Path, rel: str, new: str, **kw) -> dict:
    import select_tests
    repo = make_repo(tmp / 'repo', WEB)
    d = write(tmp / f'{rel.replace("/", "_")}.diff', diff_of(rel, WEB[rel], new))
    return select_tests.select(repo, d, **kw)


def case_template_render_links_views(tmp: Path) -> None:
    """A Jinja template is linked to the view module that renders it by name (render_template("x.html")), and a
    partial to the pages that include or extend it: a template edit selects the tests that reach those views,
    not the whole suite; with a coverage map, the test that ran the rendering view is coverage evidence."""
    import import_graph
    repo = make_repo(tmp / 'g', WEB)
    g = import_graph.build(repo)
    reach = sorted(g.tests_reaching('templates/partials/row.html'))
    assert reach == ['tests/test_about.py', 'tests/test_timetable.py'], reach
    sel = web_select(tmp, 'templates/partials/row.html', '<tr><td>{{ row }}!</td></tr>\n')
    files = sorted(s['file'] for s in sel['selected'])
    assert 'tests/test_maths.py' not in files, f'a template change widened to tests that never render it: {files}'
    assert files == ['tests/test_about.py', 'tests/test_timetable.py'], files
    line = WEB['app.py'].splitlines().index('    rows = [1, 2]') + 1
    cov = write(tmp / 'cov.json', json.dumps({
        'format': 'coverage.py-json', 'package': '.', 'tests': ['tests/test_timetable.py::test_timetable'],
        'files': {'app.py': {str(line): [0], str(line + 1): [0]}},
        'collected': {'file_hashes': {}}, 'summary': {}}))
    sel = web_select(tmp / 'c', 'templates/partials/row.html', '<tr><td>{{ row }}!</td></tr>\n', coverage=[cov])
    tt = next(s for s in sel['selected'] if s['file'] == 'tests/test_timetable.py')
    assert tt['strength'] == 3 and any(r['source'] == 'coverage' and 'timetable' in r['evidence'] for r in tt['reasons']), tt


def case_static_reaches_rendering_tests(tmp: Path) -> None:
    """A stylesheet linked from a template (url_for('static', filename=...)) widens to the tests that render a page
    linking it when the package has no e2e or visual tests, not to the whole suite."""
    sel = web_select(tmp, 'static/site.css', 'body { margin: 1px; }\n')
    spots = [b for b in sel['blind_spots'] if b['path'] == 'static/site.css']
    assert spots, sel['blind_spots']
    widened = spots[0]['widened_to']
    assert 'tests/test_maths.py' not in widened, f'a stylesheet change widened to the whole suite: {widened}'
    assert widened == ['tests/test_about.py', 'tests/test_timetable.py'], widened


def case_data_file_in_root_package(tmp: Path) -> None:
    """A data file in a directory of a single-package repository is data_or_schema, not repository-level
    global_config, so balanced mode labels it instead of running the whole suite."""
    sel = web_select(tmp, 'sailings/route-1.json', '{"from": "A", "to": "D"}\n')
    kinds = {c['path']: c['kind'] for c in sel['changes']}
    assert kinds['sailings/route-1.json'] == 'data_or_schema', kinds


def case_coverage_core_ctrace(tmp: Path) -> None:
    """coverage_map.py generate runs pytest with COVERAGE_CORE=ctrace (sys.monitoring drops per-test contexts on
    Python 3.12+), keeping a core the caller set, and says so in the recorded command."""
    import coverage_map
    env = coverage_map.coverage_env({'PATH': 'x'}, Path(tmp / '.coverage'))
    assert env.get('COVERAGE_CORE') == 'ctrace', env
    assert coverage_map.coverage_env({'COVERAGE_CORE': 'pytrace'}, Path(tmp / '.coverage'))['COVERAGE_CORE'] == 'pytrace'


def case_normalise_refuses_lost_contexts(tmp: Path) -> None:
    """normalise --junit refuses a coverage.py map whose per-test contexts name far fewer tests than the run
    executed (contexts lost to the sys.monitoring core), instead of accepting it as complete."""
    repo = make_repo(tmp / 'repo', WEB)
    data = {'meta': {'version': '7.17.3', 'show_contexts': True},
            'files': {'app.py': {'contexts': {'8': ['tests/test_timetable.py::test_timetable|run']}}}}
    cov = write(tmp / 'coverage.json', json.dumps(data))
    cases = ''.join(f'<testcase classname="tests.test_t" name="test_{i}" file="tests/test_t.py"/>' for i in range(10))
    junit = write(tmp / 'junit.xml', f'<testsuites><testsuite name="pytest" tests="10">{cases}</testsuite></testsuites>')
    r = subprocess.run([sys.executable, str(HERE / 'coverage_map.py'), 'normalise', str(cov), '--repo', str(repo),
                        '--junit', str(junit), '--out', str(tmp / 'map.json')], capture_output=True, text=True)
    assert r.returncode != 0 and 'COVERAGE_CORE' in r.stderr + r.stdout, r.stdout[-600:] + r.stderr[-600:]


def case_committed_only_hashes_head(tmp: Path) -> None:
    """--committed-only records the hash of the HEAD tree it analysed, so an uncommitted edit in the checkout
    (another tool's scratch change) does not change repo_code_hash."""
    if shutil.which('git') is None:
        raise Skip('git is not on PATH')
    import select_tests
    repo = make_repo(tmp / 'repo', WEB)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'base')
    git(repo, 'checkout', '-q', '-b', 'work')
    write(repo / 'templates/about.html', '<p>About the ferry company</p>\n')
    git(repo, 'commit', '-q', '-am', 'change')
    a = select_tests.select(repo, base='main', committed_only=True)
    write(repo / 'app.py', WEB['app.py'] + '\n# scratch edit\n')
    b = select_tests.select(repo, base='main', committed_only=True)
    assert a['repo_code_hash'] == b['repo_code_hash'], 'repo_code_hash follows the working tree in committed-only mode'


# A Cargo workspace ("tide tables") beside Python helper scripts whose unittest tests add scripts/ to sys.path.
# tides-core is a library with unit tests in #[cfg(test)] modules, an integration test, a feature-gated test and a
# bench; tides-cli is a clap-style binary depending on tides-core by path, with an integration test, a shared test
# module and a helper binary under tests/fixtures; tides-chart is an unrelated crate.
RUST = {
    'Cargo.toml': '[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n',
    'Cargo.lock': '# lockfile\nversion = 3\n',
    'crates/tides-core/Cargo.toml': ('[package]\nname = "tides-core"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                     '[features]\ndefault = []\ngauge = []\n'),
    'crates/tides-core/src/lib.rs': ('pub mod height;\npub mod gauge;\n#[cfg(feature = "gauge")]\nmod probe;\n'
                                     '#[cfg(any(feature = "gauge", test))]\nmod calib;\n\npub fn double(x: i32) -> i32 { x * 2 }\n\n'
                                     '#[cfg(test)]\nmod tests {\n    #[test]\n    fn doubles() { assert_eq!(super::double(2), 4); }\n}\n'),
    'crates/tides-core/src/height.rs': ('pub fn metres(ft: f64) -> f64 { ft * 0.3048 }\n\n#[cfg(test)]\nmod tests {\n'
                                        '    #[test]\n    fn converts() { assert!(super::metres(1.0) > 0.3); }\n}\n'),
    'crates/tides-core/src/gauge.rs': ('pub fn read() -> i32 { 1 }\n\n#[cfg(all(test, feature = "gauge"))]\nmod hw {\n'
                                       '    #[test]\n    fn reads_gauge() { assert_eq!(super::read(), 1); }\n}\n'),
    'crates/tides-core/src/calib.rs': '#[cfg(test)]\nmod tests {\n    #[test]\n    fn calibrates() {}\n}\n',
    'crates/tides-core/src/probe.rs': '#[cfg(test)]\nmod tests {\n    #[test]\n    fn probes() {}\n}\n',
    'crates/tides-core/tests/tables.rs': '#[test]\nfn table_doubles() { assert_eq!(tides_core::double(3), 6); }\n',
    'crates/tides-core/benches/lookup.rs': 'fn main() { let _ = tides_core::double(1); }\n',
    'crates/tides-cli/Cargo.toml': ('[package]\nname = "tides-cli"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                    '[[bin]]\nname = "tides"\npath = "src/main.rs"\n\n'
                                    '[dependencies]\ntides-core = { path = "../tides-core" }\n'),
    'crates/tides-cli/src/main.rs': 'mod report;\n\nfn main() { println!("{}", report::line(2)); }\n',
    'crates/tides-cli/src/report.rs': ('pub fn line(n: i32) -> String { format!("{}", tides_core::double(n)) }\n\n'
                                       '#[cfg(test)]\nmod tests {\n    #[test]\n    fn formats() { assert_eq!(super::line(2), "4"); }\n}\n'),
    'crates/tides-cli/tests/cli.rs': 'mod common;\n\n#[test]\nfn prints_help() { common::setup(); }\n',
    'crates/tides-cli/tests/common/mod.rs': 'pub fn setup() {}\n',
    'crates/tides-cli/tests/fixtures/fake_gauge.rs': 'fn main() { println!("7"); }\n',
    'crates/tides-chart/Cargo.toml': '[package]\nname = "tides-chart"\nversion = "0.1.0"\nedition = "2021"\n',
    'crates/tides-chart/src/lib.rs': ('pub fn bar() -> u8 { 1 }\n\n#[cfg(test)]\nmod tests {\n    #[test]\n'
                                      '    fn draws() { assert_eq!(super::bar(), 1); }\n}\n'),
    'scripts/scale_recipe.py': 'def scale(q, n):\n    return q * n\n',
    'scripts/weather_log.py': 'def mean(xs):\n    return sum(xs) / len(xs)\n',
    'tests/test_scale_recipe.py': ('import sys\nimport unittest\nfrom pathlib import Path\n\n'
                                   'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))\n\n'
                                   'from scale_recipe import scale  # noqa: E402\n\n\nclass ScaleTest(unittest.TestCase):\n'
                                   '    def test_double(self):\n        self.assertEqual(scale(2, 2), 4)\n'),
    'tests/test_weather_log.py': ('import os\nimport sys\nimport unittest\n\n'
                                  'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))\n\n'
                                  'import weather_log  # noqa: E402\n\n\nclass MeanTest(unittest.TestCase):\n'
                                  '    def test_mean(self):\n        self.assertEqual(weather_log.mean([1, 3]), 2)\n'),
    '.github/workflows/ci.yml': ('jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n'
                                 '      - run: cargo test --workspace --locked\n'
                                 '      - run: python -m unittest discover -s tests\n'),
}
CORE_LIB_TESTS = ['crates/tides-core/src/calib.rs', 'crates/tides-core/src/height.rs', 'crates/tides-core/src/lib.rs', 'crates/tides-core/src/probe.rs',
                  'crates/tides-core/tests/tables.rs']
CLI_TESTS = ['crates/tides-cli/src/report.rs', 'crates/tides-cli/tests/cli.rs']
PY_TESTS = ['tests/test_scale_recipe.py', 'tests/test_weather_log.py']


def rust_select(tmp: Path, rel: str, new: str, files: dict[str, str] = RUST, **kw) -> dict:
    import select_tests
    repo = make_repo(tmp / 'repo', files)
    d = write(tmp / f'{rel.replace("/", "_")}.diff', diff_of(rel, files[rel], new))
    return select_tests.select(repo, d, **kw)


def case_rust_graph(tmp: Path) -> None:
    """A Cargo workspace is read with cargo_project: files with #[test] functions in #[cfg(test)] modules and
    top-level tests/*.rs are test files; a lib file reaches its crate's unit and integration tests and those of the
    crates that depend on it by path (never an unrelated crate); tests/fixtures helper binaries and benches are not
    tests."""
    import import_graph
    g = import_graph.build(make_repo(tmp / 'repo', RUST))
    assert g.languages.get('rust') == 'parsed', g.languages
    tests = [t for t in g.tests() if t.endswith('.rs')]
    assert 'crates/tides-cli/tests/fixtures/fake_gauge.rs' not in tests, tests
    assert 'crates/tides-core/benches/lookup.rs' not in tests, tests
    assert 'crates/tides-cli/tests/common/mod.rs' not in tests, tests
    assert 'crates/tides-core/src/gauge.rs' in tests, tests
    reach = sorted(g.tests_reaching('crates/tides-core/src/height.rs'))
    assert reach == sorted(CORE_LIB_TESTS + ['crates/tides-core/src/gauge.rs'] + CLI_TESTS), reach
    reach = sorted(g.tests_reaching('crates/tides-cli/src/report.rs'))     # a binary's module: its integration tests run it
    assert reach == CLI_TESTS, reach
    reach = sorted(g.tests_reaching('crates/tides-cli/tests/common/mod.rs'))
    assert reach == ['crates/tides-cli/tests/cli.rs'], reach


def case_select_rust_change(tmp: Path) -> None:
    """A change to a library file selects its crate's tests and the tests of crates that reach it, with cargo
    commands built from libtest ids (`cargo test -p <crate> --lib --test <t> -- --exact ...`); no Python test, no
    unrelated crate, and no unmapped blind spot."""
    sel = rust_select(tmp, 'crates/tides-core/src/height.rs',
                      RUST['crates/tides-core/src/height.rs'].replace('0.3048', '0.305'))
    files = sorted(s['file'] for s in sel['selected'])
    assert files == sorted(CORE_LIB_TESTS + ['crates/tides-core/src/gauge.rs'] + CLI_TESTS), files
    assert not [b for b in sel['blind_spots'] if b['kind'] == 'unmapped_source'], sel['blind_spots']
    cmds = {c['package']: c for c in sel['command']['commands']}
    assert set(cmds) == {'crates/tides-core', 'crates/tides-cli'}, sel['command']
    assert all(c['framework'] == 'cargo-test' and c['cwd'] == '.' for c in cmds.values()), cmds
    assert cmds['crates/tides-core']['argv'] == ['cargo', 'test', '-p', 'tides-core'], cmds['crates/tides-core']
    cli = cmds['crates/tides-cli']['argv']
    assert cli == ['cargo', 'test', '-p', 'tides-cli'], cli
    gated = sel['command'].get('feature_gated') or []
    gauge = next((x for x in gated if x['feature'] == 'gauge' and x['package'] == 'crates/tides-core'), None)
    # gated in its own file (cfg on the test module) and by the cfg on its `mod probe;` declaration in lib.rs
    assert gauge and sorted(gauge['tests']) == ['tides_core::gauge::hw::reads_gauge', 'tides_core::probe::tests::probes'], gated
    assert not sel['command']['no_runner'], sel['command']


def case_rust_exact_ids(tmp: Path) -> None:
    """A selection of some of a crate's test files gives `-p <crate> --bin|--test <target> -- --exact <paths>`, the
    same arguments run_tests.py's cargo preset builds from those ids."""
    import run_tests
    sel = rust_select(tmp, 'crates/tides-cli/tests/common/mod.rs', 'pub fn setup() { let _ = 1; }\n')
    files = sorted(s['file'] for s in sel['selected'])
    assert files == ['crates/tides-cli/tests/cli.rs'], files
    cmd = sel['command']['commands']
    assert len(cmd) == 1, cmd
    argv = cmd[0]['argv']
    assert argv == ['cargo', 'test', '-p', 'tides-cli', '--test', 'cli', '--', '--exact', 'prints_help'], argv
    sel_args, names, exact = run_tests.cargo_ids(cmd[0]['test_ids'], tmp / 'repo')
    assert argv[2:argv.index('--')] == sel_args and argv[argv.index('--exact') + 1:] == names, (sel_args, names)


def case_rust_bench_and_fixture(tmp: Path) -> None:
    """A bench is not source that tests run (cargo test does not run benches): it selects nothing and is no
    unmapped blind spot. A tests/fixtures helper binary is not a test: a change to it widens to its crate's tests."""
    sel = rust_select(tmp, 'crates/tides-core/benches/lookup.rs', RUST['crates/tides-core/benches/lookup.rs'] + '// more\n')
    kinds = {c['path']: c['kind'] for c in sel['changes']}
    assert kinds['crates/tides-core/benches/lookup.rs'] == 'bench', kinds
    assert not sel['selected'] and not sel['blind_spots'], (sel['selected'], sel['blind_spots'])
    sel = rust_select(tmp / 'f', 'crates/tides-cli/tests/fixtures/fake_gauge.rs', 'fn main() { println!("8"); }\n')
    spot = next(b for b in sel['blind_spots'] if b['path'].endswith('fake_gauge.rs'))
    assert sorted(spot['widened_to']) == CLI_TESTS, spot
    assert 'helper binary' in spot['note'].lower(), spot['note']


def case_rust_bin_module_selects_integration_tests(tmp: Path) -> None:
    """A change to a module of a binary only (src/report.rs under src/main.rs, no library) selects the binary's own
    unit tests and the crate's integration tests, which cargo builds the binary for and which may run it
    (CARGO_BIN_EXE_<name>, assert_cmd): targets_reaching's 'runs_bin'."""
    sel = rust_select(tmp, 'crates/tides-cli/src/report.rs',
                      RUST['crates/tides-cli/src/report.rs'].replace('"{}"', '"{} m"'))
    files = sorted(s['file'] for s in sel['selected'])
    assert files == CLI_TESTS, files


RSTEST_CLI = ('mod common;\n\nuse rstest::rstest;\n\n#[test]\nfn prints_help() { common::setup(); }\n\n'
              '#[rstest]\n#[case(1)]\n#[case(2)]\nfn scales(#[case] n: i32) { assert!(n > 0); }\n\n'
              '#[test_case::test_case(3 ; "three")]\nfn rounds(n: i32) { assert!(n > 0); }\n')


def case_rust_parameterised_tests_prefix_matched(tmp: Path) -> None:
    """#[rstest] and #[test_case] generate child tests (`scales::case_1`), so `--exact scales` would run none of
    them: a partial selection runs them as name filters without --exact, in their own command with a note, and
    keeps --exact for plain #[test] functions."""
    files = dict(RUST)
    files['crates/tides-cli/tests/cli.rs'] = RSTEST_CLI
    sel = rust_select(tmp, 'crates/tides-cli/tests/common/mod.rs', 'pub fn setup() { let _ = 1; }\n', files=files)
    cmds = [c for c in sel['command']['commands'] if c['package'] == 'crates/tides-cli']
    argvs = sorted(c['argv'] for c in cmds)
    assert argvs == [['cargo', 'test', '-p', 'tides-cli', '--test', 'cli', '--', '--exact', 'prints_help'],
                     ['cargo', 'test', '-p', 'tides-cli', '--test', 'cli', '--', 'rounds', 'scales']], argvs
    pre = next(c for c in cmds if '--exact' not in c['argv'])
    assert 'parameteris' in pre.get('note', '') and '--exact' in pre['note'], pre


def case_rust_default_features_transitive(tmp: Path) -> None:
    """Features the default set enables transitively (default = ["std"], std = ["alloc"]) are enabled: a test
    under cfg(feature = "alloc") is not feature_gated; one under a feature nothing enables still is."""
    files = dict(RUST)
    files['crates/tides-core/Cargo.toml'] = ('[package]\nname = "tides-core"\nversion = "0.1.0"\nedition = "2021"\n\n'
                                             '[features]\ndefault = ["std"]\nstd = ["alloc"]\nalloc = []\ngauge = []\n')
    files['crates/tides-core/src/lib.rs'] = 'pub mod buffer;\n' + RUST['crates/tides-core/src/lib.rs']
    files['crates/tides-core/src/buffer.rs'] = ('pub fn fill() -> u8 { 1 }\n\n#[cfg(all(test, feature = "alloc"))]\n'
                                                'mod tests {\n    #[test]\n    fn fills() { assert_eq!(super::fill(), 1); }\n}\n')
    sel = rust_select(tmp, 'crates/tides-core/src/height.rs',
                      RUST['crates/tides-core/src/height.rs'].replace('0.3048', '0.305'), files=files)
    assert 'crates/tides-core/src/buffer.rs' in [s['file'] for s in sel['selected']], sel['selected']
    gated = {g['feature'] for g in sel['command'].get('feature_gated') or []}
    assert gated == {'gauge'}, sel['command'].get('feature_gated')


def case_rust_cfg_keyed_by_target(tmp: Path) -> None:
    """The cfg on a `mod tests;` declaration in the library gates only the library's tests module, never an inline
    `mod tests` of the binary with the same module path."""
    import import_graph
    files = dict(RUST)
    files['crates/tides-cli/Cargo.toml'] = RUST['crates/tides-cli/Cargo.toml'].replace(
        '[[bin]]', '[features]\nslow = []\n\n[[bin]]')
    files['crates/tides-cli/src/lib.rs'] = 'pub fn one() -> u8 { 1 }\n\n#[cfg(feature = "slow")]\nmod tests;\n'
    files['crates/tides-cli/src/tests.rs'] = '#[test]\nfn slow_one() { assert_eq!(super::one(), 1); }\n'
    files['crates/tides-cli/src/main.rs'] = (RUST['crates/tides-cli/src/main.rs'] +
                                             '\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn runs() {}\n}\n')
    g = import_graph.build(make_repo(tmp / 'repo', files))
    feats = {t['id']: t['features'] for ts in g.rust_tests.values() for t in ts}
    assert feats.get('tides::tests::runs') == [], feats
    assert feats.get('tides_cli::tests::slow_one') == ['slow'], feats


def case_rust_files_scanned_once(tmp: Path) -> None:
    """Building the graph scans each Rust file once: the shared reader's module trees are reused, not walked
    again with a scan cache of the graph's own."""
    import collections
    import import_graph
    import cargo_project
    counts: collections.Counter = collections.Counter()
    orig = cargo_project.scan_file

    def counting(path, rel):
        counts[Path(path).resolve()] += 1
        return orig(path, rel)
    cargo_project.scan_file = counting
    try:
        import_graph.build(make_repo(tmp / 'repo', RUST))
    finally:
        cargo_project.scan_file = orig
    assert counts and max(counts.values()) == 1, {str(k): v for k, v in counts.items() if v > 1}


def case_root_cargo_toml_widens_to_rust_only(tmp: Path) -> None:
    """The root workspace Cargo.toml (or Cargo.lock) widens to every Rust test, not to the Python tests beside it;
    a Python script change never widens to Rust tests."""
    sel = rust_select(tmp, 'Cargo.toml', RUST['Cargo.toml'].replace('"2"', '"3"'))
    spot = next(b for b in sel['blind_spots'] if b['path'] == 'Cargo.toml')
    assert spot['required_scope'] == 'language', spot
    assert spot['widened_to'] and all(t.endswith('.rs') for t in spot['widened_to']), spot['widened_to']
    assert not any(s['file'].endswith('.py') for s in sel['selected']), [s['file'] for s in sel['selected']]
    py = dict(RUST)
    py['scripts/stray.py'] = 'X = 1\n'
    sel = rust_select(tmp / 'p', 'scripts/stray.py', 'X = 2\n', files=py)
    assert not any(s['file'].endswith('.rs') for s in sel['selected']), [s['file'] for s in sel['selected']]


def case_python_sys_path_imports(tmp: Path) -> None:
    """Test files that insert scripts/ into sys.path and then import a script by its bare name reach that script
    (pathlib and os.path forms), so a script change selects them instead of reporting "No tests found"."""
    import import_graph
    g = import_graph.build(make_repo(tmp / 'repo', RUST))
    assert sorted(g.tests_reaching('scripts/scale_recipe.py')) == ['tests/test_scale_recipe.py'], g.files['tests/test_scale_recipe.py']
    assert sorted(g.tests_reaching('scripts/weather_log.py')) == ['tests/test_weather_log.py'], g.files['tests/test_weather_log.py']
    sel = rust_select(tmp / 's', 'scripts/scale_recipe.py', 'def scale(q, n):\n    return q * n * 1\n')
    assert [s['file'] for s in sel['selected']] == ['tests/test_scale_recipe.py'], sel['selected']
    assert not sel['blind_spots'], sel['blind_spots']


def case_python_sys_path_unknown_recorded(tmp: Path) -> None:
    """When sys.path is set from something the graph cannot evaluate, the bare import of a repository module is
    recorded as unresolved (never a silent missing edge), and the blind spot says so instead of "No tests found"."""
    import import_graph
    files = dict(RUST)
    files['tests/test_weather_log.py'] = ('import os\nimport sys\nimport unittest\n\nsys.path.insert(0, os.environ["LOG_DIR"])\n\n'
                                          'import weather_log  # noqa: E402\n\n\nclass MeanTest(unittest.TestCase):\n'
                                          '    def test_mean(self):\n        self.assertEqual(weather_log.mean([1, 3]), 2)\n')
    g = import_graph.build(make_repo(tmp / 'repo', files))
    unresolved = g.files['tests/test_weather_log.py']['unresolved']
    assert any(u['spec'] == 'weather_log' for u in unresolved), g.files['tests/test_weather_log.py']
    sel = rust_select(tmp / 's', 'scripts/weather_log.py', 'def mean(xs):\n    return sum(xs) / max(len(xs), 1)\n', files=files)
    spot = next(b for b in sel['blind_spots'] if b['path'] == 'scripts/weather_log.py')
    assert 'No tests found' not in spot['note'] and 'tests/test_weather_log.py' in spot['note'], spot['note']


def case_blind_spot_wording(tmp: Path) -> None:
    """An unmapped file whose only link is weak (naming) never also says "No tests found for it"."""
    files = dict(WEB)
    files['geo.py'] = 'def hops(n):\n    return n - 1\n'
    files['tests/test_geo.py'] = 'def test_nothing():\n    assert True\n'
    import select_tests
    repo = make_repo(tmp / 'repo', files)
    d = write(tmp / 'g.diff', diff_of('geo.py', files['geo.py'], 'def hops(n):\n    return n - 2\n'))
    sel = select_tests.select(repo, d)
    spot = next(b for b in sel['blind_spots'] if b['path'] == 'geo.py')
    assert 'Only weak evidence' in spot['note'] and 'No tests found' not in spot['note'], spot['note']


def case_unittest_command(tmp: Path) -> None:
    """Python tests run with the repository's runner: unittest when CI runs unittest (file arguments for a subset,
    CI's own command for the whole suite), not pytest."""
    sel = rust_select(tmp, 'scripts/scale_recipe.py', 'def scale(q, n):\n    return q * n * 1\n')
    py = [c for c in sel['command']['commands'] if c['package'] == '.']
    assert len(py) == 1 and py[0]['framework'] == 'unittest', sel['command']
    assert py[0]['argv'] == ['python', '-m', 'unittest', 'tests/test_scale_recipe.py'], py[0]
    files = dict(RUST)
    files['scripts/shared_util.py'] = 'K = 1\n'
    for t in PY_TESTS:
        files[t] = files[t].replace('import unittest\n', 'import unittest\nimport shared_util  # noqa\n', 1)
    sel = rust_select(tmp / 'f', 'scripts/shared_util.py', 'K = 2\n', files=files)
    py = [c for c in sel['command']['commands'] if c['package'] == '.']
    assert len(py) == 1 and py[0]['full_suite'] and py[0]['argv'] == ['python', '-m', 'unittest', 'discover', '-s', 'tests'], py


KOTLIN = {
    'settings.gradle.kts': 'rootProject.name = "logger"\n',
    'build.gradle.kts': 'plugins { kotlin("jvm") }\n',
    'src/main/kotlin/station/Logger.kt': 'package station\n\nfun log(x: Int) = x + 1\n',
    'src/main/kotlin/station/Rain.kt': 'package station\n\nfun rain(x: Int) = x * 2\n',
    'tools/check.py': 'def ok():\n    return True\n',
    'tools/test_check.py': 'from check import ok\n\n\ndef test_ok():\n    assert ok()\n',
}


def case_no_parser_language_warns(tmp: Path) -> None:
    """A change in a language with no import parser sets selection.language_coverage.warning, and when its package
    has no test files that could be found, the blind spot falls back to the full suite (never 0 tests)."""
    import select_tests
    repo = make_repo(tmp / 'repo', KOTLIN)
    rel = 'src/main/kotlin/station/Rain.kt'
    d = write(tmp / 'k.diff', diff_of(rel, KOTLIN[rel], KOTLIN[rel].replace('* 2', '* 3')))
    sel = select_tests.select(repo, d)
    lc = sel.get('language_coverage') or {}
    assert lc.get('warning') and 'kotlin' in lc['warning'].lower(), lc
    assert lc.get('changed_unparsed') == ['kotlin'], lc
    spot = next(b for b in sel['blind_spots'] if b['path'] == rel)
    assert spot['widened_to'], spot


def case_validator_requires_language_limitation(tmp: Path) -> None:
    """The validator rejects a report whose selection warns that a changed language has no import parser when no
    limitation names that language."""
    import validate_impact_report as v
    rep = {'metadata': {'mode': 'strict', 'change': {}}, 'sources': [], 'selected': [], 'changes': [], 'blind_spots': [],
           'total_tests': 0, 'not_selected_count': 0, 'command': {'commands': [], 'split_into': 0},
           'recommendations': {'full_suite': 'Run the full suite on merge.'}, 'limitations': ['Coverage was not given.'],
           'summary': {'headline': 'Nothing: 0 of 0 test files.'},
           'language_coverage': {'warning': 'A changed kotlin file has no import parser.', 'changed_unparsed': ['kotlin']}}
    errs = v.check(rep, None, None, None)
    assert any('kotlin' in e for e in errs), errs
    rep['limitations'].append('Kotlin has no import parser here, so the change was widened, not traced.')
    errs = v.check(rep, None, None, None)
    assert not any('kotlin' in e.lower() for e in errs), errs


def case_render_rust_and_language_warning(tmp: Path) -> None:
    """--brief shows a language_coverage warning, feature-gated tests the commands do not run, a `language` blind
    spot, and counts a cargo command's --exact test names (not its test files)."""
    ids = [f'tides_core::m{i}::tests::t' for i in range(25)]
    rep = {'metadata': {'repo': 'tides', 'mode': 'strict', 'change': {'diff': 'c.diff'}},
           'summary': {'selected': 2, 'total_tests': 4, 'blind_spots': 1, 'evidence_completeness': 0.5,
                       'headline': 'Two of four test files.', 'recall_status': 'not_assessed'},
           'command': {'commands': [{'package': 'crates/tides-core', 'cwd': '.', 'framework': 'cargo-test', 'full_suite': False,
                                     'argv': ['cargo', 'test', '-p', 'tides-core', '--lib', '--', '--exact',
                                              *[i.partition('::')[2] for i in ids]],
                                     'tests': 2, 'test_ids': ids, 'part': 1, 'parts': 1}], 'split_into': 1,
                       'feature_gated': [{'package': 'crates/tides-core', 'crate': 'tides-core', 'feature': 'gauge',
                                          'tests': ['tides_core::gauge::hw::reads_gauge']}]},
           'language_coverage': {'parsed': ['rust'], 'no_parser': ['kotlin'], 'changed_unparsed': ['kotlin'],
                                 'warning': 'One changed kotlin file has no import parser here.'},
           'selected': [], 'sources': [], 'changes': [], 'recommendations': {'full_suite': 'on merge'},
           'blind_spots': [{'id': 'B1', 'path': 'Cargo.toml', 'kind': 'global_config', 'action': 'language',
                            'required_scope': 'language', 'widened_to': ['a.rs'], 'would_widen_to': ['a.rs'],
                            'note': 'Cargo workspace manifest.'}]}
    p = write(tmp / 'r.json', json.dumps(rep))
    r = subprocess.run([sys.executable, str(HERE / 'render_report.py'), str(p), '--brief', '--color', 'never'],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert '<25 test names>' in out, out
    assert 'kotlin file has no import parser' in out, out
    assert '--features gauge' in out, out
    assert 'every test of its language' in out, out


def case_selection_speed(tmp: Path) -> None:
    """Timing check: on a 400-module repository the selection reads each unchanged file once for both trees
    (the base tree reuses the changed tree's reads), runs package detection once when no manifest changed, and
    finishes within the time budget."""
    import time
    import import_graph, select_tests
    files = {'pyproject.toml': '[project]\nname = "logger"\n'}
    for i in range(200):
        files[f'logger/m{i}.py'] = f'from logger import m{max(i - 1, 0)}\n\n\ndef f{i}(x):\n    return x + {i}\n'
        files[f'tests/test_m{i}.py'] = f'from logger.m{i} import f{i}\n\n\ndef test_f():\n    assert f{i}(0) == {i}\n'
    files['logger/__init__.py'] = ''
    repo = make_repo(tmp / 'repo', files)
    d = write(tmp / 'c.diff', diff_of('logger/m5.py', files['logger/m5.py'], files['logger/m5.py'].replace('+ 5', '+ 6')))
    import detect_conventions
    runs = []
    orig = detect_conventions.Detector.run

    def counting(self, *a, **k):
        runs.append(1)
        return orig(self, *a, **k)
    detect_conventions.Detector.run = counting
    import_graph.READS = 0
    try:
        t0 = time.perf_counter()
        sel = select_tests.select(repo, d)
        elapsed = time.perf_counter() - t0
    finally:
        detect_conventions.Detector.run = orig
    assert len(sel['selected']) == 195, len(sel['selected'])
    assert len(runs) == 1, f'package detection ran {len(runs)} times'
    assert import_graph.READS <= len(files) + 10, f'{import_graph.READS} source reads for {len(files)} files'
    assert elapsed < 60, f'{elapsed:.1f} s'


# A tool repository ("scantool"): scripts with their own --self-test, a self_test.py that embeds fixture source in
# strings, a product module named like a test (test_paths.py), benchmarks collected through pytest.ini python_files,
# a guide whose example validations are the repository's checks, and an examples/ folder of sample projects with
# planted red tests and their own lockfile, which are not the repository's tests.
SYS_PATH = 'import sys\nfrom pathlib import Path\n\nHERE = Path(__file__).resolve().parent\nsys.path.insert(0, str(HERE))\n'
TOOL = {
    'GUIDE.md': ('# Scantool\n\n```bash\npython tool/scripts/validate_report.py tool/examples/sample-app/report.json\n'
                 'python tool/scripts/scanner.py <repo> --out findings.json\npython tool/scripts/scanner.py --self-test\n'
                 'python tool/scripts/validate_report.py tool/examples/sample-app/report.json --write-summary\n```\n'),
    'tool/scripts/helpers.py': 'def norm(p):\n    return p.replace("\\\\", "/")\n',
    'tool/scripts/test_paths.py': ('import re\n\nTEST_DIR = re.compile(r"(^|/)tests?/")\n\n\n'
                                   'def is_test_path(p):\n    return bool(TEST_DIR.search(p))\n'),
    'tool/scripts/scanner.py': (SYS_PATH + 'import argparse\n\nfrom helpers import norm  # noqa: E402\n'
                                'from test_paths import is_test_path  # noqa: E402\n\n\n'
                                'def scan(paths):\n    return [norm(p) for p in paths if not is_test_path(p)]\n\n\n'
                                'def main(argv=None):\n    ap = argparse.ArgumentParser()\n'
                                '    ap.add_argument("--self-test", action="store_true")\n    args = ap.parse_args(argv)\n'
                                '    if args.self_test:\n        print("ok scan" if scan(["a/b.py"]) == ["a/b.py"] else "FAIL scan")\n'
                                '    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'),
    'tool/scripts/report.py': ('import sys\n\n\ndef total(xs):\n    return sum(xs)\n\n\n'
                               'if __name__ == "__main__":\n    if "--self-test" in sys.argv:\n'
                               '        print("ok total" if total([1, 2]) == 3 else "FAIL total")\n'),
    'tool/scripts/validate_report.py': (SYS_PATH + 'import json\n\nfrom helpers import norm  # noqa: E402\n\n\n'
                                        'def main():\n    data = json.loads(Path(norm(sys.argv[1])).read_text())\n'
                                        '    print("VALID" if "findings" in data else "INVALID")\n\n\n'
                                        'if __name__ == "__main__":\n    main()\n'),
    'tool/scripts/self_test.py': (SYS_PATH + 'import scanner  # noqa: E402\n\n'
                                  'NODE_FIXTURE = "import { test } from \'node:test\'\\ntest(\'x\', () => {})\\n"\n'
                                  'GO_FIXTURE = "package x\\nimport \\"testing\\"\\nfunc TestX(t *testing.T) {}\\n"\n\n\n'
                                  'def main():\n    print("ok scan" if scanner.scan(["x.py"]) == ["x.py"] else "FAIL scan")\n'
                                  '    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'),
    'tool/scripts/loader.mjs': ('// runs a module in a child process\n'
                                'export const code = (p) => `await import(${JSON.stringify(p)})`;\n'),
    'tool/examples/sample-app/requirements.txt': 'flask\n',
    'tool/examples/sample-app/app.py': 'def total(xs):\n    return sum(xs)\n',
    'tool/examples/sample-app/tests/test_app.py': ('from app import total\n\n\ndef test_total():\n'
                                                   '    assert total([1, 2]) == 4  # planted: red on purpose\n'),
    'tool/examples/sample-app/report.json': '{"findings": []}\n',
    'tool/examples/crate/Cargo.toml': '[package]\nname = "crate"\nversion = "0.1.0"\n',
    'tool/examples/crate/Cargo.lock': 'version = 3\n',
    'tool/examples/crate/src/lib.rs': ('pub fn one() -> u32 { 1 }\n\n#[cfg(test)]\nmod tests {\n    #[test]\n'
                                       '    fn one_is_one() { assert_eq!(super::one(), 1); }\n}\n'),
    'benchmarks/pytest.ini': '[pytest]\npython_files = bench_*.py\n',
    'benchmarks/bench_scan.py': ('import sys\nfrom pathlib import Path\n\n'
                                 'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tool" / "scripts"))\n'
                                 'import scanner  # noqa: E402\n\n\ndef test_scan_speed():\n'
                                 '    assert scanner.scan(["a.py"] * 100)\n'),
}
TOOL_TESTS = ['GUIDE.md:4', 'benchmarks/bench_scan.py', 'tool/scripts/report.py', 'tool/scripts/scanner.py',
              'tool/scripts/self_test.py']


def tool_select(tmp: Path, rel: str, new: str, files: dict[str, str] = TOOL, **kw) -> dict:
    import select_tests
    repo = make_repo(tmp / 'repo', files)
    d = write(tmp / f'{rel.replace("/", "_")}.diff', diff_of(rel, files[rel], new))
    return select_tests.select(repo, d, **kw)


def case_tool_repo_tests(tmp: Path) -> None:
    """In a tool repository the tests are its self-test scripts, its --self-test flags, its pytest.ini benchmarks and
    the guide's example validations; the planted tests of the sample projects under examples/ are not (they are
    listed as excluded fixture projects), and a product module named test_paths.py that defines no test is not."""
    import import_graph
    g = import_graph.build(make_repo(tmp / 'repo', TOOL))
    assert g.tests() == TOOL_TESTS, g.tests()
    assert sorted(x['dir'] for x in g.excluded) == ['tool/examples/crate', 'tool/examples/sample-app'], g.excluded
    assert g.files['tool/scripts/scanner.py'].get('self_test') == 'python tool/scripts/scanner.py --self-test'
    assert g.files['GUIDE.md:4'].get('self_test', '').startswith('python tool/scripts/validate_report.py'), g.files['GUIDE.md:4']
    frameworks = {fw for p in g.packages for fw in p['frameworks']}
    assert not frameworks & {'node-test', 'go-testing', 'cargo-test'}, frameworks
    g2 = import_graph.build(make_repo(tmp / 'repo', TOOL), include_fixtures=True)
    assert 'tool/examples/sample-app/tests/test_app.py' in g2.tests() and not g2.excluded, g2.tests()
    g3 = import_graph.build(make_repo(tmp / 'repo', TOOL), exclude=['benchmarks'])
    assert 'benchmarks/bench_scan.py' not in g3.tests() and any(x['dir'] == 'benchmarks' for x in g3.excluded), g3.excluded


def case_tool_repo_selection(tmp: Path) -> None:
    """A change to a script selects its own --self-test, the self-test script and the benchmark that import it, with
    commands that run them (never a bare pytest at the root, which would collect the planted red tests)."""
    rel = 'tool/scripts/test_paths.py'
    sel = tool_select(tmp / 'a', rel, TOOL[rel].replace('tests?', 'tests?|spec'))
    assert next(c for c in sel['changes'] if c['path'] == rel)['kind'] == 'source', sel['changes']
    files = sorted(s['file'] for s in sel['selected'])
    assert files == ['benchmarks/bench_scan.py', 'tool/scripts/scanner.py', 'tool/scripts/self_test.py'], files
    assert sel['total_tests'] == len(TOOL_TESTS), sel['total_tests']
    cmds = {(c['cwd'], ' '.join(c['argv'])) for c in sel['command']['commands']}
    assert ('.', 'python tool/scripts/scanner.py --self-test') in cmds, cmds
    assert ('.', 'python tool/scripts/self_test.py') in cmds, cmds
    assert ('benchmarks', 'python -m pytest bench_scan.py') in cmds or ('benchmarks', 'python -m pytest') in cmds, cmds
    assert not any(cwd == '.' and 'pytest' in argv for cwd, argv in cmds), cmds
    assert sel.get('excluded') and {x['dir'] for x in sel['excluded']} == {'tool/examples/crate', 'tool/examples/sample-app'}


def case_tool_repo_fixture_changes(tmp: Path) -> None:
    """A fixture project's lockfile is fixture-local: it never forces the full suite. A changed sample file selects
    the guide's validation that names it (import-graph evidence) and widens to the tests of the tool that owns the
    sample, not to every test."""
    rel = 'tool/examples/crate/Cargo.lock'
    sel = tool_select(tmp / 'a', rel, 'version = 4\n')
    ch = next(c for c in sel['changes'] if c['path'] == rel)
    assert ch['kind'] == 'fixture', ch
    assert not any(b['kind'] == 'global_config' or b['required_scope'] == 'full_suite' for b in sel['blind_spots']), sel['blind_spots']
    spot = next(b for b in sel['blind_spots'] if b['path'] == rel)
    assert spot['kind'] == 'fixture_project' and spot['required_scope'] == 'fixture_host', spot
    assert 'benchmarks/bench_scan.py' not in spot['widened_to'] and 'tool/scripts/self_test.py' in spot['widened_to'], spot
    rel = 'tool/examples/sample-app/report.json'
    sel = tool_select(tmp / 'b', rel, '{"findings": [1]}\n')
    reasons = {s['file']: [r['source'] for r in s['reasons']] for s in sel['selected']}
    assert 'import_graph' in reasons.get('GUIDE.md:4', []), reasons
    cmd = next(c for c in sel['command']['commands'] if c['framework'] == 'command')
    assert cmd['cwd'] == '.' and cmd['argv'] == ['python', 'tool/scripts/validate_report.py',
                                                  'tool/examples/sample-app/report.json'], cmd


def case_bare_root_widens_to_top_folder(tmp: Path) -> None:
    """With no manifest at the root, a top-level folder that holds tests is the scope a data file or an unmapped
    script widens to (its tests and the guide checks that run its scripts), not every test of the repository."""
    files = dict(TOOL)
    files['tool/data/rules.json'] = '{"rules": []}\n'
    files['other/scripts/lint.py'] = ('import sys\n\n\ndef lint(s):\n    return s.strip()\n\n\n'
                                      'if __name__ == "__main__":\n    if "--self-test" in sys.argv:\n'
                                      '        print("ok lint" if lint(" a ") == "a" else "FAIL lint")\n')
    rel = 'tool/data/rules.json'
    sel = tool_select(tmp, rel, '{"rules": [1]}\n', files=files)
    spot = next(b for b in sel['blind_spots'] if b['path'] == rel)
    assert 'other/scripts/lint.py' not in spot['widened_to'], spot
    assert 'benchmarks/bench_scan.py' not in spot['widened_to'], spot
    assert {'tool/scripts/self_test.py', 'GUIDE.md:4'} <= set(spot['widened_to']), spot
    assert 'tool/' in spot['note'], spot['note']


def case_js_template_string_is_not_a_dynamic_import(tmp: Path) -> None:
    """`import(` inside a JavaScript template literal is text, not a computed import, and a JavaScript dynamic
    import can never load a Python module: no dynamic_reference blind spot on a Python script."""
    import import_graph
    g = import_graph.build(make_repo(tmp / 'repo', TOOL))
    assert not g.files['tool/scripts/loader.mjs']['dynamic'], g.files['tool/scripts/loader.mjs']['dynamic']
    files = dict(TOOL)
    files['tool/scripts/plugins.mjs'] = 'export async function load(name) {\n  return import(name);\n}\n'
    rel = 'tool/scripts/report.py'
    sel = tool_select(tmp / 'b', rel, TOOL[rel].replace('sum(xs)', 'sum(xs) + 0'), files=files)
    assert not any(b['kind'] == 'dynamic_reference' for b in sel['blind_spots']), sel['blind_spots']


def case_unittest_discover_runs_from_root(tmp: Path) -> None:
    """detect_conventions gives the unittest discover command relative to the repository root, so it runs from the
    root, not from the package directory (where `-s pkg/tests` would name pkg/pkg/tests)."""
    files = {'svc/requirements.txt': 'requests\n', 'svc/tally.py': 'def add(a, b):\n    return a + b\n',
             'svc/util.py': 'K = 1\n',
             'svc/tests/test_tally.py': ('import sys\nimport unittest\nfrom pathlib import Path\n\n'
                                         'sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n'
                                         'import tally  # noqa: E402\nimport util  # noqa: E402,F401\n\n\n'
                                         'class TallyTest(unittest.TestCase):\n    def test_add(self):\n'
                                         '        self.assertEqual(tally.add(1, 2), 3)\n')}
    import select_tests
    repo = make_repo(tmp / 'repo', files)
    d = write(tmp / 'u.diff', diff_of('svc/util.py', files['svc/util.py'], 'K = 2\n'))
    sel = select_tests.select(repo, d)
    cmd = next(c for c in sel['command']['commands'] if c['framework'] == 'unittest')
    if 'discover' in cmd['argv']:
        assert cmd['cwd'] == '.' and cmd['argv'][-1] == 'svc/tests', cmd
    else:
        assert cmd['cwd'] == 'svc' and cmd['argv'][-1] == 'tests/test_tally.py', cmd


def case_render_fixture_project(tmp: Path, renderer: Path | None = None) -> None:
    """The renderer names the fixture projects left out and labels a fixture_project blind spot."""
    rep = {'metadata': {'repo': 'scantool', 'mode': 'strict', 'change': {'diff': 'c.diff'}},
           'summary': {'selected': 1, 'total_tests': 5, 'blind_spots': 1, 'evidence_completeness': 0.5,
                       'headline': 'One of five test files.', 'recall_status': 'not_assessed'},
           'command': {'commands': [{'package': '.', 'cwd': '.', 'framework': 'selftest', 'full_suite': False,
                                     'argv': ['python', 'tool/scripts/self_test.py'], 'tests': 1}], 'split_into': 1},
           'excluded': [{'dir': 'tool/examples/crate', 'reason': 'fixture project: its own manifest (Cargo.toml)'}],
           'selected': [], 'sources': [], 'changes': [], 'recommendations': {'full_suite': 'on merge'},
           'blind_spots': [{'id': 'B1', 'path': 'tool/examples/crate/Cargo.lock', 'kind': 'fixture_project',
                            'action': 'fixture_host', 'required_scope': 'fixture_host',
                            'widened_to': ['tool/scripts/self_test.py'], 'would_widen_to': ['tool/scripts/self_test.py'],
                            'note': 'File in a fixture project.'}]}
    p = write(tmp / 'r.json', json.dumps(rep))
    r = subprocess.run([sys.executable, str(renderer or HERE / 'render_report.py'), str(p), '--color', 'never'],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'tool/examples/crate' in out and 'fixture project' in out, out[-600:]


def case_validator_rejects_excluded_tests(tmp: Path) -> None:
    """The validator rejects a report that selects a test inside a fixture project it says it excluded."""
    import validate_impact_report as v
    reason = {'source': 'blind_spot', 'strength': 0, 'evidence': 'B1 unmapped_source: package', 'changed_path': 'a.py'}
    rep = {'metadata': {'mode': 'strict', 'change': {}}, 'sources': [], 'changes': [], 'total_tests': 1,
           'selected': [{'test_id': 'tool/examples/app/tests/test_a.py', 'file': 'tool/examples/app/tests/test_a.py',
                         'package': '.', 'strength': 0, 'reasons': [reason], 'order': 1}],
           'blind_spots': [{'id': 'B1', 'path': 'a.py', 'kind': 'unmapped_source', 'action': 'package',
                            'required_scope': 'package', 'widened_to': [], 'would_widen_to': [], 'note': 'No evidence.'}],
           'not_selected_count': 0, 'command': {'commands': [], 'split_into': 0},
           'excluded': [{'dir': 'tool/examples/app', 'reason': 'fixture project'}],
           'recommendations': {'full_suite': 'Run the full suite on merge.'}, 'limitations': ['Coverage was not given.'],
           'summary': {'headline': 'One: 1 of 1 test files.'}}
    errs = v.check(rep, None, None, None)
    assert any('excluded' in e for e in errs), errs


CASES = [case_tool_repo_tests, case_tool_repo_selection, case_tool_repo_fixture_changes, case_bare_root_widens_to_top_folder,
         case_js_template_string_is_not_a_dynamic_import, case_unittest_discover_runs_from_root,
         case_validator_rejects_excluded_tests, case_render_fixture_project,
         case_import_graph_go,case_select_go_change, case_unmapped_go_widens_within_go, case_naming_same_package,
         case_history_gotestsum_files, case_replay_skips_and_counts_packages, case_replay_from_git_commits,
         case_baseline_failures, case_coverage_map_go_profiles, case_coverage_map_go_run, case_render_go_run_line,
         case_template_render_links_views, case_static_reaches_rendering_tests, case_data_file_in_root_package,
         case_coverage_core_ctrace, case_normalise_refuses_lost_contexts, case_committed_only_hashes_head, case_history_commit_is_never_an_option,
         case_rust_graph, case_select_rust_change, case_rust_exact_ids, case_rust_bench_and_fixture,
         case_rust_bin_module_selects_integration_tests, case_rust_parameterised_tests_prefix_matched,
         case_rust_default_features_transitive, case_rust_cfg_keyed_by_target, case_rust_files_scanned_once,
         case_root_cargo_toml_widens_to_rust_only, case_python_sys_path_imports, case_python_sys_path_unknown_recorded,
         case_blind_spot_wording, case_unittest_command, case_no_parser_language_warns,
         case_validator_requires_language_limitation, case_render_rust_and_language_warning, case_selection_speed]


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
