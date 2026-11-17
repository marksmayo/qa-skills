#!/usr/bin/env python3
"""Find the earlier test an order-dependent test depends on, by bisection (stdlib only).

    python bisect_polluter.py --runs runs.json --from-run R-12 --victim tests.test_fares::test_cache_starts_empty \
        --cwd <repo> [--preset pytest] [--relation polluter|enabler] [--out bisect.json] [--env K=V ...]
    python bisect_polluter.py --self-test

Takes the order of a run, keeps the tests that ran before the victim, and bisects them with
run_tests.py: each step runs half of the candidates followed by the victim. Every step is a real
run appended to runs.json with label "bisect", so the result can be checked.

The order is the run's own: the ids it was started with, or, when it was started from a file, a
class or a directory (`--tests tests`, the usual baseline), the order its cases were recorded in.
Class-based tests work in both forms: a unittest.TestCase or pytest class case id
`tests.test_fares.FareTest::test_cache` is run as `tests/test_fares.py::FareTest::test_cache`.

--env K=V ... is passed to every step's run_tests.py, so the steps run in the environment the
from-run had (the network guard, a scratch home, PYTEST_ADDOPTS, a variable the tests need).

Two relations:
- polluter (default): --from-run is a run in which the victim FAILED. Something earlier breaks it.
  A minimal pair needs: the victim alone passes, the polluter then the victim fails, and (from
  the bisection) the other half did not make it fail.
- enabler: --from-run is a run in which the victim PASSED, and the victim fails alone. Something
  earlier sets up what it needs (a module reloaded with other settings, a cache filled, a fixture
  left behind). A minimal pair needs: the victim alone fails, the enabler then the victim passes.

If more than one earlier test is needed, or the victim behaves the same alone, it says so instead of
naming one (needs_several, fails_alone, passes_alone, not_reproduced). A run that errored (a crash,
a collection error, an id pytest could not find) stops the bisection with outcome `errored`.

The output names the test in both forms: `polluter` is the case id as runs.json records it (the
form a report uses), `polluter_node_id` the pytest node id that was run.

Only pytest is supported: bisection needs a runner that runs explicit test ids in the order given.
Vitest and Jest rerun `file -t pattern` in file order, node:test filters by a name pattern in file order too, so "the polluter,
then the victim" cannot be expressed. For any other preset the script runs nothing, writes
`outcome: not_supported` with the reason (to --out if given) and exits 2. Find order dependence
there with fixed shuffle seeds instead (SKILL.md Step 4).
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder (the self-test starts run_tests.py from here)

HERE = Path(__file__).resolve().parent
SUPPORTED_PRESETS = ('pytest',)  # runners that run explicit ids in the order given
REPEAT_SUFFIX = re.compile(r'#\d+$')


def base_id(case_id: str) -> str:
    """Drop the `#2` suffix parse_results adds to a repeated name; a `#` inside a test title stays."""
    return REPEAT_SUFFIX.sub('', case_id)


def split_test_id(tid: str) -> tuple[str, str]:
    """(dotted module and classes, test name) of a runs.json case id (tests.test_x.Scale::test_y) or a pytest node
    id (tests/test_x.py::Scale::test_y[case]); a '::' inside a parameter id stays in the name."""
    head, br, tail = tid.partition('[')
    parts = head.split('::')
    mod = re.sub(r'\.py$', '', parts[0]).replace('/', '.').replace('\\', '.')
    return ('.'.join([mod] + parts[1:-1]) if len(parts) > 1 else ''), parts[-1] + br + tail


def _suffix(a: str, b: str) -> bool:
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def node_id(case_id: str, file: str | None, cwd: Path | None = None) -> str:
    """parse_results case id -> pytest node id: tests.test_fares::name -> tests/test_fares.py::name, and for a test in
    a class (unittest.TestCase, a pytest class, nested classes) tests.test_fares.FareTest::name ->
    tests/test_fares.py::FareTest::name. The classes are what follows the file's module in the dotted name; with no
    file recorded, the module is the longest dotted prefix that is a file under cwd."""
    dotted, name = split_test_id(base_id(case_id))
    parts = dotted.split('.') if dotted else []
    if file:
        file = file.replace('\\', '/')
        fd = re.sub(r'\.py$', '', file).replace('/', '.')
        for k in range(len(parts), 0, -1):
            if _suffix(fd, '.'.join(parts[:k])):
                return '::'.join([file, *parts[k:], name])
        return f'{file}::{name}'
    if cwd is not None:
        for k in range(len(parts), 0, -1):
            cand = '/'.join(parts[:k]) + '.py'
            if (Path(cwd) / cand).is_file():
                return '::'.join([cand, *parts[k:], name])
    return '/'.join(parts) + '.py::' + name


def same_test(case_id: str, node: str) -> bool:
    """A runs.json case id and a pytest node id name the same test, classes included, even when one is relative to
    pytest's rootdir and the other to --cwd (src.pkg.tests.test_x.C::t and pkg_dir/src/pkg/tests/test_x.py::C::t)."""
    (cm, cn), (nm, nn) = split_test_id(base_id(case_id)), split_test_id(node)
    return cn == nn and _suffix(cm, nm)


