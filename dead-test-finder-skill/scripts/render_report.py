#!/usr/bin/env python3
"""Render a dead test report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py dead_test_report.json [--color auto|always|never] [--brief]

--brief prints the headline and the ranked findings without the evidence details, the test list
or the limitations.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
PRIO = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
TOOL = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
LABEL = {'never_runs': 'never runs', 'cant_fail': "can't fail", 'mock_only': 'mock only', 'async_not_awaited': 'async not awaited',
         'swallowed_failure': 'swallowed failure', 'unreached_assertion': 'unreached assertion', 'stale_skip': 'stale skip',
         'duplicate': 'duplicate', 'overlap': 'overlap', 'weak': 'weak'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


def evidence_line(e: dict) -> str | None:
    if e.get('neutralisation'):
        n = e['neutralisation']
        parts = [f'{t["target"].split(".")[-1]}: ' + ', '.join(f'{v["variant"]} -> {v["test_status"]}' for v in t['variants'])
                 for t in n['targets']]
        return 'Neutralised: ' + ('; '.join(parts) if parts else 'nothing to neutralise')
    if e.get('collection'):
        return f'Collect output ({e["collection"]["raw_output"]}): not collected' if e['collection'].get('not_collected') else None
    if e.get('similarity'):
        s = e['similarity']
        if 'coverage_identical' not in s:
            return f'Similarity {100 * s["similarity"]:.0f}%, coverage not compared'
        return (f'Similarity {100 * s["similarity"]:.0f}%, coverage {"identical" if s["coverage_identical"] else "differs"}, '
                f'values {"identical" if s["values_identical"] else "differ"}')
    if e.get('skip'):
        s = e['skip']
        t = s.get('ticket') or {}
        return f'Skip {s["verdict"]}' + (f': {t.get("key")} is {t.get("status")}' if t else '') + f'; age {s.get("age_source")}'
    return None


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
    md, s = d['metadata'], d['summary']
    out = [P(f'# Dead tests: {md["repo"]}', BOLD),
           f'{s["tests_defined"]} tests defined, {s["collected"]} collected, {s["neutralised"]} neutralised | {len(d["findings"])} '
           f'findings | evidence completeness {100 * s["evidence_completeness"]:.0f}%', '', P(s['headline'], BOLD), '',
           P('## Findings', BOLD)]
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    for f in sorted(d['findings'], key=lambda f: (order[f['priority']], -f['score'], f['id'])):
        out.append(f'- {P(f["priority"], PRIO[f["priority"]])} {f["id"]} [{LABEL[f["category"]]}, {f["confidence"]}'
                   + (', found by review' if f.get('source') == 'agent' else '') + f'] '
                   f'{f["location"]["file"]}:{f["location"]["line"]}')
        out.append(f'    {f["why"]}')
        p = f['proposal']
        proved = p.get('proof', {}).get('result')
        out.append(f'    Proposal: {p["action"]}' + (f' ({p["patch_path"]})' if p.get('patch_path') else '') +
                   (P(f', {proved}', GREEN if proved == 'proved' else YELLOW) if proved else '') +
                   (P(', applied', CYAN) if f['applied'] else ''))
        if not args.brief:
            ev = evidence_line(f['evidence'])
            if ev:
                out.append(P(f'    {ev}', DIM))
            for b in f.get('basis') or []:
                out.append(P('    Basis: ' + (f'{b["file"]}:{b["line"]}' if b.get('file') else b.get('evidence', '')), DIM))
            if f['evidence'].get('note'):
                out.append(P(f'    {f["evidence"]["note"]}', DIM))
            out.append(P(f'    {p["rationale"]}', DIM))
    if d['dismissed']:
        out += ['', P(f'## Dismissed ({len(d["dismissed"])})', BOLD)] + [f'- {x["candidate_id"]}' + (' (scanner error)' if x.get('scanner_error') else '') + f': {x["reason"]}'
                                                                     for x in d['dismissed']]
    if not args.brief:
        col = d['collection']
        out += ['', P('## Collection', BOLD), f'- {col["collected"]} of {col["defined"]} defined tests collected']
        out += [f'- not collected: `{t}`' for t in col['not_collected']]
        out += [f'- not assessed: `{t}`' for t in col['not_assessed']]
        out += ['', P('## Tools', BOLD)] + [f'- {t["name"]}: {P(t["status"], TOOL.get(t["status"], ""))}. {t["reason"]}' for t in d['tools']]
        out += ['', P('## Limitations', BOLD)] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
