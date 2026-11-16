#!/usr/bin/env python3
"""Self-test for the mutation tester's scripts on small synthetic fixtures (stdlib only).

    python scripts/self_test.py [--only NAME] [-v]

Each case builds its fixture in a temporary directory: a tiny Go module ("parcel rates"), a Rust
file, mutation-testing-elements reports and minimal mutation reports. Cases that need the Go
toolchain are skipped (and say so) when `go` is not on PATH. Exit 0 when every case that ran passed.
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))

GO_MOD = 'module example.test/parcel\n\ngo 1.21\n'
GO_SRC = '''package rates

import "errors"

// Band names the weight band; "heavy" starts at 20 kg.
func Band(kg int) string {
	if kg >= 20 {
		return "heavy"
	}
	return "light"
}

// Total adds item weights and refuses a negative one.
func Total(items []int) (int, error) {
	sum := 0
	for _, w := range items {
		if w < 0 {
			return sum, errors.New("negative weight") // a < in a comment is never mutated
		}
		sum += w
	}
	return sum, nil
}

// Free reports whether shipping is free: members, or totals over 100.
func Free(member bool, total int) bool {
	return member || total > 100
}

// Label joins a prefix and a name.
func Label(prefix, name string) string {
	return prefix + name
}

func double(n int) int {
	n++
	return n * 2
}
'''
GO_TEST = '''package rates

import "testing"

func TestBand(t *testing.T) {
	if Band(25) != "heavy" {
		t.Fatal("25 kg")
	}
	if Band(5) != "light" {
		t.Fatal("5 kg")
	}
}

func TestTotal(t *testing.T) {
	got, err := Total([]int{1, 2})
	if err != nil || got != 3 {
		t.Fatalf("got %d %v", got, err)
	}
	t.Run("negative", func(t *testing.T) {
		if _, err := Total([]int{-1}); err == nil {
			t.Fatal("want an error")
		}
	})
}

func TestFree(t *testing.T) {
	if !Free(true, 0) {
		t.Fatal("members ship free")
	}
}

func TestLabel(t *testing.T) {
	if Label("a", "b") == "" {
		t.Fatal("empty")
	}
}
'''


def go_available() -> bool:
    return shutil.which('go') is not None


def go_module(root: Path) -> Path:
    (root / 'rates').mkdir(parents=True)
    (root / 'go.mod').write_text(GO_MOD, encoding='utf-8')
    (root / 'rates' / 'rates.go').write_text(GO_SRC, encoding='utf-8')
    (root / 'rates' / 'rates_test.go').write_text(GO_TEST, encoding='utf-8')
    return root


def scope_for(files: dict[str, list[list[int]]]) -> dict:
    return {'source': 'diff', 'files': [{'path': p, 'old_path': None, 'status': 'modified', 'kind': 'source',
                                         'kind_reason': 'test fixture', 'ranges': r, 'deleted_at': [],
                                         'lines_added': 0, 'lines_deleted': 0, 'present': True}
                                        for p, r in files.items()]}


def run(script: str, *args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / script), *map(str, args)], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', cwd=cwd)


def write_json(p: Path, data) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding='utf-8')
    return p


class Skip(Exception):
    pass


# ---------------------------------------------------------------- cases
def case_builtin_go_engine(tmp: Path) -> None:
    """builtin_mutator.py mutates the changed lines of a Go package, runs `go test` per mutant and
    leaves the tree as it was."""
    if not go_available():
        raise Skip('go is not on PATH')
    repo = go_module(tmp / 'repo')
    n = len(GO_SRC.splitlines())
    scope = write_json(tmp / 'scope.json', scope_for({'rates/rates.go': [[1, n]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run')
    assert res.returncode == 0, f'exit {res.returncode}: {res.stderr[-800:]}{res.stdout[-800:]}'
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    assert out.get('language') == 'go', out.get('language')
    by = {}
    for m in out['mutants']:
        by.setdefault((m['line'], m['operator']), []).append(m)
    lines = GO_SRC.splitlines()
    ln = lambda s: next(i for i, l in enumerate(lines, 1) if s in l)  # noqa: E731
    st = lambda s, op: [m['status'] for m in by.get((ln(s), op), [])]  # noqa: E731
    assert st('if kg >= 20', 'comparison_boundary') == ['survived'], st('if kg >= 20', 'comparison_boundary')
    assert st('if kg >= 20', 'negate_condition') == ['killed'], st('if kg >= 20', 'negate_condition')
    assert st('if w < 0', 'comparison_boundary') == ['survived']
    assert st('sum += w', 'arithmetic') == ['killed']
    assert st('member || total', 'logical') == ['killed']
    assert st('return prefix + name', 'arithmetic') == ['compile_error']
    assert st('n++', 'arithmetic') == ['no_coverage']
    assert st('return sum, nil', 'return_value') == [], 'nil is never mutated to nil'
    assert all(m['line'] != ln('a < in a comment') or m['operator'] != 'comparison_boundary' or
               '<=' not in m['mutated'].split('//')[1] for m in out['mutants']), 'a comment was mutated'
    assert not any('"heavy"' in m['original'] and m['operator'] == 'comparison_boundary' for m in out['mutants'])
    killed = [m for m in out['mutants'] if m['status'] == 'killed']
    assert all(m['killed_by'].startswith('rates/rates_test.go::Test') for m in killed), killed[:1]
    band = by[(ln('if kg >= 20'), 'comparison_boundary')][0]
    assert band['covering_tests'] == ['rates/rates_test.go::TestBand'], band['covering_tests']
    assert out['tree']['changed'] == [], out['tree']
    assert (repo / 'rates' / 'rates.go').read_text(encoding='utf-8') == GO_SRC


def case_builtin_scope_outside_repo(tmp: Path) -> None:
    """A scope naming a file outside --repo (../ or an absolute path) is refused before anything is mutated, so the
    mutator never writes a mutant into a file it was not asked to test."""
    repo = py_shop(tmp / 'repo')
    outside = tmp / 'outside.py'
    outside.write_text('def f(a, b):\n    return a + b\n', encoding='utf-8')
    for name in ('../outside.py', outside.as_posix()):
        scope = write_json(tmp / 'scope.json', scope_for({name: [[1, 2]]}))
        res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '1')
        text = res.stderr + res.stdout
        assert res.returncode != 0 and 'outside --repo' in text and not (tmp / 'run' / 'coverage.json').exists(), \
            f'{name}: exit {res.returncode}, {text[-400:]}'


def case_builtin_unsupported_language(tmp: Path) -> None:
    """A scope with only Rust source fails loudly and names the files it could not mutate."""
    repo = tmp / 'repo'
    (repo / 'src').mkdir(parents=True)
    (repo / 'src' / 'lib.rs').write_text('pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n', encoding='utf-8')
    scope = write_json(tmp / 'scope.json', scope_for({'src/lib.rs': [[1, 3]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run')
    assert res.returncode != 0, 'exit 0 for a scope it cannot mutate'
    text = res.stderr + res.stdout
    assert 'src/lib.rs' in text and 'rust' in text.lower(), text[-400:]


def case_ranges_go_only(tmp: Path) -> None:
    """run_engine.py ranges warns and exits non-zero when the scope has no JS/TS lines."""
    scope = write_json(tmp / 'scope.json', scope_for({'rates/rates.go': [[1, 9]]}))
    res = run('run_engine.py', 'ranges', '--scope', scope)
    assert res.returncode != 0, f'exit 0 (stdout {res.stdout!r})'
    assert 'rates/rates.go' in res.stderr and 'go' in res.stderr.lower(), res.stderr


def mte_report(name: str, version: str = '0.6.0') -> dict:
    src = 'package rates\n\nfunc Last(xs []int) int {\n\tfor i := range xs {\n\t\tif i > 2 {\n\t\t\tbreak\n\t\t}\n\t}\n\treturn xs[len(xs)-1]\n}\n'
    loc = lambda l, a, b: {'start': {'line': l, 'column': a}, 'end': {'line': l, 'column': b}}  # noqa: E731
    return {'schemaVersion': '2', 'thresholds': {'high': 80, 'low': 60}, 'projectRoot': '/w',
            'framework': {'name': name, 'version': version},
            'files': {'rates/last.go': {'language': 'go', 'source': src, 'mutants': [
                {'id': '1', 'mutatorName': 'INVERT_LOOPCTRL', 'replacement': 'continue', 'location': loc(6, 4, 9),
                 'status': 'Survived', 'coveredBy': [], 'killedBy': []},
                {'id': '2', 'mutatorName': 'ARITHMETIC_BASE', 'replacement': '+', 'location': loc(9, 17, 18),
                 'status': 'Killed', 'coveredBy': [], 'killedBy': []},
                {'id': '3', 'mutatorName': 'INVERT_NEGATIVES', 'replacement': '+', 'location': loc(9, 17, 18),
                 'status': 'Killed', 'coveredBy': [], 'killedBy': []},
                {'id': '4', 'mutatorName': 'CONDITIONALS_BOUNDARY', 'replacement': '>=', 'location': loc(5, 8, 9),
                 'status': 'Survived', 'coveredBy': [], 'killedBy': []}]}}}


def normalise_mte(tmp: Path, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
    repo = tmp / 'repo'
    (repo / 'rates').mkdir(parents=True)
    rep = mte_report('gremlins')
    (repo / 'rates' / 'last.go').write_text(rep['files']['rates/last.go']['source'], encoding='utf-8')
    write_json(tmp / 'gremlins.mte.json', rep)
    scope = write_json(tmp / 'scope.json', scope_for({'rates/last.go': [[1, 10]]}))
    res = run('normalize_results.py', '--scope', scope, '--repo', repo, '--out', tmp / 'mutants.json', *extra)
    data = json.loads((tmp / 'mutants.json').read_text(encoding='utf-8')) if (tmp / 'mutants.json').exists() else {}
    return res, data


def case_mte_engine_label(tmp: Path) -> None:
    """A mutation-testing-elements report from another engine keeps that engine's name."""
    res, data = normalise_mte(tmp, '--mte', tmp / 'gremlins.mte.json')
    assert res.returncode == 0, res.stderr[-600:] + res.stdout[-600:]
    names = {e['name'] for e in data['engines']}
    assert names == {'gremlins'}, names
    assert all(m['engine'] == 'gremlins' and not m['id'].startswith('S') for m in data['mutants']), \
        [(m['id'], m['engine']) for m in data['mutants']]
    # the same report given as --stryker is still named after its framework, never "stryker"
    res2, data2 = normalise_mte(tmp / 'b', '--stryker', tmp / 'gremlins.mte.json')
    assert {e['name'] for e in data2.get('engines', [])} == {'gremlins'}, res2.stdout + res2.stderr


