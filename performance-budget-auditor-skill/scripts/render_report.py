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


def path_of(url: str) -> str:
    """Path and query string, so /search and /search?q=x stay apart."""
    u = urlparse(url or '')
    return (u.path or '/') + (('?' + u.query) if u.query else '')


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
    if m.get('outliers'):
        mark += f' ({len(m["outliers"])} outlier{"s" if len(m["outliers"]) > 1 else ""})'
    return P(txt + mark, STATUS[m['status']])


def third_party_line(tp: dict | None) -> str:
    if tp is None:
        return 'Third-party requests: not recorded by this run.'
    if tp['mode'] == 'allow':
        return 'Third-party requests: allowed and measured.'
    if not tp['blocked']:
        return 'Third-party requests: blocked by the harness; none were attempted.'
    n = sum(b['requests'] for b in tp['blocked'])
    return (f'Third-party requests: {n} blocked (' + ', '.join(f'{b["host"]} x{b["requests"]}' for b in tp['blocked'][:6]) +
            (', ...' if len(tp['blocked']) > 6 else '') + '); their cost and behaviour are not measured.')


def tpl(f: dict) -> str:
    return f['template'] if not f.get('site_wide') else 'site-wide (' + ', '.join(t or '?' for t in f.get('templates') or []) + ')'


def first_party_line(bfp: list[dict] | None) -> str | None:
    if not bfp:
        return None
    n = sum(b['requests'] for b in bfp)
    return (f'First-party requests blocked by the harness: {n} (' +
            ', '.join(f'{b["method"]} {b["path"]} x{b["requests"]}' for b in bfp[:6]) + (', ...' if len(bfp) > 6 else '') +
            "); beacons, reports and telemetry failed at once, so their upload cost and the page's handling of the failure are not "
            'what production sees.')


def harness_line(hm: dict) -> str:
    r = hm.get('requests') or {}
    blocked = {k: v for k, v in (r.get('blocked') or {}).items() if v}
    return (f'Requests: {r.get("sent", 0)} sent, ' + (', '.join(f'{v} blocked ({k})' for k, v in blocked.items()) or 'none blocked') +
            ('; request budget spent, run incomplete' if r.get('budget_exhausted') else '') +
            f'. Page CSP {"bypassed (not enforced)" if hm.get("bypass_csp") else "enforced"}.')


def runs_line(sc: dict, meas: list[dict]) -> str:
    used = [m['runs_used'] for m in meas if 'runs_used' in m]
    span = (f'{min(used)}' if min(used) == max(used) else f'{min(used)}-{max(used)}') if used else '?'
    early = f', stopping from {sc["min_runs"]} when stable' if sc.get('min_runs', sc['runs']) < sc['runs'] else ''
    return (f'{sc["runs"]} measured runs planned per page, profile and variant after a warm-up{early}; cap {sc.get("max_runs", 10)} '
            f'with extra runs; {span} used')


