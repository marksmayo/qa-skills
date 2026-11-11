#!/usr/bin/env python3
"""Render a traceability report: status per requirement, the gaps, orphan tests, and the matrix.

    python render_report.py trace.json [--brief] [--csv matrix.csv]

--csv writes one row per requirement criterion and linked test (requirement, criterion, test, strength,
evidence kinds, result, fresh, status) for a spreadsheet.
"""
from __future__ import annotations
import argparse, csv, io, json, sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('trace', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--csv', type=Path)
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads(a.trace.read_text(encoding='utf-8'))
    if a.csv:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\n')
        w.writerow(['requirement', 'status', 'criterion', 'test_id', 'strength', 'evidence', 'result', 'fresh'])
        for r in d['requirements']:
            tests = {t['test_id']: t for t in r['tests']}
            for c in r['criteria']:
                ids = c['tests_explicit'] + c['tests_inferred']
                for tid in ids or ['']:
                    t = tests.get(tid, {})
                    w.writerow([r['id'], r['status'], c['id'], tid, t.get('strength', ''), ' '.join(sorted({x['kind'] for x in t.get('evidence', [])})),
                                t.get('result', ''), t.get('fresh', '')])
        a.csv.write_text(buf.getvalue(), encoding='utf-8', newline='')
        print(f'wrote {a.csv}')
        return 0
    s = d['summary']
    out = ['# Requirements traceability', '', s.get('headline', ''), '',
           f'{s["requirements"]} requirements: ' + ', '.join(f'{k.replace("_", " ")} {v}' for k, v in s['by_status'].items() if v)
           + f'. {s["criteria_with_explicit_tests"]} of {s["criteria_total"]} criteria have an explicit test; {s["tests_linked"]} tests linked; {s["orphans"]} orphan test(s).', '',
           '| Requirement | Title | Status | Criteria with explicit tests | Tests (result) | Gaps |', '|---|---|---|---|---|---|']
    for r in d['requirements']:
        ce = sum(1 for c in r['criteria'] if c['tests_explicit'])
        tests = ', '.join(f'{t["test_id"].split("::")[-1]} ({t["result"]}{", inferred" if t["strength"] == "inferred" else ""})' for t in r['tests']) or '-'
        out.append(f'| {r["id"]} | {r["title"]} | {r["status"].replace("_", " ")} | {ce}/{len(r["criteria"])} | {tests} | {"; ".join(r["gaps"]) or "-"} |')
    if not a.brief:
        for r in d['requirements']:
            out += ['', f'## {r["id"]} {r["title"]} ({r["status"].replace("_", " ")})', '']
            for c in r['criteria']:
                out.append(f'- **{c["id"]}** {c["text"]}: ' + (', '.join(c['tests_explicit']) or 'no explicit test')
                           + (f'; inferred {", ".join(c["tests_inferred"])}' if c['tests_inferred'] else ''))
            for t in r['tests']:
                out.append(f'  - `{t["test_id"]}` {t["strength"]}, {t["result"]}{"" if t["fresh"] else " (stale)"}: '
                           + '; '.join(f'{x["kind"]} {x["ref"]}' for x in t['evidence']))
            if r['implementation']:
                out.append('  - Implementation: ' + '; '.join(f'{x["kind"]} {x["ref"][:12] if x["kind"] == "commit" else x["ref"]}' for x in r['implementation']))
            if r.get('attachments'):
                out.append('  - Attachments (listed, not read): ' + ', '.join(r['attachments']))
        out += ['', '## Orphan tests', ''] + [f'- `{o["test_id"]}` ({o["result"]})' + (f': names {", ".join(o["names_out_of_scope"])}, outside this set' if o['names_out_of_scope'] else '')
                                              for o in d['orphan_tests']] or ['None.']
        dl = d['triage'].get('dropped_links', [])
        if dl:
            out += ['', '## Inferred links dropped', ''] + [f'- {x["requirement"]} -> `{x["test_id"]}`: {x["reason"]}' for x in dl]
        out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
