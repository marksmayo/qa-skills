#!/usr/bin/env python3
"""Render a Journey to Tests report as a terminal/Markdown report with optional ANSI colour."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; MAGENTA="\033[35m"; CYAN="\033[36m"
STATUS_STYLE={'pass':GREEN,'fail_product':RED,'fail_test':YELLOW,'blocked_env':MAGENTA,'flaky':YELLOW,'not_run':DIM}
STATUS_ORDER=['fail_product','flaky','fail_test','blocked_env','not_run','pass']

def supports_color(mode:str)->bool:
    if mode=='always': return True
    if mode=='never': return False
    return sys.stdout.isatty() and os.environ.get('TERM','')!='dumb' and 'NO_COLOR' not in os.environ

def paint(text:str, code:str, enabled:bool)->str:
    return f"{code}{text}{RESET}" if enabled and code else text

def bar(fraction, width=20):
    filled=max(0,min(width,round(fraction*width)))
    return '█'*filled+'░'*(width-filled)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto','always','never'], default='auto')
    args=ap.parse_args()
    try:
        data=json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read report: {exc}", file=sys.stderr); return 2
    color=supports_color(args.color)
    meta=data['metadata']; s=data['summary']; target=meta.get('target') or {}
    title=f"JOURNEY TESTS — {meta['repository']}"
    print(paint(title,BOLD,color)); print('='*len(title))
    print(f"Frameworks: {', '.join(meta.get('frameworks') or ['unknown'])}  |  Platforms: {', '.join(meta.get('platforms') or ['unknown'])}")
    print(f"Target: {target.get('kind','?')} {target.get('value','?')}  |  Stability runs per test: {meta.get('repeat')}  |  Generated: {meta['generated_at']}")
    if s.get('headline'): print('\n'+s['headline'])

    total=s.get('expects_total',0); ver=s.get('expects_verified',0); frac=ver/total if total else 0
    style=GREEN if frac>=0.9 else CYAN if frac>=0.75 else YELLOW if frac>=0.5 else RED
    print('\n'+paint('EXPECT COVERAGE',BOLD,color))
    print(paint(f"Verified by passing, stable tests: {ver}/{total} ({frac*100:.0f}%)  {bar(frac)}",style,color))
    print(f"Asserted: {s.get('expects_asserted',0)}  |  Not automatable: {s.get('expects_not_automatable',0)}  |  "
          f"Blocked by testability gap: {s.get('expects_blocked_by_gap',0)}  |  Deferred: {s.get('expects_deferred',0)}")
    print(paint('An Expect counts as verified only when a test asserting it passed every stability run.',DIM,color))

    tests={t['test_id']:(m['journey_id'],t['name'],m['test_path']) for m in data['mapping'] for t in m['tests']}
    print('\n'+paint('RESULTS',BOLD,color))
    res=s.get('results',{})
    print('  '.join(paint(f"{k}: {res.get(k,0)}",STATUS_STYLE[k] if res.get(k) else DIM,color) for k in STATUS_ORDER))
    for r in sorted(data['results'],key=lambda r:(STATUS_ORDER.index(r['status']),r['test_id'])):
        jid,name,path=tests.get(r['test_id'],(r['journey_id'],r['test_id'],r['test_path']))
        print(paint(f"  [{r['status']:12}]",STATUS_STYLE[r['status']],color)+f" {name}  ({path})")
        if r['status']!='pass' and r.get('notes'): print(paint(f"                 {r['notes']}",DIM,color))

    if data['suspected_bugs']:
        print('\n'+paint('SUSPECTED BUGS (product behaviour differs from the journey)',BOLD,color))
        for b in data['suspected_bugs']:
            print(paint(f"- {b['id']} {b['journey_id']} {b['ref']}",RED,color)+f"  likely: {b['likely']}")
            print(f"    expected: {b['expected']}\n    actual:   {b['actual']}")
    if data['testability_gaps']:
        print('\n'+paint('TESTABILITY GAPS',BOLD,color))
        for g in data['testability_gaps']:
            loc=f"  ({g['file']}:{g['line']})" if g.get('file') else ''
            print(paint(f"- {g['id']} {g['journey_id']} {g['ref']}: {g['element']}",YELLOW,color)+loc)
            print(f"    {g['suggestion']}")
    held=[e for e in data['step_coverage'] if e['coverage'] in ('deferred','not_automatable')]
    if held:
        print('\n'+paint('NOT COVERED BY DESIGN',BOLD,color))
        for e in held: print(f"- {e['journey_id']} {e['ref']} [{e['coverage']}]: {e.get('reason','')}")

    if data['files_changed']:
        print('\n'+paint('FILES CHANGED (uncommitted)',BOLD,color))
        for f in data['files_changed']:
            print(f"- {f['change']:8} {f['kind']:12} {f['path']}")
    if data.get('open_questions'):
        print('\n'+paint('OPEN QUESTIONS',BOLD,color))
        for q in data['open_questions']: print(f"- {q}")
    print('\n'+paint('Flaky, blocked and failing tests are not counted as done. Changes are left uncommitted for review.',DIM,color))
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__=='__main__': raise SystemExit(main())
