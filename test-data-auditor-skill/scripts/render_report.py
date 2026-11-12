#!/usr/bin/env python3
"""Render a test data audit report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py test_data_report.json [--color auto|always|never] [--brief]

--brief prints the headline and the ranked findings without the inventory, dismissals or
limitations, and summarises the handoffs per recipient and file. Findings found by review
(source: agent) are labelled so. Personal data never appears: the report holds only file, line, field and type.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
TOOL = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}
LABEL = {'personal_data': 'personal data', 'time_bomb': 'time bomb', 'magic_id': 'magic id',
         'environment_coupling': 'environment coupling', 'shared_mutable': 'shared mutable', 'nondeterministic': 'nondeterministic',
         'schema_drift': 'schema drift', 'snapshot_rot': 'snapshot rot', 'unused_or_duplicate': 'unused or duplicate',
         'locale_assumption': 'locale assumption', 'residue': 'residue'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text


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
    out = [P(f'# Test data audit: {md["repo"]}', BOLD),
           f'{s["files_scanned"]} files scanned, {s["not_assessed"]} not assessed | {len(d["findings"])} findings | '
           f'audit date {md["today"]} | evidence completeness {100 * s["evidence_completeness"]:.0f}%', '',
           P(s['headline'], BOLD), '']
    if s.get('earliest_time_bomb_date'):
        out += [f'Earliest time bomb: {P(s["earliest_time_bomb_date"], RED)}', '']
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    out.append(P('## Findings', BOLD))
    for f in sorted(d['findings'], key=lambda f: (order[f['priority']], -f['score'], f['id'])):
        loc = f'{f["location"]["file"]}:{f["location"]["line"]}' + (f' ({f["location"]["field"]})' if f['location'].get('field') else '')
        by_review = ', found by review' if f.get('source') == 'agent' else ''
        out.append(f'- {P(f["priority"], PRIO[f["priority"]])} {f["id"]} [{LABEL[f["category"]]}, {f["severity"]}, {f["confidence"]}'
                   f'{by_review}] {f["title"]}')
        out.append(f'    {loc}' + (f'; {f["data_type"]}' if f.get('data_type') else ''))
        out.append(f'    {f["why_flagged"]}')
        if f.get('experiment'):
            e = f['experiment']
            out.append(P(f'    Experiment: {e["variation"]} -> {e["result"].replace("_", " ")} ({", ".join(e["run_ids"])})', CYAN))
        if f.get('fails_on_date'):
            out.append(P(f'    Fails on {f["fails_on_date"]}', RED))
        if not args.brief:
            for b in f.get('basis') or []:
                out.append(P(f'    Basis: {b["evidence"]}' if 'evidence' in b else f'    Basis: {b["file"]}:{b["line"]}', DIM))
            out.append(f'    Fix: {f["fix"]}')
            out.append(P(f'    Check: {f["verification_step"]}', DIM))
            out.append(P(f'    Tests: {len(f["affected_tests"])}; score {f["score"]} (reach {f["reach"]})', DIM))
    if d['handoffs'] and args.brief:
        # brief: one line per recipient, with the files holding the most handoffs
        out += ['', P(f'## Handed off ({len(d["handoffs"])})', BOLD)]
        for to in sorted({h['to'] for h in d['handoffs']}):
            hs = [h for h in d['handoffs'] if h['to'] == to]
            files: dict[str, int] = {}
            for h in hs:
                files[h['location']['file']] = files.get(h['location']['file'], 0) + 1
            top = sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))
            shown = ', '.join(f'{f} ({n})' for f, n in top[:5]) + (f', and {len(top) - 5} more file(s)' if len(top) > 5 else '')
            out.append(f'- {to}: {len(hs)} in {len(files)} file(s): {shown}')
    elif d['handoffs']:
        out += ['', P('## Handed off', BOLD)]
        for h in d['handoffs']:
            out.append(f'- {h["to"]}: {h["location"]["file"]}:{h["location"]["line"]} - {h["reason"]}')
    if not args.brief:
        out += ['', P(f'## Dismissed ({len(d["dismissed"])})', BOLD)]
        for x in d['dismissed']:
            out.append(f'- {x["candidate_id"]} ({x["by"]}): {x["reason"]}')
        out += ['', P('## Inventory', BOLD)]
        for i in d['inventory']:
            state = '' if i['status'] == 'scanned' else P(f' not assessed: {i.get("reason")}', YELLOW)
            out.append(f'- `{i["path"]}` {i["kind"]}, {i["format"]}, loaded by {len(i["loaded_by"])} test(s){state}')
        out += ['', P('## Tools', BOLD)]
        for t in d['tools']:
            out.append(f'- {t["name"]}: {P(t["status"], TOOL.get(t["status"], ""))}. {t["reason"]}'
                       + (f' Leaves unchecked: {t["leaves_unchecked"]}.' if t.get('leaves_unchecked') else ''))
        out += ['', P('## Limitations', BOLD)] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
