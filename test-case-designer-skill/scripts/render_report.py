#!/usr/bin/env python3
"""Render designed test cases for review: coverage per criterion, questions, and every case with its steps.

    python render_report.py test_cases.json [--brief] [--markdown cases.md]

--brief prints the coverage table and the questions. --markdown writes the full page to a file.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def page(d: dict, brief: bool) -> list[str]:
    s, t = d['summary'], d['ticket']
    out = [f'# Test cases for {t["key"]}: {t["summary"]}', '',
           f'{s["cases"]} cases for {s["criteria_with_cases"]} of {s["criteria"]} criteria; design rows {s["rows_covered"]} of {s["rows_total"]} covered'
           f'{", " + str(s["rows_infeasible"]) + " infeasible" if s["rows_infeasible"] else ""}; {s["questions"]} open question(s). '
           'By priority: ' + ', '.join(f'{k} {v}' for k, v in s['by_priority'].items() if v) + '.', '',
           '## Coverage', '', '| Criterion | Techniques | Cases | Status |', '|---|---|---|---|']
    for c in d['criteria']:
        out.append(f'| {c["id"]} | {", ".join(x["technique"].replace("_", " ") for x in c["techniques"])} | {len(c["case_ids"])} | {c["status"].replace("_", " ")} |')
    out += ['', '## Open questions', ''] + [f'- **{q["id"]}** ({q["criterion_id"]}, "{q["term"]}"): {q["question"]}'
                                             + (f' Blocks {", ".join(q["blocks_cases"])}.' if q.get('blocks_cases') else '') for q in d['questions']] or ['None.']
    if brief:
        return out
    for c in d['criteria']:
        out += ['', f'## {c["id"]}', '', f'> {c["text"]}', '']
        out += [f'- **{x["technique"].replace("_", " ")}**: {x["reason"]}' for x in c['techniques']]
        cases = [k for k in d['cases'] if c['id'] in k['criterion_ids']]
        for k in cases:
            out += ['', f'### {k["id"]} {k["title"]}', '',
                    f'{k["type"]}, {k["priority"]}; rows {", ".join(k["row_ids"])}'
                    + (f'; depends on {", ".join(k["depends_on_questions"])}' if k.get('depends_on_questions') else '')
                    + ('; automation candidate' if k.get('automation_candidate') else ''), '']
            if k['preconditions']:
                out += ['Preconditions: ' + '; '.join(k['preconditions']), '']
            out += ['| # | Action | Data | Expected |', '|---|---|---|---|']
            out += [f'| {i} | {s["action"]} | {s.get("data") or ""} | {s.get("expected") or ""} |' for i, s in enumerate(k['steps'], 1)]
    out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cases', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--markdown', type=Path)
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    d = json.loads(a.cases.read_text(encoding='utf-8'))
    text = '\n'.join(page(d, a.brief)) + '\n'
    if a.markdown:
        a.markdown.write_text(text, encoding='utf-8', newline='\n')
        print(f'wrote {a.markdown}')
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
