#!/usr/bin/env python3
"""Render a migration review as a terminal or Markdown report.

    python render_report.py review.json [--brief] [--color auto|always|never]

--brief prints the headline, the findings table and the safe sequence only. Findings an agent found by
reading the code (source "agent", with a basis) rather than by a rule match are labelled "found by review".
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

COL = {'P0': '31;1', 'P1': '31', 'P2': '33', 'P3': '36', 'dim': '2', 'bold': '1', 'green': '32'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('review', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))

    def c(text, key):
        return f'\033[{COL[key]}m{text}\033[0m' if use else str(text)

    r = json.loads(a.review.read_text(encoding='utf-8'))
    s, h = r['scope'], r['headline']
    eng = s['engine']
    out = [c('# Migration review', 'bold'), '',
           f'Repository `{s["repo"]}` ({s["tool"]}), {eng["name"]} {eng.get("version") or "?"}'
           f'{" (assumed: " + eng["assumed_reason"] + ")" if eng.get("assumed") and eng.get("assumed_reason") else " (assumed)" if eng.get("assumed") else ""}, deploy model **{s["deploy_model"]}**, '
           f'migrations {", ".join(s["migrations_reviewed"])}.', '',
           c(f'Verdict: {h["verdict"].replace("_", " ")}', 'P0' if h['verdict'] == 'do_not_deploy' else 'green'),
           ' '.join(f'{c(p, p)} {n}' for p, n in h['counts'].items()), '', h['statement'], '',
           c('Scope: ', 'bold') + h['scope_statement'], '']
    if s.get('components'):
        out += ['Components deployed separately: ' + '; '.join(f'{x["name"]} ({x["deploy_model"]}{", " + x["order"] if x.get("order") else ""})'
                                                              for x in s['components']), '']
    if s.get('rollback_path'):
        rb = s['rollback_path']
        out += [f'Rollback path: {rb["method"].replace("_", " ")} ({rb["source"]}){". " + rb["notes"] if rb.get("notes") else ""}', '']
    out += ['## Findings', '', '| ID | Priority | Migration | Rule | Table | Score | Confidence |', '|---|---|---|---|---|---|---|']
    for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
        rule = f["rule"] + (' (found by review)' if f.get('source') == 'agent' else '')
        out.append(f'| {f["id"]} | {c(f["priority"], f["priority"])} | {f["migration"] or "-"} | {rule} | {f["table"] or "-"} '
                   f'| {f["hazard"]}x{f["size_factor"]}x{f["exposure"]}={f["score"]} | {f["confidence"]} |')
    out.append('')
    if not a.brief:
        for f in sorted(r['findings'], key=lambda f: (f['priority'], f['id'])):
            out += [f'### {f["id"]} {c(f["priority"], f["priority"])} {f["rule"]} ({f["category"].replace("_", " ")})'
                    + (' - found by review' if f.get('source') == 'agent' else ''), '',
                    f'`{f["file"]}:{f["line"]}`{" (down migration)" if f.get("direction") == "down" else ""}: `{f["statement"]}`', '',
                    f'- Effect: {f["effect"]}',
                    f'- Engine: {f["engine_range"]}',
                    f'- Table size: {f["size"]["class"]}{" (" + format(f["size"]["rows"], ",") + " rows)" if f["size"].get("rows") else ""}, from {f["size"]["source"]}{" (a hint, not statistics)" if f["size"].get("hinted") else ""}']
            if f.get('floor') or f.get('cap'):
                out.append(f'- Priority adjusted: {f.get("floor") or f.get("cap")}')
            if f.get('source') == 'agent':
                out.append('- Found by review: ' + '; '.join(f'`{b["file"]}:{b["line"]}` {b["excerpt"]}' if b.get('file')
                                                            else f'evidence `{b["evidence"]}`' for b in f.get('basis', [])))
            if f['code_hits']:
                out.append('- Code still using it: ' + ', '.join(f'`{(x["rev"] + ":") if x.get("rev") else ""}{x["file"]}:{x["line"]}` ({x["kind"]})'
                                                                 for x in f['code_hits']))
            out += [f'- Safer rewrite: {f["rewrite"]}', f'- Verify: {f["verification"]}', '']
    if r.get('safe_sequence'):
        out += ['## Safer sequence', '']
        for rel in r['safe_sequence']:
            label = rel['release']
            # a short label ("A", "2.4") reads as "Release A"; a phrase ("Before any production run") stands alone
            out.append(f'**Release {label}**' if re.match(r'^[A-Z0-9][\w.\-]{0,9}(\s|$)', label) and not re.match(r'^[A-Z][a-z]{3,}\b', label)
                       else f'**{label}**')
            out += [f'{n}. {x}' for n, x in enumerate(rel['steps'], 1)] + ['']
    if not a.brief:
        out += ['## Triage', '']
        for t in r['triage']:
            out.append(f'- {t["candidate"]}: ' + (f'promoted to {t["finding"]}' if t['decision'] == 'promoted'
                                                  else c(f'dismissed ({t["reason_code"]})', 'dim') + f': {t["reason"]}'))
        d = r['dynamic']
        out += ['', '## Dynamic check', '', f'Status: {d["status"]}' + (f'. {d["reason"]}' if d.get('reason') else ''), '']
        for m in d.get('migrations', []):
            out.append(f'- {m["id"]}: up {m["up"].get("status")} in {m["up"].get("ms")} ms; locks {m.get("locks")}; '
                       f'blocked {[p["query"] + " on " + p["table"] for p in m.get("probes", []) if p.get("blocked")]}; down {m["down"].get("status")}')
        out += ['## Checked', ''] + [f'- {x}' for x in s['checked']] + ['', '## Not checked', ''] + [f'- {x}' for x in s['not_checked']]
        out += ['', '## Limitations', ''] + [f'- {x}' for x in r['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
