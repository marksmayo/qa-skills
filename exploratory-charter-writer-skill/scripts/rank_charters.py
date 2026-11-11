#!/usr/bin/env python3
"""Rank exploratory charters by impact x uncertainty from the sources they cite, merge duplicates, write Markdown (stdlib only).

    python rank_charters.py draft.json --sources sources.json --out charters.json [--md-dir charters/] [--max 8]

draft.json holds the charters you wrote: {"timebox": 90, "charters": [{id, mission{target, resources,
information}, areas[], heuristics[], oracles[], personas[], setup[], risks[], out_of_scope[], timebox?,
basis[{kind, ref, excerpt}]}]}. Ranking is computed from the cited sources only (RUBRIC.md):

    impact       3: a High risk area, a P0/P1 journey, or a money, access or personal-data surface
                 2: a Medium risk area or a P2 journey
                 1: otherwise; not_assessed when the only basis is the user's request
    uncertainty  3: new code (a novel area, a risk area with no tests, an added file) or an open finding
                 2: changed code, or a risk area with only some tests
                 1: otherwise
    priority     impact x uncertainty; 6-9 do_first, 3-4 next, 1-2 if_time; unranked without impact

Two charters with the same target and the same first heuristic are merged into the first one
(its lists gain the other's items, and merged_from records the id). Groups are sorted by
priority, then by the number of distinct sources cited, then by id.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = (HERE.parent / 'templates' / 'charter.md').read_text(encoding='utf-8')
HIGH_SURFACES = {'money', 'access', 'personal_data', 'destructive'}
GROUP_ORDER = {'do_first': 0, 'next': 1, 'if_time': 2, 'unranked': 3}
LIST_FIELDS = ('areas', 'heuristics', 'oracles', 'personas', 'setup', 'risks', 'out_of_scope', 'basis')


def impact_of(basis: list[dict], src: dict) -> tuple[int | None, list[str]]:
    hits3, hits2, why = [], [], []
    for b in basis:
        s = src.get(b['ref'])
        if not s:
            continue
        a = s['attrs']
        if s['kind'] == 'risk':
            if a.get('band') == 'High':
                hits3.append(f'{b["ref"]} is a High risk area')
            elif a.get('band') == 'Medium':
                hits2.append(f'{b["ref"]} is a Medium risk area')
            if set(a.get('surfaces') or []) & HIGH_SURFACES:
                hits3.append(f'{b["ref"]} has a {", ".join(sorted(set(a["surfaces"]) & HIGH_SURFACES))} surface')
        elif s['kind'] == 'journey':
            if a.get('priority') in ('P0', 'P1'):
                hits3.append(f'{b["ref"]} is on a {a["priority"]} journey')
            elif a.get('priority') == 'P2':
                hits2.append(f'{b["ref"]} is on a P2 journey')
        elif s['kind'] == 'finding' and (a.get('severity') or '') in ('critical', 'high'):
            hits3.append(f'{b["ref"]} is a {a["severity"]} finding')
    if not any(b['kind'] != 'user_request' for b in basis):
        return None, ['the only basis is the user\'s request']
    if hits3:
        return 3, hits3
    if hits2:
        return 2, hits2
    return 1, ['no High or Medium area, P0-P2 journey or sensitive surface among the sources']


def uncertainty_of(basis: list[dict], src: dict) -> tuple[int, list[str]]:
    hits3, hits2 = [], []
    for b in basis:
        s = src.get(b['ref'])
        if not s:
            continue
        a = s['attrs']
        if s['kind'] == 'risk':
            if 'novel' in (a.get('flags') or []):
                hits3.append(f'{b["ref"]} is new code')
            elif a.get('tests') == []:
                hits3.append(f'{b["ref"]} has no tests')
            elif a.get('open_findings'):
                hits3.append(f'{b["ref"]} has open findings')
            else:
                hits2.append(f'{b["ref"]} has some tests')
        elif s['kind'] == 'diff':
            (hits3 if a.get('status') == 'added' else hits2).append(f'{b["ref"]} is {a.get("status")} in the change')
        elif s['kind'] == 'finding':
            hits3.append(f'{b["ref"]} is an open finding')
    if hits3:
        return 3, hits3
    if hits2:
        return 2, hits2
    return 1, ['no new or changed code and no open finding among the sources']


def group_of(priority: int | None) -> str:
    if priority is None:
        return 'unranked'
    return 'do_first' if priority >= 6 else 'next' if priority >= 3 else 'if_time'


def rank(charters: list[dict], src: dict) -> None:
    for c in charters:
        i, iwhy = impact_of(c['basis'], src)
        u, uwhy = uncertainty_of(c['basis'], src)
        c['impact'], c['uncertainty'] = i, u
        c['priority'] = i * u if i is not None else None
        c['group'] = group_of(c['priority'])
        c['ranking_reasons'] = {'impact': iwhy, 'uncertainty': uwhy}
    charters.sort(key=lambda c: (GROUP_ORDER[c['group']], -(c['priority'] or 0),
                                 -len({b['ref'] for b in c['basis']}), c['id']))


def norm(s: str) -> str:
    return re.sub(r'\W+', ' ', s.lower()).strip()


def merge(charters: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, merged = [], []
    for c in charters:
        twin = next((k for k in kept if norm(k['mission']['target']) == norm(c['mission']['target'])
                     and k['heuristics'][:1] == c['heuristics'][:1]), None)
        if twin:
            for f in LIST_FIELDS:
                for item in c.get(f, []):
                    if item not in twin.setdefault(f, []):
                        twin[f].append(item)
            twin.setdefault('merged_from', []).append(c['id'])
            merged.append({'id': c['id'], 'into': twin['id'], 'reason': 'same target and first heuristic'})
        else:
            kept.append(c)
    return kept, merged


def md(c: dict, heur: dict, personas: dict) -> str:
    bullet = lambda xs: '\n'.join(f'- {x}' for x in xs) or '- (none)'  # noqa: E731
    names = lambda ids, table: ', '.join(table.get(i, {}).get('name', i) for i in ids) or '-'  # noqa: E731
    label = {'do_first': 'Do first', 'next': 'Next', 'if_time': 'If time', 'unranked': 'Unranked'}[c['group']]
    return TEMPLATE.format(
        id=c['id'], target=c['mission']['target'], resources=c['mission']['resources'], information=c['mission']['information'],
        group=label, impact=c['impact'] if c['impact'] is not None else 'not assessed', uncertainty=c['uncertainty'],
        priority=c['priority'] if c['priority'] is not None else '-', timebox=c['timebox'],
        personas=names(c['personas'], personas), heuristics=names(c['heuristics'], heur),
        oracles=names(c.get('oracles', []), heur),
        areas=bullet(c['areas']), setup=bullet(c['setup']), risks=bullet(c['risks']), out_of_scope=bullet(c['out_of_scope']),
        basis=bullet([f'`{b["ref"]}`: "{b["excerpt"]}"' for b in c['basis']]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('draft', type=Path)
    ap.add_argument('--sources', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--md-dir', type=Path)
    ap.add_argument('--max', type=int, default=8)
    args = ap.parse_args(argv)
    draft = json.loads(args.draft.read_text(encoding='utf-8'))
    sources = json.loads(args.sources.read_text(encoding='utf-8'))
    src = {s['ref']: s for s in sources['sources']}
    timebox = draft.get('timebox', 90)
    charters = [dict(c, timebox=c.get('timebox', timebox)) for c in draft['charters']]
    for c in charters:
        for f in LIST_FIELDS:
            c.setdefault(f, [])
    charters, merged = merge(charters)
    rank(charters, src)
    dropped = charters[args.max:]
    charters = charters[:args.max]
    data = json.loads((HERE.parent / 'data' / 'heuristics.json').read_text(encoding='utf-8'))
    heur = {**data['heuristics'], **data['oracles']}
    personas = json.loads((HERE.parent / 'data' / 'personas.json').read_text(encoding='utf-8'))['personas']
    if args.md_dir:
        args.md_dir.mkdir(parents=True, exist_ok=True)
        for c in charters:
            (args.md_dir / f'{c["id"]}.md').write_text(md(c, heur, personas), encoding='utf-8', newline='\n')
    out = {'metadata': {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'timebox': timebox,
                        'max_charters': args.max, 'sources_file': args.sources.name,
                        'sources_used': sources['sources_used'], 'sources_missing': sources['sources_missing'],
                        'suspicious_instructions': sources.get('suspicious_instructions', [])},
           'charters': charters, 'merged': merged,
           'over_limit': [{'id': c['id'], 'group': c['group']} for c in dropped]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    for c in charters:
        print(f'{c["id"]:<5} {c["group"]:<9} {c["impact"]} x {c["uncertainty"]} = {c["priority"]}  {c["mission"]["target"]}')
    if merged:
        print('merged: ' + ', '.join(f'{m["id"]} into {m["into"]}' for m in merged))
    return 0


if __name__ == '__main__':
    sys.exit(main())
