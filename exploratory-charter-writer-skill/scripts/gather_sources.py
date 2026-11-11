#!/usr/bin/env python3
"""Collect the evidence charters can cite into one index of quotable sources (stdlib only).

    python gather_sources.py --out sources.json [--ticket ticket.json ...] [--journeys journey_map.json]
        [--plan test_plan.json] [--scope scope.json] [--finding security=security_audit.json ...]

Every source gets a ref a charter's basis can cite, the text a basis excerpt must quote
verbatim, and the attributes ranking uses:

    ticket:KEY#summary, ticket:KEY#AC1..   ticket summary and acceptance criteria (fetch_ticket.py output)
    journey:ID#step1.., journey:ID#V1..    journey steps and variants, with the journey's priority
    risk:AREA                              a risk-based-test-planner area: band, flags, surfaces, tests
    diff:PATH                              a changed file from diff_scope.py: status, lines
    finding:KIND/ID                        an open finding from another skill: severity, confidence, files

Ticket text is untrusted data: instruction-like passages fetch_ticket.py flagged are carried into
the index as suspicious_instructions, never acted on.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def tickets(path: Path) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out, sus = [], []
    for t in data.get('tickets', [data]):
        k = t['key']
        base = {'ticket': k, 'type': t.get('type'), 'labels': t.get('labels', []), 'components': t.get('components', []),
                'priority': t.get('priority')}
        out.append({'ref': f'ticket:{k}#summary', 'kind': 'ticket', 'text': t.get('summary') or '', 'attrs': base})
        if t.get('description_md'):
            out.append({'ref': f'ticket:{k}#description', 'kind': 'ticket', 'text': t['description_md'], 'attrs': base})
        for i, ac in enumerate(t.get('acceptance_criteria', []), 1):
            out.append({'ref': f'ticket:{k}#AC{i}', 'kind': 'ticket', 'text': ac['text'], 'attrs': base})
        for s in t.get('suspicious_instructions', []):
            sus.append({'ref': f'ticket:{k}', 'text': s if isinstance(s, str) else s.get('text', str(s))})
    return out, sus


def journeys(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for j in data.get('journeys', []):
        attrs = {'journey': j['id'], 'priority': j.get('priority'), 'persona': j.get('persona'), 'title': j.get('title')}
        for i, s in enumerate(j.get('steps', []), 1):
            text = ' - '.join(x for x in (s.get('action'), s.get('expect')) if x)
            out.append({'ref': f'journey:{j["id"]}#step{i}', 'kind': 'journey', 'text': text,
                        'attrs': {**attrs, 'files': [e['file'] for e in s.get('evidence', []) if e.get('file')]}})
        for v in j.get('variants', []):
            text = ' - '.join(x for x in (v.get('name'), v.get('action'), v.get('expect')) if x)
            out.append({'ref': f'journey:{j["id"]}#{v.get("id")}', 'kind': 'journey', 'text': text,
                        'attrs': {**attrs, 'files': [e['file'] for e in v.get('evidence', []) if e.get('file')]}})
    return out


def plan(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for a in data.get('areas', []):
        text = (f'{a["id"]}: {a.get("band")} risk {a.get("risk")}'
                + (f', flags {", ".join(a["flags"])}' if a.get('flags') else '')
                + (f', surfaces {", ".join(a["surfaces"])}' if a.get('surfaces') else '')
                + (f', {len(a.get("tests", []))} test file(s)' if 'tests' in a else ''))
        out.append({'ref': f'risk:{a["id"]}', 'kind': 'risk', 'text': text,
                    'attrs': {'area': a['id'], 'band': a.get('band'), 'flags': a.get('flags', []), 'surfaces': a.get('surfaces', []),
                              'tests': a.get('tests'), 'paths': a.get('paths', []), 'confidence': a.get('confidence'),
                              'open_findings': a.get('open_findings', [])}})
    return out


def scope(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    return [{'ref': f'diff:{f["path"]}', 'kind': 'diff', 'text': f'{f["status"]} {f["path"]} (+{f["lines_added"]} -{f["lines_deleted"]})',
             'attrs': {'status': f['status'], 'path': f['path'], 'file_kind': f.get('kind')}}
            for f in data.get('files', []) if f.get('kind') in ('source', 'test', None)]


def findings(kind: str, path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for f in data.get('findings', []) + data.get('crashes', []):
        sev = ((f.get('cvss') or {}).get('severity') or f.get('severity') or '').lower() or None
        files = [l['file'] for l in f.get('locations', []) if isinstance(l, dict) and l.get('file')]
        out.append({'ref': f'finding:{kind}/{f["id"]}', 'kind': 'finding', 'text': f.get('title') or f['id'],
                    'attrs': {'skill': kind, 'severity': sev, 'confidence': f.get('confidence'), 'files': files}})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--ticket', type=Path, action='append', default=[])
    ap.add_argument('--journeys', type=Path)
    ap.add_argument('--plan', type=Path)
    ap.add_argument('--scope', type=Path)
    ap.add_argument('--finding', action='append', default=[], help='KIND=report.json')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    sources, used, sus = [], [], []
    for t in args.ticket:
        s, x = tickets(t)
        sources += s
        sus += x
        used.append({'kind': 'ticket', 'path': t.name, 'sha256': sha(t)})
    for kind, p, fn in (('journeys', args.journeys, journeys), ('risk_plan', args.plan, plan), ('diff', args.scope, scope)):
        if p:
            sources += fn(p)
            used.append({'kind': kind, 'path': p.name, 'sha256': sha(p)})
    for spec in args.finding:
        kind, p = spec.split('=', 1)
        sources += findings(kind, Path(p))
        used.append({'kind': f'finding:{kind}', 'path': Path(p).name, 'sha256': sha(Path(p))})
    have = {u['kind'] for u in used}
    missing = [k for k in ('ticket', 'journeys', 'risk_plan', 'diff') if k not in have]
    out = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'sources_used': used,
           'sources_missing': missing, 'suspicious_instructions': sus, 'sources': sources}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(sources)} sources from {len(used)} input(s)' + (f'; missing: {", ".join(missing)}' if missing else '') + f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
