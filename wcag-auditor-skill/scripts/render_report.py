#!/usr/bin/env python3
"""Render a WCAG audit as a terminal/Markdown report with optional ANSI colour.

    python render_report.py audit.json [--color auto|always|never] [--brief]

--brief prints the headline, the ranked findings table and the SC summary without per-finding
detail, needs-review items or the manual test plan.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
SEV_STYLE = {'critical': RED + BOLD, 'serious': RED, 'moderate': YELLOW, 'minor': DIM}
CONF_STYLE = {'high': GREEN, 'medium': CYAN, 'low': DIM}
OUT_STYLE = {'failed': RED, 'passed': GREEN, 'cantTell': YELLOW, 'inapplicable': DIM, 'untested': MAGENTA}
TOOL_STYLE = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED}
VERDICT = {'does_not_conform': 'Does not conform', 'not_established': 'Conformance not established'}


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
    sc, meta, h = d['scope'], d['metadata'], d['headline']
    pages = {p['id']: p for p in meta['sample']}
    out = []
    w = out.append
    w(P(f"# WCAG {sc['wcag_version']} {sc['level']} audit", BOLD))
    w('')
    w(f"Targets: {', '.join(sc['targets'])}  |  generated {d['generated_at']}  |  {d['skill']['name']} {d['skill']['version']}")
    audited = [p for p in meta['sample'] if p['reason'] not in ('unreachable', 'blocked')]
    lost = len(meta['sample']) - len(audited)
    w(f"Browser: {meta['browser']['name']} {meta['browser']['version']}  |  viewports {', '.join(meta['viewports'])}  |  "
      f"{len(audited)} page(s) audited" + (f" ({lost} more unreachable or blocked, not audited)" if lost else '') +
      f", {meta['unsampled_count']} not sampled  |  authorised by: {sc['authorised_by']}")
    w('')
    verdict_style = RED + BOLD if h['conformance_verdict'] == 'does_not_conform' else YELLOW + BOLD
    w(P(f"Verdict: {VERDICT[h['conformance_verdict']]}", verdict_style))
    w(h['verdict_text'])
    w('')
    sv = h['severity_counts']
    w('Findings: ' + '  '.join(P(f"{sv[k]} {k}", SEV_STYLE[k]) for k in ('critical', 'serious', 'moderate', 'minor')))
    cv = h['sc_coverage']
    w(f"Success criteria ({cv['total']}): " + '  '.join(P(f"{cv[k]} {k}", OUT_STYLE[k]) for k in ('failed', 'cantTell', 'untested', 'inapplicable', 'passed')))
    w(f"Evidence completeness {h['evidence_completeness']:.0%}  |  overall confidence {P(h['overall_confidence'], CONF_STYLE[h['overall_confidence']])}")
    if h['blocked_critical_paths']:
        w(P(f"Blocked critical paths: {', '.join(h['blocked_critical_paths'])}", RED + BOLD))
    w(P('Severity is this skill\'s published rubric layered on WCAG (RUBRIC.md); WCAG itself defines levels, not severity.', DIM))
    w('')
    w(P('## Tools', BOLD))
    for t in meta['tools']:
        w(f"- {t['name']} {t['version']} ({t['license']}): {P(t['status'], TOOL_STYLE[t['status']])}" + (f" - {t['reason']}" if t['reason'] else ''))
    cut = {}
    for c in meta['checks']:
        if c['status'] == 'partial':
            cut.setdefault(c['name'], []).append(f"{c['page']} {c['viewport']}")
    for name, where in sorted(cut.items()):
        w(P(f"- {name} probe partial (stopped before a full pass) on {len(where)} check(s): {', '.join(where[:6])}"
            + (' ...' if len(where) > 6 else ''), YELLOW))
    w('')
    w(P('## Findings', BOLD))
    if not d['findings']:
        w('No failures were found by automated and assisted testing. This does not establish conformance; see the manual test plan.')
    else:
        w('| # | Priority | Severity | Confidence | SC | Finding | Pages |')
        w('|---|---|---|---|---|---|---|')
        for f in d['findings']:
            tag = ' (agent judgment)' if f['evidence_type'] == 'agent_judgment' else ''
            cp = ' **[critical path]**' if f['critical_path'] else ''
            w(f"| {f['id']} | {P(f['priority']['band'], PRIO_STYLE[f['priority']['band']])} | {P(f['severity'], SEV_STYLE[f['severity']])} | "
              f"{P(f['confidence'], CONF_STYLE[f['confidence']])} | {', '.join(f['sc'])} | {f['title']}{tag}{cp} | {len(f['pages'])} |")
    if not args.brief:
        for f in d['findings']:
            w('')
            w(P(f"### {f['id']} {f['title']}", BOLD))
            w(f"{f['priority']['band']} (score {f['priority']['score']} = severity x reach {f['priority']['reach']} x leverage {f['priority']['leverage']})  |  "
              f"{f['severity']} (base {f['base_severity']}, pattern {f['pattern']})  |  {f['confidence']} confidence, {f['evidence_type']}  |  WCAG {', '.join(f['sc'])} ({f['level']})"
              + (f"  |  EN 301 549 {', '.join(f['en301549'])}" if f.get('en301549') else ''))
            if f['non_interference']:
                w(P('Non-interference SC: a failure blocks use of the whole page (conformance requirement 5).', RED))
            if f['critical_path']:
                w(f"Critical path: {f['critical_path']['name']}, step \"{f['critical_path']['step']}\"" + (P(' - blocks the path', RED + BOLD) if f['blocks_critical_path'] else ''))
            w(f"Component: `{f['component']}` on {', '.join(pages[p]['url'] for p in f['pages'] if p in pages)}")
            w(f"Who is affected: {', '.join(f['affected_users'])}")
            w(f"Impact: {f['user_impact']}")
            w(f"Severity: {f['severity_rationale']}")
            if f.get('resolution'):
                w(f"How it was judged: {f['resolution']}")
            srcs = ', '.join(f"{s['tool']} {s['version'] or ''} {s['rule_id']}".replace('  ', ' ') + (f" (ACT {', '.join(s['act_rules'])})" if s['act_rules'] else '') for s in f['sources'])
            if srcs:
                w(f"Sources: {srcs}")
            for i in f['instances'][:5]:
                w(f"- {i['url']} @ {i['viewport']} [{i['state']}]: `{i['selector'] or i['xpath']}` {('- ' + i['html_snippet']) if i['html_snippet'] else ''}" +
                  (f" (screenshot {i['screenshot']})" if i['screenshot'] else ''))
            if len(f['instances']) > 5:
                w(f"- ... and {len(f['instances']) - 5} more instance(s)")
            w(f"Fix: {f['remediation']}")
            w(f"Verify: {f['verification_step']}")
    w('')
    w(P('## Success criteria', BOLD))
    by = {}
    for r in d['sc_results']:
        by.setdefault(r['outcome'], []).append(r['sc'])
    for k in ('failed', 'cantTell', 'passed', 'inapplicable', 'untested'):
        if by.get(k):
            w(f"- {P(k, OUT_STYLE[k])} ({len(by[k])}): {', '.join(by[k])}")
    if not args.brief:
        if d['needs_review']:
            w('')
            w(P('## Needs review (cantTell)', BOLD))
            for n in d['needs_review']:
                w(f"- {n['id']} [{', '.join(n['sc'])}] {n['what_to_check']} (pages {', '.join(n['pages'])})")
        if d['advisories']:
            w('')
            w(P('## Advisories (not WCAG failures at the target level)', BOLD))
            for a in d['advisories']:
                w(f"- {a['id']} [{a['kind']}{(' ' + ', '.join(a['sc'])) if a['sc'] else ''}] {a['title']}: {a['note']}")
        if d['manual_test_plan']:
            w('')
            w(P('## Manual test plan', BOLD))
            for m in d['manual_test_plan']:
                w(f"- {P(m['sc'], MAGENTA)} {m['title']}: {m['why_untested']}")
                for s in m['steps']:
                    w(f"    - {s}")
        dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
        w('')
        w(P('## Triage', BOLD))
        counts = {}
        for t in d['triage']:
            counts[t['decision']] = counts.get(t['decision'], 0) + 1
        w(f"{len(d['triage'])} candidate(s): " + ', '.join(f"{v} {k}" for k, v in sorted(counts.items())))
        for t in dismissed:
            w(f"- {t['candidate_id']} dismissed: {t['reason']}")
    w('')
    w(P('## Limitations', BOLD))
    for l in d['limitations']:
        w(f"- {l}")
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
