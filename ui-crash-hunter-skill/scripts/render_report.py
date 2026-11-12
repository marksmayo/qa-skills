#!/usr/bin/env python3
"""Render a UI crash hunter report as a terminal/Markdown report with optional ANSI colour,
and optionally export the findings as SARIF.

    python render_report.py crash_report.json [--brief] [--color auto|always|never] [--sarif crashes.sarif]
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sarif  # noqa: E402

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"
PRIO_STYLE = {'P0': RED + BOLD, 'P1': RED, 'P2': YELLOW, 'P3': DIM}
CONF_STYLE = {'reproduced': GREEN, 'traced': CYAN, 'pattern': DIM}
TOOL_STYLE = {'ran': GREEN, 'not_run': YELLOW, 'errored': RED, 'not_applicable': DIM}

def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ

def paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled and code else text

def loc(f: dict) -> str:
    l = f['locations'][0]
    return f"{l['file']}:{l['line']}"

def to_sarif(d: dict) -> dict:
    results = []
    for f in d['crashes'] + d['degradations']:
        r = f.get('ranking') or {}
        level = 'note' if f['tier'] in ('B3', 'B4') else 'error' if r.get('priority') in ('P0', 'P1') else 'warning'
        for l in f['locations'][:1]:
            results.append({'rule_id': f.get('pitfall_id') or 'agent-finding', 'level': level, 'file': l['file'], 'line': l['line'],
                            'message': f"[{f['id']} {f['tier']}{' ' + r['priority'] if r.get('priority') else ''} {f['confidence']}] "
                                       f"{f['title']}. Trigger: {f['trigger']['description']}",
                            'help': f.get('remediation'),
                            'properties': {'tier': f['tier'], 'confidence': f['confidence'], 'priority': r.get('priority'),
                                           'finding_id': f['id']}})
    return sarif.build('ui-crash-hunter', d['metadata'].get('skill_version'), results)

EFFECT_LABEL = {'app_fallback': 'app-wide fallback replaced every page', 'white_screen': 'white screen',
                'app_terminated': 'app terminated', 'crash_loop': 'crash loop', 'screen_replaced': 'screen replaced by a fallback',
                'screen_frozen': 'screen frozen', 'component_replaced': 'component replaced by a fallback',
                'component_broken': 'component broken', 'console_only': 'console only'}
STACK_LINES = 12

def stack_excerpt(st: str) -> list[str]:
    """The error line and the first frames; framework internals after the app's frames are cut."""
    lines = [l.rstrip() for l in st.strip().splitlines() if l.strip()]
    if len(lines) <= STACK_LINES:
        return lines
    return lines[:STACK_LINES] + [f'... {len(lines) - STACK_LINES} more frame(s)']

def found_by(f: dict) -> str:
    return ' (found by review)' if f.get('source') == 'agent' else ''

