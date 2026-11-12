#!/usr/bin/env python3
"""Find files a test run leaves behind in the working tree (stdlib only).

    python residue.py snapshot <copy> --out <run>/tree.before.json
    ... run the tests in <copy> ...
    python residue.py diff <copy> --before <run>/tree.before.json --out <run>/residue.json

Tests that write uploads, exports, caches or databases into the repository share that state
across runs and machines, and a file under a served directory (public/, static/) can reach
users. `snapshot` records every file's size and SHA-256; `diff` lists what was added, changed
or removed, grouped by directory. The walk prunes dependency, build and VCS directories before
descending (node_modules, .git, .next, dist, build, coverage, virtualenvs and caches).

The diff is evidence for a `residue` finding (source: agent, basis {"evidence": "residue.json"}).
It names paths only; it never copies a file's contents.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from pathlib import Path, PurePosixPath

PRUNE = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__', '.turbo',
         '.cache', '.pytest_cache', '.mypy_cache', '.tox', '.nox', 'test-results', 'playwright-report', '.svelte-kit', '.gradle'}
SERVED = ('public', 'static', 'wwwroot', 'www', 'htdocs', 'uploads')


def snapshot(root: Path) -> dict[str, list]:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in PRUNE and not d.endswith('.egg-info'))
        for f in fns:
            p = Path(dp) / f
            try:
                size = p.stat().st_size
                h = hashlib.sha256()
                with open(p, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b''):
                        h.update(chunk)
            except OSError:
                continue
            out[p.relative_to(root).as_posix()] = [size, h.hexdigest()[:16]]
    return out


def diff(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p][1] != after[p][1])
    groups: dict[str, int] = {}
    for p in added + changed:
        d = PurePosixPath(p).parent.as_posix()
        groups[d] = groups.get(d, 0) + 1
    served = sorted(p for p in added + changed if set(PurePosixPath(p).parts[:-1]) & set(SERVED))
    return {'added': added, 'changed': changed, 'removed': removed,
            'by_directory': dict(sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))), 'in_served_directories': served,
            'summary': {'added': len(added), 'changed': len(changed), 'removed': len(removed), 'served': len(served)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('root', type=Path)
    s.add_argument('--out', type=Path, required=True)
    d = sub.add_parser('diff')
    d.add_argument('root', type=Path)
    d.add_argument('--before', type=Path, required=True)
    d.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.cmd == 'snapshot':
        snap = snapshot(args.root)
        args.out.write_text(json.dumps({'root': args.root.name, 'files': snap}, indent=1) + '\n', encoding='utf-8')
        print(f'{len(snap)} files -> {args.out}')
        return 0
    before = json.loads(args.before.read_text(encoding='utf-8'))['files']
    res = diff(before, snapshot(args.root))
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["added"]} added, {s["changed"]} changed, {s["removed"]} removed ({s["served"]} in served directories) -> {args.out}')
    for k, n in list(res['by_directory'].items())[:10]:
        print(f'  {k}/: {n}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
