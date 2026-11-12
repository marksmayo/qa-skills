#!/usr/bin/env python3
"""Render a Journey Mapper journey_map.json as a terminal/Markdown report with optional ANSI colour.
In a scoped run the coverage headline counts only the run's domains; the whole-repository figure follows it.

    python render_report.py journeys/journey_map.json [--brief] [--color auto|always|never]"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; MAGENTA="\033[35m"; CYAN="\033[36m"
PRIORITY_STYLE={'P1':RED,'P2':YELLOW,'P3':DIM}
AUTOMATION_STYLE={'automated':GREEN,'partial':YELLOW,'none':RED}
PRIORITY_ORDER={'P1':0,'P2':1,'P3':2}

def supports_color(mode:str)->bool:
    if mode=='always': return True
    if mode=='never': return False
    return sys.stdout.isatty() and os.environ.get('TERM','')!='dumb' and 'NO_COLOR' not in os.environ

def paint(text:str, code:str, enabled:bool)->str:
    return f"{code}{text}{RESET}" if enabled else text

def bar(fraction, width=20):
    filled=max(0,min(width,round(fraction*width)))
    return '█'*filled+'░'*(width-filled)

def scoped_counts(data):
    """(in-scope total, covered, out-of-scope count) for a scoped run: every item except out_of_scope orphans."""
    cov=data['coverage']
    out={o['item'] for o in cov['orphans'] if o['reason']=='out_of_scope'}
    return cov['surface_items_total']-len(out), cov['in_journey'], len(out)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto','always','never'], default='auto')
    ap.add_argument('--brief', action='store_true', help='coverage headline, untested P1 journeys and counts only')
    args=ap.parse_args()
    try:
        data=json.loads(args.json_file.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f"Unable to read journey map: {exc}", file=sys.stderr); return 2
    color=supports_color(args.color)
    meta=data['metadata']; cov=data['coverage']; journeys=data['journeys']
    title=f"JOURNEY MAP — {meta['repository']}"
    print(paint(title,BOLD,color)); print('='*len(title))
    scope=meta['run_scope']['domains']
    print(f"Platforms: {', '.join(meta.get('platforms') or ['unknown'])}  |  Frameworks: {', '.join(meta.get('frameworks') or ['unknown'])}")
    print(f"Scope: {'all domains' if scope=='all' else ', '.join(scope)}  |  Generated: {meta['generated_at']}")

    total=cov['surface_items_total']; inj=cov['in_journey']
    scoped=scope!='all'
    head_total,head_inj,n_out=scoped_counts(data) if scoped else (total,inj,0)
    frac=head_inj/head_total if head_total else 0
    style=GREEN if frac>=0.9 else CYAN if frac>=0.75 else YELLOW if frac>=0.5 else RED
    print('\n'+paint('SURFACE COVERAGE',BOLD,color))
    label=f"In journeys (run scope: {', '.join(scope)})" if scoped else 'In journeys'
    print(paint(f"{label}: {head_inj}/{head_total} ({frac*100:.0f}%)  {bar(frac)}",style,color))
    if scoped:
        print(f"Whole repository: {inj}/{total} ({(inj/total*100 if total else 0):.0f}%); "
              f"{n_out} items in domains outside this run were not mapped")
    not_mapped=[o for o in cov['orphans'] if o['reason']=='not_mapped_this_run']
    if not_mapped:
        print(paint(f"{len(not_mapped)} in-scope item(s) have a user path but were not mapped in this run",YELLOW,color))
    counts={k:len(v) for k,v in data['surface'].items()}
    print(f"Found: {counts['routes']} routes, {counts['screens']} screens, {counts['endpoints']} endpoints, "
          f"{counts['entry_points']} entry points, {counts['goals']} goals")
    print(f"Orphans: {len(cov['orphans'])}  |  Unreachable: {len(cov['unreachable'])}")
    print(paint('Surface found by static analysis plus review; dynamically built routes may be missing, so this is a lower bound.',DIM,color))

    untested=[j for j in journeys if j['priority']=='P1' and j['automation']=='none']
    if args.brief:
        by_p={p:sum(1 for j in journeys if j['priority']==p) for p in ('P1','P2','P3')}
        by_a={a:sum(1 for j in journeys if j['automation']==a) for a in ('automated','partial','none')}
        print(f"\nJourneys: {len(journeys)} (P1 {by_p['P1']}, P2 {by_p['P2']}, P3 {by_p['P3']}); automation: "
              f"{by_a['automated']} automated, {by_a['partial']} partial, {by_a['none']} none")
        if untested:
            print(paint('Untested P1 journeys:',BOLD,color))
            for j in untested: print(paint(f"- {j['id']} {j['title']}",RED,color))
        else:
            print('Untested P1 journeys: none')
        nq=len(data.get('open_questions',[]))+sum(len(j.get('open_questions',[])) for j in journeys)
        print(f"Open questions: {nq}")
        print(paint('All journeys are status "inferred": they describe current behaviour and need human review.',DIM,color))
        return 0
    if untested:
        print('\n'+paint('CRITICAL JOURNEYS WITHOUT TESTS',BOLD,color))
        for j in untested:
            print(paint(f"[P1] {j['id']} {j['title']}",RED,color)+f"  ({j['persona']}; {', '.join(j['platforms'])})")
            print('  '+j['priority_reason'])

    print('\n'+paint('JOURNEYS',BOLD,color))
    for j in sorted(journeys,key=lambda j:(PRIORITY_ORDER[j['priority']],j['automation']!='none',j['id'])):
        auto=j['automation']
        line=f"{j['priority']}  {j['id']:14} {j['title'][:46]:46} {len(j['steps']):>2} steps {len(j.get('variants',[])):>2} var  "
        print(paint(line,PRIORITY_STYLE[j['priority']] if j['priority']=='P1' else '',color)+paint(f"[{auto}]",AUTOMATION_STYLE[auto],color))

    if cov['unreachable']:
        print('\n'+paint('UNREACHABLE (not turned into journeys)',BOLD,color))
        for u in cov['unreachable']:
            ev=u['evidence']; print(f"- {u['item']}: {u['reason']}  ({ev['file']}:{ev['line']})")
    if cov['orphans']:
        print('\n'+paint('ORPHANS (surface items with no journey)',BOLD,color))
        by_reason={}
        for o in cov['orphans']: by_reason.setdefault(o['reason'],[]).append(o['item'])
        for reason,items in sorted(by_reason.items()):
            code=MAGENTA if reason in ('no_user_path_found','needs_review','not_mapped_this_run') else DIM
            print(paint(f"- {reason}: {len(items)}",code,color)+f"  {', '.join(items[:8])}{' …' if len(items)>8 else ''}")

    questions=[(q['id'],q['question']) for q in data.get('open_questions',[])]
    questions+=[(j['id'],q) for j in journeys for q in j.get('open_questions',[])]
    if questions:
        print('\n'+paint('OPEN QUESTIONS',BOLD,color))
        for qid,q in questions: print(f"- {qid}: {q}")

    if data.get('graph_declined'):
        print('\n'+paint('GRAPH NOT BUILT',BOLD,color)+f"  {data['graph_declined']}")

    files=data.get('files')
    if files:
        print('\n'+paint('FILES',BOLD,color))
        counts={}
        for f in files: counts[f['action']]=counts.get(f['action'],0)+1
        print(', '.join(f"{v} {k}" for k,v in sorted(counts.items())))
        for f in files:
            if f['action'] in ('proposed','stale'): print(paint(f"  {f['action'].upper()}: {f['path']}",YELLOW,color))
    print('\n'+paint('All journeys are status "inferred": they describe current behaviour and need human review.',DIM,color))
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__=='__main__': raise SystemExit(main())