def case_gremlins_operators(tmp: Path) -> None:
    """Gremlins operator names map to the common operators: break <-> continue is control_flow (a branch)."""
    res, data = normalise_mte(tmp, '--mte', tmp / 'gremlins.mte.json')
    ops = {m['engine_operator']: (m['operator'], m['kind']) for m in data['mutants']}
    assert ops.get('INVERT_LOOPCTRL') == ('control_flow', 'branch'), ops
    assert ops.get('CONDITIONALS_BOUNDARY') == ('comparison_boundary', 'branch'), ops
    assert ops.get('ARITHMETIC_BASE', ops.get('INVERT_NEGATIVES')) == ('arithmetic', 'value'), ops


def case_duplicate_mutants_merged(tmp: Path) -> None:
    """Two operators producing the same edit on the same line are one mutant."""
    res, data = normalise_mte(tmp, '--mte', tmp / 'gremlins.mte.json')
    line9 = [m for m in data['mutants'] if m['line'] == 9]
    assert len(line9) == 1, [(m['id'], m['engine_operator']) for m in line9]
    assert 'INVERT_NEGATIVES' in line9[0].get('note', '') or 'ARITHMETIC_BASE' in line9[0].get('note', ''), line9[0]
    e = data['engines'][0]
    assert e['in_scope'] == 3 and e.get('duplicates_merged') == 1, e


def go_report_fixture(tmp: Path) -> tuple[Path, dict]:
    """A minimal valid mutation report on the Go module: one survivor in Band, covered by TestBand."""
    import validate_mutation_report as v
    repo = go_module(tmp / 'repo')
    import run_tests
    line = next(i for i, l in enumerate(GO_SRC.splitlines(), 1) if 'if kg >= 20' in l)
    m = {'id': 'B1', 'engine': 'builtin', 'raw_id': 'B1', 'file': 'rates/rates.go', 'line': line, 'end_line': line,
         'function': 'Band', 'operator': 'comparison_boundary', 'kind': 'branch', 'original': '\tif kg >= 20 {',
         'mutated': '\tif kg > 20 {', 'status': 'survived', 'engine_status': 'survived',
         'covering_tests': ['rates/rates_test.go::TestBand'], 'killed_by': None}
    eng = {'name': 'builtin', 'version': '1.2.0', 'command': 'x', 'config_source': 'builtin', 'config': None,
           'status': 'ran', 'reason': '', 'raw': 'builtin.json', 'generated': 1, 'in_scope': 1, 'out_of_scope': 0,
           'sampled': None, 'timeout_s': 10, 'tree_changed': [], 'counts': {'survived': 1}}
    f = {'id': 'F1', 'triage': 'missing_case', 'mutant_ids': ['B1'], 'file': 'rates/rates.go', 'function': 'Band',
         'summary': 'No test weighs exactly 20 kg.', 'reason': 'The boundary at 20 is never tested.',
         'should_have_caught': 'rates/rates_test.go::TestBand',
         'suggestion': {'test_file': 'rates/rates_test.go', 'code': 'func TestBandAt20(t *testing.T) {}',
                        'assertion_or_input': 'Weigh exactly 20 kg.'},
         'proof_ids': [], 'criticality': 2, 'criticality_reason': 'Prices depend on it.', 'kind_factor': None,
         'proximity': None, 'score': None, 'priority': None}
    d = {'schema_version': v.SCHEMA_VERSIONS[-1] if hasattr(v, 'SCHEMA_VERSIONS') else '1.1.0',
         'metadata': {'repo': 'parcel', 'code_hash': run_tests.code_hash(repo), 'generated_at': '2026-11-14T00:00:00Z',
                      'scope': {'mode': 'changed', 'base_ref': None, 'diff_file': 'change.diff',
                                'files': [{'path': 'rates/rates.go', 'ranges': [[1, 40]]}], 'excluded': []},
                      'write_tests': False, 'coverage': {'file': 'coverage.json', 'method': 'go test coverprofile per test'},
                      'engines': [eng], 'budget_minutes': 30},
         'tools': [], 'baseline': {'passed': True, 'run_ids': ['G1', 'G2'], 'flaky_tests': [], 'note': 'go test ./rates/'},
         'mutants': [m], 'findings': [f], 'coverage_gaps': [],
         'summary': {'headline': 'One survivor in the weight bands.', 'by_engine': [], 'findings': {}, 'by_priority': {}, 'coverage_gaps': 0,
                     'evidence_completeness': 0},
         'limitations': [], 'files_changed': []}
    return repo, d


