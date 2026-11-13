#!/usr/bin/env python3
"""Find files a test run leaves behind in the working tree (stdlib only).

    python residue.py snapshot <copy> --out <run>/tree.before.json [--also home=<isolated home>]
    ... run the tests in <copy> ...
    python residue.py diff <copy> --before <run>/tree.before.json --out <run>/residue.json [--also home=<same>]

Tests that write uploads, exports, caches or databases into the repository share that state
across runs and machines, and a file under a served directory (public/, static/) can reach
users. `snapshot` records every file's size and SHA-256; `diff` lists what was added, changed
or removed, grouped by directory. The walk prunes dependency, build and VCS directories before
descending (node_modules, .git, .next, dist, build, coverage, virtualenvs and caches).

Tests also write outside the tree: caches, downloads and settings under the home directory or a
temp folder. `--also LABEL=DIR` (repeatable) snapshots another directory too; its files are listed
as `LABEL:relative/path`, never by the directory's own path. Point HOME (and USERPROFILE on
Windows) at an empty folder for the test run and pass `--also home=<that folder>`: a person's real
home is slow to walk and its paths are theirs. `diff` needs the same labels as the snapshot.

The diff is evidence for a `residue` finding (source: agent, basis {"evidence": "residue.json"}).
It names paths only; it never copies a file's contents.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from pathlib import Path, PurePosixPath

PRUNE = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__', '.turbo',
         '.cache', '.pytest_cache', '.mypy_cache', '.tox', '.nox', 'test-results', 'playwright-report', '.svelte-kit', '.gradle'}
SERVED = ('public', 'static', 'wwwroot', 'www', 'htdocs', 'uploads')


def snapshot(root: Path, prune: set[str] = PRUNE) -> dict[str, list]:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in prune and not d.endswith('.egg-info'))
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


def snapshot_all(root: Path, also: dict[str, Path] | None = None) -> dict[str, list]:
    """The tree's snapshot plus each --also directory's, its paths prefixed with the label (home:.toolcache/x). Nothing
    is pruned in an --also directory: a cache a test fills there (.cache, node_modules) is the residue."""
    out = snapshot(root)
    for label, d in sorted((also or {}).items()):
        out.update({f'{label}:{k}': v for k, v in snapshot(d, {'.git'}).items()})
    return out


def parse_also(items: list[str]) -> dict[str, Path]:
    out = {}
    for it in items:
        label, sep, d = it.partition('=')
        if not sep or not label.isidentifier() or not d:
            raise SystemExit(f'--also takes LABEL=DIR (a word, then the directory): {it!r}')
        if not Path(d).is_dir():
            raise SystemExit(f'--also {label}: not a directory')
        out[label] = Path(d)
    return out


def diff(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p][1] != after[p][1])
    groups: dict[str, int] = {}
    for p in added + changed:
        d = PurePosixPath(p).parent.as_posix()
        groups[d] = groups.get(d, 0) + 1
    served = sorted(p for p in added + changed if ':' not in p.split('/')[0] and set(PurePosixPath(p).parts[:-1]) & set(SERVED))
    outside = sorted({p.split(':', 1)[0] for p in added + changed if ':' in p.split('/')[0]})
    return {'added': added, 'changed': changed, 'removed': removed,
            'by_directory': dict(sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))), 'in_served_directories': served,
            'outside_tree': outside,
            'summary': {'added': len(added), 'changed': len(changed), 'removed': len(removed), 'served': len(served)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('root', type=Path)
    s.add_argument('--out', type=Path, required=True)
    s.add_argument('--also', action='append', default=[], metavar='LABEL=DIR',
                   help='also snapshot DIR (an isolated HOME or temp folder), listed as LABEL:path; repeatable')
    d = sub.add_parser('diff')
    d.add_argument('root', type=Path)
    d.add_argument('--before', type=Path, required=True)
    d.add_argument('--out', type=Path, required=True)
    d.add_argument('--also', action='append', default=[], metavar='LABEL=DIR', help='the same --also labels as the snapshot')
    args = ap.parse_args(argv)
    also = parse_also(args.also)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.cmd == 'snapshot':
        snap = snapshot_all(args.root, also)
        args.out.write_text(json.dumps({'root': args.root.name, 'also': sorted(also), 'files': snap}, indent=1) + '\n',
                            encoding='utf-8')
        print(f'{len(snap)} files -> {args.out}')
        return 0
    doc = json.loads(args.before.read_text(encoding='utf-8'))
    if sorted(doc.get('also', [])) != sorted(also):
        print(f'the snapshot was taken with --also labels {sorted(doc.get("also", []))}; pass the same to diff', file=sys.stderr)
        return 2
    res = diff(doc['files'], snapshot_all(args.root, also))
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["added"]} added, {s["changed"]} changed, {s["removed"]} removed ({s["served"]} in served directories) -> {args.out}')
    for k, n in list(res['by_directory'].items())[:10]:
        print(f'  {k}/: {n}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
