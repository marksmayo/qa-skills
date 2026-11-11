#!/usr/bin/env python3
"""Copy accepted current screenshots over their baselines, from the user's decisions in review.md.

    python apply_review.py run/review.md --groups run/groups.json --diff run/diff.json [--dry-run]

Run this only when the user asks. It reads the ticked boxes (`- [x] accept G3`, `- [x] reject G4`) and,
for each pair, overwrites the baseline image with the current one only when every group with an
instance on that pair is accepted, noise included (an unticked or rejected group on the same pair
would be accepted silently otherwise). Groups ticked both ways are treated as undecided. Nothing is
committed; the files written are printed, and should be listed in the report's files_written.
"""
from __future__ import annotations
import argparse, json, re, shutil, sys
from pathlib import Path

TICK = re.compile(r'^\s*-\s*\[[xX]\]\s*(accept|reject)\s+(G\d+)\b')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('review', type=Path)
    ap.add_argument('--groups', type=Path, required=True)
    ap.add_argument('--diff', type=Path, required=True)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    decisions: dict[str, set] = {}
    for line in a.review.read_text(encoding='utf-8').splitlines():
        m = TICK.match(line)
        if m:
            decisions.setdefault(m.group(2), set()).add(m.group(1))
    accepted = {g for g, d in decisions.items() if d == {'accept'}}
    groups = json.loads(a.groups.read_text(encoding='utf-8'))['groups']
    pairs = {p['rel']: p for p in json.loads(a.diff.read_text(encoding='utf-8'))['pairs']}
    on_pair: dict[str, set] = {}
    for g in groups:
        for inst in g['instances']:
            on_pair.setdefault(inst['pair'], set()).add(g['id'])
    written, skipped = [], []
    for rel, gids in sorted(on_pair.items()):
        if gids <= accepted:
            p = pairs[rel]
            if not a.dry_run:
                shutil.copyfile(p['current'], p['baseline'])
            written.append(p['baseline'])
        else:
            skipped.append(f'{rel}: {", ".join(sorted(gids - accepted))} not accepted')
    for w in written:
        print(f'{"would write" if a.dry_run else "wrote"} {w}')
    for s in skipped:
        print(f'kept baseline {s}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
