#!/usr/bin/env python3
"""Find the earlier test an order-dependent test depends on, by bisection (stdlib only).

    python bisect_polluter.py --runs runs.json --from-run R-12 --victim tests.test_fares::test_cache_starts_empty \
        --cwd <repo> [--preset pytest] [--relation polluter|enabler] [--out bisect.json]

Takes the order of a run, keeps the tests that ran before the victim, and bisects them with
run_tests.py: each step runs half of the candidates followed by the victim. Every step is a real
run appended to runs.json with label "bisect", so the result can be checked.

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
Vitest and Jest rerun `file -t pattern` in file order, node:test has no id filter, so "the polluter,
then the victim" cannot be expressed. For any other preset the script runs nothing, writes
`outcome: not_supported` with the reason (to --out if given) and exits 2. Find order dependence
there with fixed shuffle seeds instead (SKILL.md Step 4).
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUPPORTED_PRESETS = ('pytest',)  # runners that run explicit ids in the order given
REPEAT_SUFFIX = re.compile(r'#\d+$')


def base_id(case_id: str) -> str:
    """Drop the `#2` suffix parse_results adds to a repeated name; a `#` inside a test title stays."""
    return REPEAT_SUFFIX.sub('', case_id)


def node_id(case_id: str, file: str | None) -> str:
    """parse_results case id (tests.test_fares::name) -> pytest node id (tests/test_fares.py::name)."""
    cls, _, name = case_id.partition('::')
    name = base_id(name)
    if file:
        return f'{file}::{name}'
    return cls.replace('.', '/') + '.py::' + name


def same_test(case_id: str, node: str) -> bool:
    """A runs.json case id and a pytest node id name the same test, even when one is relative to pytest's rootdir
    and the other to --cwd (src.pkg.tests.test_x::t and pkg_dir/src/pkg/tests/test_x.py::t)."""
    cm, _, cn = base_id(case_id).partition('::')
    nm, _, nn = node.partition('::')
    if cn != nn:
        return False
    dotted = re.sub(r'\.py$', '', nm).replace('/', '.').replace('\\', '.')
    return dotted == cm or dotted.endswith('.' + cm) or cm.endswith('.' + dotted)


class Runner:
    def __init__(self, args):
        self.args, self.steps = args, []

    def run(self, tests: list[str], why: str) -> tuple[str, str]:
        """Run tests in this order; return (run_id, victim status)."""
        before = self._ids()
        cmd = [sys.executable, str(HERE / 'run_tests.py'), '--preset', self.args.preset, '--cwd', str(self.args.cwd),
               '--out', str(self.args.runs), '--label', 'bisect', '--run-prefix', 'B', '--tests', *tests]
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


def main(argv=None) -> int:
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
    order = run['tests_selected'] or [node_id(c['id'], c.get('file')) for c in cases]
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