def brief(d: dict, c: bool) -> None:
    meta, s, cov = d['metadata'], d['summary'], d['coverage']
    run = meta['app_run']
    print(paint(f"UI crash hunt — {meta['repository']}", BOLD, c))
    if s.get('headline'):
        print(s['headline'])
    bp, bc = s['by_priority'], s['by_confidence']
    print(f"Crashes {s['crashes']} (P0 {bp.get('P0', 0)}, P1 {bp.get('P1', 0)}, P2 {bp.get('P2', 0)}, P3 {bp.get('P3', 0)}; "
          f"reproduced {bc.get('reproduced', 0)}, traced {bc.get('traced', 0)}, pattern {bc.get('pattern', 0)}), "
          f"degradations {s['degradations']}")
    print(f"Dynamic half: {d.get('run_status', 'unknown')}"
          + (f" ({d['run_status_reason']})" if d.get('run_status_reason') else '')
          + f" | app run: {run['status']}{' at ' + run['target'] if run.get('target') else ''} | probes {len(d['probes'])}")
    print(f"Tools ran {s['tools_ran']}/{s['tools_applicable']} | candidates {s['candidates_total']} "
          f"({s['candidates_promoted']} promoted, {s['candidates_dismissed']} dismissed) | "
          f"routes analysed {cov['routes_analysed']}/{cov['routes_total']}")
    for f in d['crashes']:
        r = f['ranking']
        print(f"  {f['rank']}. {paint(r['priority'], PRIO_STYLE.get(r['priority'], ''), c)} {f['tier']} {f['id']} "
              f"[{f['confidence']}] {f['title']}{found_by(f)}  ({loc(f)})")
    for f in d['degradations']:
        print(f"  - {f['tier']} {f['id']} [{f['confidence']}] {f['title']}{found_by(f)}  ({loc(f)})")
    not_ran = [t['name'] for t in d['tools'] if t['status'] in ('not_run', 'errored')]
    if not_ran:
        print(paint(f"Not run or errored: {', '.join(not_ran)}", YELLOW, c))
    print(paint(f"{len(d['limitations'])} limitation(s); render without --brief for the full report.", DIM, c))

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--brief', action='store_true', help='a short summary: headline, counts and one line per finding')
    ap.add_argument('--sarif', type=Path, help='also write the findings as SARIF 2.1.0')
    args = ap.parse_args()
    try:
        d = json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr)
        return 2
    c = supports_color(args.color)
    if args.brief:
        brief(d, c)
        if args.sarif:
            args.sarif.write_text(json.dumps(to_sarif(d), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return 0
    meta, s, cov = d['metadata'], d['summary'], d['coverage']
    title = f"UI CRASH HUNT — {meta['repository']}"
    print(paint(title, BOLD, c)); print('=' * min(len(title), 100))
    run = meta['app_run']
    print(f"Platforms: {', '.join(meta['platforms'])}  |  Frameworks: {', '.join(meta.get('frameworks') or []) or 'unknown'}  |  "
          f"App run: {run['status']}{' (' + run['target'] + ')' if run.get('target') else ''}")
    print(f"Generated: {meta['generated_at']}  |  Dynamic half: {d.get('run_status', 'unknown')}"
          + (f" ({d['run_status_reason']})" if d.get('run_status_reason') else ''))
    if run.get('authorised_by') or run.get('read_only') is not None:
        print(f"Target approved by: {run.get('authorised_by') or 'n/a (loopback)'}  |  read-only: {run.get('read_only', 'not stated')}")
    site, blast = d['recon'].get('site_model'), (d['recon'].get('blast_radius') or {}).get('model')
    if site or blast:
        print(f"Site: {site or 'unknown'}  |  Blast-radius model: {blast or 'unknown'}")
    if s.get('headline'):
        print('\n' + s['headline'])

    print('\n' + paint('SUMMARY', BOLD, c))
    bp, bc = s['by_priority'], s['by_confidence']
    print(f"Crashes {s['crashes']}  (P0 {bp.get('P0', 0)} · P1 {bp.get('P1', 0)} · P2 {bp.get('P2', 0)} · P3 {bp.get('P3', 0)})  |  "
          f"reproduced {bc.get('reproduced', 0)} · traced {bc.get('traced', 0)} · pattern {bc.get('pattern', 0)}  |  degradations {s['degradations']}")
    print(f"Candidates {s['candidates_total']}: {s['candidates_promoted']} promoted, {s['candidates_dismissed']} dismissed  |  "
          f"tools ran {s['tools_ran']}/{s['tools_applicable']} (evidence completeness {s['evidence_completeness']:.0%})  |  "
          f"routes analysed {cov['routes_analysed']}/{cov['routes_total']}")

    print('\n' + paint('CRASHES (ranked)', BOLD, c))
    if not d['crashes']:
        print('  None found in the analysed scope. This is not evidence that the app cannot crash; see coverage and limitations.')
    for f in d['crashes']:
        r, rp = f['ranking'], f.get('repro') or {}
        head = paint(f"{r['priority']}", PRIO_STYLE.get(r['priority'], ''), c)
        print(f"\n{f['rank']}. {head} {paint(f['tier'], BOLD, c)} {f['id']}  {f['title']}  "
              + paint(f"[{f['confidence']}]", CONF_STYLE.get(f['confidence'], ''), c) + paint(f"  score {r['score']}", DIM, c)
              + paint(found_by(f), DIM, c))
        print(f"   Where:   {loc(f)}" + (f"  (+{len(f['locations']) - 1} more)" if len(f['locations']) > 1 else ''))
        print(f"   Trigger: {f['trigger']['description']}")
        caught = f['boundary']['caught_by'] or 'nothing'
        eff = f['boundary']['effect']
        print(f"   Effect:  {EFFECT_LABEL.get(eff, eff.replace('_', ' '))} (caught by {caught})")
        print(paint(f"   Ranking: reach {r['reach']} · likelihood {r['likelihood']} · recoverability {r['recoverability']}"
                    + (f" · {r['capped']}" if r.get('capped') else ''), DIM, c))
        for n, step in enumerate(rp.get('steps') or [], 1):
            print(f"   {n}) {step}")
        if rp.get('actual'):
            print(f"   Actual:  {rp['actual']}")
        if rp.get('probe_id'):
            print(paint(f"   Probe:   {rp['probe_id']}", DIM, c))
        if rp.get('stack_trace'):
            for l in stack_excerpt(rp['stack_trace']):
                print(paint('   | ' + l, DIM, c))
        if rp.get('source_mapping'):
            print(paint(f"   Frames mapped: {rp['source_mapping']}", DIM, c))
        if rp.get('observed_effect'):
            print(f"   Seen:    {rp['observed_effect']}")
        if rp.get('repro_spec'):
            print(paint(f"   Repro spec: {rp['repro_spec']}", DIM, c))
        if rp.get('not_reproduced_reason'):
            print(paint(f"   Not reproduced: {rp['not_reproduced_reason']}", DIM, c))
        print(f"   Fix:     {f['remediation']}")

    if d['degradations']:
        print('\n' + paint('DEGRADATIONS (not crashes; not ranked)', BOLD, c))
        for f in d['degradations']:
            print(f"- {f['tier']} {f['id']} {f['title']}  " + paint(f"[{f['confidence']}]", CONF_STYLE.get(f['confidence'], ''), c)
                  + f"  {loc(f)}" + paint(found_by(f), DIM, c))
            seen = (f.get('repro') or {}).get('observed_effect')
            if seen:
                print(paint(f"    seen: {seen}", DIM, c))

    print('\n' + paint('PROBES', BOLD, c))
    if not d['probes']:
        print('  None: the app was not run, so nothing was confirmed dynamically.')
    for p in d['probes']:
        style = {'reproduced': GREEN, 'baseline_clean': GREEN, 'not_reproduced': YELLOW,
                 'baseline_not_clean': RED, 'errored': RED, 'inconclusive': YELLOW}.get(p['outcome'], '')
        fl = f" -> {', '.join(p['findings'])}" if p.get('findings') else ''
        env = f"  {p['environment']}" if p.get('environment') else ''
        print(paint(f"  [{p['outcome']:18}]", style, c) + f" {p['id']}{fl}{env}")
        if p.get('note'):
            print(paint(f"      {p['note']}", DIM, c))

    tri = d['triage']
    if tri.get('dismissed_groups'):
        print('\n' + paint('DISMISSED IN GROUPS', BOLD, c))
        for g in tri['dismissed_groups']:
            print(f"  {g['rule_id']}: {len(g['candidates'])} candidate(s) matching {json.dumps(g['match'])} — {g['reason']}")

    print('\n' + paint('TOOLS', BOLD, c))
    for t in d['tools']:
        note = f"  ({t['note']})" if t.get('note') else ''
        ver = f" {t['version']}" if t.get('version') else ''
        print(paint(f"  [{t['status']:14}]", TOOL_STYLE.get(t['status'], ''), c) + f" {t['name']}{ver}{note}")

    print('\n' + paint('COVERAGE', BOLD, c))
    reasons = {x['item']: x['reason'] for x in cov['not_analysed']}
    shown = set()
    for r in d['recon']['routes_screens']:
        if r['analysed']:
            print(f"  {r['id']:6} {r['path']:22} analysed      reach {r['reach']}")
        else:
            why = reasons.get(r['id']) or reasons.get(r['path']) or r.get('not_analysed_reason') or ''
            shown |= {r['id'], r['path']}
            print(paint(f"  {r['id']:6} {r['path']:22} NOT analysed  reach {r['reach']}" + (f"  ({why})" if why else ''), YELLOW, c))
    for x in cov['not_analysed']:
        if x['item'] not in shown:
            print(paint(f"  not analysed: {x['item']} ({x['reason']})", YELLOW, c))
    if cov.get('platforms_not_covered'):
        print(paint(f"  platforms not covered: {', '.join(cov['platforms_not_covered'])}", YELLOW, c))

    print('\n' + paint('LIMITATIONS', BOLD, c))
    for l in d['limitations']:
        print(f"- {l}")
    if d.get('next_steps'):
        print('\n' + paint('NEXT STEPS', BOLD, c))
        for n in d['next_steps']:
            print(f"- {n}")
    if args.sarif:
        args.sarif.write_text(json.dumps(to_sarif(d), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(paint(f"\nSARIF written to {args.sarif}", DIM, c))
    print('\n' + paint('Findings without "reproduced" are likely crashes, not confirmed ones. The source was not modified.', DIM, c))
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
