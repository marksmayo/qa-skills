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
# ('skill:other/path' when the skill keeps it elsewhere)
SHARED = {
    'JOURNEY_FORMAT.md': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'schemas/journey.schema.json': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'scripts/journey_md.py': ['journey-mapper-skill', 'journey-to-tests-skill'],
    'scripts/detect_conventions.py': ['journey-to-tests-skill', 'ticket-to-tdd-skill', 'flaky-test-hunter-skill',
                                      'mutation-tester-skill', 'test-impact-selector-skill', 'test-data-auditor-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill', 'bug-reproducer-skill',
                                      'test-suite-evaluator-skill'],
    'scripts/parse_results.py': ['journey-to-tests-skill', 'ticket-to-tdd-skill', 'flaky-test-hunter-skill',
                                 'mutation-tester-skill', 'test-impact-selector-skill', 'test-data-auditor-skill', 'dead-test-finder-skill', 'bug-reproducer-skill',
                                 'release-readiness-gate-skill', 'ci-failure-triager-skill', 'requirements-traceability-auditor-skill',
                                 'ci-suite-runner-skill', 'test-suite-evaluator-skill'],
    'scripts/run_tests.py': ['flaky-test-hunter-skill', 'mutation-tester-skill', 'test-impact-selector-skill',
                             'test-data-auditor-skill', 'dead-test-finder-skill', 'bug-reproducer-skill'],
    'scripts/diff_scope.py': ['mutation-tester-skill', 'test-impact-selector-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill', 'db-migration-reviewer-skill'],
    'scripts/unified_diff.py': ['test-impact-selector-skill', 'dead-test-finder-skill'],
    'scripts/stats.py': ['flaky-test-hunter-skill', 'test-impact-selector-skill'],
    'scripts/sarif.py': ['ui-crash-hunter-skill', 'code-security-auditor-skill', 'api-contract-tester-skill', 'db-migration-reviewer-skill', 'privacy-flow-auditor-skill'],
    'scripts/fetch_ticket.py': ['ticket-to-tdd-skill', 'ticket-readiness-reviewer-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill', 'exploratory-charter-writer-skill', 'bug-reproducer-skill',
                                'test-case-designer-skill', 'requirements-traceability-auditor-skill'],
    'scripts/safe_http.py': ['ci-suite-runner-skill', 'ci-failure-triager-skill', 'hypercare-anomaly-watcher-skill',
                             # fetch_ticket.py sends Jira tokens through it
                             'ticket-to-tdd-skill', 'ticket-readiness-reviewer-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill',
                             'exploratory-charter-writer-skill', 'bug-reproducer-skill', 'test-case-designer-skill',
                             'requirements-traceability-auditor-skill'],
    'scripts/term_safe.py': ['api-contract-tester-skill',
                             'bug-reproducer-skill',
                             'ci-failure-triager-skill',
                             'ci-suite-runner-skill',
                             'code-security-auditor-skill',
                             'cross-browser-checker-skill',
                             'db-migration-reviewer-skill',
                             'dead-test-finder-skill',
                             'exploratory-charter-writer-skill',
                             'flaky-test-hunter-skill',
                             'hypercare-anomaly-watcher-skill',
                             'i18n-checker-skill',
                             'journey-mapper-skill',
                             'journey-to-tests-skill',
                             'mobile-app-auditor-skill',
                             'mutation-tester-skill',
                             'performance-budget-auditor-skill',
                             'post-deploy-validator-skill',
                             'privacy-flow-auditor-skill',
                             'qa-orchestrator-skill',
                             'release-readiness-gate-skill',
                             'requirements-traceability-auditor-skill',
                             'resilience-prober-skill',
                             'risk-based-test-planner-skill',
                             'test-case-designer-skill',
                             'test-data-auditor-skill',
                             'test-impact-selector-skill',
                             'test-suite-evaluator-skill',
                             'ticket-readiness-reviewer-skill',
                             'ticket-to-tdd-skill',
                             'ui-crash-hunter-skill',
                             'visual-regression-reviewer-skill',
                             'wcag-auditor-skill'],
    'scripts/browser_harness.py': ['bug-reproducer-skill', 'performance-budget-auditor-skill', 'visual-regression-reviewer-skill',
                                   'cross-browser-checker-skill', 'i18n-checker-skill',
                                   'resilience-prober-skill', 'privacy-flow-auditor-skill', 'wcag-auditor-skill'],
    'scripts/clock_shift.py': ['test-data-auditor-skill', 'flaky-test-hunter-skill:plugins/clock_shift.py'],
    'scripts/clock_shift.mjs': ['test-data-auditor-skill', 'flaky-test-hunter-skill:plugins/clock_shift.mjs'],
    'scripts/net_guard.py': ['test-suite-evaluator-skill', 'flaky-test-hunter-skill:plugins/net_guard.py',
                             'test-data-auditor-skill', 'dead-test-finder-skill'],
    'scripts/net_guard.cjs': ['test-suite-evaluator-skill', 'flaky-test-hunter-skill:plugins/net_guard.cjs',
                              'test-data-auditor-skill', 'dead-test-finder-skill'],
    'scripts/net_guard_proxy.py': ['test-suite-evaluator-skill', 'flaky-test-hunter-skill:plugins/net_guard_proxy.py',
                                   'test-data-auditor-skill', 'dead-test-finder-skill'],
    'scripts/py_routes.py': ['journey-mapper-skill', 'api-contract-tester-skill', 'code-security-auditor-skill',
                             'test-suite-evaluator-skill', 'ui-crash-hunter-skill'],
    'scripts/lang_census.py': ['code-security-auditor-skill', 'dead-test-finder-skill', 'flaky-test-hunter-skill',
                               'test-data-auditor-skill', 'privacy-flow-auditor-skill', 'ui-crash-hunter-skill',
                               'cross-browser-checker-skill', 'risk-based-test-planner-skill', 'test-impact-selector-skill',
                               'mutation-tester-skill', 'ci-failure-triager-skill', 'test-suite-evaluator-skill',
                               'db-migration-reviewer-skill', 'api-contract-tester-skill',
                               # detect_conventions.py decides dist/ and build/ with it
                               'journey-to-tests-skill', 'ticket-to-tdd-skill', 'bug-reproducer-skill'],
    'scripts/next_app.py': ['code-security-auditor-skill', 'ui-crash-hunter-skill', 'journey-mapper-skill',
                            'risk-based-test-planner-skill', 'privacy-flow-auditor-skill', 'cross-browser-checker-skill',
                            'wcag-auditor-skill', 'performance-budget-auditor-skill', 'resilience-prober-skill',
                            'i18n-checker-skill', 'journey-to-tests-skill'],
    'scripts/cargo_project.py': ['code-security-auditor-skill', 'privacy-flow-auditor-skill', 'risk-based-test-planner-skill',
                                 'test-impact-selector-skill', 'test-suite-evaluator-skill',
                                 'flaky-test-hunter-skill', 'mutation-tester-skill', 'dead-test-finder-skill',
                                 'ci-failure-triager-skill',
                                 # run_tests.py maps cargo test ids to their targets with it
                                 'test-data-auditor-skill', 'bug-reproducer-skill'],
    'scripts/png_io.py': ['visual-regression-reviewer-skill', 'cross-browser-checker-skill', 'mobile-app-auditor-skill'],
    'data/wcag-sc.json': ['wcag-auditor-skill', 'mobile-app-auditor-skill'],
    'data/en301549-map.json': ['wcag-auditor-skill', 'mobile-app-auditor-skill'],
    'data/ambiguity-lexicon.json': ['ticket-readiness-reviewer-skill', 'test-case-designer-skill'],
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
        for entry in skills:
            skill, _, where = entry.partition(':')  # 'skill' (same relative path) or 'skill:other/path'
            if not (ROOT / skill).is_dir():
                continue  # skill not built yet
            copy = ROOT / skill / (where or rel)
            rel_shown = where or rel
            state = 'missing' if not copy.exists() else ('ok' if normalised(copy) == normalised(canonical) else 'drift')
            if state == 'ok':
                continue
            if args.fix:
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(normalised(canonical))
                print(f'FIXED ({state}): {skill}/{rel_shown}')
            else:
                print(f'{state.upper()}: {skill}/{rel_shown} differs from shared/{rel}'); problems += 1
    if problems:
        print(f'{problems} problem(s). Run: python tools/check_shared.py --fix')
        return 1
    print('Shared files in sync.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
