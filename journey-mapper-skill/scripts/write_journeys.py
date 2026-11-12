#!/usr/bin/env python3
"""Write Journey Markdown files, index.md, coverage.md and journey-graph.json from a validated
journey_map.json. Idempotent: re-runs update files by journey ID and never duplicate them.

Merge rules (see JOURNEY_FORMAT.md):
- A file whose status is reviewed/approved is never overwritten; the new version goes next to it
  as <name>.proposed.md.
- The human-owned ## Notes section is preserved; `tests` lists are merged, not replaced.
- Journey files are never deleted. IDs in scope that the map no longer contains are reported as stale.

    python write_journeys.py journeys/journey_map.json --out journeys
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from journey_md import ORDER, JourneyError, is_journey_file, load, render  # noqa: E402

PRIORITY_ORDER = {'P1': 0, 'P2': 1, 'P3': 2}


def slug(text: str, limit: int = 48) -> str:
    """Lowercase hyphenated slug, cut at a word boundary (never mid-word) when longer than limit."""
    s = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    if len(s) > limit:
        cut = s[:limit + 1]
        s = cut[:cut.rfind('-')] if '-' in cut else s[:limit]
    return s.rstrip('-') or 'journey'

def ref(ev: dict) -> str:
    return f"`{ev['file']}:{ev['line']}`" + (f" — {ev['note']}" if ev.get('note') else '')

def to_doc(jm: dict) -> dict:
    meta = {k: jm[k] for k in ORDER if k in jm}
    meta['status'] = 'inferred'
    meta.setdefault('automation', 'none')
    meta.setdefault('tests', [])
    evidence = [{'step': n, 'text': ref(ev)} for n, s in enumerate(jm['steps'], 1) for ev in s.get('evidence', [])]
    evidence += [{'step': None, 'text': f"{v['id']}: {ref(ev)}"} for v in jm.get('variants', []) for ev in v.get('evidence', [])]
    evidence += [{'step': None, 'text': ref(ev)} for ev in jm.get('evidence', [])]
    return {
        'meta': meta,
        'preconditions': jm.get('preconditions', []),
        'steps': [{'action': s['action'], 'expect': s['expect']} for s in jm['steps']],
        'variants': [{'id': v['id'], 'name': v['name'], 'from_step': v['from_step'],
                      'action': v.get('action', ''), 'expect': v['expect']} for v in jm.get('variants', [])],
        'test_data': jm.get('test_data', []),
        'not_automatable': jm.get('not_automatable', []),
        'open_questions': jm.get('open_questions', []),
        'notes': '',
        'evidence': evidence,
    }

def existing_journeys(out: Path) -> dict:
    found = {}
    if not out.is_dir():
        return found
    for f in sorted(out.rglob('*.md')):
        if f.name.endswith('.proposed.md') or not is_journey_file(f):
            continue
        try:
            j = load(f)
        except (JourneyError, OSError) as exc:
            print(f'WARNING: skipping unparseable journey {f}: {exc}', file=sys.stderr)
            continue
        if j['meta'].get('id'):
            found.setdefault(j['meta']['id'], (f, j))
    return found

def merge_graph(out: Path, graph: dict | None) -> dict | None:
    path = out / 'journey-graph.json'
    old = {'nodes': [], 'edges': []}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            pass
    if graph is None:
        return old if old['nodes'] else None
    nodes = {n['id']: n for n in old.get('nodes', [])}
    nodes.update({n['id']: n for n in graph.get('nodes', [])})
    edges = {(e['from'], e['to'], e['action']): e for e in old.get('edges', [])}
    edges.update({(e['from'], e['to'], e['action']): e for e in graph.get('edges', [])})
    return {'nodes': list(nodes.values()), 'edges': [e for e in edges.values() if e['from'] in nodes and e['to'] in nodes]}

def mermaid(graph: dict, domain: str) -> list[str]:
    nodes = [n for n in graph['nodes'] if n.get('domain') == domain]
    ids = {n['id'] for n in nodes}
    edges = [e for e in graph['edges'] if e['from'] in ids or e['to'] in ids]
    if not edges:
        return []
    safe = lambda s: re.sub(r'\W', '_', s)
    label = lambda s: s.replace('"', "'")
    shape = {'entry': ('(["', '"])'), 'goal': ('{{"', '"}}'), 'state': ('("', '")'), 'screen': ('["', '"]')}
    lookup = {n['id']: n for n in graph['nodes']}
    lines = ['```mermaid', 'flowchart LR']
    for nid in sorted({e['from'] for e in edges} | {e['to'] for e in edges}):
        n = lookup[nid]
        a, b = shape.get(n.get('kind'), ('["', '"]'))
        lines.append(f"  {safe(nid)}{a}{label(n['label'])}{b}")
    for e in edges:
        guard = f" [{', '.join(e['guards'])}]" if e.get('guards') else ''
        lines.append(f"  {safe(e['from'])} -->|\"{label(e['action'] + guard)}\"| {safe(e['to'])}")
    lines.append('```')
    return lines

def write_index(out: Path, graph: dict | None, diagrams: bool) -> str:
    rows = []
    for jid, (f, j) in existing_journeys(out).items():
        rows.append((j['meta'], f.relative_to(out).as_posix()))
    proposals = sorted(p.relative_to(out).as_posix() for p in out.rglob('*.proposed.md'))
    by_domain: dict[str, list] = {}
    for meta, rel in rows:
        by_domain.setdefault(meta.get('domain') or 'unassigned', []).append((meta, rel))
    total = len(rows)
    untested_p1 = sum(1 for m, _ in rows if m.get('priority') == 'P1' and m.get('automation', 'none') == 'none')
    statuses = {s: sum(1 for m, _ in rows if m.get('status') == s) for s in ('inferred', 'reviewed', 'approved')}
    lines = ['# User journeys', '',
             f'{total} journeys across {len(by_domain)} domains. '
             f'Status: {statuses["inferred"]} inferred, {statuses["reviewed"]} reviewed, {statuses["approved"]} approved. '
             f'**P1 journeys with no automated tests: {untested_p1}.**', '',
             '> Journeys marked `inferred` were derived from the code and describe how the software behaves '
             'today, not how it is meant to behave. A person should review each one and set `status: reviewed`.', '']
    if proposals:
        lines += ['## Pending proposals', '', 'These reviewed/approved journeys have a newer generated version waiting to be compared:', '']
        lines += [f'- [{p}]({p})' for p in proposals] + ['']
    for domain in sorted(by_domain):
        items = sorted(by_domain[domain], key=lambda x: (PRIORITY_ORDER.get(x[0].get('priority'), 9), x[0].get('id', '')))
        lines += [f'## {domain}', '', '| ID | Journey | Persona | Priority | Platforms | Automation | Status |',
                  '|---|---|---|---|---|---|---|']
        for m, rel in items:
            lines.append(f"| {m.get('id')} | [{m.get('title')}]({rel}) | {m.get('persona')} | {m.get('priority')} | "
                         f"{', '.join(m.get('platforms') or [])} | {m.get('automation', 'none')} | {m.get('status')} |")
        lines.append('')
        if diagrams and graph:
            lines += mermaid(graph, domain) + ['']
    return '\n'.join(lines).rstrip() + '\n'

def write_coverage(d: dict) -> str:
    cov = d['coverage']
    covers: dict[str, list[str]] = {}
    for j in d['journeys']:
        for c in j['covers']:
            covers.setdefault(c, []).append(j['id'])
    orphan = {o['item']: o for o in cov['orphans']}
    unreach = {u['item']: u for u in cov['unreachable']}
    total, inj = cov['surface_items_total'], cov['in_journey']
    pct = f'{inj / total * 100:.0f}%' if total else 'n/a'
    scope = d['metadata']['run_scope']['domains']
    measures = [f'| Surface items found | {total} |']
    if scope != 'all':
        n_out = sum(1 for o in cov['orphans'] if o['reason'] == 'out_of_scope')
        in_scope = total - n_out
        spct = f'{inj / in_scope * 100:.0f}%' if in_scope else 'n/a'
        measures += [f'| In this run\'s scope ({", ".join(scope)}) | {in_scope} |',
                     f'| In at least one journey, of the run\'s scope | {inj} ({spct}) |',
                     f'| In at least one journey, of the whole surface | {inj} ({pct}) |',
                     f'| Out of scope (other domains, not mapped) | {n_out} |']
    else:
        measures += [f'| In at least one journey | {inj} ({pct}) |']
    not_mapped = sum(1 for o in cov['orphans'] if o['reason'] == 'not_mapped_this_run')
    if not_mapped:
        measures += [f'| In scope with a user path, not mapped this run | {not_mapped} |']
    lines = ['# Journey coverage of the application surface', '',
             f"Run scope: {'all domains' if scope == 'all' else ', '.join(scope)}. Generated {d['metadata']['generated_at']}.", '',
             '| Measure | Count |', '|---|---|', *measures,
             f"| Orphans (no journey) | {len(cov['orphans'])} |", f"| Unreachable | {len(cov['unreachable'])} |", '',
             '> The surface was found by static analysis plus agent review. Items the analysis could not see '
             '(e.g. dynamically built routes) are not counted, so this is a lower bound on the real surface.', '']
    titles = {'routes': 'Routes', 'screens': 'Screens', 'endpoints': 'API endpoints', 'entry_points': 'Entry points', 'goals': 'Goal events'}
    for bucket, title in titles.items():
        items = d['surface'][bucket]
        if not items:
            continue
        lines += [f'## {title}', '', '| ID | Name | Location | Status |', '|---|---|---|---|']
        for it in items:
            sid = it['id']
            if sid in covers:
                status = ', '.join(covers[sid])
            elif sid in unreach:
                status = f"unreachable ({unreach[sid]['reason']})"
            elif sid in orphan:
                status = f"**orphan** ({orphan[sid]['reason']})" + (f" — {orphan[sid]['note']}" if orphan[sid].get('note') else '')
            else:
                status = '**unaccounted**'
            name = it['name'].replace('|', '\\|')
            lines.append(f"| {sid} | `{name}` | `{it['file']}:{it['line']}` | {status} |")
        lines.append('')
    questions = [(q['id'], q['question']) for q in d.get('open_questions', [])]
    questions += [(j['id'], q) for j in d['journeys'] for q in j.get('open_questions', [])]
    if questions:
        lines += ['## Open questions', ''] + [f'- **{qid}:** {q}' for qid, q in questions] + ['']
    return '\n'.join(lines).rstrip() + '\n'

def main() -> int:
    ap = argparse.ArgumentParser(description='Write journey files from a journey_map.json.')
    ap.add_argument('journey_map', type=Path)
    ap.add_argument('--out', type=Path, default=Path('journeys'))
    ap.add_argument('--no-diagrams', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='report what would change without writing')
    args = ap.parse_args()

    d = json.loads(args.journey_map.read_text(encoding='utf-8'))
    out = args.out
    existing = existing_journeys(out)
    actions: list[dict] = []

    def write(path: Path, text: str):
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def rel(p: Path) -> str:
        return p.as_posix()

    map_ids = set()
    for jm in d['journeys']:
        jid = jm['id']; map_ids.add(jid)
        doc = to_doc(jm)
        if jid in existing:
            path, old = existing[jid]
            merged = list(dict.fromkeys(list(old['meta'].get('tests') or []) + doc['meta']['tests']))
            doc['meta']['tests'] = merged
            doc['notes'] = old.get('notes', '')
            if old['meta'].get('status') in ('reviewed', 'approved'):
                comparable = json.loads(json.dumps(doc)); comparable['meta']['status'] = old['meta']['status']
                if render(comparable) == render(old):
                    actions.append({'path': rel(path), 'journey': jid, 'action': 'unchanged'})
                else:
                    prop = path.with_name(path.name[:-3] + '.proposed.md')
                    write(prop, render(doc))
                    actions.append({'path': rel(prop), 'journey': jid, 'action': 'proposed'})
                continue
            text = render(doc)
            if text == render(old):
                actions.append({'path': rel(path), 'journey': jid, 'action': 'unchanged'})
            else:
                write(path, text)
                actions.append({'path': rel(path), 'journey': jid, 'action': 'updated'})
        else:
            path = out / jm['domain'] / f"{jid}-{slug(jm['title'])}.md"
            write(path, render(doc))
            actions.append({'path': rel(path), 'journey': jid, 'action': 'created'})

    scope = d['metadata']['run_scope']['domains']
    for jid, (path, j) in sorted(existing.items()):
        if jid not in map_ids and (scope == 'all' or j['meta'].get('domain') in scope):
            actions.append({'path': rel(path), 'journey': jid, 'action': 'stale'})

    graph = merge_graph(out, d.get('graph'))
    if graph:
        write(out / 'journey-graph.json', json.dumps(graph, indent=2, ensure_ascii=False) + '\n')
        actions.append({'path': rel(out / 'journey-graph.json'), 'action': 'generated'})
    else:
        why = d.get('graph_declined') or 'the map has no graph section'
        print(f'NOTE: journey-graph.json not written ({why}); index.md has no flow diagrams', file=sys.stderr)
    if not args.dry_run:
        write(out / 'index.md', write_index(out, graph, not args.no_diagrams))
        write(out / 'coverage.md', write_coverage(d))
    actions += [{'path': rel(out / 'index.md'), 'action': 'generated'}, {'path': rel(out / 'coverage.md'), 'action': 'generated'}]

    if not args.dry_run:
        d['files'] = actions
        args.journey_map.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    counts: dict[str, int] = {}
    for a in actions:
        counts[a['action']] = counts.get(a['action'], 0) + 1
    print(('DRY RUN: ' if args.dry_run else '') + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
    for a in actions:
        if a['action'] in ('proposed', 'stale'):
            print(f"  {a['action'].upper()}: {a['path']}")
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
