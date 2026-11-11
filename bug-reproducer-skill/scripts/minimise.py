#!/usr/bin/env python3
"""Cut a reproduction down to its minimal steps with delta debugging (ddmin) (Playwright for Python).

    python minimise.py spec.json --candidate A1 --out minimal.json [--runs 2] [--need 2] [--artifacts DIR] [--base DIR]

Each subset of the candidate's steps is run `runs` times through reproduce.py; it still fails
when at least `need` of those runs match the spec's failure checks (default: all of them, so an
intermittent failure doesn't make a step look removable when it is not). The result is
1-minimal: removing any single remaining step loses the failure. The final steps are confirmed
with 3 more runs, recorded with their artifacts, and written as a runnable spec beside --out
(`<key>.minimal.spec.json`) so the reproduction can be re-run with reproduce.py.

Only steps are minimised; input data is left as it is, and the report says so.
"""
from __future__ import annotations
import argparse, copy, json, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reproduce  # noqa: E402


def ddmin(steps: list, fails, log: list) -> list:
    n = 2
    while len(steps) >= 2:
        size = len(steps) // n
        chunks = [steps[i:i + size] for i in range(0, len(steps), size)] if size else [[s] for s in steps]
        reduced = False
        for i, chunk in enumerate(chunks):
            complement = [s for j, c in enumerate(chunks) if j != i for s in c]
            for cand in (chunk, complement):
                if cand and fails(cand):
                    steps, n, reduced = cand, max(n - 1, 2) if cand is complement else 2, True
                    break
            if reduced:
                break
        if not reduced:
            if n >= len(steps):
                break
            n = min(len(steps), n * 2)
    return steps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('spec', type=Path)
    ap.add_argument('--candidate', required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--need', type=int)
    ap.add_argument('--artifacts', type=Path)
    ap.add_argument('--base', type=Path)
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    cand = next(c for c in spec['candidates'] if c['id'] == args.candidate)
    need = args.need or args.runs
    base = args.base or args.out.parent
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='ddmin-'))
    log, cache, counter = [], {}, [0]

    def fails(steps: list) -> bool:
        key = json.dumps(steps, sort_keys=True)
        if key in cache:
            return cache[key]
        counter[0] += 1
        trial = {**copy.deepcopy(cand), 'id': f'T{counter[0]}', 'runs': args.runs, 'steps': steps}
        runs = reproduce.run_spec({**spec, 'candidates': [trial]}, tmp, tmp)
        m = sum(r['result'] == 'matched' for r in runs)
        cache[key] = m >= need
        log.append({'trial': trial['id'], 'steps': [cand['steps'].index(s) + 1 for s in steps], 'matched': m, 'runs': len(runs),
                    'still_fails': cache[key]})
        return cache[key]

    if not fails(cand['steps']):
        print(f'{spec["key"]} {cand["id"]}: the full steps did not fail {need} of {args.runs} times; nothing to minimise')
        args.out.write_text(json.dumps({'key': spec['key'], 'candidate': cand['id'], 'minimised': False, 'trials': log}, indent=2) + '\n',
                            encoding='utf-8', newline='\n')
        return 1
    minimal = ddmin(list(cand['steps']), fails, log)
    final = {**copy.deepcopy(cand), 'id': 'M1', 'label': f'minimal reproduction of {cand["id"]}', 'variation': cand.get('variation'),
             'runs': 3, 'steps': minimal}
    min_spec = {**spec, 'candidates': [final]}
    spec_path = args.out.with_name(f'{spec["key"]}.minimal.spec.json')
    spec_path.write_text(json.dumps(min_spec, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    art = args.artifacts or args.out.parent / 'artifacts' / spec['key']
    confirm = reproduce.run_spec(min_spec, art, base)
    removed = [{'index': i + 1, 'step': s} for i, s in enumerate(cand['steps']) if s not in minimal]
    out = {'key': spec['key'], 'candidate': cand['id'], 'minimised': True, 'runs_per_trial': args.runs, 'need': need,
           'original_steps': cand['steps'], 'steps': minimal, 'removed_steps': removed, 'trials': log,
           'confirmation': confirm, 'confirmed': sum(r['result'] == 'matched' for r in confirm),
           'script_path': spec_path.resolve().relative_to(base.resolve()).as_posix(),
           'limitations': ['Only steps were minimised; input data and environment were kept as in the candidate.']}
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{spec["key"]}: {len(cand["steps"])} -> {len(minimal)} steps in {len(log)} trials; confirmed {out["confirmed"]} of 3')
    return 0


if __name__ == '__main__':
    sys.exit(main())
