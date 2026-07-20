#!/usr/bin/env python3
from __future__ import annotations
import json, sys
from pathlib import Path

def fail(msg): print(f"INVALID: {msg}",file=sys.stderr); raise SystemExit(1)

def main():
    if len(sys.argv)!=2: fail('usage: validate_evaluation.py evaluation.json')
    try: d=json.loads(Path(sys.argv[1]).read_text())
    except Exception as e: fail(str(e))
    for key in ('metadata','headline','dimensions','critical_findings','recommendations','evidence_inventory'):
        if key not in d: fail(f'missing {key}')
    score=d['headline'].get('overall_score')
    if not isinstance(score,(int,float)) or not 1<=score<=10: fail('overall_score must be 1..10')
    ids=set()
    for dim in d['dimensions']:
        if dim.get('id') in ids: fail(f"duplicate dimension id {dim.get('id')}")
        ids.add(dim.get('id'))
        s=dim.get('score')
        if s is not None and (not isinstance(s,int) or not 1<=s<=10): fail(f"bad score in {dim.get('id')}")
    print('VALID')
if __name__=='__main__': main()
