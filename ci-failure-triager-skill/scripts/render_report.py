#!/usr/bin/env python3
"""Render a CI failure triage as a terminal or Markdown report, and write the drafted defects.

    python render_report.py triage.json [--brief] [--defects-md drafts.md] [--color auto|always|never]

--brief prints the headline and the failure table. --defects-md writes each drafted defect and
test-data issue as Markdown, ready to paste into a tracker by a person (the skill never files them).
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

COL = {'real': '31;1', 'data': '35', 'flaky': '33', 'environment': '36', 'unclassified': '2', 'bold': '1', 'dim': '2'}


def draft_md(f: dict) -> list[str]:
    dd, b = f['draft_defect'], f['draft_defect']['body']
    out = [f'## {dd["title"]}', '', f'*{dd["kind"].replace("_", " ")}, drafted from CI failure {f["id"]}; not filed.*', '', b['summary'], '',
           '**Steps to reproduce**', ''] + [f'{i}. {s}' for i, s in enumerate(b['steps'], 1)] + ['',
           f'**Expected:** {b["expected"]}', '', f'**Actual:** {b["actual"]}', '']
    if b.get('suspect_commit'):
        out += [f'**Suspect commit:** `{b["suspect_commit"]["commit"][:12]}` {b["suspect_commit"]["subject"]} '
                f'(shares {", ".join(x["file"] for x in b["suspect_commit"]["files_shared"])})', '']
    out += ['**Evidence**', ''] + [f'- {x}' for x in b['evidence']] + ['']
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('triage', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--defects-md', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads(a.triage.read_text(encoding='utf-8'))
    if a.defects_md:
        lines = ['# Drafted defects', '', f'From {d["run"]["system"]} run {d["run"]["id"]} on `{d["run"]["commit"][:12]}` ({d["run"]["branch"]}).', '']
        for f in d['failures']:
            if f.get('draft_defect'):
                lines += draft_md(f)
        a.defects_md.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
        print(f'wrote {a.defects_md}')
        return 0
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))
    c = lambda t, k: f'\033[{COL[k]}m{t}\033[0m' if use and k in COL else str(t)  # noqa: E731
    r, s = d['run'], d['summary']
    out = [c('# CI failure triage', 'bold'), '',
           f'{r["system"]} run {r["id"]} (attempt {r["attempt"]}) on {r["branch"]} @ `{r["commit"][:12]}`; last green run '
           f'{(r.get("last_green") or {}).get("id", "none")}. {r.get("url") or ""}', '', s['headline'], '',
           'By classification: ' + ', '.join(f'{c(k, k)} {v}' for k, v in s['by_classification'].items() if v)
           + f'. Drafts: {s["defects_drafted"]}. Re-runs: {s["reruns"]}. Evidence completeness {s["evidence_completeness"]}.', '',
           '| ID | Job | Failure | Class | Confidence | Owning change | Next action |', '|---|---|---|---|---|---|---|']
    for f in d['failures']:
        oc = f.get('owning_change') or {}
        own = f'`{oc["commit"][:8]}` {oc["subject"]}' if oc.get('status') == 'identified' else 'not identified'
        out.append(f'| {f["id"]} | {f["job"]} | {f.get("test_id") or f.get("step")} | {c(f["classification"], f["classification"])} | {f["confidence"]} | {own} | {f["next_action"]} |')
    if not a.brief:
        for f in d['failures']:
            out += ['', f'### {f["id"]} {c(f["classification"], f["classification"])}: {f.get("test_id") or f.get("step")}', '',
                    f'Rule: {f["rule"]}.' + (f' Override: {f["override_reason"]}' if f.get('override_reason') else '') +
                    f' Re-run: {f["signals"]["rerun"]}. Signatures: {", ".join(x["id"] for x in f["signals"]["signatures"]) or "none"}.'
                    + (f' Dates before the run date: {", ".join(f["signals"]["date_markers"])}.' if f['signals'].get('date_markers') else '')
                    + (f' Priority {f["priority"]}.' if f.get('priority') else ''), '',
                    f'`{f["log_excerpt"]["path"]}`:', '```'] + [f'{l["n"]:>4}  {l["text"]}' for l in f['log_excerpt']['lines']] + ['```']
            if f.get('draft_defect'):
                out += ['', f'Draft {f["draft_defect"]["kind"].replace("_", " ")}: **{f["draft_defect"]["title"]}** (in the result log; not filed)']
        out += ['', '## Re-runs', ''] + [f'- attempt {x["new_attempt"]} re-ran {", ".join(x["jobs_rerun"])} ({x["mode"]}), authorised by {x["authorised_by"]}: '
                                         + ', '.join(f'{k} {v}' for k, v in x['outcomes'].items()) for x in d['reruns']] or ['None.']
        out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