def fit_schema(d: dict) -> dict:
    """Drop or fill metadata fields so the fixture fits whatever the schema requires."""
    import validate_mutation_report as v
    schema = json.loads(v.SCHEMA.read_text(encoding='utf-8'))
    md = schema['properties']['metadata']
    props, req = md.get('properties', {}), md.get('required', [])
    for k in list(d['metadata']):
        if k not in props:
            del d['metadata'][k]
    for k in req:
        if k not in d['metadata']:
            t = props[k].get('type')
            d['metadata'][k] = None if (isinstance(t, list) and 'null' in t) else ([] if t == 'array' else '')
    d['schema_version'] = schema['properties']['schema_version']['enum'][-1]
    return d


def case_proximity_go(tmp: Path) -> None:
    """Proximity reads Go tests: TestBand calls Band directly, so proximity is 2."""
    import validate_mutation_report as v
    repo, d = go_report_fixture(tmp)
    d = fit_schema(d)
    v.write_summary(d, repo)
    f = d['findings'][0]
    assert f['proximity'] == 2, f['proximity']
    assert f['score'] == 2 * 3 * 2 and f['priority'] == 'P0', (f['score'], f['priority'])
    errs, _ = v.check(d, None, None, None, repo)
    assert not [e for e in errs if 'proximity' in e], errs


def case_proximity_unparsed(tmp: Path) -> None:
    """A covering test in a language the validator cannot parse gets no made-up proximity of 1."""
    import validate_mutation_report as v
    repo, d = go_report_fixture(tmp)
    d = fit_schema(d)
    d['mutants'][0]['covering_tests'] = ['tests/band_test.rs::band_at_20']
    d['findings'][0]['should_have_caught'] = 'tests/band_test.rs::band_at_20'
    v.write_summary(d, repo)
    f = d['findings'][0]
    assert f['proximity'] is None, f['proximity']
    errs, _ = v.check(d, None, None, None, repo)
    assert any('proximity' in e and 'parse' in e for e in errs), errs


def case_completeness_external_engine(tmp: Path) -> None:
    """Evidence completeness is below 1.0 when the only engine was converted from another tool's output
    and no suggestion was proved."""
    import validate_mutation_report as v
    repo, d = go_report_fixture(tmp)
    d = fit_schema(d)
    e = d['metadata']['engines'][0]
    e.update(name='gremlins', config_source=None, raw='gremlins.mte.json', run_by='external')
    for m in d['mutants']:
        m['engine'] = 'gremlins'
    d['mutants'][0]['id'] = d['mutants'][0]['raw_id'] = 'X1'
    d['findings'][0]['mutant_ids'] = ['X1']
    v.write_summary(d, repo)
    assert d['summary']['evidence_completeness'] < 1.0, d['summary']['evidence_completeness']
    errs, _ = v.check(d, None, None, None, repo)
    assert not [x for x in errs if 'run_by' in x or 'unexpected field' in x], errs


def case_prove_kill_go(tmp: Path) -> None:
    """prove_kill.py runs a Go test with `go test -run` (never pytest) and leaves the repo untouched."""
    if not go_available():
        raise Skip('go is not on PATH')
    repo = go_module(tmp / 'repo')
    line = next(i for i, l in enumerate(GO_SRC.splitlines(), 1) if 'if kg >= 20' in l)
    muts = write_json(tmp / 'mutants.json', {'mutants': [
        {'id': 'B1', 'engine': 'builtin', 'file': 'rates/rates.go', 'line': line, 'end_line': line,
         'original': '\tif kg >= 20 {', 'mutated': '\tif kg > 20 {'}]})
    code = tmp / 'new_test.go'
    code.write_text('func TestBandAt20(t *testing.T) {\n\tif Band(20) != "heavy" {\n\t\tt.Fatal("20 kg is heavy")\n\t}\n}\n',
                    encoding='utf-8')
    res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'B1', '--test-file', 'rates/rates_test.go',
              '--test-code', code, '--test-name', 'TestBandAt20', '--run-dir', tmp / 'run', '--out', tmp / 'proofs.json',
              '--finding', 'F1')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    p = json.loads((tmp / 'proofs.json').read_text(encoding='utf-8'))['proofs'][0]
    assert p['original_run']['test_status'] == 'passed', p['original_run']
    assert p['original_run']['command'].startswith('go test'), p['original_run']['command']
    assert p['mutants'][0]['verdict'] == 'kills', p['mutants'][0]
    assert p['repo_files_changed'] == [], p['repo_files_changed']
    assert (repo / 'rates' / 'rates_test.go').read_text(encoding='utf-8') == GO_TEST


def case_prove_kill_unsupported(tmp: Path) -> None:
    """prove_kill.py refuses a test file in a language it has no runner for, before copying anything."""
    repo = tmp / 'repo'
    (repo / 'src').mkdir(parents=True)
    (repo / 'src' / 'Add.java').write_text('class Add {\n    int add(int a) {\n        return a + 1;\n    }\n}\n',
                                           encoding='utf-8')
    muts = write_json(tmp / 'mutants.json', {'mutants': [{'id': 'P1', 'engine': 'pit', 'file': 'src/Add.java',
                                                          'line': 3, 'end_line': 3, 'original': '        return a + 1;',
                                                          'mutated': '        return a - 1;'}]})
    code = tmp / 'AddTest.java'
    code.write_text('@Test\nvoid adds() { assertEquals(2, new Add().add(1)); }\n', encoding='utf-8')
    res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'P1', '--test-file', 'src/AddTest.java',
              '--test-code', code, '--test-name', 'adds', '--run-dir', tmp / 'run', '--out', tmp / 'proofs.json')
    assert res.returncode != 0, 'exit 0'
    text = res.stderr + res.stdout
    assert 'Traceback' not in text and '.java' in text and 'no runner' in text, text[-600:]
    assert 'not run' not in text and not (tmp / 'proofs.json').exists(), 'ran a runner on a Java test file'
    assert not (tmp / 'run').exists() or not any((tmp / 'run').iterdir()), 'wrote a scratch copy before refusing'


