#!/usr/bin/env python3
"""Render a Test Suite Evaluator JSON report with optional ANSI colour."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; MAGENTA="\033[35m"; CYAN="\033[36m"

def supports_color(mode:str)->bool:
    if mode=='always': return True
    if mode=='never': return False
    return sys.stdout.isatty() and os.environ.get('TERM','')!='dumb' and 'NO_COLOR' not in os.environ

def paint(text:str, code:str, enabled:bool)->str:
    return f"{code}{text}{RESET}" if enabled else text

def score_style(score):
    if score is None: return DIM
    if score>=8: return GREEN
    if score>=6.5: return CYAN
    if score>=5: return YELLOW
    if score>=3: return MAGENTA
    return RED

def bar(score, width=20):
    if score is None: return '·'*width
    filled=max(0,min(width,round(score/10*width)))
    return '█'*filled+'░'*(width-filled)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto','always','never'], default='auto')
    args=ap.parse_args()
    try:
        data=json.loads(args.json_file.read_text())
    except Exception as exc:
        print(f"Unable to read evaluation: {exc}", file=sys.stderr); return 2
    color=supports_color(args.color)
    meta=data['metadata']; head=data['headline']
    title=f"TEST SUITE EVALUATION — {meta['repository']}"
    print(paint(title,BOLD,color)); print('='*len(title))
    overall=head['overall_score']; label=head['rating_band']
    print(f"Mode: {meta['assessment_mode'].replace('_',' ')}  |  Confidence: {head['overall_confidence']}  |  Evidence: {head['evidence_completeness']:.0f}%")
    print(paint(f"Overall: {overall:.1f}/10  {bar(overall)}  {label}",score_style(overall),color))
    if head.get('applied_caps'):
        print(paint('Caps: '+'; '.join(head['applied_caps']),MAGENTA,color))

    findings=data.get('critical_findings',[])
    if findings:
        print('\n'+paint('CRITICAL FINDINGS',BOLD,color))
        for f in findings:
            sev=f['severity'].upper(); code=RED if sev in ('CRITICAL','HIGH') else YELLOW
            print(paint(f"[{sev}] {f['title']}",code,color))
            if f.get('impact'): print('  '+f['impact'])
            for e in f.get('evidence',[])[:4]: print('  - '+e)

    print('\n'+paint('DIMENSION SCORECARD',BOLD,color))
    for d in data['dimensions']:
        score=d.get('score'); shown='N/A' if score is None else f"{score}/10"
        line=f"{d['name'][:34]:34} {shown:>5} {bar(score,12)}  {d['confidence']}/{d['evidence_level']}"
        print(paint(line,score_style(score),color))
        print('  '+d['finding'])

    recs=sorted(data.get('recommendations',[]),key=lambda x:x.get('priority',999))
    if recs:
        print('\n'+paint('HIGHEST-LEVERAGE IMPROVEMENTS',BOLD,color))
        for r in recs[:5]:
            print(f"{r['priority']}. {paint(r['title'],BOLD,color)} [{r['impact']} impact / {r['effort']} effort]")
            print('   '+r['action'])

    missing=[x for x in data.get('evidence_inventory',[]) if x['status'] in ('missing','partial')]
    if missing:
        print('\n'+paint('EVIDENCE LIMITATIONS',BOLD,color))
        for item in missing: print(f"- {item['name']}: {item['status']} — {item.get('details','')}")
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__=='__main__': raise SystemExit(main())
