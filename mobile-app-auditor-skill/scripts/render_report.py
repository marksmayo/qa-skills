#!/usr/bin/env python3
"""Render a mobile audit as a terminal or Markdown report.

    python render_report.py audit.json [--brief] [--color auto|always|never]

--brief prints the headline, the findings and crash tables only.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

COL = {'P0': '31;1', 'P1': '31', 'P2': '33', 'P3': '36', 'dim': '2', 'bold': '1'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('audit', type=Path)
    ap.add_argument('--brief', action='store_true')
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    use = a.color == 'always' or (a.color == 'auto' and sys.stdout.isatty() and not os.environ.get('NO_COLOR'))

    def c(t, k):
        return f'\033[{COL[k]}m{t}\033[0m' if use else str(t)

    d = json.loads(a.audit.read_text(encoding='utf-8'))
    s, h = d['scope'], d['headline']
    dev = s['devices'][0] if s['devices'] else {}
    cov = h['sc_coverage']
    out = [c('# Mobile app audit', 'bold'), '',
           f'App `{s["app"]["package"]}` {s["app"].get("version") or ""} on {dev.get("model", "?")} (Android {dev.get("android", "?")}, API {dev.get("api", "?")}); '
           f'WCAG {s["wcag_version"]} {s["level"]}; platforms: ' + ', '.join(f'{k} {v}' for k, v in s['platforms'].items()) + '.', '',
           c(h['verdict_text'], 'bold'), '',
           f'Severity: ' + ', '.join(f'{k} {v}' for k, v in h['severity_counts'].items()) + '. Crashes: ' + ', '.join(f'{c(k, k)} {v}' for k, v in h['crash_counts'].items()) + '.',
           f'SCs: {cov["total"]} - failed {cov["failed"]}, passed {cov["passed"]}, cannot tell {cov["cantTell"]}, inapplicable {cov["inapplicable"]}, untested {cov["untested"]}. '
           f'Evidence completeness {h["evidence_completeness"]}, overall confidence {h["overall_confidence"]}.', '']
    if d['metadata'].get('run_status', 'complete') != 'complete':
        out += [c(f'PARTIAL RUN: {d["metadata"].get("run_status_reason") or "not every sampled screen was audited"}', 'P1'), '']
    if h['blocked_critical_paths']:
        out += [f'Blocked critical paths: {", ".join(h["blocked_critical_paths"])}', '']
    out += ['## Accessibility findings', '', '| ID | Priority | SC | Severity | Component | Screens | Confidence |', '|---|---|---|---|---|---|---|']
    for f in sorted(d['findings'], key=lambda f: (f['priority']['band'], -f['priority']['score'])):
        out.append(f'| {f["id"]} | {c(f["priority"]["band"], f["priority"]["band"])} | {f["sc"]} | {f["severity"]} | {f["component"]} | {", ".join(f["screens"])} | {f["confidence"]} |')
    out += ['', '## Crashes and ANRs', '', '| ID | Priority | Kind | Tier | Score | Confidence | What |', '|---|---|---|---|---|---|---|']
    for k in d['crashes']:
        out.append(f'| {k["id"]} | {c(k["priority"]["band"], k["priority"]["band"])} | {k["kind"]} | {k["tier"]} | {k["priority"]["score"]} | {k["confidence"]} | {k["title"]} |')
    out.append('')
    if not a.brief:
        for f in sorted(d['findings'], key=lambda f: (f['priority']['band'], -f['priority']['score'])):
            out += [f'### {f["id"]} {c(f["priority"]["band"], f["priority"]["band"])} {f["title"]}', '',
                    f'SC {f["sc"]} ({f["level"]}), {f["pattern"]}; severity {f["severity"]} ({f["severity_rationale"]}); EN 301 549 {f.get("en301549") or "-"}.', '']
            for i in f['instances']:
                el = i['element']
                out.append(f'- {i["screen"]}: `{el.get("resource_id") or el["class"]}` "{el.get("label", "")}" {el.get("size_dp")} dp - `{i["screenshot"]}` {i.get("measured") or ""}')
            out += ['', f'Impact: {f["user_impact"]}', f'Fix: {f["remediation"]}', f'Verify: {f["verification_step"]}', '']
        for k in d['crashes']:
            out += [f'### {k["id"]} {c(k["priority"]["band"], k["priority"]["band"])} {k["title"]}', '',
                    f'{k["kind"]}, {k["tier"]}; reach {k["reach"]}, likelihood {k["likelihood"]}, recoverability {k["recoverability"]}; replay: {k["replay"]}.', '',
                    'Steps: ' + ' -> '.join(k['steps']), '', '```'] + k['log_excerpt'][:8] + ['```', f'Screenshot: `{k["screenshot"]}`', f'Fix: {k["fix"]}', f'Verify: {k["verification"]}', '']
        out += ['## Success criteria', '', '| SC | Outcome | Confidence | EN 301 549 | Note |', '|---|---|---|---|---|']
        for r in d['sc_results']:
            out.append(f'| {r["sc"]} {r["title"]} | {r["outcome"]} | {r["confidence"]} | {r.get("en301549_clause") or "-"} | {r["evidence_note"][:90]} |')
        out += ['', '## Needs review', ''] + [f'- {n["id"]} ({n["sc"]}): {n["what_to_check"]}' for n in d['needs_review']]
        out += ['', '## Advisories', ''] + [f'- {x["id"]}: {x["title"]}' for x in d['advisories']]
        out += ['', '## Triage', ''] + [f'- {t["candidate_id"]}: {t["decision"]}{" " + t["ref"] if t.get("ref") else ""}' +
                                        (c(f' - {t["reason"]}', 'dim') if t.get('reason') else '') for t in d['triage']]
        out += ['', '## Device settings changed and restored', ''] + [f'- {x.get("ns", "")} {x.get("key", x["kind"])}: {x.get("before")} (restored: {x["restored"]})' for x in d['metadata']['settings_changed']]
        out += ['', '## Manual test plan', ''] + [f'- {m["sc"]} {m["title"]}: {m["why_untested"]}' for m in d['manual_test_plan']]
        out += ['', '## Limitations', ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
