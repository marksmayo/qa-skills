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
        data=json.loads(args.json_file.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        print(f"Unable to read evaluation: {exc}", file=sys.stderr); return 2
    color=supports_color(args.color)
    meta=data['metadata']; head=data['headline']
    title=f"TEST SUITE EVALUATION — {meta['repository']}"
    print(paint(title,BOLD,color)); print('='*len(title))
    overall=head['overall_score']; label=head['rating_band']
    execution=meta.get('execution','unknown').replace('_',' ')
    print(f"Mode: {meta['assessment_mode'].replace('_',' ')}  |  Execution: {execution}  |  Confidence: {head['overall_confidence']}  |  Evidence: {head['evidence_completeness']:.0f}%")
    if meta.get('provisional'): print(paint('PROVISIONAL evaluation',MAGENTA,color))
    print(paint(f"Overall: {overall:.1f}/10  {bar(overall)}  {label}",score_style(overall),color))
    raw=head.get('raw_weighted_score')
    if raw is not None and abs(raw-overall)>1e-9: print(f"Raw weighted score: {raw:.1f}/10 (capped to {overall:.1f})")
    for key,name in (('testing_effectiveness','Testing effectiveness'),('suite_trustworthiness','Suite trustworthiness'),
                     ('engineering_quality','Engineering quality'),('operational_health','Operational health')):
        if key in head:
            v=head[key]; shown='not assessed' if v is None else f"{v:.1f}/10"
            print(paint(f"  {name:22} {shown}",score_style(v),color))
    for cap in head.get('applied_caps',[]):
        text=cap if isinstance(cap,str) else f"cap {cap['rule']} (max {cap['limit']:g}): {cap['explanation']}"
        print(paint('! '+text,MAGENTA,color))

    findings=data.get('critical_findings',[])
    if findings:
        print('\n'+paint('CRITICAL FINDINGS',BOLD,color))
        for f in findings:
            sev=f['severity'].upper(); code=RED if sev in ('CRITICAL','HIGH') else YELLOW
            cat=f.get('category','other'); tag='' if cat=='other' else f" ({cat.replace('_',' ')})"
            print(paint(f"[{sev}] {f['title']}{tag}",code,color))
            if f.get('impact'): print('  '+f['impact'])
            for e in f.get('evidence',[])[:4]: print('  - '+e)

    print('\n'+paint('DIMENSION SCORECARD',BOLD,color))
    for d in data['dimensions']:
        score=d.get('score'); shown='N/A' if score is None else f"{score}/10"
        line=f"{d['name'][:34]:34} {shown:>5} {bar(score,12)}  {d['confidence']}/{d['evidence_level']}"
        print(paint(line,score_style(score),color))
        print('  '+d['finding'])
        if d.get('applicable') is False: print('  not applicable: '+d.get('not_applicable_reason',''))

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

# Write UTF-8 whatever the locale (cp1252 on Windows would crash on "ō" or the bar glyphs), and
# replace anything that still cannot be encoded rather than failing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')

if __name__=='__main__': raise SystemExit(main())
