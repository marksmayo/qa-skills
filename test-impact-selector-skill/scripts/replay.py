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
separately, and so are failures of tests in --baseline-failures (tests that fail at the base
before any change, e.g. only in this environment). Changes without a full run are skipped and
listed, and so is a change whose diff does not apply: it is skipped with the reason, never the
end of the replay.

A change can name where its diff applies: `base` (a git ref of --repo, checked out fresh with
`git archive` for that change), or `commit` instead of `diff` (the diff is the commit against its
parent, and the parent is the base). That makes real git history usable while --repo is at HEAD.
Go failures from gotestsum JUnit map to their _test.go files through go.mod; a package that
failed to build is `<dir>/(package)`, selected when any test file of that directory is.

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


def checkout(repo: Path, ref: str, dest: Path) -> Path:
    """A fresh copy of the repository at ref (git archive), for a change whose diff applies to that ref."""
    dest.mkdir(parents=True)
    select_tests.extract(select_tests.git(repo, 'archive', '--format=tar', ref, binary=True), dest)
    return dest


def replay(repo: Path, history: Path, mode: str, coverage: list[Path], journeys: Path | None, related: Path | None,
           flaky_path: Path | None, max_chars: int = 8000, baseline_failures: Path | None = None) -> dict:
    import tempfile
    repo = repo.resolve()
    entries = select_tests.load_history(history, repo)
    flaky = load_flaky(flaky_path)
    at_base = select_tests.load_baseline_failures(baseline_failures)
    changes, skipped, misses, flaky_failures, base_failures = [], [], [], [], []
    total = missed = 0
    for i, h in enumerate(entries):
        if not h['full_run']:
            skipped.append({'change': h['id'], 'reason': 'no full run recorded'})
            continue
        with tempfile.TemporaryDirectory(prefix='impact-replay-') as tmp:
            target = repo
            try:
                if h['base']:
                    target = checkout(repo, h['base'], Path(tmp) / 'base')
                diff = history.parent / h['diff'] if h['diff'] else Path(tmp) / f'{h["id"]}.diff'
                if not h['diff']:
                    diff.write_text(h['diff_text'], encoding='utf-8')
                sel = select_tests.select(target, diff, mode=mode, coverage=coverage, journeys=journeys,
                                          history=history, related=related, max_chars=max_chars,
                                          history_entries=entries[:i], baseline_failures=baseline_failures)
            except (SystemExit, Exception) as exc:     # one change that cannot be replayed never stops the others
                why = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
                skipped.append({'change': h['id'], 'reason': f'could not be replayed on '
                                f'{h["base"] or "--repo"}: {why}' + ('' if h['base'] else
                                ' (give the change a base ref or its commit in history.json)')})
                continue
        chosen = {s['file'] for s in sel['selected']}
        failed = []
        for f in h['failed']:
            ids = {c['id'] for c in h['cases'] if c['file'] == f and c['status'] in ('failed', 'error')}
            is_flaky = f in flaky or any(f'{f}::{x.split("::")[-1]}' in flaky or x in flaky for x in ids)
            is_base = f in at_base or any(x in at_base for x in ids)
            hit = f in chosen or select_tests.package_level_selected(f, chosen)
            row = {'file': f, 'selected': hit, 'flaky': is_flaky, 'failed_cases': sorted(ids)}
            if is_base:
                row['at_base'] = True
            failed.append(row)
            if is_flaky:
                flaky_failures.append({'change': h['id'], 'test_id': f})
                continue
            if is_base:
                base_failures.append({'change': h['id'], 'test_id': f})
                continue
            total += 1
            if not hit:
                missed += 1
                misses.append({'change': h['id'], 'test_id': f, 'changed_paths': h['changed'],
                               'selected_count': len(chosen), 'blind_spot_kinds': sorted({b['kind'] for b in sel['blind_spots']})})
        changes.append({'id': h['id'], 'description': h['description'], 'diff': h['diff'] or f'git:{h["commit"]}', **({'base': h['base']} if h['base'] else {}), 'changed_paths': h['changed'],
                        'selected_count': len(chosen), 'total_tests': sel['total_tests'], 'selected': sorted(chosen),
                        'blind_spots': [{'kind': b['kind'], 'path': b['path'], 'action': b['action']} for b in sel['blind_spots']],
                        'failed': failed})
    low, high = stats.wilson(total - missed, total)
    return {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'tool': 'replay.py', 'mode': mode,
        'inputs': {'repo': select_tests.rel_input(repo), 'history': select_tests.rel_input(history),
                   'history_sha256': hashlib.sha256(history.read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
                   'coverage': [select_tests.rel_input(c) for c in coverage], 'journeys': select_tests.rel_input(journeys),
                   'related': select_tests.rel_input(related), 'flaky': select_tests.rel_input(flaky_path),
                   **({'baseline_failures': select_tests.rel_input(baseline_failures)} if baseline_failures else {})},
        'approximation': 'Each past change was selected against the current coverage map, journey map and file kinds.',
        'changes_replayed': len(changes), 'changes_skipped': skipped, 'failures_total': total, 'failures_missed': missed,
        'failures_flaky': len(flaky_failures), 'recall': round((total - missed) / total, 4) if total else None,
        'wilson_low': low if total else None, 'wilson_high': high if total else None,
        'mean_selected_share': round(sum(c['selected_count'] / c['total_tests'] for c in changes) / len(changes), 4)
        if changes else None,
        'failures_baseline': len(base_failures), 'baseline_failures': base_failures,
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
    ap.add_argument('--baseline-failures', type=Path, help='tests that fail at the base already (JSON list, or {"failures": [...]}): not counted')
    ap.add_argument('--max-chars', type=int, default=8000)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = replay(args.repo, args.history, args.mode, args.coverage, args.journeys, args.related, args.flaky, args.max_chars,
                 baseline_failures=args.baseline_failures)
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
    for s in res['changes_skipped']:
        print(f'  skipped {s["change"]}: {s["reason"]}')
    if res['failures_baseline']:
        print(f'  {res["failures_baseline"]} failure(s) of tests that fail at the base already were not counted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
