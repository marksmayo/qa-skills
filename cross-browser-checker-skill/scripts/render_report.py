#!/usr/bin/env python3
"""Render a cross-browser report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py report.json [--color auto|always|never] [--brief]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
SEV = {'Critical': RED + BOLD, 'High': RED, 'Medium': YELLOW, 'Low': CYAN}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def third_party_line(tp: dict | None) -> str:
    if tp is None:
        return 'Third-party requests: not recorded by this capture.'
    if tp['mode'] == 'allow':
        return 'Third-party requests: allowed and compared.'
    if not tp['blocked']:
        return 'Third-party requests: blocked by the harness; none were attempted.'
    n = sum(b['requests'] for b in tp['blocked'])
    return (f'Third-party requests: {n} blocked (' + ', '.join(f'{b["host"]} x{b["requests"]}' for b in tp['blocked'][:6]) +
            (', ...' if len(tp['blocked']) > 6 else '') + '); their behaviour was not compared.')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    P = (lambda s, st: f'{st}{s}{RESET}') if supports_color(a.color) else (lambda s, st: s)
    sc, hl = d['scope'], d['headline']
    verdict = {'differences_found': P('DIFFERENCES FOUND', RED + BOLD),
               'inconclusive': P('INCONCLUSIVE', YELLOW + BOLD) + ' (engine comparisons were not all made; see limitations)'}.get(
        hl['verdict'], P('NO DIFFERENCES DETECTED', GREEN + BOLD) + ' (for these pages, states and engines only)')
    if d.get('run_status') == 'not_run':
        verdict = P('INCONCLUSIVE', YELLOW + BOLD) + ' (static only: no browser engine ran; feature findings stay possible)'
    elif d.get('run_status') == 'partial' and d.get('run_status_reason'):
        verdict += f' Capture partial: {d["run_status_reason"]}'
    engines = ', '.join(f'{e["name"]} {e["version"] or ""} ({e["status"]})'.replace(' ()', '') for e in sc['engines'])
    ck = hl['checks']
    out = [P('# Cross-browser check', BOLD), '', verdict, '', hl['summary'], '',
           f'Engines: {engines}; reference {sc["reference_engine"]}; {sc["reruns"]} runs each. Checks: {ck["ran"]} of '
           f'{ck["planned"]} ran; {ck["comparisons_made"]} of {ck["comparisons_planned"]} engine comparisons made. '
           f'Flaky: {hl["flaky_count"]}. Evidence {hl["evidence_completeness"]}, confidence {hl["overall_confidence"]}.',
           f'Targets: {sc["browserslist"]["query"]} ({sc["browserslist"]["resolved_count"]} versions, {sc["browserslist"]["coverage"]}% '
           f'of global usage).', third_party_line(sc.get('third_party'))]
    if sc.get('capture'):
        c = sc['capture']
        out.append(f'Capture: page CSP {"bypassed" if c.get("bypass_csp") else "enforced"}; reduced motion {c.get("reduced_motion")}; '
                   f'animations {"frozen" if c.get("freeze_animations") else "running"}; {c.get("rate_limit_seconds")} s between loads.')
    if sc.get('requests'):
        r = sc['requests']
        out.append(f'Requests sent: {r["sent"]} (' + ', '.join(f'{k} {v}' for k, v in r['by_engine'].items()) + ')'
                   + (f'; budget {r["budget"]}' + (' reached' if r['budget_reached'] else '') if r['budget'] is not None else '')
                   + (f'; {r["backoffs"]} back-off(s) on 429/503' if r['backoffs'] else '') + '.')
    out.append(P('Out of scope: ' + '; '.join(sc['out_of_scope']), DIM))
    if a.brief:
        out.append('')
        out += [f'{P(x["id"], BOLD)} {P(x["priority"] + " " + x["severity"], SEV[x["severity"]])} {", ".join(x["engines_affected"])}: '
                f'{x["title"]}' for x in d['differences']]
        # needs review and limitations are often the only actionable part: never left out of the brief
        if d.get('needs_review'):
            out += ['', P(f'Needs review ({len(d["needs_review"])}):', BOLD)] + \
                   [f'- {n["candidate_id"]}: {n["reason"]}' for n in d['needs_review']]
        if d.get('limitations'):
            out += ['', P('Limitations:', BOLD)] + [f'- {x}' for x in d['limitations']]
        print('\n'.join(out))
        return 0
    if d['differences']:
        out += ['', P('## Differences', BOLD)]
    for x in d['differences']:
        out += ['', P(f'### {x["id"]} {x["priority"]} {x["severity"]}: {x["title"]}', SEV[x['severity']]), '',
                f'{x["kind"]}/{x["subkind"]} on {x["page"]} ({", ".join(x["states"])}) in {", ".join(x["engines_affected"])}, not in '
                f'{x["reference"]}; confidence {x["confidence"]}; {x["usage_share"]}% of global usage on the affected engines.',
                '- Runs: ' + '; '.join(f'{k}: {"".join("x" if v else "." for v in vs)}' for k, vs in x['runs'].items())]
        for e in x['evidence']:
            msg = f' {e["messages"][0][:120]}' if e['messages'] else ''
            out.append(f'- {e["engine"]}: {", ".join(e["screenshots"][:1])}{msg}')
        if x['cause']:
            out.append(f'- Linked features: {", ".join(x["cause"]["features"])}')
        out += [f'- Fix: {x["remediation"]}', f'- Verify: {x["verification_step"]}']
    if d['feature_findings']:
        out += ['', P('## Features the targets do not all support', BOLD), '']
        for f in d['feature_findings']:
            browsers = sorted({u['browser'] for u in f['unsupported_in']})
            where = ', '.join(f'{u["file"]}:{u["line"]}' + (f':{u["column"]}' if u.get('column') else '')
                              + (f' (x{u["occurrences"]})' if (u.get('occurrences') or 1) > 1 else '')
                              + (' (CSS-in-JS)' if u.get('via') == 'css-in-js' else '') for u in f['files'])
            out.append(f'- {f["name"]} ({f["status"].replace("_", " ")}): unsupported in {", ".join(browsers)} '
                       f'({f.get("unsupported_usage", 0)}% of usage); {where}; fallback {"found" if f["fallback"] else "none"}'
                       + (f'; linked {", ".join(f["linked_differences"])}' if f['linked_differences'] else ''))
    if d['flaky']:
        out += ['', P('## Flaky (not reported as differences)', BOLD), '']
        out += [f'- {f["page"]} {f["subkind"]} {f["key"]}: ' + '; '.join(f'{k} {sum(v)}/{len(v)}' for k, v in f['runs'].items()) for f in d['flaky']]
    if d['needs_review']:
        out += ['', P('## Needs review', BOLD), ''] + [f'- {n["candidate_id"]}: {n["reason"]}' for n in d['needs_review']]
    out += ['', f'{d["dismissed_automatically"]} differences dismissed automatically (font metrics, native controls, consequences, '
                f'cumulative drift).']
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
