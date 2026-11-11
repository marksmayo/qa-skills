#!/usr/bin/env python3
"""Render a post-deploy report: the recommendation, findings, checks and timings against the previous run.

    python render_report.py pvt_report.json [--brief] [--color auto|always|never]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

COL = {'no_go': '31;1', 'not_established': '33;1', 'go': '32;1', 'fail': '31', 'pass': '32', 'blocked': '33', 'error': '33', 'skipped': '2',
       'regressed': '31', 'ok': '2', 'improved': '32', 'no_baseline': '33', 'bold': '1'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))
    c = lambda t, k: f'\033[{COL[k]}m{t}\033[0m' if use and k in COL else str(t)  # noqa: E731
    d = json.loads(a.report.read_text(encoding='utf-8'))
    r, dep, w = d['recommendation'], d['deployed'], d['window']
    out = [c('# Post-deploy validation', 'bold'), '',
           f'{d["target"]["base_url"]} ({d["target"]["environment"]}), {w["started_at"]} to {w["finished_at"]}; deployed {dep["version"]} '
           f'(expected {dep["expected"]}: {"matches" if dep["matches"] else "does not match" if dep["matches"] is False else "not read"}).', '',
           c(f'Recommendation: {r["value"]}', r['value']) + f' - for {r["decision_owner"]} to decide (decision: {r["decision"] or "not yet made"}).', '',
           d['summary']['headline'], '', 'Reasons: ' + '; '.join(r['reasons']) + '.', '', '## Findings', '']
    out += [f'- **{f["id"]}** ({f["severity"]}, {f["kind"].replace("_", " ")}): {f["title"]}' for f in d['findings']] or ['None.']
    if not a.brief:
        out += ['', '## Checks', '', '| Check | Kind | Critical | Request | Result |', '|---|---|---|---|---|']
        out += [f'| {x["id"]} | {x["kind"]} | {"yes" if x["critical"] else "no"} | {x["method"]} {x["path"]} | {c(x["status"], x["status"])}'
                f'{" " + str(x["http_status"]) if x.get("http_status") else ""}{" - " + x["reason"] if x.get("reason") else ""} |' for x in d['checks']]
        p = d['performance']
        out += ['', f'## Timings against the previous run ({p["samples"]} samples, regression above {p["tolerance_pct"]}% and {p["min_delta_ms"]} ms)', '',
                f'Baseline: {p["baseline_run"] or "none"}', '', '| Check | Median ms | Baseline ms | Change | Status |', '|---|---|---|---|---|']
        out += [f'| {x["check_id"]} | {x["median_ms"]} | {x["baseline_median_ms"] if x["baseline_median_ms"] is not None else "-"} | '
                f'{(str(x["change_pct"]) + "%") if x["change_pct"] is not None else "-"} | {c(x["status"], x["status"])} |' for x in p['comparisons']]
        out += ['', '## Sign-in and safety', '']
        out += [f'- Credential set `{x["name"]}`: {"signed in" if x["signed_in"] else "not signed in (" + (x["reason"] or "") + ")"}' for x in d['credential_sets']]
        out += [f'- {w["requests_sent"]} read-only request(s) sent; refused: ' + (', '.join(f'{b["method"]} {b["path"]}' for b in w['blocked']) or 'none')]
        out += [f'- {n}' for n in r.get('notes', [])]
        out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