def recorded_order(run: dict, cases: list[dict], cwd: Path | None = None) -> list[str]:
    """The node ids of a run in the order they ran. A run started with explicit test ids keeps them. A run started
    from a file, a class or a directory (`--tests tests`) records only that selection, so the order is the one its
    cases were recorded in (JUnit order: the order pytest ran them in one process)."""
    sel = list(run.get('tests_selected') or [])
    if sel and (not cases or all(any(same_test(c['id'], s) for c in cases) for s in sel)):
        return sel
    order: list[str] = []
    for c in cases:
        n = next((s for s in sel if same_test(c['id'], s)), None) or node_id(c['id'], c.get('file'), cwd)
        if n not in order:
            order.append(n)
    return order


class Runner:
    def __init__(self, args):
        self.args, self.steps = args, []

    def run(self, tests: list[str], why: str) -> tuple[str, str]:
        """Run tests in this order; return (run_id, victim status)."""
        before = self._ids()
        cmd = [sys.executable, str(HERE / 'run_tests.py'), '--preset', self.args.preset, '--cwd', str(self.args.cwd),
               '--out', str(self.args.runs), '--label', 'bisect', '--run-prefix', 'B', '--tests', *tests]
        pairs = [kv for group in self.args.env or [] for kv in group]
        if pairs:
            cmd += ['--env', *pairs]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if res.returncode not in (0,):
            raise SystemExit(f'run_tests.py failed: {res.stdout[-500:]}{res.stderr[-500:]}')
        data = json.loads(Path(self.args.runs).read_text(encoding='utf-8'))
        new = [r for r in data['runs'] if r['id'] not in before]
        run = new[-1]
        rid = run['id']
        victim = next((c for c in data['cases'] if c['run_id'] == rid and base_id(c['id']) == self.args.victim), None)
        status = 'errored' if run.get('errored') else victim['status'] if victim else 'not_run'
        self.steps.append({'run_id': rid, 'why': why, 'tests': tests, 'victim_status': status}
                          | ({'error': run.get('error')} if run.get('errored') else {}))
        print(f'{rid}: {why}: victim {status}' + (f" ({run.get('error')})" if run.get('errored') else ''))
        return rid, status

    def _ids(self) -> set[str]:
        p = Path(self.args.runs)
        return {r['id'] for r in json.loads(p.read_text(encoding='utf-8'))['runs']} if p.exists() else set()


class Errored(Exception):
    pass


SELF_TEST_FILES = {
    'tests/shared_state.py': 'STATE = {}\n',
    'tests/test_a_cache.py': (
        'import unittest\n\nimport shared_state\n\n\n'
        'class CacheTest(unittest.TestCase):\n'
        '    def test_first_lookup(self):\n        self.assertEqual(len("a"), 1)\n\n'
        '    def test_fills_cache(self):\n        shared_state.STATE["rate"] = 2\n'
        '        self.assertEqual(shared_state.STATE["rate"], 2)\n\n'
        '    def test_last_lookup(self):\n        self.assertTrue(True)\n'),
    'tests/test_b_quote.py': (
        'import os\nimport unittest\n\nimport shared_state\n\n\n'
        'class QuoteTest(unittest.TestCase):\n'
        '    def test_cache_starts_empty(self):\n'
        '        self.assertEqual(os.environ.get("QA_BISECT_FLAG"), "1")\n'
        '        self.assertEqual(shared_state.STATE, {})\n'),
}


