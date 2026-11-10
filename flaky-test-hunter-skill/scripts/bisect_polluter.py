#!/usr/bin/env python3
"""Find the test that makes an order-dependent victim fail, by bisection (stdlib only).

    python bisect_polluter.py --runs runs.json --from-run R-12 --victim tests.test_fares::test_cache_starts_empty \
        --cwd <repo> [--preset pytest] [--out bisect.json]

Takes the order of a run in which the victim failed, keeps the tests that ran before it, and
bisects them with run_tests.py: each step runs half of the candidates followed by the victim.
Every step is a real run appended to runs.json with label "bisect", so the result can be checked.

The result is a minimal pair only when three runs agree: the victim alone passes, the polluter
then the victim fails, and (from the bisection) the other half did not make it fail. If the
failure needs more than one earlier test, or the victim also fails alone, it says so instead of
naming a polluter. Works for presets that run explicit test ids in the order given (pytest).
"""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def node_id(case_id: str, file: str | None) -> str:
    """parse_results case id (tests.test_fares::name) -> pytest node id (tests/test_fares.py::name)."""
    cls, _, name = case_id.partition('::')
    name = name.split('#')[0]
    if file:
        return f'{file}::{name}'
    return cls.replace('.', '/') + '.py::' + name


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
        rid = new[-1]['id']
        victim = next((c for c in data['cases'] if c['run_id'] == rid and c['id'].split('#')[0] == self.args.victim), None)
        status = victim['status'] if victim else 'not_run'
        self.steps.append({'run_id': rid, 'why': why, 'tests': tests, 'victim_status': status})
        print(f'{rid}: {why}: victim {status}')
        return rid, status

    def _ids(self) -> set[str]:
        p = Path(self.args.runs)
        return {r['id'] for r in json.loads(p.read_text(encoding='utf-8'))['runs']} if p.exists() else set()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', type=Path, required=True)
    ap.add_argument('--from-run', required=True, help='a run in which the victim failed')
    ap.add_argument('--victim', required=True, help='case id as in runs.json')
    ap.add_argument('--cwd', type=Path, required=True)
    ap.add_argument('--preset', default='pytest')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    data = json.loads(args.runs.read_text(encoding='utf-8'))
    run = next((r for r in data['runs'] if r['id'] == args.from_run), None)
    if not run:
        raise SystemExit(f'run {args.from_run} not found')
    cases = [c for c in data['cases'] if c['run_id'] == args.from_run]
    vic = next((c for c in cases if c['id'].split('#')[0] == args.victim), None)
    if not vic or vic['status'] not in ('failed', 'error'):
        raise SystemExit(f'{args.victim} did not fail in {args.from_run}')
    ids = {c['id'].split('#')[0]: node_id(c['id'], c.get('file')) for c in cases}
    order = run['tests_selected'] or [ids[c['id'].split('#')[0]] for c in cases]
    victim_node = ids[args.victim]
    if victim_node not in order:
        raise SystemExit(f'{victim_node} is not in the recorded order of {args.from_run}')
    before = order[:order.index(victim_node)]
    r = Runner(args)
    result = {'victim': args.victim, 'from_run': args.from_run, 'candidates': len(before), 'polluter': None,
              'outcome': None, 'steps': r.steps}

    _, alone = r.run([victim_node], 'victim alone')
    if alone != 'passed':
        result['outcome'] = 'fails_alone'
    else:
        _, full = r.run(before + [victim_node], 'all earlier tests then victim')
        if full == 'passed':
            result['outcome'] = 'not_reproduced'
        else:
            cand = before
            while len(cand) > 1:
                half = len(cand) // 2
                first, second = cand[:half], cand[half:]
                _, s1 = r.run(first + [victim_node], f'first {len(first)} of {len(cand)} then victim')
                if s1 != 'passed':
                    cand = first
                    continue
                _, s2 = r.run(second + [victim_node], f'last {len(second)} of {len(cand)} then victim')
                if s2 != 'passed':
                    cand = second
                    continue
                result['outcome'] = 'needs_several'
                result['remaining'] = cand
                break
            if result['outcome'] is None:
                _, pair = r.run([cand[0], victim_node], 'polluter then victim')
                if pair != 'passed':
                    result.update(outcome='minimal_pair', polluter=cand[0])
                else:
                    result['outcome'] = 'not_reproduced'
    text = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    print(f"outcome: {result['outcome']}" + (f"; polluter {result['polluter']}" if result['polluter'] else ''))
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
