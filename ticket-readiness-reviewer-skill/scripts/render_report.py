#!/usr/bin/env python3
"""Render a ticket readiness review as a terminal/Markdown report with optional ANSI colour.

    python render_report.py readiness.json [--color auto|always|never] [--brief] [--comment KEY]

--brief prints the batch ranking (or the one verdict) with blocking questions only.
--comment KEY prints only the plain text to paste into that ticket's Jira comment.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
VERDICT_STYLE = {'ready': GREEN + BOLD, 'ready_with_questions': YELLOW + BOLD, 'not_ready': RED + BOLD,
                 'not_assessed': MAGENTA + BOLD}
VERDICT = {'ready': 'Ready (testable)', 'ready_with_questions': 'Ready, with questions', 'not_ready': 'Not ready',
           'not_assessed': 'Not assessed'}
CONF_STYLE = {'high': GREEN, 'medium': CYAN, 'low': DIM}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def score_cell(d: dict) -> str:
    if d['status'] == 'not_applicable':
        return 'n/a'
    if d['status'] == 'not_assessed':
        return 'not assessed'
    return f"{d['score']}/3"


def render_review(r: dict, P, brief: bool) -> list[str]:
    t, v = r['ticket'], r['verdict']
    out = []
    w = out.append
    score = f"{v['readiness_score']:.2f}/3" if v['readiness_score'] is not None else 'no score'
    w(P(f"## {t['key']}: {t['summary']}", BOLD))
    w('')
    w(f"{t['type']} ({t['kind']} rubric)  |  source: {t['source']}  |  {len(r['criteria'])} criteria  |  "
      f"readiness {score}")
    w(P(f"Verdict: {VERDICT[v['value']]}", VERDICT_STYLE[v['value']]))
    for reason in v['reasons']:
        w(f"  - {reason}")
    w('')
    blocking = [q for q in r['questions'] if q['blocking']]
    if brief:
        for q in blocking:
            w(f"  {P('BLOCKING', RED)} {q['id']}: {q['question']}")
        if blocking:
            w('')
        return out
    w('| Dimension | Weight | Score | Why |')
    w('|---|---|---|---|')
    for d in r['dimensions']:
        w(f"| {d['name'].replace('_', ' ')} | {d['weight']} | {score_cell(d)} | {d['rationale']} |")
    w('')
    if r['criteria']:
        w(P('### Criteria', BOLD))
        for c in r['criteria']:
            oracle = '' if c['oracle'] == 'present' else P(f" [oracle {c['oracle']}]", YELLOW)
            w(f"- **{c['id']}**{oracle} {c['text']}")
            if c.get('oracle_note'):
                w(f"    {P(c['oracle_note'], DIM)}")
            for f in c['flags']:
                src = 'lint' if f['evidence_type'] == 'tool' else 'judgment'
                term = f" \"{f['term']}\"" if f.get('term') else ''
                w(f"    - {f['id']} {f['kind'].replace('_', ' ')}{term} ({src}, "
                  f"{P(f['confidence'], CONF_STYLE[f['confidence']])}): {f['note']}")
        w('')
    if r['dismissed_flags']:
        w(P(f"Dismissed lint flags ({len(r['dismissed_flags'])})", DIM))
        for x in r['dismissed_flags']:
            w(P(f"  - {x['lint_id']}: {x['reason']}", DIM))
        w('')
    if r['questions']:
        w(P('### Questions for the author', BOLD))
        for q in r['questions']:
            tag = P('BLOCKING', RED) if q['blocking'] else P('question', CYAN)
            rel = f" ({', '.join(q['related_criteria'])})" if q['related_criteria'] else ''
            w(f"- {tag} {q['id']}{rel}: {q['question']}")
            w(f"    {P(q['why'], DIM)}")
        w('')
    if r['suggestions']:
        w(P('### Suggested criteria (suggestions, not requirements)', BOLD))
        for s in r['suggestions']:
            dep = f" (assumes an answer to {s['depends_on_answer']})" if s.get('depends_on_answer') else ''
            if s['criterion_id']:
                w(f"- {s['id']} rewrite of {s['criterion_id']}{dep}")
                w(f"    was:       {s['original']}")
                w(f"    suggested: {s['suggested']}")
            else:
                w(f"- {s['id']} new{dep}: {s['suggested']}")
        w('')
    if r['dependencies']:
        w(P('### Dependencies', BOLD))
        for dp in r['dependencies']:
            state = P('unstated', YELLOW) if dp['status'] == 'unstated' else 'named'
            w(f"- {dp['kind'].replace('_', ' ')} ({state}): {dp['description']}")
        w('')
    if r['split_proposal']:
        sp = r['split_proposal']
        w(P('### Split proposal', BOLD))
        w(sp['reason'])
        for part in sp['parts']:
            w(f"- {part['title']}: {', '.join(part['criteria']) or 'new criteria needed'}")
        w('')
    dor = r['definition_of_ready']
    if dor['extra_items']:
        w(P(f"### Definition of Ready ({dor['source']})", BOLD))
        for item in dor['extra_items']:
            style = GREEN if item['status'] == 'met' else RED if item['status'] == 'not_met' else MAGENTA
            w(f"- {P(item['status'], style)} {item['id']}: {item['text']} ({item['evidence']})")
        w('')
    cc = r['code_checks']
    if cc['status'] == 'ran':
        missing = [x['name'] for x in cc['references'] if not x['found']]
        w(f"Code check: {len(cc['references'])} reference(s) looked up"
          + (f", not found (questions, not defects): {', '.join(missing)}" if missing else ', all found'))
    else:
        w(P(f"Code check not run: {cc.get('reason', '')}", DIM))
    if r['suspicious_instructions_ignored']:
        w(P(f"Instruction-like text in the ticket was ignored ({len(r['suspicious_instructions_ignored'])}):", MAGENTA))
        for s in r['suspicious_instructions_ignored']:
            w(f"  - {s['field']}: \"{s['excerpt']}\"")
    for lim in r['limitations']:
        w(P(f"Limitation: {lim}", DIM))
    w('')
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--comment', metavar='KEY')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    if args.comment:
        r = next((x for x in d['reviews'] if x['ticket']['key'] == args.comment), None)
        if not r:
            print(f"No review for {args.comment}", file=sys.stderr)
            return 2
        print(r['comment_text'])
        return 0
    c = supports_color(args.color)
    P = lambda t, s: paint(t, s, c)  # noqa: E731
    out = [P('# Ticket readiness review', BOLD), '',
           f"{d['skill']['name']} {d['skill']['version']}  |  rubric {d['rubric_version']}  |  generated {d['generated_at']}",
           "A ready verdict means the criteria are specific enough to test. It doesn't mean the requirement is right.", '']
    if 'batch' in d:
        b = d['batch']
        short = {'ready': 'ready', 'ready_with_questions': 'ready with questions', 'not_ready': 'not ready',
                 'not_assessed': 'not assessed'}
        counts = ', '.join(f"{n} {short[k]}" for k, n in b['counts'].items() if n)
        out += [P('## Batch (worst first)', BOLD), '', f"Query: {b['query'] or '(list of tickets)'}  |  {counts}", '',
                '| # | Ticket | Verdict | Score | Blocking |', '|---|---|---|---|---|']
        for row in b['ranking']:
            sc = f"{row['readiness_score']:.2f}" if row['readiness_score'] is not None else '-'
            out.append(f"| {row['rank']} | {row['key']} | {P(VERDICT[row['verdict']], VERDICT_STYLE[row['verdict']])} | "
                       f"{sc} | {row['blocking_questions']} |")
        out.append('')
        order = {row['key']: row['rank'] for row in b['ranking']}
        reviews = sorted(d['reviews'], key=lambda r: order.get(r['ticket']['key'], 0))
    else:
        reviews = d['reviews']
    for r in reviews:
        out += render_review(r, P, args.brief)
    print('\n'.join(out).rstrip() + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
