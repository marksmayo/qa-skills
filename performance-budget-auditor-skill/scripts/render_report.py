#!/usr/bin/env python3
"""Render a performance budget report as a terminal/Markdown report with optional ANSI colour.

    python render_report.py perf_report.json [--color auto|always|never] [--brief]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
from urllib.parse import urlparse

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
SEV = {'Critical': RED + BOLD, 'High': RED, 'Medium': YELLOW, 'Low': CYAN}
STATUS = {'over': RED, 'within': GREEN, 'unstable': YELLOW, 'measured': '', 'untested': DIM, 'not_supported': DIM}
COLS = ['LCP', 'CLS', 'INP', 'TBT', 'FCP', 'TTFB', 'transfer_total', 'transfer_script']
HEAD = {'transfer_total': 'total', 'transfer_script': 'JS'}


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def fmt(v, unit: str) -> str:
    if v is None:
        return '-'
    if unit == 'bytes':
        return f'{v / 1024:.0f} KB' if v < 1024 * 1024 else f'{v / 1024 / 1024:.2f} MB'
    if unit == 'score':
        return f'{v:.3f}'
    return f'{v / 1000:.2f} s' if v >= 1000 else f'{v:.0f} ms'


def cell(m: dict, P) -> str:
    if m['status'] in ('untested', 'not_supported'):
        return P(m['status'].replace('_', ' '), DIM)
    txt = fmt(m['median'], m['unit'])
    mark = {'over': ' OVER', 'unstable': ' ~', 'within': '', 'measured': ''}[m['status']]
    if m.get('bimodal'):
        mark += ' (2 groups)'
    return P(txt + mark, STATUS[m['status']])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('report', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true')
    a = ap.parse_args()
    d = json.loads(a.report.read_text(encoding='utf-8'))
    P = (lambda s, st: f'{st}{s}{RESET}' if st else s) if supports_color(a.color) else (lambda s, st: s)
    hl, sc, md = d['headline'], d['scope'], d['metadata']
    verdict = P('OVER BUDGET', RED + BOLD) if hl['verdict'] == 'over_budget' else \
        P('WITHIN BUDGET IN THE LAB', GREEN + BOLD) + ' (under the tested profiles; field performance not established)'
    out = [P('# Performance budget (lab data)', BOLD), '', verdict, '', hl['summary'], '',
           f'Over budget: ' + (', '.join(f'{k} x{n}' for k, n in hl['over_budget_counts'].items()) or 'none') +
           f'; unstable: {hl["unstable_metrics"]}. Evidence {hl["evidence_completeness"]}, confidence {hl["overall_confidence"]}.',
           f'{sc["runs"]} runs per page and profile after a warm-up; cache {", ".join(sc["cache"])}; browsers {", ".join(sc["browsers"])}. '
           f'Budget: {sc["budget"]["source"]}.',
           f'Machine: {md["machine"]["cpu"]}, {md["machine"]["cores"]} cores, {md["machine"]["os"]}; calibration '
           f'{md["calibration"]["median_ms"]} ms (CV {md["calibration"]["cv"]}){" BUSY" if md["calibration"]["busy"] else ""}.']
    if a.brief:
        out.append('')
        for f in d['findings']:
            out.append(f'{P(f["id"], BOLD)} {P(f["priority"] + " " + f["severity"], SEV[f["severity"]])} {f["metric"]} '
                       f'{f["template"]}: {f["title"]}')
        print('\n'.join(out))
        return 0
    rows: dict[tuple, dict] = {}
    for m in d['measurements']:
        rows.setdefault((m['url'], m['profile'], m['cache'], m['browser']), {})[m['metric']] = m
    out += ['', P('## Medians', BOLD), '', '| Page | Profile | ' + ' | '.join(HEAD.get(c, c) for c in COLS) + ' |',
            '|---|---|' + '---|' * len(COLS)]
    for (url, prof, cache, browser), ms in rows.items():
        label = f'{prof}' + (f' {cache}' if len(sc['cache']) > 1 else '') + (f' ({browser})' if browser != 'chromium' else '')
        out.append(f'| {urlparse(url).path or "/"} | {label} | ' + ' | '.join(cell(ms[c], P) if c in ms else '-' for c in COLS) + ' |')
    out += ['', 'OVER: median over budget. ~: unstable (CV above 0.25), can\'t pass or fail on its own. Untested: INP needs an '
                'interaction. Not supported: a Chromium-only metric in another browser.']
    if d['findings']:
        out += ['', P('## Findings', BOLD)]
    for f in d['findings']:
        w = f['worst']
        unit = next((m['unit'] for m in d['measurements'] if m['metric'] == f['metric']), 'ms')
        out += ['', P(f'### {f["id"]} {f["priority"]} {f["severity"]}: {f["title"]}', SEV[f['severity']]), '',
                f'{f["metric"]} on {", ".join(urlparse(p).path or "/" for p in f["pages"])} (template {f["template"]})'
                f'{", critical path" if f["critical_path"] else ""}: worst {fmt(w["median"], unit)} against {fmt(w["budget"], unit)} '
                f'({w["ratio"]}x, {w["profile"]}); confidence {f["confidence"]}; reach {f["reach"]}, leverage {f["leverage"]}, score {f["score"]}.',
                '', f'- Cause: {f["cause"]}']
        attr = f['attribution']
        if f['metric'] == 'LCP' and attr.get('breakdown_ms'):
            b = attr['breakdown_ms']
            out.append(f'- LCP breakdown: TTFB {b["timeToFirstByte"]} ms, load delay {b["resourceLoadDelay"]} ms, load '
                       f'{b["resourceLoadDuration"]} ms, render delay {b["elementRenderDelay"]} ms; fetchpriority '
                       f'{attr.get("fetchpriority") or "not set"}; size attributes {"set" if attr.get("has_size_attributes") else "missing"}')
        if attr.get('render_blocking'):
            out.append('- Render-blocking: ' + ', '.join(f'{urlparse(r["url"]).path} ({r["duration"]} ms)' for r in attr['render_blocking']))
        if f['metric'] == 'CLS' and attr.get('shifts'):
            out.append('- Shifts: ' + '; '.join(f'{s["value"]} at {s["start"]} ms moving {", ".join(s["sources"])}' for s in attr['shifts']))
        if f['metric'] == 'INP':
            out.append(f'- Interaction: {attr.get("interaction_type")} on {attr.get("interaction_target")}: input delay '
                       f'{attr.get("input_delay_ms")} ms, processing {attr.get("processing_ms")} ms, presentation '
                       f'{attr.get("presentation_delay_ms")} ms')
        if attr.get('scripts'):
            out.append('- Scripts: ' + ', '.join(f'{urlparse(s["url"]).path} {s["script_ms_median"]} ms' for s in attr['scripts'][:3]))
        if attr.get('largest'):
            out.append('- Largest: ' + ', '.join(f'{urlparse(r["url"]).path} {fmt(r["bytes"], "bytes")}' +
                                                  (f' ({fmt(r["unused_bytes"], "bytes")} unused)' if r.get('unused_bytes') else '')
                                                  for r in attr['largest'][:3]))
        if attr.get('server_timing'):
            out.append('- Server-Timing: ' + ', '.join(f'{e["name"]} {e["dur_median"]} ms' for e in attr['server_timing']))
        out += [f'- Fix: {f["remediation"]}', f'- Verify: {f["verification_step"]}',
                '- Evidence: ' + '; '.join(f'{e["trace"] or "no trace"}, runs {len(e["run_ids"])}' for e in f['evidence'])]
    if d['server_timing']:
        out += ['', P('## Server-Timing by route', BOLD), '']
        out += [f'- {s["route"]}: TTFB median {s["ttfb_median"]} ms; ' + ', '.join(f'{e["name"]} {e["dur_median"]} ms' +
                                                                                (f' ({e["desc"]})' if e['desc'] else '') for e in s['entries'])
                for s in d['server_timing']]
    if d['regressions']:
        out += ['', P('## Against the baseline', BOLD), '']
        for r in d['regressions']:
            out.append(f'- {P(r["direction"].upper(), RED if r["direction"] == "regressed" else GREEN)} {urlparse(r["url"]).path or "/"} '
                       f'{r["profile"]} {r["metric"]}: {fmt(r["baseline_median"], r["unit"])} -> {fmt(r["current_median"], r["unit"])} '
                       f'({r["change_pct"]}%; noise threshold {fmt(r["threshold"], r["unit"])})')
    if d['field_data']:
        out += ['', P('## Field data (not lab, not mixed in)', BOLD), '', f'{d["field_data"]["source"]}, {d["field_data"]["period"]}: '
                + ', '.join(f'{k} {v}' for k, v in d['field_data']['metrics'].items())]
    if d['needs_review']:
        out += ['', P('## Needs review', BOLD), ''] + [f'- {n["candidate_id"]}: {n["reason"]}' for n in d['needs_review']]
    dismissed = [t for t in d['triage'] if t['decision'] == 'dismissed']
    if dismissed:
        out += ['', P('## Dismissed', BOLD), ''] + [f'- {t["candidate_id"]}: {t["reason"]}' for t in dismissed]
    out += ['', P('## Tools', BOLD), ''] + [f'- {t["name"]}{" " + t["version"] if t["version"] else ""}: {t["status"]}' +
                                            (f' ({t["note"]})' if t['note'] else '') for t in md['tools']]
    if d['limitations']:
        out += ['', P('## Limitations', BOLD), ''] + [f'- {x}' for x in d['limitations']]
    if d['next_steps']:
        out += ['', P('## Next steps', BOLD), ''] + [f'- {x}' for x in d['next_steps']]
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