def short(text: str, n: int = 90) -> str:
    first = text.split('. ')[0]
    return first if len(first) <= n else first[:n - 3] + '...'


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
           f'{runs_line(sc, d["measurements"])}; cache {", ".join(sc["cache"])}; browsers {", ".join(sc["browsers"])}. '
           f'Budget: {short(sc["budget"]["source"]) if a.brief else sc["budget"]["source"]}' +
           (f' ({sc["budget"]["note"]})' if sc['budget'].get('note') else '') + '.',
           third_party_line(sc.get('third_party'))] + ([first_party_line(sc.get('blocked_first_party'))] if sc.get('blocked_first_party') else []) + [
           *([harness_line(md['harness'])] if md.get('harness') else []),
           f'Machine: {md["machine"]["cpu"]}, {md["machine"]["cores"]} cores, {md["machine"]["os"]}; calibration '
           f'{md["calibration"]["median_ms"]} ms (CV {md["calibration"]["cv"]}){" BUSY" if md["calibration"]["busy"] else ""}; '
           # older measurements have no note: they compared nothing with an idle reference
           f'{md["calibration"].get("note") or "no idle reference was compared, so a steady load would not show"}.']
    if a.brief:
        out.append('')
        for f in d['findings']:
            out.append(f'{P(f["id"], BOLD)} {P(f["priority"] + " " + f["severity"], SEV[f["severity"]])} {f["metric"]} '
                       f'{tpl(f)}: {f["title"]}')
        print('\n'.join(out))
        return 0
    rows: dict[tuple, dict] = {}
    for m in d['measurements']:
        rows.setdefault((m['url'], m['profile'], m['cache'], m['browser'], m.get('variant')), {})[m['metric']] = m
    out += ['', P('## Medians', BOLD), '', '| Page | Profile | ' + ' | '.join(HEAD.get(c, c) for c in COLS) + ' |',
            '|---|---|' + '---|' * len(COLS)]
    for (url, prof, cache, browser, variant), ms in rows.items():
        label = (f'{variant} ' if variant else '') + f'{prof}' + (f' {cache}' if len(sc['cache']) > 1 else '') + (f' ({browser})' if browser != 'chromium' else '')
        out.append(f'| {path_of(url)} | {label} | ' + ' | '.join(cell(ms[c], P) if c in ms else '-' for c in COLS) + ' |')
    out += ['', 'OVER: median over budget. ~: unstable (CV above 0.25 with a spread that matters for the budget), can\'t pass or fail on its own. Untested: INP needs an '
                'interaction. Not supported: a Chromium-only metric in another browser. Outliers: stable by the robust rule, the '
                'listed runs set aside.']
    notes = sorted({(path_of(m['url']) + (f' ({m["variant"]})' if m.get('variant') else ''), m['note']) for m in d['measurements'] if m.get('note')})
    if notes:
        out += ['', P('## Measurement notes', BOLD), ''] + [f'- {u}: {n}' for u, n in notes]
    if d['findings']:
        out += ['', P('## Findings', BOLD)]
    for f in d['findings']:
        w = f['worst']
        unit = next((m['unit'] for m in d['measurements'] if m['metric'] == f['metric']), 'ms')
        out += ['', P(f'### {f["id"]} {f["priority"]} {f["severity"]}: {f["title"]}', SEV[f['severity']]), '',
                f'{f["metric"]} on {", ".join(path_of(p) for p in f["pages"])} ' +
                (f'(template {f["template"]})' if not f.get('site_wide') else f'(site-wide: templates {", ".join(f["templates"])})') +
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
            out.append('- Render-blocking: ' + ', '.join(f'{path_of(r["url"])} ({r["duration"]} ms)' for r in attr['render_blocking']))
        if f['metric'] == 'CLS' and attr.get('shifts'):
            out.append('- Shifts: ' + '; '.join(f'{s["value"]} at {s["start"]} ms moving {", ".join(s["sources"])}' for s in attr['shifts']))
        if f['metric'] == 'INP':
            out.append(f'- Interaction: {attr.get("interaction_type")} on {attr.get("interaction_target")}: input delay '
                       f'{attr.get("input_delay_ms")} ms, processing {attr.get("processing_ms")} ms, presentation '
                       f'{attr.get("presentation_delay_ms")} ms')
        if attr.get('scripts'):
            out.append('- Scripts: ' + ', '.join(f'{path_of(s["url"])} {s["script_ms_median"]} ms' for s in attr['scripts'][:3]))
        if attr.get('largest'):
            out.append('- Largest: ' + ', '.join(f'{path_of(r["url"])} {fmt(r["bytes"], "bytes")}' +
                                                  (f' ({fmt(r["unused_bytes"], "bytes")} unused)' if r.get('unused_bytes') else '')
                                                  for r in attr['largest'][:3]))
        if attr.get('request_classes'):
            out.append('- Request classes: ' + ', '.join(f'{k} {v["requests"]} ({fmt(v["bytes"], "bytes")})' for k, v in attr['request_classes'].items()))
        if attr.get('server_timing'):
            out.append('- Server-Timing: ' + ', '.join(f'{e["name"]} {e["dur_median"]} ms' for e in attr['server_timing']))
        out += [f'- Fix: {f["remediation"]}', f'- Verify: {f["verification_step"]}',
                '- Evidence: ' + '; '.join(f'{e["trace"] or "no trace"}, runs {len(e["run_ids"])}' for e in f['evidence'])]
    if d['server_timing']:
        out += ['', P('## Server-Timing by route', BOLD), '']
        out += [f'- {s["route"]}: TTFB median {s["ttfb_median"]} ms; ' + ', '.join(f'{e["name"]} {e["dur_median"]} ms' +
                                                                                (f' ({e["desc"]})' if e['desc'] else '') for e in s['entries'])
                for s in d['server_timing']]
    if d.get('cdn_cache'):
        out += ['', P('## CDN cache state of the measured documents', BOLD), '']
        out += [f'- {path_of(c["url"])} {c["profile"]}' + (f' ({c["variant"]})' if c.get('variant') else '') + ': ' +
                ', '.join(f'{s["value"]} x{s["runs"]}' for s in c['cache_status']) +
                (f'; age {c["age_min"]}-{c["age_max"]} s' if c.get('age_min') is not None else '') for c in d['cdn_cache']]
    if d.get('request_summary'):
        out += ['', P('## Requests the resource list hides (median per load)', BOLD), '']
        out += [f'- {path_of(r["url"])} {r["profile"]}' + (f' ({r["variant"]})' if r.get('variant') else '') + ': ' +
                (', '.join(f'{k} {v["requests_median"]} ({fmt(v["bytes_median"], "bytes")})' for k, v in r['classes'].items()) or 'none') +
                ('; repeated: ' + ', '.join(f'{x["path"]} in {x["runs"]} run(s)' for x in r['repeated_fetches']) if r['repeated_fetches'] else '')
                for r in d['request_summary']]
    if d['regressions']:
        out += ['', P('## Against the baseline', BOLD), '']
        for r in d['regressions']:
            out.append(f'- {P(r["direction"].upper(), RED if r["direction"] == "regressed" else GREEN)} {path_of(r["url"])} '
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
