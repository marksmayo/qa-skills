#!/usr/bin/env python3
"""Check that shared files copied into skills are identical to their canonical copy in shared/.

Skills must be self-contained (installed by copying one directory), so shared files are
duplicated rather than imported. Copies are compared with line endings normalised.

    python tools/check_shared.py          # report drift, exit 1 if any
    python tools/check_shared.py --fix    # overwrite copies with the canonical file
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# canonical file (relative to shared/) -> skills that must carry a copy at the same relative path
SHARED = {
    'JOURNEY_FORMAT.md': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'schemas/journey.schema.json': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'scripts/journey_md.py': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'scripts/detect_conventions.py': ['journey-to-tests-skill', 'ticket-to-tdd-skill', 'flaky-test-hunter-skill',
                                      'mutation-tester-skill', 'test-impact-selector-skill', 'test-data-auditor-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill'],
    'scripts/parse_results.py': ['journey-to-tests-skill', 'ticket-to-tdd-skill', 'flaky-test-hunter-skill',
                                 'mutation-tester-skill', 'test-impact-selector-skill', 'test-data-auditor-skill', 'dead-test-finder-skill'],
    'scripts/run_tests.py': ['flaky-test-hunter-skill', 'mutation-tester-skill', 'test-impact-selector-skill',
                             'test-data-auditor-skill', 'dead-test-finder-skill'],
    'scripts/diff_scope.py': ['mutation-tester-skill', 'test-impact-selector-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill'],
    'scripts/unified_diff.py': ['test-impact-selector-skill', 'dead-test-finder-skill'],
    'scripts/stats.py': ['flaky-test-hunter-skill', 'test-impact-selector-skill'],
    'scripts/sarif.py': ['ui-crash-hunter-skill', 'code-security-auditor-skill'],
    'scripts/fetch_ticket.py': ['ticket-to-tdd-skill', 'ticket-readiness-reviewer-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill', 'exploratory-charter-writer-skill'],
}

def normalised(path: Path) -> bytes:
    return path.read_bytes().replace(b'\r\n', b'\n')

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fix', action='store_true', help='overwrite drifted or missing copies')
    args = ap.parse_args()
    problems = 0
    for rel, skills in SHARED.items():
        canonical = ROOT / 'shared' / rel
        if not canonical.exists():
            print(f'MISSING CANONICAL: shared/{rel}'); problems += 1; continue
        for skill in skills:
            if not (ROOT / skill).is_dir():
                continue  # skill not built yet
            copy = ROOT / skill / rel
            state = 'missing' if not copy.exists() else ('ok' if normalised(copy) == normalised(canonical) else 'drift')
            if state == 'ok':
                continue
            if args.fix:
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(normalised(canonical))
                print(f'FIXED ({state}): {skill}/{rel}')
            else:
                print(f'{state.upper()}: {skill}/{rel} differs from shared/{rel}'); problems += 1
    if problems:
        print(f'{problems} problem(s). Run: python tools/check_shared.py --fix')
        return 1
    print('Shared files in sync.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
