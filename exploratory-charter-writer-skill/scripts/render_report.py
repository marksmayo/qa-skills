#!/usr/bin/env python3
"""Render charters.json or debrief.json as a terminal/Markdown report with optional ANSI colour.

    python render_report.py charters.json [--color auto|always|never] [--brief]
    python render_report.py debrief.json [--color auto|always|never] [--brief]

The file type is recognised from its contents. --brief prints the charter list or the debrief's
bugs and PROOF summary only.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
GROUP = {'do_first': ('Do first', RED + BOLD), 'next': ('Next', YELLOW), 'if_time': ('If time', DIM), 'unranked': ('Unranked', DIM)}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def charters(d: dict, P, brief: bool) -> list[str]:
    m = d['metadata']
    out = [P('# Exploratory charters', BOLD),
           f'{len(d["charters"])} charters, {m["timebox"]}-minute sessions. Sources: '
           + ', '.join(u['kind'] for u in m['sources_used']) + (f'; missing: {", ".join(m["sources_missing"])}' if m['sources_missing'] else ''),
           '']
    for c in d['charters']:
        label, colour = GROUP[c['group']]
        mi = c['mission']
        out.append(f'- {P(label, colour)} {c["id"]} ({c["impact"] if c["impact"] is not None else "?"} x {c["uncertainty"]}): '
                   f'Explore {mi["target"]} with {mi["resources"]} to discover {mi["information"]}')
        if not brief:
            out.append(P(f'    Areas: {"; ".join(c["areas"])}', DIM))
            out.append(P(f'    Heuristics: {", ".join(c["heuristics"])}; personas: {", ".join(c["personas"])}', DIM))
            out.append(P('    Basis: ' + '; '.join(b['ref'] for b in c['basis']), DIM))
    if d['merged']:
        out += ['', 'Merged: ' + ', '.join(f'{x["id"]} into {x["into"]} ({x["reason"]})' for x in d['merged'])]
    if m.get('suspicious_instructions'):
        out += ['', P(f'{len(m["suspicious_instructions"])} instruction-like passage(s) in the sources were ignored.', YELLOW)]
    return out


def debrief(d: dict, P, brief: bool) -> list[str]:
    m = d['metadata']
    out = [P(f'# Session debrief: {m["notes"]["path"]}' + (f' ({m["charter_id"]})' if m['charter_id'] else ''), BOLD)]
    mt = d['metrics']
    if mt:
        out.append(f'{mt["duration_min"]} minutes: test {mt["test_pct"]}%, bug investigation {mt["bug_pct"]}%, setup {mt["setup_pct"]}%; '
                   f'{mt["on_charter_pct"]}% on charter')
    out += ['', P('## Draft bugs', BOLD)]
    if not d['bugs']:
        out.append('- None noted.')
    for b in d['bugs']:
        state = P('complete', GREEN) if b['status'] == 'complete' else P(f'incomplete (missing {", ".join(b["missing"])})', YELLOW)
        out.append(f'- {b["id"]} {b["title"]} [{state}; suggested {b["suggested_severity"]}, {b["confidence"]}; '
                   f'{b["reproduction"]["status"].replace("_", " ")}]')
        if not brief:
            for i, s in enumerate(b['steps'], 1):
                out.append(P(f'    {i}. {s}', DIM))
            if b['expected']:
                out.append(P(f'    Expected: {b["expected"]}', DIM))
            if b['actual']:
                out.append(P(f'    Actual: {b["actual"]}', DIM))
            out.append(P(f'    Notes lines: {", ".join(str(q["line"]) for q in b["quotes"])}', DIM))
    if not brief:
        for key, title in (('questions', 'Questions'), ('obstacles', 'Obstacles'), ('ideas', 'Ideas'), ('off_charter', 'Off charter')):
            if d[key]:
                out += ['', P(f'## {title}', BOLD)] + [f'- {x["text"]} (line {", ".join(str(q["line"]) for q in x["quotes"])})' for x in d[key]]
        out += ['', P('## Coverage', BOLD)]
        for x in d['coverage']['areas']:
            colour = GREEN if x['status'] == 'explored' else YELLOW
            out.append(f'- {P(x["status"].replace("_", " "), colour)}: {x["name"]}' +
                       (f' (lines {", ".join(map(str, x["note_lines"]))})' if x['note_lines'] else ''))
        if d['suspicious_instructions_ignored']:
            out += ['', P('## Ignored instructions', BOLD)] + [f'- line {s["line"]}: {s["text"]}' for s in d['suspicious_instructions_ignored']]
        if d['handoffs']:
            out += ['', P('## Hand-offs', BOLD)] + [f'- {h["to"]}: {h["item"]} - {h["why"]}' for h in d['handoffs']]
    p = d['proof']
    out += ['', P('## PROOF', BOLD), f'- Past: {p["past"]}', f'- Results: {p["results"]}', f'- Obstacles: {p["obstacles"]}',
            f'- Outlook: {p["outlook"]}', f'- Feelings: {p["feelings"]}']
    return out


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
        print(f"Unable to read file: {exc}", file=sys.stderr)
        return 2
    c = supports_color(args.color)
    P = lambda t, s: f'{s}{t}{RESET}' if c and s else t  # noqa: E731
    print('\n'.join(charters(d, P, args.brief) if 'charters' in d else debrief(d, P, args.brief)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
