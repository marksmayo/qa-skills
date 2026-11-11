#!/usr/bin/env python3
"""Load requirements from normalised Jira tickets and/or a requirements document (stdlib only).

    python load_requirements.py --tickets tickets.json [--doc requirements.md] --out requirements.json

Tickets: tickets.json from the shared fetch_ticket.py (one ticket or a search result); each ticket is a
requirement and its acceptance criteria are traced as <key>/AC-n. Issue links, comments and attachment
names are kept as link evidence (attachments are listed, never downloaded).
Document: Markdown or text where each requirement is a heading or a line starting with its id
(`## REQ-NFR-1 Points calculation time`); the text up to the next requirement is its statement, and a
bullet list under it becomes its criteria.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

DOC_ID = re.compile(r'^(?:#{1,6}\s*)?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+)\b[\s:.-]*(.*)$')


def from_tickets(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding='utf-8'))
    tickets = d['tickets'] if 'tickets' in d else [d]
    out = []
    for t in tickets:
        out.append({'id': t['key'], 'source': 'jira', 'title': t['summary'], 'priority': t.get('priority'), 'type': t.get('type'),
                    'status': t.get('status'), 'statement': (t.get('description_md') or '')[:600],
                    'criteria': [{'id': f'{t["key"]}/{a["id"]}', 'text': a['text']} for a in t.get('acceptance_criteria', [])],
                    'links': t.get('links', []), 'comments': [{'author': c.get('author'), 'created': c.get('created'), 'text': c.get('body_md', '')} for c in t.get('comments', [])],
                    'attachments': [a.get('filename') for a in t.get('attachments', [])], 'content_sha256': t.get('content_sha256')})
    return out


def from_doc(path: Path) -> list[dict]:
    lines = path.read_text(encoding='utf-8').splitlines()
    out, cur = [], None
    for n, line in enumerate(lines, 1):
        m = DOC_ID.match(line.strip())
        if m and (line.lstrip().startswith('#') or line[:1].isalpha()):
            cur = {'id': m.group(1), 'source': f'document:{path.name}', 'title': m.group(2).strip(), 'priority': None, 'type': 'Requirement',
                   'status': None, 'statement': '', 'criteria': [], 'links': [], 'comments': [], 'attachments': [], 'line': n}
            out.append(cur)
            continue
        if cur is None:
            continue
        if line.lstrip().startswith(('- ', '* ')):
            cur['criteria'].append({'id': f'{cur["id"]}/AC-{len(cur["criteria"]) + 1}', 'text': line.strip()[2:].strip()})
        elif line.strip() and not line.lstrip().startswith('#'):
            cur['statement'] = (cur['statement'] + ' ' + line.strip()).strip()
    for r in out:
        if not r['criteria']:
            r['criteria'] = [{'id': f'{r["id"]}/AC-1', 'text': r['statement']}]
        r['content_sha256'] = hashlib.sha256((r['title'] + '\n' + r['statement']).encode()).hexdigest()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tickets', type=Path)
    ap.add_argument('--doc', type=Path, action='append', default=[])
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    if not a.tickets and not a.doc:
        raise SystemExit('give --tickets and/or --doc')
    reqs = (from_tickets(a.tickets) if a.tickets else []) + [r for d in a.doc for r in from_doc(d)]
    ids = [r['id'] for r in reqs]
    if len(ids) != len(set(ids)):
        raise SystemExit(f'duplicate requirement ids: {sorted({i for i in ids if ids.count(i) > 1})}')
    out = {'sources': {'tickets': a.tickets.as_posix() if a.tickets else None, 'documents': [d.as_posix() for d in a.doc]}, 'requirements': reqs}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(reqs)} requirement(s), {sum(len(r["criteria"]) for r in reqs)} criteria -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
