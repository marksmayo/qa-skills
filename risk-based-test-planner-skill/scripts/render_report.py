#!/usr/bin/env python3
"""Render a risk-based test plan for the terminal, or write it as test_plan.md (stdlib only).

    python render_report.py test_plan.json [--color auto|always|never] [--brief]
    python render_report.py test_plan.json --markdown test_plan.md

The Markdown plan follows the sections of ISO/IEC/IEEE 29119-3 loosely: scope, risk register,
approach, effort, entry and exit criteria, what is not tested, assumptions and open questions.
--brief prints the headline, the risk register and the effort only.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
BAND = {'High': RED + BOLD, 'Medium': YELLOW, 'Low': DIM}
FACTOR_LABEL = {'churn': 'Churn', 'defects': 'Defect history', 'complexity': 'Complexity', 'change_in_scope': 'Change in scope',
                'test_weakness': 'Test weakness', 'journey_criticality': 'Journey criticality', 'surface': 'Surface', 'reach': 'Reach'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def lines(d: dict, P, brief: bool, md: bool) -> list[str]:
    m, s = d['metadata'], d['summary']
    by = {a['id']: a for a in d['areas']}
    budget = m['budget_sessions']
    out = [P(f'# Test plan: {m.get("title") or m["repo"]}', BOLD), '',
           P(s['headline'], BOLD), '']
    scope = m.get('scope') or {}
    h = m['history']
    out += [P('## Scope', BOLD),
            f'- Repository `{m["repo"]}` at `{m["head"]}`; scope: {scope.get("kind") or "whole repository"}'
            + (f' since `{scope.get("value")}`' if scope.get('value') else ''),
            f'- History: {h["from"]} to {h["to"]}, {h["commits"]} commits'
            + (f'; {len(h.get("excluded_formatting", []))} formatting-only commit(s) left out of churn' if h.get('excluded_formatting') else ''),
            f'- Effort: {budget} sessions of {m.get("session_minutes", 90)} minutes' if budget is not None else
            '- Effort: no budget given, so relative shares only',
            f'- Inputs used: ' + (', '.join(f'{i["skill"]} ({i["path"]})' for i in m['inputs_used']) or 'none')
            + (f'; missing: {", ".join(m["inputs_missing"])}' if m['inputs_missing'] else ''), '']
    out += [P('## Risk register', BOLD), '',
            '| Area | Likelihood | Impact | Risk | Band | Confidence | Flags |', '|---|---|---|---|---|---|---|']
    for a in d['areas']:
        out.append(f'| {a["id"]} | {a["likelihood"]} | {a["impact"]} | {a["risk"]} | {P(a["band"], BAND[a["band"]])} | '
                   f'{a["confidence"]} | {", ".join(a["flags"]) or "-"} |')
    out.append('')
    if not brief:
        out.append(P('### Evidence per area', BOLD))
        for a in d['areas']:
            out.append(f'**{a["id"]}** (`{"`, `".join(a["paths"])}`)' if md else P(f'{a["id"]} ({", ".join(a["paths"])})', BOLD))
            for k, f in a['factors'].items():
                score = '-' if f['score'] is None else f['score']
                tag = {'not_assessed': ' (not assessed)', 'judged': ' (judged)'}.get(f['status'], '')
                out.append(f'- {FACTOR_LABEL[k]}: {score}{tag} - ' + '; '.join(f['evidence'][:3])
                           + (f' Judged: {f["judged_reason"]}' if f.get('judged_reason') else ''))
            for o in a['open_findings']:
                out.append(f'- Open finding: {o["skill"]} {o["id"]} {o.get("title") or ""} ({o["severity"]}, confidence {o["confidence"]})')
            out.append('')
    out += [P('## Approach and effort', BOLD), '']
    if budget is not None:
        out += ['| Area | Band | Sessions | Test types |', '|---|---|---|---|']
        for st in d['strategy']:
            a = by[st['area_id']]
            out.append(f'| {a["id"]} | {a["band"]} | {st["effort_sessions"]} | {", ".join(t["type"] for t in st["test_types"])} |')
    else:
        out += ['| Area | Band | Share of effort | Test types |', '|---|---|---|---|']
        for st in d['strategy']:
            a = by[st['area_id']]
            out.append(f'| {a["id"]} | {a["band"]} | {100 * st["effort_share"]:.0f}% | {", ".join(t["type"] for t in st["test_types"])} |')
    out.append('')
    if s['sessions_short']:
        out += [P(f'The budget is {s["sessions_short"]} session(s) short of the High-risk floors.', RED), '']
    if not brief:
        for st in d['strategy']:
            out.append(f'**{st["area_id"]}**' if md else P(st['area_id'], BOLD))
            for t in st['test_types']:
                out.append(f'- {t["type"]}' + (f' ({t["owner_skill"]})' if t.get('owner_skill') else '') + f': {t["why"]}')
            for c in st.get('charters', []):
                out.append(f'- Charter: {c}')
            out.append('')
        out += [P('## Not tested', BOLD)]
        if not d['not_tested']:
            out.append('- Every area in scope has planned testing.')
        for n in d['not_tested']:
            out.append(f'- **{n["area_id"]}** (residual risk {n["residual_band"]}): {n["reason"]} Accepted by: '
                       f'{n["accepted_by"] or "nobody yet"}.')
        out += ['', P('## Entry criteria', BOLD)] + [f'- {e}' for e in d['entry_criteria']]
        out += ['', P('## Exit criteria', BOLD)] + [f'- {e["condition"]} ({e["measure"]})' for e in d['exit_criteria']]
        if d['assumptions']:
            out += ['', P('## Assumptions', BOLD)] + [f'- {x}' for x in d['assumptions']]
        if d['open_questions']:
            out += ['', P('## Open questions', BOLD)] + [f'- {x}' for x in d['open_questions']]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--markdown', type=Path, help='write the full plan as Markdown to this file')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read plan: {exc}", file=sys.stderr)
        return 2
    if args.markdown:
        text = '\n'.join(lines(d, lambda t, s: t, False, True)) + '\n'
        args.markdown.write_text(text, encoding='utf-8', newline='\n')
        print(f'written {args.markdown}')
        return 0
    c = supports_color(args.color)
    print('\n'.join(lines(d, (lambda t, s: f'{s}{t}{RESET}' if c and s else t), args.brief, False)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