def self_test() -> int:
    """Ids of class-based tests in both forms, the order of a run started from a path, and (when pytest is
    installed) a real bisection of a unittest.TestCase suite whose baseline was started with a directory."""
    import importlib.util
    import tempfile
    checks: list[tuple[str, bool, str]] = []
    got = node_id('tests.test_x.Scale::test_y', 'tests/test_x.py')
    checks.append(('a class-based case id becomes file::Class::test', got == 'tests/test_x.py::Scale::test_y', got))
    got = node_id('tests.test_x.Outer.Inner::test_y#2', 'tests/test_x.py')
    checks.append(('nested classes and a repeat suffix', got == 'tests/test_x.py::Outer::Inner::test_y', got))
    got = node_id('tests.test_x::test_y[a::b]', 'tests/test_x.py')
    checks.append(('a module-level test keeps its form', got == 'tests/test_x.py::test_y[a::b]', got))
    got = node_id('src.pkg.tests.test_x.Scale::test_y', 'pkg_dir/src/pkg/tests/test_x.py')
    checks.append(('a file relative to --cwd and an id relative to the rootdir', got ==
                   'pkg_dir/src/pkg/tests/test_x.py::Scale::test_y', got))
    pairs = [('tests.test_x.Scale::test_y', 'tests/test_x.py::Scale::test_y', True),
             ('tests.test_x::test_y', 'tests/test_x.py::test_y', True),
             ('tests.test_x.Scale::test_y', 'tests/test_x.py::Other::test_y', False),
             ('tests.test_x.Scale::test_y#2', 'tests/test_x.py::Scale::test_y', True),
             ('src.pkg.tests.test_x::test_y', 'pkg_dir/src/pkg/tests/test_x.py::test_y', True)]
    wrong = [(a, b) for a, b, want in pairs if same_test(a, b) != want]
    checks.append(('a case id and a node id name one test, classes included', not wrong, str(wrong)))
    cases = [{'id': 'tests.test_a.A::test_one', 'file': 'tests/test_a.py'},
             {'id': 'tests.test_a.A::test_two', 'file': 'tests/test_a.py'},
             {'id': 'tests.test_b::test_three', 'file': 'tests/test_b.py'}]
    got = recorded_order({'tests_selected': ['tests']}, cases)
    checks.append(('a run started from a directory is expanded to the order its cases ran in',
                   got == ['tests/test_a.py::A::test_one', 'tests/test_a.py::A::test_two', 'tests/test_b.py::test_three'],
                   str(got)))
    sel = ['tests/test_b.py::test_three', 'tests/test_a.py::A::test_two', 'tests/test_a.py::A::test_one']
    got = recorded_order({'tests_selected': sel}, list(reversed(cases)))
    checks.append(('a run started with explicit ids keeps them', got == sel, str(got)))

    if importlib.util.find_spec('pytest') is None:
        print('SKIP a real bisection of a unittest.TestCase suite: pytest is not installed')
    else:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / 'repo'
            for rel, text in SELF_TEST_FILES.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(text, encoding='utf-8')
            runs = Path(d) / 'runs.json'
            env = ['--env', 'QA_BISECT_FLAG=1', 'PYTHONDONTWRITEBYTECODE=1']
            base = subprocess.run([sys.executable, str(HERE / 'run_tests.py'), '--preset', 'pytest', '--cwd', str(repo),
                                   '--out', str(runs), '--label', 'baseline', '--run-prefix', 'R', '--tests', 'tests',
                                   *env], capture_output=True, text=True, encoding='utf-8', errors='replace')
            victim = 'tests.test_b_quote.QuoteTest::test_cache_starts_empty'
            out = Path(d) / 'bisect.json'
            code, detail, res = None, base.stdout[-300:] + base.stderr[-300:], {}
            if runs.is_file():
                try:
                    code = main(['--runs', str(runs), '--from-run', 'R-1', '--victim', victim, '--cwd', str(repo),
                                 '--out', str(out), *env])
                except SystemExit as e:
                    code, detail = 1, str(e)
                if out.is_file():
                    res = json.loads(out.read_text(encoding='utf-8'))
                    detail = f"outcome {res.get('outcome')}, polluter {res.get('polluter')}, {len(res.get('steps', []))} steps"
            checks.append(('a unittest.TestCase suite whose baseline ran from a directory is bisected to its polluter '
                           '(with --env passed to every step)',
                           code == 0 and res.get('outcome') == 'minimal_pair'
                           and res.get('polluter') == 'tests.test_a_cache.CacheTest::test_fills_cache'
                           and res.get('polluter_node_id') == 'tests/test_a_cache.py::CacheTest::test_fills_cache',
                           detail))
    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(('ok   ' + name) if ok else f'FAIL {name}  [{detail}]')
    print('bisect_polluter self-test ' + ('FAILED' if bad else 'passed'))
    return 1 if bad else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', type=Path, required=True)
    ap.add_argument('--from-run', required=True, help='a run in which the victim failed (polluter) or passed (enabler)')
    ap.add_argument('--victim', required=True, help='case id as in runs.json')
    ap.add_argument('--cwd', type=Path, required=True)
    ap.add_argument('--preset', default='pytest')
    ap.add_argument('--relation', choices=['polluter', 'enabler'], default='polluter',
                    help='polluter: an earlier test makes the victim fail; enabler: the victim fails alone and an '
                         'earlier test makes it pass')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V',
                    help='environment for every step, passed to run_tests.py --env (the from-run\'s own: network '
                         'guard, scratch home, PYTEST_ADDOPTS); one quoted word per pair')
    ap.add_argument('--self-test', action='store_true', help='run the built-in checks and exit')
    args = ap.parse_args(argv)
    if args.preset not in SUPPORTED_PRESETS:
        result = {'victim': args.victim, 'from_run': args.from_run, 'relation': args.relation, 'polluter': None,
                  'polluter_node_id': None, 'outcome': 'not_supported', 'steps': [],
                  'reason': f'preset {args.preset!r} cannot run explicit test ids in a given order; bisection is '
                            'pytest-only. Confirm order dependence with fixed shuffle seeds (file order passes, '
                            'two seeds fail) and name no polluter.'}
        if args.out:
            args.out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(f"outcome: not_supported ({result['reason']})")
        return 2
    enabler = args.relation == 'enabler'
    data = json.loads(args.runs.read_text(encoding='utf-8'))
    run = next((r for r in data['runs'] if r['id'] == args.from_run), None)
    if not run:
        raise SystemExit(f'run {args.from_run} not found')
    cases = [c for c in data['cases'] if c['run_id'] == args.from_run]
    vic = next((c for c in cases if base_id(c['id']) == args.victim), None)
    if enabler and (not vic or vic['status'] != 'passed'):
        raise SystemExit(f'{args.victim} did not pass in {args.from_run}; an enabler search starts from a passing run')
    if not enabler and (not vic or vic['status'] not in ('failed', 'error')):
        raise SystemExit(f'{args.victim} did not fail in {args.from_run}')
    order = recorded_order(run, cases, args.cwd)
    victim_node = next((n for n in order if same_test(args.victim, n)), None)
    if victim_node is None:
        raise SystemExit(f'{args.victim} is not in the recorded order of {args.from_run}')
    case_of = {}
    for c in cases:
        n = next((o for o in order if same_test(c['id'], o)), None)
        if n:
            case_of.setdefault(n, base_id(c['id']))
    before = order[:order.index(victim_node)]
    r = Runner(args)
    result = {'victim': args.victim, 'from_run': args.from_run, 'relation': args.relation, 'candidates': len(before),
              'polluter': None, 'polluter_node_id': None, 'outcome': None, 'steps': r.steps}
    # "bad" is the victim's state that the earlier tests cause: a failure for a polluter, a pass for an enabler
    bad = (lambda s: s == 'passed') if enabler else (lambda s: s in ('failed', 'error'))

    def step(tests, why):
        _, s = r.run(tests, why)
        if s in ('errored', 'not_run'):
            raise Errored(why)
        return s

    try:
        alone = step([victim_node], 'victim alone')
        if bad(alone):
            result['outcome'] = 'passes_alone' if enabler else 'fails_alone'
        elif not bad(step(before + [victim_node], 'all earlier tests then victim')):
            result['outcome'] = 'not_reproduced'
        else:
            cand = before
            while len(cand) > 1:
                half = len(cand) // 2
                first, second = cand[:half], cand[half:]
                if bad(step(first + [victim_node], f'first {len(first)} of {len(cand)} then victim')):
                    cand = first
                    continue
                if bad(step(second + [victim_node], f'last {len(second)} of {len(cand)} then victim')):
                    cand = second
                    continue
                result['outcome'] = 'needs_several'
                result['remaining'] = cand
                break
            if result['outcome'] is None:
                if bad(step([cand[0], victim_node], f'{args.relation} then victim')):
                    result.update(outcome='minimal_pair', polluter=case_of.get(cand[0], cand[0]),
                                  polluter_node_id=cand[0])
                else:
                    result['outcome'] = 'not_reproduced'
    except Errored as e:
        result['outcome'] = 'errored'
        result['errored_step'] = str(e)
    text = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    print(f"outcome: {result['outcome']}" + (f"; {args.relation} {result['polluter']}" if result['polluter'] else ''))
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
