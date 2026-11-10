#!/usr/bin/env python3
"""Render a Ticket to TDD report as a terminal/Markdown report with optional ANSI colour."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; MAGENTA="\033[35m"; CYAN="\033[36m"
RED_STYLE={'valid_red':RED,'unexpected_green':YELLOW,'invalid_red':MAGENTA,'not_run':DIM}
REQ_STYLE={'red tests':RED,'already works':YELLOW,'needs answer':MAGENTA,'manual check':CYAN,'not run':DIM,'no test':MAGENTA}

def supports_color(mode:str)->bool:
    if mode=='always': return True
    if mode=='never': return False
    return sys.stdout.isatty() and os.environ.get('TERM','')!='dumb' and 'NO_COLOR' not in os.environ

def paint(text:str, code:str, enabled:bool)->str:
    return f"{code}{text}{RESET}" if enabled and code else text

def req_state(r, rows):
    st={(t.get('red') or {}).get('status') for t in rows}
    if r['testability']=='needs_clarification': return 'needs answer'
    if r['testability']=='not_unit_testable': return 'manual check'
    if 'valid_red' in st: return 'red tests'
    if 'unexpected_green' in st: return 'already works'
    if st=={'not_run'}: return 'not run'
    return 'no test'

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
    meta=data['metadata']; s=data['summary']; tk=meta['ticket']; integ=data['integrity']
    title=f"TICKET TO TDD — {tk['key']}: {tk['summary']}"
    print(paint(title,BOLD,color)); print('='*min(len(title),100))
    conv=', '.join(sorted({c['framework'] for c in data['conventions']})) or 'unknown'
    print(f"Repository: {meta['repository']}  |  Type: {tk['type']}  |  Ticket source: {tk['source']}  |  Mode: {meta['mode']}  |  Framework: {conv}")
    print(f"Generated: {meta['generated_at']}")
    if s.get('headline'): print('\n'+s['headline'])

    print('\n'+paint('REQUIREMENTS',BOLD,color))
    print(f"Total {s['reqs_total']}  |  red tests {s['reqs_covered']}  |  already work {s['reqs_already_implemented']}  |  "
          f"need an answer {s['reqs_blocked']}  |  manual check {s['reqs_not_unit_testable']}  |  not run {s['reqs_not_run']}")
    rows_by={}
    for t in data['traceability']: rows_by.setdefault(t['req_id'],[]).append(t)
    for r in data['requirements']:
        state=req_state(r, rows_by.get(r['id'],[]))
        print(paint(f"  [{state:13}]",REQ_STYLE.get(state,''),color)+f" {r['id']} {r['text']}")
        print(paint(f"                  from {r['source_field']}: \"{r['source_excerpt']}\"",DIM,color))

    print('\n'+paint('TESTS',BOLD,color))
    print(f"Written {s['tests_written']}  |  valid red {s['valid_reds']}  |  unexpected green {s['unexpected_greens']}  |  "
          f"not run {s['tests_not_run']}" + ('' if meta['mode']=='red_only' else f"  |  green {s['greens']}"))
    for t in data['traceability']:
        red=t.get('red') or {}; st=red.get('status','?')
        green='' if meta['mode']=='red_only' else f"  green: {(t.get('green') or {}).get('status')}"
        print(paint(f"  [{st:16}]",RED_STYLE.get(st,''),color)+f" {t['test_id']} {t['req_id']} {t['level']}/{t['kind']}  {t['test_path']}::{t['test_name']}{green}")
        detail=red.get('failure_excerpt') if st=='valid_red' else (red.get('investigation') or {}).get('detail') or red.get('reason')
        if detail: print(paint(f"                     {detail}",DIM,color))
    if integ.get('test_hashes_at_red'):
        print(paint(f"Locked at red (run {integ.get('red_lock_run')}): {', '.join(sorted(integ['test_hashes_at_red']))}",DIM,color))
    if meta['mode']!='red_only':
        rr=integ.get('re_red') or {}
        print(paint(f"Re-red check: {'confirmed' if rr.get('confirmed') else 'NOT confirmed'}",GREEN if rr.get('confirmed') else RED,color))

    if data['regressions']:
        print('\n'+paint('REGRESSIONS',BOLD,color))
        for r in data['regressions']:
            tag='pre-existing' if r.get('pre_existing') else 'new'
            print(paint(f"- [{tag}] {r['test']}",YELLOW if r.get('pre_existing') else RED,color)+f"  (run {r['run_id']})")
    if data['open_questions']:
        print('\n'+paint('OPEN QUESTIONS',BOLD,color))
        for q in data['open_questions']:
            tag=paint('BLOCKING ',RED,color) if q.get('blocking') and not q.get('answer') else ''
            print(f"- {q['id']} {tag}({', '.join(q.get('related_reqs') or [])}) {q['question']}")
            if q.get('answer'): print(paint(f"    answer: {q['answer']}",DIM,color))
    if data['manual_checks']:
        print('\n'+paint('MANUAL CHECKS',BOLD,color))
        for m in data['manual_checks']: print(f"- {m['req_id']}: {m['check']}  ({m['reason']})")
    ignored=tk.get('suspicious_instructions_ignored') or []
    if ignored:
        print('\n'+paint('INSTRUCTIONS IN THE TICKET THAT WERE NOT FOLLOWED',BOLD,color))
        for x in ignored: print(paint(f"- {x['field']}: \"{x['excerpt']}\"",YELLOW,color))
    if integ.get('stubs_added'):
        print('\n'+paint('STUBS ADDED (signature only)',BOLD,color))
        for st in integ['stubs_added']:
            print(f"- {st['path']}{':'+str(st['line']) if st.get('line') else ''}  {st['symbol']}" + (f"  ({st['note']})" if st.get('note') else ''))
    if data['files_changed']:
        print('\n'+paint('FILES CHANGED (uncommitted)',BOLD,color))
        for f in data['files_changed']: print(f"- {f['change']:8} {f['kind']:8} {f['path']}")
    if data.get('next_steps'):
        print('\n'+paint('NEXT STEPS',BOLD,color))
        for n in data['next_steps']: print(f"- {n}")
    note='Red tests are the specification: they should pass once the feature is built, without being edited.' if meta['mode']=='red_only' \
        else 'Tests were locked at red; green was reached without editing them.'
    print('\n'+paint(note+' Changes are left uncommitted for review.',DIM,color))
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__=='__main__': raise SystemExit(main())
