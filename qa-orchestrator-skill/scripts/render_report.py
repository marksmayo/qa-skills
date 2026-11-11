#!/usr/bin/env python3
"""Render a QA orchestrator run: each gate's status, evidence, criteria and the person's decision.

    python render_report.py <state dir> [--brief] [--markdown summary.md]

--markdown exports the page (for example into the repository or a ticket) when asked; the run state itself
stays in its temporary folder.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def page(d: dict, brief: bool) -> list[str]:
    out = [f'# QA run {d["run_id"]}', '', f'Subject: {d["subject"]}', f'Status: **{d["status"]}**. State: `{d["state_dir"]}`'
           + (' (temporary folder)' if d.get('state_in_temp') else '') + '.', '']
    if d.get('summary', {}).get('headline'):
        out += [d['summary']['headline'], '']
    out += ['| Gate | Status | Decision | By | Evidence |', '|---|---|---|---|---|']
    for g in d['gates']:
        dec = g.get('decision') or {}
        ev = ', '.join(f'{s["skill"]} (validator {s["validator"]["exit_code"]})' for s in g['steps'] if s.get('validator')) or '-'
        out.append(f'| {g["id"]} {g["name"]} | {g["status"].replace("_", " ")} | {(dec.get("kind") or "-").replace("_", " ")} | {dec.get("by") or "-"} | {ev} |')
    if brief:
        return out
    for g in d['gates']:
        if g['status'] == 'pending':
            continue
        dec = g.get('decision') or {}
        out += ['', f'## {g["id"]} {g["name"]} ({g["status"].replace("_", " ")})', '']
        for s in g['steps']:
            if s.get('report'):
                out.append(f'- {s["skill"]}: `{s["report"]}` (SHA-256 {s["sha256"][:12]}..., validator exit {s["validator"]["exit_code"]}'
                           + (f': {s["validator"]["output"][-1]}' if s['validator'].get('output') else '') + ')')
            else:
                out.append(f'- {s["skill"]}: not recorded' + ('' if s['required'] else ' (optional)'))
        for c in g.get('criteria', []):
            out.append(f'- criterion {c["id"]}: **{c["result"]}** ({c["detail"]})')
        if dec:
            line = f'- decision: {dec.get("kind", "").replace("_", " ")}'
            if dec.get('by'):
                line += f' by {dec["by"]} ({dec.get("how") or "-"}) at {dec.get("at")}'
            if dec.get('override'):
                line += f'; override: {dec["override"]}'
            if dec.get('reason'):
                line += f'; reason: {dec["reason"]}'
            out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('state', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--markdown', type=Path)
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads((a.state / 'state.json').read_text(encoding='utf-8'))
    text = '\n'.join(page(d, a.brief)) + '\n'
    if a.markdown:
        a.markdown.write_text(text, encoding='utf-8', newline='\n')
        print(f'wrote {a.markdown}')
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
