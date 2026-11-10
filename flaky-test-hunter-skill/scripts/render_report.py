#!/usr/bin/env python3
"""Render a flaky test report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py flaky_report.json [--color auto|always|never] [--brief]

--brief prints the headline, the ranked flakes and the per-test table without fixes or smells.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
CONF_STYLE = {'confirmed': GREEN, 'indicated': CYAN, 'suspected': DIM}
STATUS_STYLE = {'flaky': YELLOW, 'consistently_failing': RED, 'not_observed_flaky': DIM, 'not_run': MAGENTA}
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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    c = supports_color(args.color)
    P = lambda t, s: paint(t, s, c)  # noqa: E731
    m, s = d['metadata'], d['summary']
    out = []
    w = out.append
    w(P('# Flaky test report', BOLD))
    w('')
    w(f"Repo: {m['repo']}  |  {m['framework']}  |  {m['platform']}  |  generated {d['generated_at']}")
    w(f"Budget: {m['budget']['repeats']} repeats, {m['budget']['minutes']}-minute cap"
      + (P(' (stopped early)', YELLOW) if m['budget']['stopped_early'] else '')
      + f"  |  {s['total_runs']} runs, {s['wall_minutes']} min  |  gate: {m['gate']['level']}")
    w('')
    w(P(s['headline'], BOLD))
    w(f"{s['tests_run']} tests run: {P(str(s['flaky']) + ' flaky', YELLOW)}, "
      f"{P(str(s['consistently_failing']) + ' consistently failing', RED)}, "
      f"{s['not_observed_flaky']} not observed flaky, {s['not_run']} not run  |  "
      f"evidence completeness {pct(s['evidence_completeness'])}")
    w('Rates are failures/runs with a 95% Wilson interval. "Not observed flaky" means no failure in the runs shown, '
      'not that the test cannot fail.')
    w('')
    w(P('## Flakes', BOLD))
    w('')
    w('| # | Priority | Test | Cause | Confidence | Worst rate | Score |')
    w('|---|---|---|---|---|---|---|')
    tests = {t['id']: t for t in d['tests']}
    for f in d['flakes']:
        t = tests.get(f['test_id'])
        worst = f"{t['worst']['fails']}/{t['worst']['runs']} {t['worst']['label']}" if t else '-'
        w(f"| {f['id']} | {P(f['priority'], PRIO_STYLE[f['priority']])} | {short(f['test_id'])} | "
          f"{f['cause'].replace('_', ' ')} | {P(f['cause_confidence'], CONF_STYLE[f['cause_confidence']])} | "
          f"{worst} | {f['score']} |")
    w('')
    if not args.brief:
        for f in d['flakes']:
            w(P(f"### {f['id']} {f['test_id']}", BOLD))
            w(f"{f['cause'].replace('_', ' ')}, {f['cause_confidence']}: {f['experiment']['what_changed']}")
            w(f"  Result: {f['experiment']['result']}")
            if f['polluter']:
                w(f"  Polluter: {f['polluter']['test_id']} (pair run {f['polluter']['pair_run_id']} fails, "
                  f"victim alone {f['polluter']['alone_run_id']} passes)")
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
    w(P('## Tests', BOLD))
    w('')
    w('| Test | Status | Normal runs | Worst | Runs |')
    w('|---|---|---|---|---|')
    for t in d['tests']:
        worst = f"{t['worst']['label']}: {t['worst']['fails']}/{t['worst']['runs']}" if t['worst']['label'] else '-'
        w(f"| {t['id']} | {P(t['status'].replace('_', ' '), STATUS_STYLE[t['status']])} | {rate_text(t['normal'])} | "
          f"{worst} | {t['runs']} |")
    w('')
    if d['consistently_failing']:
        w(P('## Consistently failing (not flaky)', BOLD))
        for x in d['consistently_failing']:
            w(f"- {x['test_id']} ({x['runs']} runs): {x['note']}")
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
                w(f"- {sm['file']}:{sm['line']} {sm['smell']} ({sm['status'].replace('_', ' ')}{link}): {sm['note']}")
            w('')
        for lim in d['limitations']:
            w(P(f'Limitation: {lim}', DIM))
        if d['files_changed']:
            w('Files changed (uncommitted): ' + ', '.join(f['path'] for f in d['files_changed']))
    print('\n'.join(out).rstrip() + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
