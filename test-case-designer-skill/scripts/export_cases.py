#!/usr/bin/env python3
"""Export designed test cases for import: Xray (JSON) and a generic CSV (TestRail, Zephyr, spreadsheets).

    python export_cases.py test_cases.json [--xray xray.json] [--csv cases.csv] [--project SHOP]

Xray: the JSON used to import manual tests in bulk - a list of tests, each with testtype "Manual",
fields (summary, project, labels, priority, description naming the criterion and the technique) and steps
(action, data, result). After import, link each test to its requirement (the description names it), or
let Xray's requirement coverage pick up the "Tests" link if your import adds one.
CSV: one row per step with the case id, title, criterion, technique, type, priority, preconditions, step
number, action, data and expected result; map the columns in the tool's CSV importer.
Nothing is sent anywhere: the files are written for a person to import.
"""
from __future__ import annotations
import argparse, csv, io, json, sys
from pathlib import Path

PRIO_NAME = {'P0': 'Highest', 'P1': 'High', 'P2': 'Medium', 'P3': 'Low'}
CSV_HEAD = ['case_id', 'title', 'criteria', 'technique', 'rows', 'type', 'priority', 'preconditions', 'step', 'action', 'data', 'expected', 'depends_on_questions']


def to_xray(d: dict, project: str | None = None) -> list[dict]:
    key = d['ticket']['key']
    proj = project or key.split('-')[0]
    out = []
    for c in d['cases']:
        desc = (f'Covers {key} {", ".join(c["criterion_ids"])} ({c["technique"].replace("_", " ")}; rows {", ".join(c["row_ids"])}).'
                + (f'\nPreconditions: ' + '; '.join(c['preconditions']) if c['preconditions'] else '')
                + (f'\nDepends on open question(s) {", ".join(c["depends_on_questions"])}.' if c.get('depends_on_questions') else ''))
        out.append({'testtype': 'Manual',
                    'fields': {'summary': f'{c["id"]} {c["title"]}', 'project': {'key': proj}, 'priority': {'name': PRIO_NAME[c['priority']]},
                               'labels': sorted({key, c['technique'], c['type']} | ({'automation-candidate'} if c.get('automation_candidate') else set())),
                               'description': desc},
                    'steps': [{'action': s['action'], 'data': s.get('data') or '', 'result': s.get('expected') or ''} for s in c['steps']]})
    return out


def to_csv_rows(d: dict) -> list[list[str]]:
    rows = [CSV_HEAD]
    for c in d['cases']:
        for i, s in enumerate(c['steps'], 1):
            rows.append([c['id'], c['title'], ' '.join(c['criterion_ids']), c['technique'], ' '.join(c['row_ids']), c['type'], c['priority'],
                         ' | '.join(c['preconditions']), str(i), s['action'], s.get('data') or '', s.get('expected') or '', ' '.join(c.get('depends_on_questions', []))])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cases', type=Path)
    ap.add_argument('--xray', type=Path)
    ap.add_argument('--csv', type=Path)
    ap.add_argument('--project')
    a = ap.parse_args()
    d = json.loads(a.cases.read_text(encoding='utf-8'))
    if a.xray:
        a.xray.write_text(json.dumps(to_xray(d, a.project), indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        print(f'wrote {a.xray} ({len(d["cases"])} tests)')
    if a.csv:
        buf = io.StringIO()
        csv.writer(buf, lineterminator='\n').writerows(to_csv_rows(d))
        a.csv.write_text(buf.getvalue(), encoding='utf-8', newline='')
        print(f'wrote {a.csv} ({sum(len(c["steps"]) for c in d["cases"])} steps)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
