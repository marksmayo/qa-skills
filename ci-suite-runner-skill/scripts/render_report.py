#!/usr/bin/env python3
"""Render one or more suite run records: what was asked, who authorised it, how it went, and the results.

    python render_report.py run/suite_run.json [more.json ...] [--brief]
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def one(d: dict, brief: bool) -> list[str]:
    r, t, o = d['request'], d.get('trigger') or {}, d['outcome']
    au = next((x for x in reversed(d['authorisations']) if x['decision'] == 'sent'), None)
    out = [f'## {r["suite"]} on {r["environment"]}{" (protected)" if r["protected"] else ""}', '',
           f'{r["system"]} {r["pipeline"]}, ref `{r["ref"]}`, tags `{r["tags"]}`.', '']
    if d.get('summary', {}).get('headline'):
        out += [d['summary']['headline'], '']
    for x in d['authorisations']:
        out.append(f'- {x["at"]}: {x["decision"]}' + (f' ({x["kind"]} by {x["by"]}' + (f', grant {x["grant_id"]} until {x["expires"]}' if x.get('grant_id') else '') + ')' if x['decision'] == 'sent' else f': {x["reason"]}'))
    if t.get('sent'):
        out += [f'- Triggered: {t["method"]} {t["url"]} -> {t["status"]}', f'- Run: {d["run"]["id"]} {d["run"]["url"] or ""} ({d["run"]["found_by"]})',
                f'- Outcome: {o["status"]} {o.get("conclusion") or ""}'.rstrip()]
        if not brief:
            out += ['- Polling: ' + ' -> '.join(f'{p["status"]} ({p["at"][11:19]})' for p in d['polling'])]
    res = d.get('results')
    if res:
        if res['status'] == 'collected':
            tt = res['totals']
            out += ['', f'{tt["passed"]} of {tt["tests"]} tests passed, {tt["failed"]} failed, {tt["errors"]} errors, {tt["skipped"]} skipped (JUnit: {", ".join(res["junit"])}).']
            out += [f'- FAILED `{f["id"]}`: {f["message"]}' for f in res['failed_tests']]
        else:
            out += ['', f'Results not collected: {res.get("reason")}']
    if not brief:
        if d.get('artifacts'):
            out += ['', 'Artifacts: ' + ', '.join(f'{x["name"]}' + (f' -> {x["downloaded_to"]}' if x.get('downloaded_to') else ' (not downloaded)') for x in d['artifacts'])]
        if d.get('logs'):
            out += ['Failed-job logs: ' + ', '.join(f'{x["job"]} -> {x["path"]}' for x in d['logs'])]
        if d.get('next_steps'):
            out += ['', 'Next steps:'] + [f'- {x}' for x in d['next_steps']]
        if d.get('limitations'):
            out += ['', 'Limitations:'] + [f'- {x}' for x in d['limitations']]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('records', type=Path, nargs='+')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    out = ['# CI suite runs', '']
    for p in a.records:
        out += one(json.loads(p.read_text(encoding='utf-8')), a.brief) + ['']
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
