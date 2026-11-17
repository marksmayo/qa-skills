#!/usr/bin/env python3
"""Render a flaky test report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py flaky_report.json [--color auto|always|never] [--brief]

--brief prints the headline, the ranked flakes with each one's experiment and result, only the tests
that need attention (flaky, consistently failing, platform dependent, unable to run alone; the rest
as one line with their run counts), what was not run, and the limitations. It leaves out fixes,
smells, tools and the full test table. --self-test checks what each form must keep.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import stats  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from term_safe import load_json  # noqa: E402  (report strings lose terminal control characters)

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
CONF_STYLE = {'confirmed': GREEN, 'indicated': CYAN, 'suspected': DIM}
STATUS_STYLE = {'flaky': YELLOW, 'consistently_failing': RED, 'platform_dependent': RED, 'cannot_run_alone': RED,
                'not_observed_flaky': DIM, 'not_run': MAGENTA}
ATTENTION = ('flaky', 'consistently_failing', 'platform_dependent', 'cannot_run_alone')
ARTEFACT = 'failed only under the {probe} shift this skill applied: a probe artefact unless control runs confirm it'
TOOL_STYLE = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def pct(x) -> str:
    return f'{100 * x:.1f}%'


def rate_text(r: dict) -> str:
    if not r['runs']:
        return 'no runs'
    return f"{r['fails']}/{r['runs']} ({pct(r['wilson_low'])}-{pct(r['wilson_high'])})"


def short(tid: str) -> str:
    return tid.split('::')[-1]


def main() -> int:
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--self-test', action='store_true', help='run the built-in checks and exit')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = load_json(args.json_file)
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render(d, args.brief, supports_color(args.color)))
    return 0


def render(d: dict, brief: bool = False, c: bool = False) -> str:
    """The report as text. brief: the headline, the ranked flakes with their experiments, the tests that need
    attention and the limitations; without fixes, smells, tools or the full test table."""
    args = argparse.Namespace(brief=brief)
    P = lambda t, s: paint(t, s, c)  # noqa: E731
    m, s = d['metadata'], d['summary']
    out = []
    w = out.append
    w(P('# Flaky test report', BOLD))
    w('')
    w(f"Repo: {m['repo']}  |  {m['framework']}  |  {m['platform']}  |  generated {d['generated_at']}")
    w(f"Budget: {m['budget']['repeats']} repeats, {m['budget']['minutes']}-minute cap"
      + (P(' (stopped early)', YELLOW) if m['budget']['stopped_early'] else '')
      + f"  |  {s['total_runs']} runs"
      + (f" ({s['total_runs'] - s['single_test_runs']} suite or batch runs, {s['single_test_runs']} single-test runs)"
         if s.get('single_test_runs') else '')
      + f", {s['wall_minutes']} min  |  gate: {m['gate']['level']}")
    for su in m.get('suites') or []:
        n = (f"{su['tests']} tests" if su.get('tests') is not None
             else 'tests counted from its runs' if su['status'] == 'ran' else 'tests (count unknown)')
        state = P('ran', GREEN) if su['status'] == 'ran' else P('NOT RUN', MAGENTA)
        w(f"Suite {su['language']}: {n}, {state} ({', '.join(su['presets']) or 'no preset'}): {su['reason']}")
    w('')
    w(P(s['headline'], BOLD))
    w(f"{s['tests_run']} tests run: {P(str(s['flaky']) + ' flaky', YELLOW)}, "
      f"{P(str(s['consistently_failing']) + ' consistently failing', RED)}, "
      + (f"{P(str(s['cannot_run_alone']) + ' cannot run alone', RED)}, " if s.get('cannot_run_alone') else '')
      + f"{s['not_observed_flaky']} not observed flaky, {s['not_run']} not run  |  "
      f"evidence completeness {pct(s['evidence_completeness'])}")
    w('Rates are failures/runs with a 95% Wilson interval. "Not observed flaky" means no failure in the runs shown, '
      'not that the test cannot fail.')
    w('')
    w(P('## Flakes', BOLD))
    w('')
    if d['flakes']:
        w('| # | Priority | Test | Cause | Confidence | Worst rate | Score |')
        w('|---|---|---|---|---|---|---|')
    else:
        w(f"No test both passed and failed in these {s['total_runs']} runs. That is not proof the suite is free of "
          'flakes: see the upper bounds below.')
    tests = {t['id']: t for t in d['tests']}

    def artefact(f) -> str | None:
        """The probe a flake's failures all came under, when no control run confirmed it another way."""
        probe = (tests.get(f['test_id']) or {}).get('probe_only')
        return probe if probe and not f.get('probe_confirmation') else None
    for f in d['flakes']:
        t = tests.get(f['test_id'])
        worst = f"{t['worst']['fails']}/{t['worst']['runs']} {t['worst']['label']}" if t else '-'
        cause = f['cause'].replace('_', ' ') + (P(' (probe artefact?)', MAGENTA) if artefact(f) else '')
        w(f"| {f['id']} | {P(f['priority'], PRIO_STYLE[f['priority']])} | {short(f['test_id'])} | "
          f"{cause} | {P(f['cause_confidence'], CONF_STYLE[f['cause_confidence']])} | "
          f"{worst} | {f['score']} |")
    w('')
    if args.brief:
        for f in d['flakes']:
            w(f"- {f['id']} ({f['cause'].replace('_', ' ')}, {f['cause_confidence']}): {f['experiment']['what_changed']} "
              f"Result: {f['experiment']['result']}"
              + (P(f" [{ARTEFACT.format(probe=artefact(f))}]", MAGENTA) if artefact(f) else ''))
        if d['flakes']:
            w('')
    if not args.brief:
        for f in d['flakes']:
            w(P(f"### {f['id']} {f['test_id']}", BOLD))
            w(f"{f['cause'].replace('_', ' ')}, {f['cause_confidence']}: {f['experiment']['what_changed']}")
            w(f"  Result: {f['experiment']['result']}")
            if artefact(f):
                w(P(f"  Probe artefact? It {ARTEFACT.format(probe=artefact(f))} (file times left real, "
                    'TDA_CLOCK_FILES=0, still failing).', MAGENTA))
            elif f.get('probe_confirmation'):
                pc = f['probe_confirmation']
                w(f"  Failed only under the clock shift; confirmed by control runs {', '.join(pc['run_ids'][:6])}: "
                  f"{pc['note']}")
            if f['polluter'] and f['polluter'].get('relation') == 'enabler':
                w(f"  Enabler: {f['polluter']['test_id']} (pair run {f['polluter']['pair_run_id']} passes, "
                  f"test alone {f['polluter']['alone_run_id']} fails: it depends on that test running first)")
            elif f['polluter']:
                w(f"  Polluter: {f['polluter']['test_id']} (pair run {f['polluter']['pair_run_id']} fails, "
                  f"victim alone {f['polluter']['alone_run_id']} passes)")
            if f.get('history_only'):
                w(P('  Seen in CI history only, never reproduced in the local runs (priority capped at P2):', MAGENTA))
                for h in f.get('history_evidence', [])[:6]:
                    w(f"    {h['id']} {h['outcome']}: {h['source']}, {h['ref']}" + (f" - {h['excerpt'][:100]}" if h['excerpt'] else ''))
            w(f"  Passing runs: {', '.join(f['evidence_runs']['passing'][:6])}"
              f"{' ...' if len(f['evidence_runs']['passing']) > 6 else ''}  |  failing: "
              f"{', '.join(f['evidence_runs']['failing'][:6])}{' ...' if len(f['evidence_runs']['failing']) > 6 else ''}")
            w(f"  Failure: {f['failure_excerpts'][0].splitlines()[0][:160]}")
            w(f"  Priority: {f['priority_note']}")
            w(f"  Fix ({'applied' if f['fix']['applied'] else 'proposed, not applied'}): {f['fix']['rationale']}")
            w('```diff')
            w(f['fix']['diff'].rstrip())
            w('```')
            if f['quarantine']:
                q = f['quarantine']
                w(P(f"  Stopgap (proposed, not applied): {q['how']}; owner {q['owner']}, ticket {q['ticket']}", DIM))
            w('')
    shown = [t for t in d['tests'] if not args.brief or t['status'] in ATTENTION]
    if shown or d['tests']:
        w(P('## Tests', BOLD))
        w('')
    if shown:
        w('| Test | Status | Normal runs | Worst | Runs |')
        w('|---|---|---|---|---|')
    for t in shown:
        worst = f"{t['worst']['label']}: {t['worst']['fails']}/{t['worst']['runs']}" if t['worst']['label'] else '-'
        w(f"| {t['id']} | {P(t['status'].replace('_', ' '), STATUS_STYLE[t['status']])} | {rate_text(t['normal'])} | "
          f"{worst} | {t['runs']} |")
    if args.brief:
        quiet = [t for t in d['tests'] if t['status'] == 'not_observed_flaky']
        if quiet:
            by_runs: dict[int, int] = {}
            for t in quiet:
                by_runs[t['runs']] = by_runs.get(t['runs'], 0) + 1
            fewest = min(by_runs)
            groups = ', '.join(f'{n} with {r} run{"s" if r != 1 else ""}' for r, n in sorted(by_runs.items(), reverse=True)[:6])
            if shown:
                w('')
            w(f"{len(quiet)} other tests were not observed flaky ({groups}{', ...' if len(by_runs) > 6 else ''}). "
              f"0/{fewest} has a 95% upper bound of {pct(stats.wilson(0, fewest)[1])}, so a test with "
              f"{fewest} run(s) could still fail that often; the full report (without --brief) lists every test.")
    static = [t for t in d['tests'] if t['status'] == 'not_run']
    if static:
        why: dict[str, list[str]] = {}
        for t in static:
            why.setdefault(t.get('not_run_reason') or 'no reason recorded', []).append(t['id'])
        if shown or args.brief:
            w('')
        w(f"{len(static)} test(s) were not run on this code:")
        for reason, ids in sorted(why.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            w(f"- {len(ids)} not run ({reason}): {', '.join(ids[:4])}{', ...' if len(ids) > 4 else ''}")
    elif args.brief and s['not_run']:
        w(f"{s['not_run']} test(s) were not run on this code.")
    w('')
    if d.get('cannot_run_alone'):
        w(P('## Cannot run alone (order or import dependence; not flaky, not unobserved)', BOLD))
        for x in d['cannot_run_alone']:
            ids = x['test_ids']
            where = x['file'] or ids[0]
            w(f"- {where}: {len(ids)} test(s) errored in every run alone ({', '.join(x['alone_run_ids'][:4])}"
              f"{' ...' if len(x['alone_run_ids']) > 4 else ''}) and passed in the suite. "
              f"{x['cause'].replace('_', ' ')}: {x['error_excerpt'].splitlines()[0][:160]}")
            if x.get('needs'):
                w(f"  Runs after {x['needs']} ({', '.join(x['needs_run_ids'][:4])}).")
            w(f"  {x['note']}")
            if not args.brief:
                w(f"  Tests: {', '.join(ids[:8])}{', ...' if len(ids) > 8 else ''}")
        w('')
    if d['consistently_failing']:
        w(P('## Consistently failing (not flaky)', BOLD))
        for x in d['consistently_failing']:
            on = f" on {', '.join(x['platforms'])} only" if x.get('platforms') else ''
            w(f"- {x['test_id']} ({x['runs']} runs{on}): {x['note']}")
        w('')
    if not args.brief:
        w(P('## Variations', BOLD))
        for v in d['variations']:
            extra = f" [{v['scope']}]" if v.get('scope') else ''
            w(f"- {v['kind']}: {P(v['status'], TOOL_STYLE[v['status']])}{extra} {v['reason']}")
        w('')
        w(P('## Tools', BOLD))
        for t in d['tools']:
            w(f"- {t['name']} {t['version'] or ''}: {P(t['status'], TOOL_STYLE[t['status']])} {t['reason']}")
        w('')
        if d['static_smells']:
            w(P('## Static smells', BOLD))
            for sm in d['static_smells']:
                link = f" -> {sm['flake_id']}" if sm['flake_id'] else ''
                status = 'not run: its test has no runs' if sm['status'] == 'not_run' else sm['status'].replace('_', ' ')
                w(f"- {sm['file']}:{sm['line']} {sm['smell']} ({status}{link}): {sm['note']}")
            w('')
    if args.brief:
        not_ran = [v for v in d['variations'] if v['status'] == 'not_run']
        if not_ran:
            w('Variations not run: ' + '; '.join(f"{v['kind']} ({v['reason']})" for v in not_ran))
    for lim in d['limitations']:
        w(P(f'Limitation: {lim}', DIM))
    if d['files_changed']:
        w('Files changed (uncommitted): ' + ', '.join(f['path'] for f in d['files_changed']))
    return '\n'.join(out).rstrip() + '\n\n'


def self_test() -> int:
    """Checks on a small report built here: what --brief must keep, and the 1.4.0 statuses."""
    rate0 = {'runs': 0, 'fails': 0, 'rate': None, 'wilson_low': 0.0, 'wilson_high': 1.0}

    def row(tid, status, runs, fails, **extra):
        return {'id': tid, 'file': 'tests/test_tide.py', 'runs': runs, 'passes': runs - fails, 'fails': fails,
                'normal': {'runs': runs, 'fails': 0, 'rate': 0.0, 'wilson_low': 0.0, 'wilson_high': 0.2} if runs else rate0,
                'worst': {'label': 'clock_minus_1d' if fails else None, 'runs': fails, 'fails': fails, 'wilson_low': 0.4},
                'by_label': [], 'status': status, **extra}
    flake = {'id': 'FL-1', 'test_id': 'tests.test_tide::test_stamp', 'cause': 'time_and_locale',
             'cause_confidence': 'indicated',
             'experiment': {'run_ids': ['C-1'], 'what_changed': 'Moved the clock back a day.',
                            'result': 'Failed 3/3 under the shift and 0/3 with file times left real.'},
             'evidence_runs': {'passing': ['R-1'], 'failing': ['C-1']}, 'polluter': None,
             'failure_excerpts': ['OSError: [Errno 22]'], 'rate_factor': 4, 'gate': 2, 'masking': 1, 'score': 8,
             'priority': 'P3', 'priority_note': '4 x gate 2 x masking 1 = 8; capped at P3: a probe artefact',
             'fix': {'diff': '--- a/tests/test_tide.py\n+++ b/tests/test_tide.py\n@@ -1,1 +1,1 @@\n-a\n+b\n',
                     'rationale': 'Compare with the stamped time.', 'applied': False, 'verification_run_ids': []},
             'quarantine': None}
    d = {'schema_version': '1.4.0', 'skill': {'name': 'flaky-test-hunter', 'version': 'self-test'},
         'generated_at': '2026-11-18T00:00:00Z',
         'metadata': {'repo': 'tide-tables', 'commit': None, 'code_hash': 'a' * 64, 'framework': 'pytest',
                      'run_command': 'pytest', 'runs_file': 'runs.json', 'platform': 'linux',
                      'budget': {'repeats': 3, 'minutes': 10, 'stopped_early': False},
                      'gate': {'level': 'ci', 'evidence': 'self-test'}, 'critical_tests': []},
         'tools': [], 'variations': [{'kind': 'clock', 'status': 'ran', 'labels': ['clock_minus_1d'], 'reason': 'x'}],
         'tests': [row('tests.test_tide::test_stamp', 'flaky', 6, 3, probe_only='clock'),
                   row('tests.test_report.ReportTest::test_rows', 'cannot_run_alone', 3, 0,
                       alone={'attempts': 2, 'errored': 2, 'error': 'pytest exit 4: collection error'}),
                   row('tests.test_tide::test_height', 'not_observed_flaky', 3, 0),
                   row('quay::tests::slow', 'not_run', 0, 0, not_run_reason='ignored: #[ignore]')],
         'flakes': [flake], 'consistently_failing': [],
         'cannot_run_alone': [{'test_ids': ['tests.test_report.ReportTest::test_rows'], 'file': 'tests/test_report.py',
                               'alone_run_ids': ['I-1', 'I-2'], 'error_excerpt': "No module named 'publication'",
                               'cause': 'import_order', 'needs': 'tests/test_collection.py', 'needs_run_ids': ['E-1'],
                               'note': 'Loads a script whose sibling import needs scripts/ on sys.path.'}],
         'static_smells': [],
         'summary': {'headline': '1 of 3 tests failed only under the clock shift across 9 runs.', 'tests_run': 3,
                     'flaky': 1, 'consistently_failing': 0, 'not_observed_flaky': 1, 'not_run': 1, 'cannot_run_alone': 1,
                     'by_cause': {'time_and_locale': 1}, 'by_priority': {'P0': 0, 'P1': 0, 'P2': 0, 'P3': 1},
                     'total_runs': 9, 'wall_minutes': 0.3, 'evidence_completeness': 0.75},
         'limitations': ['The locale variation was not run on this host.'], 'files_changed': []}
    brief, full = render(d, True), render(d, False)
    checks = [
        ('--brief keeps the limitations', 'Limitation: The locale variation was not run' in brief),
        ('--brief keeps each flake\'s experiment and its result', 'Moved the clock back a day.' in brief
         and 'Failed 3/3 under the shift' in brief),
        ('--brief leaves out the fix diff and the tools', '```diff' not in brief and '## Tools' not in brief),
        ('a failure only under the clock shift is worded as a probe artefact', 'probe artefact' in brief
         and 'probe artefact' in full and 'only under the clock shift' in full),
        ('cannot-run-alone tests have their own section and count', 'Cannot run alone' in brief
         and 'Cannot run alone' in full and "No module named 'publication'" in brief and '1 cannot run alone' in full
         and 'tests/test_collection.py' in full),
        ('tests that never ran are listed with the reason', 'ignored: #[ignore]' in full and 'quay::tests::slow' in brief),
        ('the full report still shows the fix', '```diff' in full and 'Limitation: ' in full),
    ]
    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(('ok   ' if ok else 'FAIL ') + name)
    print('render_report self-test ' + ('FAILED' if bad else 'passed'))
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
