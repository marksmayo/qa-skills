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
    (repo / 'tests').mkdir(parents=True)
    (repo / 'src').mkdir()
    (repo / 'src' / 'lib.rs').write_text('pub fn add(a: i32) -> i32 {\n    a + 1\n}\n', encoding='utf-8')
    muts = write_json(tmp / 'mutants.json', {'mutants': [{'id': 'X1', 'engine': 'cargo-mutants', 'file': 'src/lib.rs',
                                                          'line': 2, 'end_line': 2, 'original': '    a + 1',
                                                          'mutated': '    a - 1'}]})
    code = tmp / 't.rs'
    code.write_text('#[test]\nfn adds() { assert_eq!(add(1), 2); }\n', encoding='utf-8')
    res = run('prove_kill.py', '--repo', repo, '--mutants', muts, '--mutant', 'X1', '--test-file', 'tests/add.rs',
              '--test-code', code, '--test-name', 'adds', '--run-dir', tmp / 'run', '--out', tmp / 'proofs.json')
    assert res.returncode != 0, 'exit 0'
    text = res.stderr + res.stdout
    assert 'Traceback' not in text and '.rs' in text and 'no runner' in text, text[-600:]
    assert 'not run' not in text and not (tmp / 'proofs.json').exists(), 'ran a runner on a Rust test file'
    assert not (tmp / 'run').exists() or not any((tmp / 'run').iterdir()), 'wrote a scratch copy before refusing'


CASES = [case_builtin_go_engine, case_builtin_unsupported_language, case_ranges_go_only, case_mte_engine_label,
         case_gremlins_operators, case_duplicate_mutants_merged, case_proximity_go, case_proximity_unparsed,
         case_completeness_external_engine, case_prove_kill_go, case_prove_kill_unsupported]


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
