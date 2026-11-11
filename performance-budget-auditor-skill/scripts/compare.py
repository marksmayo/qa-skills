#!/usr/bin/env python3
"""Compare measurements with a baseline: report a change only when it is larger than the noise.

    python compare.py baseline/candidates.json candidates.json --out regressions.json

Paths in the output are relative to the output file.

Both inputs are attribute.py outputs. For each (url, profile, cache, browser, metric) present in
both with a median, the change counts only when

    |current median - baseline median| > max(10% of the baseline median,
                                             (baseline range + current range) / 2,
                                             a floor of 10 ms, 0.01 CLS or 1 KB)

where a range is max - min of that measurement's runs. Otherwise it is `unchanged`. Measurements
that are unstable in either run are `not_compared`. Different machines are flagged: the baseline
and current measurement files record their machine, and a comparison across machines says so.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

FLOOR = {'ms': 10.0, 'score': 0.01, 'bytes': 1024.0}


def compare(base: dict, cur: dict) -> list[dict]:
    key = lambda m: (m['url'], m['profile'], m['cache'], m['browser'], m['metric'])  # noqa: E731
    b = {key(m): m for m in base['measurements']}
    out = []
    for m in cur['measurements']:
        o = b.get(key(m))
        if not o or o['median'] is None or m['median'] is None:
            continue
        entry = {'url': m['url'], 'profile': m['profile'], 'cache': m['cache'], 'browser': m['browser'], 'metric': m['metric'],
                 'unit': m['unit'], 'baseline_median': o['median'], 'current_median': m['median']}
        if 'unstable' in (o['status'], m['status']):
            entry.update(change=None, change_pct=None, threshold=None, direction='not_compared')
            out.append(entry)
            continue
        change = m['median'] - o['median']
        threshold = max(0.10 * abs(o['median']), ((o['max'] - o['min']) + (m['max'] - m['min'])) / 2, FLOOR[m['unit']])
        direction = 'unchanged' if abs(change) <= threshold else ('regressed' if change > 0 else 'improved')
        entry.update(change=round(change, 4), change_pct=round(100 * change / o['median'], 1) if o['median'] else None,
                     threshold=round(threshold, 4), direction=direction)
        out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('baseline', type=Path)
    ap.add_argument('current', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    base = json.loads(a.baseline.read_text(encoding='utf-8'))
    cur = json.loads(a.current.read_text(encoding='utf-8'))
    machines = []
    for c, p in ((base, a.baseline), (cur, a.current)):
        mp = p.parent / c['measurements_file']
        machines.append(json.loads(mp.read_text(encoding='utf-8'))['machine'] if mp.exists() else None)
    rows = compare(base, cur)
    rel = lambda f: Path(os.path.relpath(f.resolve(), a.out.resolve().parent)).as_posix()  # noqa: E731
    out = {'baseline': rel(a.baseline), 'current': rel(a.current), 'same_machine': machines[0] == machines[1],
           'comparisons': rows}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    counts = {}
    for r in rows:
        counts[r['direction']] = counts.get(r['direction'], 0) + 1
    print(f'{len(rows)} comparison(s) {counts}; same machine: {out["same_machine"]} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