def cmd_shim_fixture(tmp: Path) -> tuple[Path, Path, Path, str]:
    """A folder for PATH holding a stand-in npx.cmd (it only echoes its arguments) and qa_marker.cmd (which writes
    marker.txt beside itself when cmd.exe runs it), a package folder, and a test file name that would make cmd.exe
    run the marker: a&qa_marker&.test.ts."""
    shim = tmp / 'shim'
    shim.mkdir()
    (shim / 'npx.cmd').write_text('@echo off\r\necho shim got: %*\r\n', encoding='ascii')
    (shim / 'qa_marker.cmd').write_text('@echo ran> "%~dp0marker.txt"\r\n', encoding='ascii')
    pkg = tmp / 'pkg'
    pkg.mkdir()
    return shim, pkg, shim / 'marker.txt', 'a&qa_marker&.test.ts'


def case_prove_kill_cmd_shim_metacharacters(tmp: Path) -> None:
    """A test file name with cmd.exe metacharacters never reaches cmd.exe through the npx .cmd shim (Windows only)."""
    if os.name != 'nt':
        raise Skip('the .cmd shim re-parsing only happens on Windows (cmd.exe)')
    import prove_kill
    shim, pkg, marker, name = cmd_shim_fixture(tmp)
    (pkg / name).write_text('test("x", () => {})\n', encoding='utf-8')
    old = os.environ.get('PATH', '')
    os.environ['PATH'] = str(shim) + os.pathsep + old
    try:
        env = {**os.environ}
        rec = prove_kill.run_test(pkg, 'vitest', name, 'x', tmp / 'junit.xml', env, 30, [])
    finally:
        os.environ['PATH'] = old
    assert not marker.exists(), 'cmd.exe ran a second command taken from the test file name'
    assert rec['test_status'] == 'not_run', rec


# A tiny Python package ("tea shop prices") whose rate table is loaded once, while conftest is imported:
# the table's lines run only during collection, never inside a test (a cached loader, data built in conftest).
PY_PRICES = '''_CACHE = {}


def load_rates():
    if 'rates' not in _CACHE:
        _CACHE['rates'] = {'tea': 3, 'cake': 5}
    return _CACHE['rates']


def price(item, qty):
    total = load_rates()[item] * qty
    if qty >= 10:
        total = total - 1
    return total
'''
PY_CONFTEST = 'from shop.prices import load_rates\n\nRATES = load_rates()\n'
PY_TEST = '''from shop.prices import price


def test_tea():
    assert price('tea', 2) == 6


def test_cake_bulk():
    assert price('cake', 10) == 49
'''
RATES_LINE = next(i for i, l in enumerate(PY_PRICES.splitlines(), 1) if "_CACHE['rates'] = {" in l)


def py_shop(root: Path, conftest: str = PY_CONFTEST, slow_s: float = 0) -> Path:
    (root / 'shop').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / 'pytest.ini').write_text('[pytest]\npythonpath = .\ntestpaths = tests\n', encoding='utf-8')
    (root / 'shop' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'shop' / 'prices.py').write_text(PY_PRICES, encoding='utf-8')
    (root / 'tests' / 'conftest.py').write_text(conftest, encoding='utf-8')
    (root / 'tests' / 'test_prices.py').write_text(PY_TEST, encoding='utf-8')
    if slow_s:
        # a slow test that never touches the scoped file: it makes one whole-suite run long
        (root / 'tests' / 'test_slow.py').write_text(
            f'import time\n\n\ndef test_slow_report():\n    time.sleep({slow_s})\n', encoding='utf-8')
    return root


