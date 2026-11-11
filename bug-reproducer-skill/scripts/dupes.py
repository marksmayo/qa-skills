#!/usr/bin/env python3
"""Score a bug report against other reports for likely duplicates (stdlib only, read-only).

    python dupes.py --ticket ticket.json --key TASK-14 --candidates tickets.json [--out dupes.json]

Candidates come from fetch_ticket.py (a JQL search with --jql, a connector export, or a GitHub
issue list saved as JSON with key, summary, labels and body). Nothing is closed or linked.

    similarity = 0.5 x signature match (0 or 1, signature.match) + 0.3 x summary token Jaccard
                 + 0.2 x same component or label

>= 0.7 likely duplicate, 0.4-0.7 related, below that not listed.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signature  # noqa: E402


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return round(len(sa & sb) / len(sa | sb), 4) if sa | sb else 0.0


def score(t: dict, c: dict) -> dict:
    st, sc = signature.of_ticket(t), signature.of_ticket(c)
    sig = signature.match(st, sc)
    summ = jaccard(signature.terms(t.get('summary') or ''), signature.terms(c.get('summary') or ''))
    groups = lambda x: {g.lower() for g in (x.get('components') or []) + (x.get('labels') or [])}  # noqa: E731
    comp = 1 if groups(t) & groups(c) else 0
    sim = round(0.5 * sig + 0.3 * summ + 0.2 * comp, 4)
    return {'key': c['key'], 'summary': c.get('summary'), 'similarity': sim,
            'components': {'signature': sig, 'summary': summ, 'component': comp},
            'verdict': 'likely_duplicate' if sim >= 0.7 else 'related' if sim >= 0.4 else 'none',
            'shared_terms': sorted(set(st['terms']) & set(sc['terms']))[:12]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--ticket', type=Path, required=True)
    ap.add_argument('--key', required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    load = lambda p: json.loads(p.read_text(encoding='utf-8'))  # noqa: E731
    pool = load(args.ticket).get('tickets', [load(args.ticket)])
    t = next(x for x in pool if x['key'] == args.key)
    cands = [c for c in load(args.candidates).get('tickets', []) if c['key'] != args.key]
    rows = sorted((score(t, c) for c in cands), key=lambda r: -r['similarity'])
    out = {'key': args.key, 'candidates_searched': len(cands), 'results': [r for r in rows if r['verdict'] != 'none']}
    text = json.dumps(out, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    for r in out['results']:
        print(f'{r["key"]}: {r["similarity"]} {r["verdict"]} ({r["summary"]})')
    if not args.out:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
