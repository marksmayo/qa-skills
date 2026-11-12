#!/usr/bin/env python3
"""Render a mutation testing report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py mutation_report.json [--color auto|always|never] [--brief]

--brief prints the headline, the scores and the ranked findings without the code, the
unranked triage or the mutant list.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
STATUS_STYLE = {'killed': GREEN, 'survived': RED, 'no_coverage': MAGENTA, 'timeout': YELLOW, 'runtime_error': YELLOW,
                'compile_error': DIM, 'not_run': DIM}
TOOL_STYLE = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
TRIAGE_LABEL = {'weak_assertion': 'weak assertion', 'missing_case': 'missing case', 'equivalent': 'equivalent',
                'accepted': 'accepted'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def short(tid: str | None) -> str:
    return tid.split('::')[-1] if tid else '-'


def mid_label(mid: str, m: dict) -> str:
    """The report id, with the engine's own id when it differs (what the engine printed and wrote)."""
    raw = m.get('raw_id')
    if not raw or raw == mid:
        return mid
    return f'{mid} (engine id {raw if len(raw) <= 24 else "..." + raw[-21:]})'


def where(f: dict) -> str:
    return f['file'] + (f' {f["function"]}()' if f.get('function') else ' (module level)')


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
    md, s = d['metadata'], d.get('summary') or {}
    missing = [k for k in ('by_engine', 'findings', 'coverage_gaps', 'evidence_completeness') if k not in s]
    if missing:
        print(f'The report has no computed summary ({", ".join(missing)} missing). Run '
              'validate_mutation_report.py <report> --repo <repo> --write-summary first.', file=sys.stderr)
        return 2
    mutants = {m['id']: m for m in d['mutants']}
    sc = md['scope']
    src = sc.get('diff_file') or (f'changes since {sc["base_ref"]}' if sc.get('base_ref') else sc['mode'])
    print(P(f'# Mutation testing: {md["repo"]}', BOLD))
    print(f'Scope: {sc["mode"]} ({src}), {len(sc["files"])} file(s); ' +
          ', '.join(f'{e["name"]} {e["version"] or ""} ({e["config_source"]})'.replace(' (', ' (', 1) for e in md['engines']))
    print()
    print(s['headline'])
    print()
    print(P('## Scores', BOLD))
    for e in s['by_engine']:
        print(f'- {e["score_text"]}')
    print(f'- Findings: {s["findings"]["weak_assertion"]} weak assertion, {s["findings"]["missing_case"]} missing case, '
          f'{s["findings"]["equivalent"]} equivalent, {s["findings"]["accepted"]} accepted; '
          f'{s["coverage_gaps"]} coverage gap(s); evidence completeness {s["evidence_completeness"]:.2f}')
    b = d['baseline']
    print(f'- Baseline: {"green" if b["passed"] else P("NOT green", RED)} in {len(b["run_ids"])} run(s) '
          f'({", ".join(b["run_ids"])})' + (f'; flaky: {", ".join(b["flaky_tests"])}' if b['flaky_tests'] else ''))
    print()
    ranked = [f for f in d['findings'] if f['priority']]
    print(P('## Surviving mutants that matter', BOLD))
    if not ranked:
        print('None ranked.')
    for f in ranked:
        print(P(f'### {f["priority"]} {f["id"]} {TRIAGE_LABEL[f["triage"]]}: {where(f)}', PRIO_STYLE.get(f['priority'], '')) +
              P(f'  score {f["score"]} = {f["criticality"]} x {f["kind_factor"]} x {f["proximity"]}', DIM))
        print(f['summary'])
        for mid in f['mutant_ids']:
            m = mutants.get(mid, {})
            print(P(f'  {mid_label(mid, m)} {m.get("engine", "")} line {m.get("line")}: ', DIM) +
                  f'{m.get("original", "").strip()}  ->  {m.get("mutated", "").strip()}')
        print(f'Should have caught: {short(f["should_have_caught"])}' if f['should_have_caught'] else 'No existing test uses a distinguishing input.')
        if f['suggestion']:
            print(f'Suggest ({f["suggestion"]["test_file"]}): {f["suggestion"]["assertion_or_input"]}')
            if f['suggestion'].get('code') and not args.brief:
                print('```python\n' + f['suggestion']['code'] + '\n```')
        if f['proof_ids']:
            print(P(f'Proved: {", ".join(f["proof_ids"])} (passes on the original, fails on each mutant)', GREEN))
        print()
    if d['coverage_gaps']:
        print(P('## Coverage gaps (no test runs these lines)', BOLD))
        for g in d['coverage_gaps']:
            print(P(f'- {g["id"]} {g["file"]} {g["function"] or ""} lines {g["lines"][0]}-{g["lines"][-1]}: ', MAGENTA) +
                  f'{len(g["mutant_ids"])} mutants. {g["note"]}')
            if g['suggestion']:
                print(f'  Suggest: {g["suggestion"]}' + (f' Proved: {", ".join(g["proof_ids"])}.' if g['proof_ids'] else ''))
        print()
    if args.brief:
        return 0
    others = [f for f in d['findings'] if not f['priority']]
    if others:
        print(P('## Not ranked', BOLD))
        for f in others:
            print(f'- {f["id"]} {TRIAGE_LABEL[f["triage"]]} ({", ".join(f["mutant_ids"])}), {where(f)}: {f["reason"]}')
        print()
    odd = [m for m in d['mutants'] if m['status'] in ('timeout', 'runtime_error', 'compile_error', 'not_run')]
    if odd:
        print(P('## Timeouts, errors and mutants not run (never counted as kills)', BOLD))
        for m in odd:
            print(P(f'- {mid_label(m["id"], m)} {m["status"]}', STATUS_STYLE[m['status']]) +
                  f' {m["file"]}:{m["line"]} {m["original"].strip()}  ->  {m["mutated"].strip()}' +
                  (f' ({m["note"]})' if m.get('note') else ''))
        print()
    print(P('## Engines and tools', BOLD))
    for e in md['engines']:
        line = f'- {e["name"]} {e["version"] or ""}: {e["status"]}, {e["in_scope"]} mutants in scope ({e["out_of_scope"]} outside)'
        if e.get('sampled') and e['sampled'].get('skipped'):
            line += f', {e["sampled"]["skipped"]} skipped by the budget'
        if e.get('timeout_s'):
            line += f', timeout {e["timeout_s"]}s'
        print(line + (f'. {e["reason"]}' if e['reason'] else ''))
    for t in d['tools']:
        print(P(f'- {t["name"]}: {t["status"]}', TOOL_STYLE[t['status']]) + f' ({t["reason"]})')
    if sc['excluded']:
        print('Excluded from scope: ' + '; '.join(f'{x["path"]} ({x["why"]})' for x in sc['excluded']))
    print()
    if d['limitations']:
        print(P('## Limitations', BOLD))
        for l in d['limitations']:
            print(f'- {l}')
    if d['files_changed']:
        print(P('\nFiles changed: ', BOLD) + ', '.join(d['files_changed']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
