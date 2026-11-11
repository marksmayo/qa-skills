#!/usr/bin/env python3
"""Validate exploratory charters against the schema, their sources and the ranking rules (stdlib only).

    python validate_charters.py charters.json --sources sources.json [--md-dir charters/] [--personas journey_map.json]

Checks: every charter has a basis, and every basis ref exists in sources.json with its excerpt
quoted verbatim (whitespace and quote style aside); a user_request basis is allowed only when
there are no sources at all; impact, uncertainty, priority and group recompute from the cited
sources; charters are sorted by group and priority; heuristics, oracles and personas are known
(data/heuristics.json, data/personas.json, or the journey map's personas); no two charters share
a target and first heuristic; at most max_charters; each Markdown charter exists and carries the
mission line.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rank_charters, schema_check  # noqa: E402

SCHEMA = json.loads((HERE.parent / 'schemas' / 'charters.schema.json').read_text(encoding='utf-8'))
HEUR = json.loads((HERE.parent / 'data' / 'heuristics.json').read_text(encoding='utf-8'))
PERSONAS = json.loads((HERE.parent / 'data' / 'personas.json').read_text(encoding='utf-8'))['personas']


def squash(s: str) -> str:
    return re.sub(r'\s+', ' ', s.replace('“', '"').replace('”', '"').replace('’', "'")).strip().lower()


def check(doc: dict, sources: dict, md_dir: Path | None, journey_personas: set[str]) -> list[str]:
    errs = []
    src = {s['ref']: s for s in sources['sources']}
    for c in doc['charters']:
        w = f'charters[{c["id"]}]'
        for b in c['basis']:
            if b['kind'] == 'user_request':
                if src:
                    errs.append(f'{w}: a user_request basis is only for when no sources were found; cite the sources')
                continue
            s = src.get(b['ref'])
            if not s:
                errs.append(f'{w}: basis {b["ref"]} is not in the sources file')
                continue
            if s['kind'] != b['kind']:
                errs.append(f'{w}: basis {b["ref"]} is a {s["kind"]} source, not {b["kind"]}')
            if squash(b['excerpt']) not in squash(s['text']):
                errs.append(f'{w}: excerpt for {b["ref"]} is not verbatim from the source')
        want = json.loads(json.dumps(c))
        rank_charters.rank([want], src)
        for k in ('impact', 'uncertainty', 'priority', 'group'):
            if c[k] != want[k]:
                errs.append(f'{w}.{k}: {c[k]!r} but the cited sources give {want[k]!r}')
        for h in c['heuristics']:
            if h not in HEUR['heuristics'] and h not in HEUR['oracles']:
                errs.append(f'{w}: unknown heuristic {h!r} (data/heuristics.json)')
        for o in c.get('oracles', []):
            if o not in HEUR['oracles']:
                errs.append(f'{w}: unknown oracle {o!r}')
        for p in c['personas']:
            if p not in PERSONAS and p not in journey_personas:
                errs.append(f'{w}: unknown persona {p!r} (data/personas.json or the journey map)')
        if md_dir is not None:
            p = md_dir / f'{c["id"]}.md'
            if not p.is_file():
                errs.append(f'{w}: {p.name} is missing')
            elif c['mission']['target'] not in p.read_text(encoding='utf-8'):
                errs.append(f'{w}: {p.name} does not carry the mission line')
    order = [(rank_charters.GROUP_ORDER[c['group']], -(c['priority'] or 0)) for c in doc['charters']]
    if order != sorted(order):
        errs.append('charters: must be sorted by group, then priority')
    seen = {}
    for c in doc['charters']:
        key = (rank_charters.norm(c['mission']['target']), tuple(c['heuristics'][:1]))
        if key in seen:
            errs.append(f'charters[{c["id"]}]: same target and first heuristic as {seen[key]}; merge them')
        seen[key] = c['id']
    if len(doc['charters']) > doc['metadata']['max_charters']:
        errs.append(f'charters: more than max_charters ({doc["metadata"]["max_charters"]})')
    if [u['kind'] for u in doc['metadata']['sources_used']] != [u['kind'] for u in sources['sources_used']]:
        errs.append('metadata.sources_used: differs from the sources file')
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('charters', type=Path)
    ap.add_argument('--sources', type=Path, required=True)
    ap.add_argument('--md-dir', type=Path)
    ap.add_argument('--personas', type=Path, help='journey map whose personas are also allowed')
    args = ap.parse_args(argv)
    doc = json.loads(args.charters.read_text(encoding='utf-8'))
    errs = schema_check.errors(doc, SCHEMA)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    jp = set()
    if args.personas:
        jp = {j.get('persona') for j in json.loads(args.personas.read_text(encoding='utf-8')).get('journeys', []) if j.get('persona')}
    errs = check(doc, json.loads(args.sources.read_text(encoding='utf-8')), args.md_dir, jp)
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    groups = {}
    for c in doc['charters']:
        groups[c['group']] = groups.get(c['group'], 0) + 1
    print(f'VALID: {len(doc["charters"])} charters (' + ', '.join(f'{v} {k}' for k, v in groups.items()) + ')' +
          (f', {len(doc["merged"])} merged' if doc['merged'] else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
