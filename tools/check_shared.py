#!/usr/bin/env python3
"""Check that shared files copied into skills are identical to their canonical copy in shared/.

Skills must be self-contained (installed by copying one directory), so shared files are
duplicated rather than imported. Copies are compared with line endings normalised. It also reports
scripts that two or more skills carry under the same name with the same or nearly the same content
but that SHARED does not register (UNREGISTERED): --fix cannot pick their canonical copy, so they
stay problems until one is put in shared/ and registered.

    python tools/check_shared.py              # report drift and unregistered copies, exit 1 if any
    python tools/check_shared.py --fix        # overwrite copies with the canonical file
    python tools/check_shared.py --self-test  # check the unregistered-copy detector on a scratch tree
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPO_AWARE = ['code-security-auditor-skill', 'privacy-flow-auditor-skill', 'test-suite-evaluator-skill',
              'flaky-test-hunter-skill', 'dead-test-finder-skill', 'risk-based-test-planner-skill',
              'test-impact-selector-skill', 'mutation-tester-skill', 'test-data-auditor-skill',
              # their detect_conventions.py or lang_census.py imports the two
              'journey-to-tests-skill', 'ticket-to-tdd-skill', 'bug-reproducer-skill', 'ui-crash-hunter-skill',
              'cross-browser-checker-skill', 'ci-failure-triager-skill', 'db-migration-reviewer-skill',
              'api-contract-tester-skill']

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
                             'test-data-auditor-skill', 'dead-test-finder-skill', 'bug-reproducer-skill',
                             'test-suite-evaluator-skill'],
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
    'scripts/schema_check.py': ['api-contract-tester-skill', 'bug-reproducer-skill', 'cross-browser-checker-skill',
                                'exploratory-charter-writer-skill', 'i18n-checker-skill', 'performance-budget-auditor-skill',
                                'post-deploy-validator-skill', 'resilience-prober-skill', 'visual-regression-reviewer-skill'],
    'scripts/py_extras.py': ['test-suite-evaluator-skill', 'journey-mapper-skill'],
    # py_loads.py: the files a Python test loads by path (spec_from_file_location, runpy, sys.path folders) and
    # in-process main() calls; for every skill that links tests to the scripts they exercise
    'scripts/py_loads.py': ['risk-based-test-planner-skill', 'test-impact-selector-skill', 'dead-test-finder-skill',
                            'test-data-auditor-skill', 'test-suite-evaluator-skill', 'flaky-test-hunter-skill',
                            'mutation-tester-skill'],
    # archive_text.py: the text inside committed zip archives and .log files, read in memory under caps
    'scripts/archive_text.py': ['privacy-flow-auditor-skill', 'test-data-auditor-skill'],
    # repo_layout.py (fixture projects, copy groups) and code_mask.py (strings and comments blanked): the skills that
    # audit a host repository, plus every skill whose detect_conventions.py or lang_census.py imports them
    'scripts/repo_layout.py': REPO_AWARE,
    'scripts/code_mask.py': REPO_AWARE,
    'data/wcag-sc.json': ['wcag-auditor-skill', 'mobile-app-auditor-skill'],
    'data/en301549-map.json': ['wcag-auditor-skill', 'mobile-app-auditor-skill'],
    'data/ambiguity-lexicon.json': ['ticket-readiness-reviewer-skill', 'test-case-designer-skill'],
}

def normalised(path: Path) -> bytes:
    return path.read_bytes().replace(b'\r\n', b'\n')


def unregistered_copies(root: Path, shared: dict | None = None) -> list[dict]:
    """Scripts under two or more skills' scripts/ or plugins/ with the same file name and the same (or nearly the
    same) content, that SHARED does not register: copies that drift unnoticed.

    Same-named scripts that merely share a role (render_report.py, self_test.py) differ in most lines and are not
    reported: two copies are related when their lines are at least COPY_SIMILARITY alike. Each result is
    {name, skills, paths, identical}."""
    import difflib
    shared = SHARED if shared is None else shared
    registered = set()
    for rel, skills in shared.items():
        for entry in skills:
            skill, _, where = entry.partition(':')
            registered.add(f'{skill}/{where or rel}')
    by_name: dict[str, list[str]] = {}
    for top in ('scripts', 'plugins'):
        for p in sorted(root.glob(f'*-skill/{top}/**/*')):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and p.suffix in COPY_SUFFIXES and '__pycache__' not in p.parts and rel not in registered:
                by_name.setdefault(p.name, []).append(rel)
    out = []
    for name, rels in sorted(by_name.items()):
        if len({r.split('/', 1)[0] for r in rels}) < 2:
            continue
        texts = {r: (root / r).read_bytes().replace(b'\r\n', b'\n').decode('utf-8', 'replace') for r in rels}
        lines = {r: t.splitlines() for r, t in texts.items()}
        related: set[str] = set()
        for i, a in enumerate(rels):
            for b in rels[i + 1:]:
                if texts[a] == texts[b]:
                    related |= {a, b}
                    continue
                sm = difflib.SequenceMatcher(None, lines[a], lines[b], autojunk=False)
                if (sm.real_quick_ratio() >= COPY_SIMILARITY and sm.quick_ratio() >= COPY_SIMILARITY
                        and sm.ratio() >= COPY_SIMILARITY):
                    related |= {a, b}
        if len({r.split('/', 1)[0] for r in related}) >= 2:
            paths = sorted(related)
            out.append({'name': name, 'skills': sorted({r.split('/', 1)[0] for r in paths}), 'paths': paths,
                        'identical': len({texts[r] for r in paths}) == 1})
    return out


COPY_SUFFIXES = {'.py', '.mjs', '.cjs', '.js'}
COPY_SIMILARITY = 0.8


def self_test() -> int:
    import tempfile
    checks = []
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        helper = 'def helper(x):\n' + ''.join(f'    x = x + {i}\n' for i in range(30)) + '    return x\n'
        files = {
            'alpha-skill/scripts/helper.py': helper, 'beta-skill/scripts/helper.py': helper.replace('\n', '\r\n'),
            'gamma-skill/scripts/helper.py': helper.replace('x + 29', 'x + 290'),        # a drifted copy
            'alpha-skill/scripts/render_report.py': 'print("alpha")\n',              # same name, unrelated
            'beta-skill/scripts/render_report.py': 'import json\nprint(json.dumps({}))\n',
            'shared/scripts/common.py': helper + '# common\n',
            'alpha-skill/scripts/common.py': helper + '# common\n', 'beta-skill/scripts/common.py': helper + '# common\n',
            'alpha-skill/plugins/guard.py': helper + '# guard\n', 'beta-skill/scripts/guard.py': helper + '# guard\n',
            'alpha-skill/examples/app/helper.py': helper,
            'alpha-skill/scripts/only_here.py': helper + '# alone\n',
        }
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(body.encode())
        got = {u['name']: (sorted(u['skills']), u['identical']) for u in
               unregistered_copies(root, {'scripts/common.py': ['alpha-skill', 'beta-skill']})}
        checks.append(('an unregistered script copied into three skills (one drifted) is reported, drift noted',
                       got.get('helper.py') == (['alpha-skill', 'beta-skill', 'gamma-skill'], False), got))
        checks.append(('identical copies under scripts/ and plugins/ are reported as identical',
                       got.get('guard.py') == (['alpha-skill', 'beta-skill'], True), got))
        checks.append(('a registered shared file, same-named unrelated scripts, examples and single copies are not',
                       set(got) == {'helper.py', 'guard.py'}, sorted(got)))
    failed = [c for c in checks if not c[1]]
    for name, ok, extra in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{len(failed)} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fix', action='store_true', help='overwrite drifted or missing copies')
    ap.add_argument('--self-test', action='store_true', help='check the unregistered-copy detector on a scratch tree')
    args = ap.parse_args()
    if args.self_test:
        return self_test()
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
    # copies nobody registered drift silently: --fix cannot choose their canonical version, so they stay problems
    unregistered = unregistered_copies(ROOT)
    for u in unregistered:
        print(f'UNREGISTERED: {u["name"]} is copied into {len(u["skills"])} skills '
              f'({"identical" if u["identical"] else "copies differ"}: {", ".join(u["paths"])}); put the canonical copy '
              'in shared/ and add it to SHARED in tools/check_shared.py')
    problems += len(unregistered)
    if problems:
        print(f'{problems} problem(s). Run: python tools/check_shared.py --fix')
        return 1
    print('Shared files in sync.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
