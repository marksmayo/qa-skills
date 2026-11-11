#!/usr/bin/env python3
"""Render a resilience report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py resilience_report.json [--color auto|always|never] [--brief]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
PRI = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': CYAN}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    P = (lambda s, st: f'{st}{s}{RESET}') if supports_color(a.color) else (lambda s, st: s)
    hl = d['headline']
    out = [P('# Resilience probe', BOLD), '', hl['summary'], '',
           f'Scenarios run: {hl["scenarios_run"]} of {hl["scenarios_planned"]}. Findings by priority: ' +
           (', '.join(f'{k} {v}' for k, v in sorted(hl['priority_counts'].items())) or 'none') +
           f'. Flows with a P0: {", ".join(hl["flows_with_p0"]) or "none"}. Confidence {hl["overall_confidence"]}.',
           'Faults tried: ' + ', '.join(d['scope']['faults']) + '. Devices: ' + ', '.join(d['scope']['devices']) + '.']
    if a.brief:
        out.append('')
        out += [f'{P(f["id"], BOLD)} {P(f["priority"], PRI[f["priority"]])} {f["effect"]} on {f["flow"]}/{f["step"]}: {f["title"]}' for f in d['findings']]
        print('\n'.join(out))
        return 0
    for f in d['findings']:
        out += ['', P(f'### {f["id"]} {f["priority"]}: {f["title"]}', PRI[f['priority']]), '',
                f'{f["effect"]}{" on a write" if f["on_write"] else ""} in {f["flow"]} / {f["step"]} ({f["impact"]}, likelihood {f["likelihood"]}, '
                f'reach {f["reach"]}, score {f["score"]}; {f["confidence"]}).' + (f' {"; ".join(f["floors"])}.' if f['floors'] else ''),
                f'- Faults: {", ".join(f["faults"])} on {", ".join(f["endpoints"])} (scenarios {", ".join(f["scenarios"])})',
                f'- Expected: {f["expected"]}', f'- Actual: {f["actual"]}', f'- For the user: {f["user_impact"]}',
                f'- Evidence: ' + '; '.join(f'{e["scenario"]} {e["run"]}: {json.dumps(e["measure"])[:140]} [{", ".join(e["screenshots"])}]' for e in f['evidence'][:2]),
                f'- Repro: {f["repro"]["script"]}', f'- Fix: {f["fix"]}', f'- Verify: {f["verification_step"]}']
    out += ['', P('## Scenarios', BOLD), '', '| Scenario | Flow / step | Fault | Endpoint | Status | Effects |', '|---|---|---|---|---|---|']
    out += [f'| {s["id"]} | {s["flow"]} / {s["step"]} | {s["fault"]} | `{s["endpoint"]}` | {s["status"]} | {", ".join(s["effects"]) or "none"} |'
            for s in d['scenarios']]
    out += ['', P('## Coverage by fault group', BOLD), ''] + [f'- {g}: {c["ran"]} ran, {c["untested"]} untested, {c["errored"]} errored'
                                                            for g, c in sorted(d['coverage'].items())]
    if d['notes']:
        out += ['', P('## Notes (not findings)', BOLD), ''] + [f'- {n["kind"]}: {"; ".join(n["detail"])}' + (f' ({n["scenario"]})' if n.get('scenario') else
                                                                                                               f' (baseline, {n.get("flow")})') for n in d['notes']]
    dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
    if dismissed:
        out += ['', P('## Dismissed', BOLD), ''] + [f'- {t["candidate_id"]}: {t["reason"]}' for t in dismissed]
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
