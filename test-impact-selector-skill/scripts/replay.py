#!/usr/bin/env python3
"""Measure how often the selection would have missed a failure, by replaying past changes (stdlib only).

    python replay.py --repo <repo> --history history.json --out replay.json [--mode strict]
        [--coverage coverage_map.json ...] [--journeys journey_map.json] [--related related.json] [--flaky flaky.json]

history.json lists past changes: {changes: [{id, diff, full_run: true, runs: [{package, junit}]}]}, each
diff applying to --repo and each run the full suite of a package after the change. For every
change with a full run, the selection is computed as select_tests.py would (co-failure history
uses only the changes before it), and every test file that failed is checked against it. A
failing test file outside the selection is a miss. Recall is selected failures / all failures,
counted per (change, failing test file), with a 95% Wilson interval. Failures of tests listed in
--flaky (a JSON list of test files or ids, or {"flaky": [...]}) are not counted and are listed
separately. Changes without a full run are skipped and listed.

Every change is selected against today's coverage map, journey map and file kinds, not the
ones that existed when it was made; replay.json records this.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import select_tests, stats  # noqa: E402


def load_flaky(path: Path | None) -> set[str]:
    if not path:
        return set()
    data = json.loads(path.read_text(encoding='utf-8'))
    items = data.get('flaky', []) if isinstance(data, dict) else data
    return {str(x).replace('\\', '/') for x in items}


def replay(repo: Path, history: Path, mode: str, coverage: list[Path], journeys: Path | None, related: Path | None,
           flaky_path: Path | None, max_chars: int = 8000) -> dict:
    entries = select_tests.load_history(history)
    flaky = load_flaky(flaky_path)
    changes, skipped, misses, flaky_failures = [], [], [], []
    total = missed = 0
    for i, h in enumerate(entries):
        if not h['full_run']:
            skipped.append({'change': h['id'], 'reason': 'no full run recorded'})
            continue
        sel = select_tests.select(repo, history.parent / h['diff'], mode=mode, coverage=coverage, journeys=journeys,
                                  history=history, related=related, max_chars=max_chars, history_entries=entries[:i])
        chosen = {s['file'] for s in sel['selected']}
        failed = []
        for f in h['failed']:
            ids = {c['id'] for c in h['cases'] if c['file'] == f and c['status'] in ('failed', 'error')}
            is_flaky = f in flaky or any(f'{f}::{x.split("::")[-1]}' in flaky or x in flaky for x in ids)
            row = {'file': f, 'selected': f in chosen, 'flaky': is_flaky, 'failed_cases': sorted(ids)}
            failed.append(row)
            if is_flaky:
                flaky_failures.append({'change': h['id'], 'test_id': f})
                continue
            total += 1
            if f not in chosen:
                missed += 1
                misses.append({'change': h['id'], 'test_id': f, 'changed_paths': h['changed'],
                               'selected_count': len(chosen), 'blind_spot_kinds': sorted({b['kind'] for b in sel['blind_spots']})})
        changes.append({'id': h['id'], 'description': h['description'], 'diff': h['diff'], 'changed_paths': h['changed'],
                        'selected_count': len(chosen), 'total_tests': sel['total_tests'], 'selected': sorted(chosen),
                        'blind_spots': [{'kind': b['kind'], 'path': b['path'], 'action': b['action']} for b in sel['blind_spots']],
                        'failed': failed})
    low, high = stats.wilson(total - missed, total)
    return {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'tool': 'replay.py', 'mode': mode,
        'inputs': {'repo': select_tests.rel_input(repo), 'history': select_tests.rel_input(history),
                   'history_sha256': hashlib.sha256(history.read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
                   'coverage': [select_tests.rel_input(c) for c in coverage], 'journeys': select_tests.rel_input(journeys),
                   'related': select_tests.rel_input(related), 'flaky': select_tests.rel_input(flaky_path)},
        'approximation': 'Each past change was selected against the current coverage map, journey map and file kinds.',
        'changes_replayed': len(changes), 'changes_skipped': skipped, 'failures_total': total, 'failures_missed': missed,
        'failures_flaky': len(flaky_failures), 'recall': round((total - missed) / total, 4) if total else None,
        'wilson_low': low if total else None, 'wilson_high': high if total else None,
        'mean_selected_share': round(sum(c['selected_count'] / c['total_tests'] for c in changes) / len(changes), 4)
        if changes else None,
        'misses': misses, 'flaky_failures': flaky_failures, 'changes': changes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--history', type=Path, required=True)
    ap.add_argument('--mode', choices=('strict', 'balanced', 'aggressive'), default='strict')
    ap.add_argument('--coverage', type=Path, action='append', default=[])
    ap.add_argument('--journeys', type=Path)
    ap.add_argument('--related', type=Path)
    ap.add_argument('--flaky', type=Path)
    ap.add_argument('--max-chars', type=int, default=8000)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = replay(args.repo, args.history, args.mode, args.coverage, args.journeys, args.related, args.flaky, args.max_chars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    if res['failures_total']:
        print(f'{res["changes_replayed"]} changes replayed: {res["failures_total"] - res["failures_missed"]} of '
              f'{res["failures_total"]} failures selected ({100 * res["recall"]:.1f}%, 95% CI '
              f'{100 * res["wilson_low"]:.1f}%-{100 * res["wilson_high"]:.1f}%), {res["failures_missed"]} missed -> {args.out}')
    else:
        print(f'{res["changes_replayed"]} changes replayed, no failures to check -> {args.out}')
    for m in res['misses']:
        print(f'  missed {m["test_id"]} in {m["change"]} ({", ".join(m["changed_paths"])})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