def case_builtin_import_time_coverage(tmp: Path) -> None:
    """A function that runs only while conftest is imported (a cached loader) is covered by the tests that use
    the file, not reported no_coverage: its mutants run and the tests that depend on the loaded data kill them."""
    repo = py_shop(tmp / 'repo')
    n = len(PY_PRICES.splitlines())
    scope = write_json(tmp / 'scope.json', scope_for({'shop/prices.py': [[1, n]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '5')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    on_rates = [m for m in out['mutants'] if m['line'] == RATES_LINE]
    assert on_rates, 'no mutants on the rate table line'
    bad = [(m['id'], m['status']) for m in on_rates if m['status'] == 'no_coverage']
    assert not bad, f'lines run at import (collection) reported no_coverage: {bad}'
    assert any(m['status'] == 'killed' for m in on_rates), [(m['id'], m['status']) for m in on_rates]
    assert all('import' in m.get('note', '') for m in on_rates), [m.get('note') for m in on_rates]


def case_builtin_budget_uses_covering_tests(tmp: Path) -> None:
    """The budget is not spent as if every mutant ran the whole --tests selection: a slow test that never
    touches the scoped file must not sample out mutants that only run two fast tests; the plan is printed."""
    repo = py_shop(tmp / 'repo', slow_s=6)
    n = len(PY_PRICES.splitlines())
    scope = write_json(tmp / 'scope.json', scope_for({'shop/prices.py': [[10, n]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '0.75')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    unrun = [(m['id'], m.get('note')) for m in out['mutants'] if m['status'] == 'not_run']
    assert not unrun, f'{len(unrun)} mutants left unrun with budget to spare: {unrun[:4]}'
    assert 'plan:' in res.stdout, res.stdout[:600]


def py_mutants(tmp: Path, mutated: str, timeout_s: float | None = None) -> Path:
    original = PY_PRICES.splitlines()[RATES_LINE - 1]
    return write_json(tmp / 'mutants.json', {
        'engines': [{'name': 'builtin', 'timeout_s': timeout_s}],
        'mutants': [{'id': 'B1', 'engine': 'builtin', 'file': 'shop/prices.py', 'line': RATES_LINE,
                     'end_line': RATES_LINE, 'original': original, 'mutated': mutated}]})


PROOF_TEST = "def test_tea_rate():\n    from shop.prices import load_rates\n    assert load_rates()['tea'] == 3\n"


def prove_py(tmp: Path, repo: Path, muts: Path, *extra) -> tuple[subprocess.CompletedProcess, dict]:
    code = tmp / 'new_test.py'
    code.write_text(PROOF_TEST, encoding='utf-8')
    res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'B1', '--test-file',
              'tests/test_prices.py', '--test-code', code, '--test-name', 'test_tea_rate', '--run-dir', tmp / 'run',
              '--out', tmp / 'proofs.json', *extra)
    assert (tmp / 'proofs.json').exists(), res.stdout[-800:] + res.stderr[-800:]
    return res, json.loads((tmp / 'proofs.json').read_text(encoding='utf-8'))['proofs'][0]


def case_prove_kill_original_timeout(tmp: Path) -> None:
    """When the new test times out on the original code (a slow app import), the proof is inconclusive
    (original_not_run), never "fails_on_original": the test never ran."""
    repo = py_shop(tmp / 'repo', conftest='import time\n\ntime.sleep(4)\n' + PY_CONFTEST)
    muts = py_mutants(tmp, "        _CACHE['rates'] = {'tea': 4, 'cake': 5}")
    res, p = prove_py(tmp, repo, muts, '--timeout', '1.5')
    assert p['original_run']['test_status'] == 'timeout', p['original_run']
    verdict = p['mutants'][0]['verdict']
    assert verdict == 'original_not_run', f'original timeout recorded as {verdict}'
    assert res.returncode != 0


def case_prove_kill_default_timeout(tmp: Path) -> None:
    """Without --timeout, the proof uses the engine's measured per-mutant timeout from mutants.json (never less
    than 60 s), so a repository whose import is slow is not cut off at a fixed 60 s."""
    repo = py_shop(tmp / 'repo')
    muts = py_mutants(tmp, "        _CACHE['rates'] = {'tea': 4, 'cake': 5}", timeout_s=150.0)
    res, p = prove_py(tmp, repo, muts)
    assert p.get('timeout_s') == 150.0, f"timeout_s {p.get('timeout_s')}"
    assert p['mutants'][0]['verdict'] == 'kills', p['mutants'][0]


def case_prove_kill_collection_error(tmp: Path) -> None:
    """A mutant that breaks collection (conftest raises while importing) never ran the new test: the verdict is
    runtime_error, never does_not_kill."""
    repo = py_shop(tmp / 'repo')
    muts = py_mutants(tmp, "        _CACHE[''] = {'tea': 3, 'cake': 5}")
    res, p = prove_py(tmp, repo, muts, '--timeout', '120')
    assert p['original_run']['test_status'] == 'passed', p['original_run']
    r = p['mutants'][0]
    assert r['run']['test_status'] == 'not_run', r['run']
    assert r['verdict'] == 'runtime_error', f"collection error on the mutant recorded as {r['verdict']}"


# ---------------------------------------------------------------- a Rust workspace with Python tooling
# A synthetic tide-gauge workspace: the product is a Rust crate; Python sits in scripts/ (helpers) with
# unittest tests that put scripts/ on sys.path, as many Rust repositories have.
RUST_FN = '''/// Height band {i}: readings from {lo} cm count.
pub fn band_{i}(height_cm: u32) -> bool {{
    if height_cm >= {lo} {{
        return true;
    }}
    false
}}
'''
LEVELS_PY = '''def band(cm):
    if cm >= 300:
        return 'high'
    return 'low'


def describe(reading, defaults=('north', 'south')):
    station = reading.get('station', defaults[0])
    return station + ':' + band(reading['cm'])
'''
LEVELS_TEST = '''import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import levels  # noqa: E402


class LevelsTest(unittest.TestCase):
    def test_band(self):
        self.assertEqual(levels.band(450), 'high')
        self.assertEqual(levels.band(10), 'low')

    def test_describe(self):
        self.assertEqual(levels.describe({'station': 'pier', 'cm': 500}), 'pier:high')
'''
NO_PYTEST = "raise SystemExit('pytest must not run in this repository: CI runs unittest')\n"


def rust_tooling_repo(root: Path, rust_fns: int = 80) -> Path:
    (root / 'crates' / 'gauge' / 'src').mkdir(parents=True)
    (root / 'scripts').mkdir()
    (root / 'tests').mkdir()
    (root / 'Cargo.toml').write_text('[workspace]\nmembers = ["crates/gauge"]\nresolver = "2"\n', encoding='utf-8')
    (root / 'crates' / 'gauge' / 'Cargo.toml').write_text('[package]\nname = "gauge"\nversion = "0.1.0"\nedition = "2021"\n',
                                                          encoding='utf-8')
    (root / 'crates' / 'gauge' / 'src' / 'lib.rs').write_text(
        '\n'.join(RUST_FN.format(i=i, lo=100 + i) for i in range(rust_fns)), encoding='utf-8')
    (root / 'scripts' / 'levels.py').write_text(LEVELS_PY, encoding='utf-8')
    (root / 'tests' / 'test_levels.py').write_text(LEVELS_TEST, encoding='utf-8')
    return root


def case_detect_tools_unscripted_dominant(tmp: Path) -> None:
    """The language with the most product lines (Rust here) has no engine that can run: detect_tools.py prints a
    loud WARNING and records it, and says the Python it would mutate is tooling (scripts/), not the product."""
    repo = rust_tooling_repo(tmp / 'repo')
    res = run('detect_tools.py', repo, '--out', tmp / 'tools.json')
    assert res.returncode == 0, res.stderr[-600:]
    warn = [l for l in res.stderr.splitlines() if l.startswith('WARNING')]
    assert any('Rust' in l for l in warn), res.stderr[-800:]
    assert any('tooling' in l and 'Python' in l for l in warn), res.stderr[-800:]
    out = json.loads((tmp / 'tools.json').read_text(encoding='utf-8'))
    lc = out.get('language_coverage') or {}
    assert lc.get('status') == 'unsupported_dominant' and lc.get('dominant') == 'Rust', lc
    assert 'scripts' in json.dumps(out.get('tooling') or {}), out.get('tooling')


def lc_report(tmp: Path) -> tuple[Path, dict, dict]:
    """A minimal report mutating scripts/levels.py in the Rust workspace, built from normalize_results.py output."""
    import validate_mutation_report as v
    import run_tests
    repo = rust_tooling_repo(tmp / 'repo')
    line = 2
    raw = {'engine': {'name': 'builtin', 'version': '1.3.0', 'command': 'x'}, 'ids': 'position', 'timeout_s': 10,
           'sampled': None, 'tree': {'changed': []},
           'mutants': [{'id': 'B1', 'file': 'scripts/levels.py', 'line': line, 'end_line': line,
                        'operator': 'comparison_boundary', 'original': '    if cm >= 300:', 'mutated': '    if cm > 300:',
                        'status': 'survived', 'covering_tests': ['tests/test_levels.py::LevelsTest::test_band'],
                        'killed_by': None}]}
    write_json(tmp / 'builtin.json', raw)
    scope = write_json(tmp / 'scope.json', scope_for({'scripts/levels.py': [[1, 9]]}))
    res = run('normalize_results.py', '--scope', scope, '--repo', repo, '--builtin', tmp / 'builtin.json',
              '--out', tmp / 'mutants.json')
    assert (tmp / 'mutants.json').exists(), res.stdout + res.stderr
    mfile = json.loads((tmp / 'mutants.json').read_text(encoding='utf-8'))
    _, d = go_report_fixture(tmp / 'go')
    d = fit_schema(d)
    m = dict(mfile['mutants'][0])
    d['mutants'] = [m]
    e = dict(mfile['engines'][0])
    d['metadata']['engines'] = [e]
    d['metadata']['code_hash'] = run_tests.code_hash(repo)
    d['metadata']['scope'] = {'mode': 'path', 'base_ref': None, 'diff_file': None, 'paths': ['scripts/levels.py'],
                              'files': [{'path': 'scripts/levels.py', 'ranges': [[1, 9]]}], 'excluded': []}
    d['metadata']['coverage'] = None
    if 'language_coverage' in mfile:
        d['metadata']['language_coverage'] = mfile['language_coverage']
    d['findings'] = [{'id': 'F1', 'triage': 'missing_case', 'mutant_ids': ['B1'], 'file': 'scripts/levels.py',
                      'function': 'band', 'summary': 'No reading of exactly 300 cm.', 'reason': 'The boundary is untested.',
                      'should_have_caught': None, 'suggestion': {'test_file': 'tests/test_levels.py', 'code': None,
                                                                 'assertion_or_input': 'A reading of 300 cm.'},
                      'proof_ids': [], 'criticality': 1, 'criticality_reason': 'A display band.', 'kind_factor': None,
                      'proximity': None, 'score': None, 'priority': None}]
    d['summary']['headline'] = 'F1: no test reads exactly 300 cm in the tide band helper.'
    v.write_summary(d, repo)
    return repo, d, mfile


def case_report_language_limitation(tmp: Path) -> None:
    """normalize_results.py records which language dominates the product and whether the mutated files are tooling;
    the validator then requires limitations saying Rust was not mutated and that scripts/ is tooling."""
    import validate_mutation_report as v
    repo, d, mfile = lc_report(tmp)
    lc = mfile.get('language_coverage') or {}
    assert lc.get('unmutated_dominant') == 'Rust', lc
    assert lc.get('tooling_scope_files') == ['scripts/levels.py'], lc
    d['limitations'] = []
    errs, _ = v.check(d, mfile, None, None, repo)
    assert any('Rust' in e and 'limitation' in e for e in errs), errs
    assert any('tooling' in e and 'limitation' in e for e in errs), errs
    d['limitations'] = ['Rust, the product language, was not mutated: cargo-mutants is not installed here.',
                        'scripts/levels.py is tooling (a helper script), not the product.']
    errs, _ = v.check(d, mfile, None, None, repo)
    assert not errs, errs


def case_headline_names_p0(tmp: Path) -> None:
    """A placeholder headline is rejected: it must name every P0 finding by id."""
    import validate_mutation_report as v
    repo, d = go_report_fixture(tmp)
    d = fit_schema(d)
    d['summary']['headline'] = 'HEADLINE PLACEHOLDER TEXT'
    v.write_summary(d, repo)
    assert d['findings'][0]['priority'] == 'P0', d['findings'][0]['priority']
    errs, _ = v.check(d, None, None, None, repo)
    assert any('headline' in e and 'F1' in e for e in errs), errs
    d['summary']['headline'] = 'F1: no test weighs a parcel of exactly 20 kg, so the heavy band can move.'
    errs, _ = v.check(d, None, None, None, repo)
    assert not [e for e in errs if 'headline' in e], errs


def case_schema_version_hint(tmp: Path) -> None:
    """A wrong schema_version says which version to write."""
    import validate_mutation_report as v
    repo, d = go_report_fixture(tmp)
    d = fit_schema(d)
    current = d['schema_version']
    d['schema_version'] = '1.0'
    errs, _ = v.check(d, None, None, None, repo)
    assert any('schema_version' in e and f'write "{current}"' in e for e in errs), errs


def case_render_path_scope(tmp: Path) -> None:
    """The rendered header names the paths of a path scope, not "path (path)"."""
    import validate_mutation_report as v
    repo, d, _ = lc_report(tmp)
    d['limitations'] = ['Rust was not mutated.', 'scripts/levels.py is tooling.']
    p = write_json(tmp / 'report.json', d)
    res = run('render_report.py', p, '--color', 'never')
    assert res.returncode == 0, res.stderr
    head = next(l for l in res.stdout.splitlines() if l.startswith('Scope:'))
    assert 'path (path)' not in head and 'scripts/levels.py' in head, head


def case_path_scope_records_head(tmp: Path) -> None:
    """path_scope.py ties the scope to the commit it read (source.head), like diff_scope.py."""
    repo = py_shop(tmp / 'repo')
    git = lambda *a: subprocess.run(['git', *a], cwd=repo, capture_output=True, text=True)  # noqa: E731
    if shutil.which('git') is None:
        raise Skip('git is not on PATH')
    git('init', '-q')
    git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'add', '-A')
    git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'init')
    head = git('rev-parse', 'HEAD').stdout.strip()
    res = run('path_scope.py', '--repo', repo, '--path', 'shop', '--out', tmp / 'scope.json')
    assert res.returncode == 0, res.stderr
    scope = json.loads((tmp / 'scope.json').read_text(encoding='utf-8'))
    assert scope['source']['head'] == head, scope['source']


def case_operator_keys_and_defaults(tmp: Path) -> None:
    """A string used as a dict key (subscript, .get) or held in a default collection decides behaviour: it ranks as
    a value, never as display text."""
    import ast
    import builtin_mutator as bm
    tree = ast.parse(LEVELS_PY + "\n\ndef claims(c, platform):\n    return c.get('platforms', [platform])\n\n\n"
                     "def kinds(x=('sea', 'river')):\n    return x\n")
    ops = {}
    for node, _rep, op in bm.candidates(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            ops[node.value] = op
    for key in ('station', 'cm', 'platforms', 'north', 'sea', 'river'):
        assert ops.get(key) == 'constant_string_data', (key, ops.get(key))
    import normalize_results as nr
    assert nr.kind_of('constant_string_data') == 'value'
    assert nr.refine_string('constant_string', "    station = reading.get('station', 'x')") == 'constant_string_data'


def case_builtin_budget_estimate_measured(tmp: Path) -> None:
    """The plan's estimate counts the measured start-up of a test run (collection, conftest), not a fixed second.

    The conftest sleeps 2 s, so every run's start-up takes at least 2 s of wall time whatever the machine's load (load
    only lengthens it): a measured start-up, and so the estimate per mutant, is at least that floor, while a fixed or
    missing start-up is not. The estimate is not compared with the measured per-mutant times: those are wall-clock
    times taken later, and a machine whose load changes between the estimate and the runs moves their ratio anywhere
    (0.26 when a CPU load started after the plan was printed), which made this check flaky."""
    conftest = 'import time\n\ntime.sleep(2)\n' + PY_CONFTEST
    repo = py_shop(tmp / 'repo', conftest=conftest)
    (repo / 'tests' / 'test_other.py').write_text(
        'from shop.prices import load_rates\n\n\n' +
        ''.join(f"def test_rate_{i}():\n    assert load_rates()['tea'] == 3\n\n\n" for i in range(12)), encoding='utf-8')
    n = len(PY_PRICES.splitlines())
    scope = write_json(tmp / 'scope.json', scope_for({'shop/prices.py': [[10, n]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '5',
              '--max-mutants', '4')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    ran = [m for m in out['mutants'] if 'duration_s' in m]
    assert len(ran) == 4, [(m['id'], m['status']) for m in out['mutants']]
    floor = 2.0   # the conftest's sleep: a lower bound on any run's start-up, under any load
    assert all(m['duration_s'] >= floor for m in ran), [m['duration_s'] for m in ran]
    estimate = out.get('estimate') or {}
    startup, per, est = estimate.get('startup_s'), estimate.get('per_mutant_planned_s'), estimate.get('planned_s')
    assert startup is not None and startup >= floor, f'measured start-up {startup}s is under the {floor}s conftest sleep'
    assert per is not None and per >= startup - 0.05, f'estimate {per}s per mutant is under the measured start-up {startup}s'
    assert est is not None and abs(est - per * len(ran)) <= 0.1 * len(ran), (est, per, len(ran))
    assert f'start-up {startup:.1f}s' in res.stdout, res.stdout[-800:]


def ut_repo(root: Path) -> Path:
    repo = rust_tooling_repo(root, rust_fns=1)
    (repo / 'pytest.py').write_text(NO_PYTEST, encoding='utf-8')
    return repo


def case_builtin_unittest_runner(tmp: Path) -> None:
    """A unittest suite (CI runs `python -m unittest`) is run with unittest itself, never pytest: coverage per
    TestCase method, mutants killed and survived, ids in the file::Class::method form."""
    repo = ut_repo(tmp / 'repo')
    scope = write_json(tmp / 'scope.json', scope_for({'scripts/levels.py': [[1, 9]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '5',
              '--runner', 'unittest', '--unittest-args', '-s tests')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    assert out.get('runner') == 'unittest', out.get('runner')
    st = {(m['line'], m['operator']): m for m in out['mutants']}
    boundary = st[(2, 'comparison_boundary')]
    assert boundary['status'] == 'survived', boundary
    assert boundary['covering_tests'] == ['tests/test_levels.py::LevelsTest::test_band',
                                          'tests/test_levels.py::LevelsTest::test_describe'], boundary['covering_tests']
    neg = st[(2, 'negate_condition')]
    assert neg['status'] == 'killed' and neg['killed_by'].startswith('tests/test_levels.py::LevelsTest::'), neg
    assert out['tree']['changed'] == [], out['tree']


def case_prove_kill_unittest(tmp: Path) -> None:
    """prove_kill.py proves a unittest TestCase method with unittest (pytest is not used)."""
    repo = ut_repo(tmp / 'repo')
    muts = write_json(tmp / 'mutants.json', {'mutants': [
        {'id': 'B1', 'engine': 'builtin', 'file': 'scripts/levels.py', 'line': 2, 'end_line': 2,
         'original': '    if cm >= 300:', 'mutated': '    if cm > 300:'}]})
    code = tmp / 'new_test.py'
    code.write_text("class BoundaryTest(unittest.TestCase):\n    def test_300_is_high(self):\n"
                    "        self.assertEqual(levels.band(300), 'high')\n", encoding='utf-8')
    res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'B1', '--test-file', 'tests/test_levels.py',
              '--test-code', code, '--test-name', 'BoundaryTest.test_300_is_high', '--run-dir', tmp / 'run',
              '--out', tmp / 'proofs.json', '--runner', 'unittest', '--unittest-args', '-s tests')
    assert (tmp / 'proofs.json').exists(), res.stdout[-800:] + res.stderr[-800:]
    p = json.loads((tmp / 'proofs.json').read_text(encoding='utf-8'))['proofs'][0]
    assert p['original_run']['test_status'] == 'passed', p['original_run']
    assert 'unittest' in p['original_run']['command'], p['original_run']['command']
    assert p['mutants'][0]['verdict'] == 'kills', p['mutants'][0]
    assert p['test_id'] == 'tests/test_levels.py::BoundaryTest::test_300_is_high', p['test_id']


REPORT_PY = '''import sys


def summary(levels):
    high = [x for x in levels if x >= 300]
    return f'{len(high)} high'


if __name__ == '__main__':
    print(summary([int(a) for a in sys.argv[1:]]))
'''
REPORT_TEST = '''import os
import subprocess
import sys

HERE = os.path.dirname(__file__)


def test_cli_prints_summary():
    out = subprocess.run([sys.executable, os.path.join(HERE, '..', 'tools', 'report.py'), '400', '10'],
                         capture_output=True, text=True).stdout
    assert out.strip() == '1 high'
'''


def case_builtin_subprocess_only_coverage(tmp: Path) -> None:
    """A file the tests only run as a child process has no in-process coverage: its mutants are no_coverage, with a
    note naming the test that launches it."""
    repo = tmp / 'repo'
    (repo / 'tools').mkdir(parents=True)
    (repo / 'tests').mkdir()
    (repo / 'tools' / 'report.py').write_text(REPORT_PY, encoding='utf-8')
    (repo / 'tests' / 'test_report.py').write_text(REPORT_TEST, encoding='utf-8')
    scope = write_json(tmp / 'scope.json', scope_for({'tools/report.py': [[1, 10]]}))
    res = run('builtin_mutator.py', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run', '--minutes', '2')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    out = json.loads((tmp / 'run' / 'builtin.json').read_text(encoding='utf-8'))
    unc = [m for m in out['mutants'] if m['status'] == 'no_coverage']
    assert unc, [(m['id'], m['status']) for m in out['mutants']]
    assert all('tests/test_report.py' in m.get('note', '') and 'child process' in m.get('note', '') for m in unc), \
        [m.get('note') for m in unc][:2]


CARGO_EXAMPLE = SKILL / 'examples' / 'cargo-mutants'


def cargo_scope(tmp: Path) -> Path:
    return write_json(tmp / 'scope.json', scope_for({'src/lib.rs': [[1, 36]]}))


def case_cargo_mutants_normalise(tmp: Path) -> None:
    """cargo-mutants' outcomes.json (a synthetic recording in its format, committed under examples/cargo-mutants) is
    normalised: statuses, operators from the genre and the replaced text, the original lines, and killed_by from the
    mutant's log."""
    res = run('normalize_results.py', '--scope', cargo_scope(tmp), '--repo', CARGO_EXAMPLE / 'tide-cli',
              '--cargo-mutants', CARGO_EXAMPLE / 'mutants.out', '--out', tmp / 'mutants.json')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    data = json.loads((tmp / 'mutants.json').read_text(encoding='utf-8'))
    e = data['engines'][0]
    assert e['name'] == 'cargo-mutants' and e['version'] == '25.3.1' and e['run_by'] == 'external', e
    assert e['counts'] == {'killed': 4, 'survived': 5, 'timeout': 1}, e['counts']
    ms = data['mutants']
    assert [m['id'] for m in ms][:2] == ['C1', 'C2'], [m['id'] for m in ms]
    by = {(m['line'], m['mutated'].strip()): m for m in ms}
    b = by[(5, 'if height_cm > 300 {')]
    assert b['operator'] == 'comparison_boundary' and b['status'] == 'survived' and b['function'] == 'band', b
    assert b['original'] == '    if height_cm >= 300 {', b['original']
    assert 'coverage' in b.get('note', ''), b.get('note')
    lt = by[(5, 'if height_cm < 300 {')]
    assert lt['status'] == 'killed' and lt['killed_by'] == 'tide_cli::tests::bands', lt
    assert by[(17, '24 * 60 + from + to')]['operator'] == 'arithmetic'
    assert by[(23, 'pilot && height_cm > draught_cm + 50')]['operator'] == 'logical'
    fv = [m for m in ms if m['engine_operator'] == 'FnValue']
    assert fv and all(m['operator'] == 'return_value' and m['kind'] == 'value' for m in fv), fv[:1]
    assert any(m['status'] == 'timeout' for m in fv)


FAKE_CARGO = '''import shutil, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['mutants', '--version']:
    print('cargo-mutants 25.3.1')
    sys.exit(0)
if args[:1] == ['mutants']:
    out = Path(args[args.index('--output') + 1])
    shutil.copytree(sys.argv[0].rsplit('fake_cargo.py', 1)[0] + 'mutants.out', out / 'mutants.out')
    sys.exit(2)    # cargo-mutants exits 2 when some mutants were missed
print('error: no such command', file=sys.stderr)
sys.exit(101)
'''


def case_run_engine_cargo_mutants(tmp: Path) -> None:
    """run_engine.py cargo-mutants runs cargo-mutants (here a stand-in that replays the recorded output) with the tree
    hashed before and after and the code hash recorded, so normalize_results.py counts it as the skill's own run; a
    missing cargo-mutants is refused with exit 2 and the install command."""
    repo = tmp / 'tide-cli'
    shutil.copytree(CARGO_EXAMPLE / 'tide-cli', repo)
    fake_dir = tmp / 'fake'
    fake_dir.mkdir()
    shutil.copytree(CARGO_EXAMPLE / 'mutants.out', fake_dir / 'mutants.out')
    (fake_dir / 'fake_cargo.py').write_text(FAKE_CARGO, encoding='utf-8')
    cargo = f'"{sys.executable}" "{fake_dir / "fake_cargo.py"}"'
    scope = cargo_scope(tmp)
    res = run('run_engine.py', 'cargo-mutants', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run',
              '--cargo', cargo)
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    rec = json.loads((tmp / 'run' / 'cargo-mutants.run.json').read_text(encoding='utf-8'))
    assert rec['status'] == 'ran' and rec['tree_changed'] == [] and len(rec['code_hash']) == 64, rec
    assert '--file src/lib.rs' in rec['engine']['command'], rec['engine']['command']
    res = run('normalize_results.py', '--scope', scope, '--repo', repo, '--cargo-mutants', tmp / 'run' / 'mutants.out',
              '--cargo-mutants-run', tmp / 'run' / 'cargo-mutants.run.json', '--out', tmp / 'mutants.json')
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-800:]
    e = json.loads((tmp / 'mutants.json').read_text(encoding='utf-8'))['engines'][0]
    assert e['run_by'] == 'skill' and e['tree_changed'] == [] and e.get('code_hash') == rec['code_hash'], e
    (fake_dir / 'fake_cargo.py').write_text("import sys\nprint('error: no such command: `mutants`', file=sys.stderr)\n"
                                           "sys.exit(101)\n", encoding='utf-8')
    res = run('run_engine.py', 'cargo-mutants', '--repo', repo, '--scope', scope, '--run-dir', tmp / 'run2',
              '--cargo', cargo)
    assert res.returncode == 2 and 'cargo install' in res.stderr, (res.returncode, res.stderr[-400:])


def case_prove_kill_rust(tmp: Path) -> None:
    """prove_kill.py proves a Rust test with cargo test (run_tests.py's cargo preset ids) in a scratch copy: it
    passes on the original and fails on the mutant; the repository is untouched."""
    import run_tests
    usable, why = run_tests.cargo_usable()   # a rustup proxy with no usable toolchain is not cargo
    if not usable:
        raise Skip(why)
    repo = tmp / 'tide-cli'
    shutil.copytree(CARGO_EXAMPLE / 'tide-cli', repo)
    muts = write_json(tmp / 'mutants.json', {'mutants': [
        {'id': 'C2', 'engine': 'cargo-mutants', 'file': 'src/lib.rs', 'line': 5, 'end_line': 5,
         'original': '    if height_cm >= 300 {', 'mutated': '    if height_cm > 300 {'}]})
    code = tmp / 'new_test.rs'
    code.write_text('#[test]\nfn high_from_300() {\n    assert_eq!(tide_cli::band(300), "high");\n}\n', encoding='utf-8')
    before = (repo / 'tests' / 'crossing.rs').read_text(encoding='utf-8')
    # a build directory shared by two proofs (as with --cargo-target-dir): the second proof's copy must not reuse the
    # first proof's last build, which was a mutant
    shared = Path(os.environ['CARGO_TARGET_DIR']) / 'mt-self-test' if os.environ.get('CARGO_TARGET_DIR') else \
        tmp / 'cargo-target'
    try:
        for i in (1, 2):
            res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'C2', '--test-file',
                      'tests/crossing.rs', '--test-code', code, '--test-name', 'high_from_300', '--run-dir',
                      tmp / f'run{i}', '--out', tmp / 'proofs.json', '--cargo-target-dir', shared)
            assert (tmp / 'proofs.json').exists(), res.stdout[-1200:] + res.stderr[-1200:]
            p = json.loads((tmp / 'proofs.json').read_text(encoding='utf-8'))['proofs'][-1]
            assert p['original_run']['test_status'] == 'passed', (i, p['original_run'])
            assert p['original_run']['command'].startswith('cargo test'), p['original_run']['command']
            assert p['test_id'] == 'crossing::high_from_300', p['test_id']
            assert p['mutants'][0]['verdict'] == 'kills', (i, p['mutants'][0])
            assert p['repo_files_changed'] == [] and (repo / 'tests' / 'crossing.rs').read_text(encoding='utf-8') == before
    finally:
        shutil.rmtree(shared, ignore_errors=True)


CASES = [case_builtin_go_engine, case_builtin_scope_outside_repo, case_builtin_unsupported_language, case_ranges_go_only, case_mte_engine_label,
         case_gremlins_operators, case_duplicate_mutants_merged, case_proximity_go, case_proximity_unparsed,
         case_completeness_external_engine, case_prove_kill_go, case_prove_kill_unsupported, case_prove_kill_cmd_shim_metacharacters,
         case_builtin_import_time_coverage, case_builtin_budget_uses_covering_tests, case_prove_kill_original_timeout,
         case_prove_kill_default_timeout, case_prove_kill_collection_error,
         case_detect_tools_unscripted_dominant, case_report_language_limitation, case_headline_names_p0,
         case_schema_version_hint, case_render_path_scope, case_path_scope_records_head,
         case_operator_keys_and_defaults, case_builtin_budget_estimate_measured, case_builtin_unittest_runner,
         case_prove_kill_unittest, case_builtin_subprocess_only_coverage, case_cargo_mutants_normalise,
         case_run_engine_cargo_mutants, case_prove_kill_rust]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--only', help='run only cases whose name contains this')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)
    failed = skipped = 0
    for case in CASES:
        name = case.__name__[5:]
        if args.only and args.only not in name:
            continue
        with tempfile.TemporaryDirectory(prefix='mt-self-') as d:
            try:
                case(Path(d))
                print(f'ok    {name}')
            except Skip as exc:
                skipped += 1
                print(f'SKIP  {name}: {exc}')
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f'FAIL  {name}: {type(exc).__name__}: {str(exc)[:500]}')
                if args.verbose:
                    traceback.print_exc()
    print(f'{"FAILED" if failed else "passed"}: {failed} failed, {skipped} skipped')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
