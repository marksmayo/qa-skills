#!/usr/bin/env python3
"""Walk a repository for the i18n scans: prune before descending, and record every directory left out and why.

dist/, build/ and out/ are build output only when something in the repository builds them: a package.json with
a build script, a bundler or framework config (vite, webpack, rollup, parcel, next, nuxt, astro, svelte, angular,
gatsby), a tsconfig outDir naming the folder, or a .gitignore entry for it. A committed, hand-written front end in
dist/ that a server mounts as static files has none of those, so it is scanned as source. --include DIR (repeatable)
scans a skipped directory anyway. public/ and www/ are always scanned.

    python repo_walk.py <repo> [--include dist]     # prints the files scanned and the directories skipped
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

# never product source: dependencies, VCS, caches, virtual environments
ALWAYS = {'node_modules', '.git', 'vendor', '__pycache__', 'Pods', '.venv', 'venv', '.tox', 'bower_components',
          '.next', '.nuxt', '.svelte-kit', '.cache', '.mypy_cache', '.pytest_cache'}
BUILD_NAMES = {'dist', 'build', 'out'}
BUNDLER = re.compile(r'^(vite|webpack|rollup|parcel|next|nuxt|astro|svelte|gatsby|esbuild|tsup|snowpack)\.config\.[cm]?[jt]s$|^angular\.json$|^\.parcelrc$')


def _ignored(repo: Path) -> set[str]:
    out = set()
    gi = repo / '.gitignore'
    if gi.is_file():
        for ln in gi.read_text(encoding='utf-8', errors='replace').splitlines():
            ln = ln.strip().strip('/')
            if ln and not ln.startswith(('#', '!')) and '*' not in ln:
                out.add(ln)
    return out


def build_reason(repo: Path, rel_dir: str) -> str | None:
    """Why rel_dir (dist, build or out, at any depth) is build output, or None when nothing builds it."""
    name = rel_dir.rsplit('/', 1)[-1]
    if name not in BUILD_NAMES:
        return None
    root = repo / rel_dir
    if rel_dir in _ignored(repo) or name in _ignored(repo):
        return f'{rel_dir}/ is listed in .gitignore (build output)'
    for d in [root.parent, *root.parent.parents]:
        pkg = d / 'package.json'
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text(encoding='utf-8')).get('scripts') or {}
            except (ValueError, OSError):
                scripts = {}
            if any(k == 'build' or k.startswith('build:') for k in scripts):
                return f'{rel_dir}/ is build output: {pkg.relative_to(repo).as_posix()} has a build script'
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        cfg = next((n for n in names if BUNDLER.match(n)), None)
        if cfg:
            return f'{rel_dir}/ is build output: {(d / cfg).relative_to(repo).as_posix()} configures a bundler'
        ts = d / 'tsconfig.json'
        if ts.is_file() and re.search(r'"outDir"\s*:\s*"\.?/?' + re.escape(name) + r'/?"', ts.read_text(encoding='utf-8', errors='replace')):
            return f'{rel_dir}/ is build output: tsconfig.json outDir'
        if d == repo:
            break
    return None


def walk(repo: Path, include=()) -> tuple[list[Path], list[dict]]:
    """Every file under repo, pruned before descending. Returns (files, skipped), where each skipped entry is
    {dir, reason, files}."""
    repo = Path(repo)
    inc = {i.strip('/').replace('\\', '/') for i in include if i}
    files, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(repo):
        d = Path(dirpath)
        rel_d = d.relative_to(repo).as_posix()
        keep = []
        for n in sorted(dirnames):
            rel = n if rel_d == '.' else f'{rel_d}/{n}'
            if rel in inc or n in inc:
                keep.append(n)
                continue
            reason = (f'{rel}/ is a dependency, VCS or cache directory' if n in ALWAYS else build_reason(repo, rel))
            if reason:
                count = sum(len(f) for _, _, f in os.walk(d / n)) if n not in ('.git', 'node_modules') else None
                skipped.append({'dir': rel, 'reason': reason, 'files': count})
            else:
                keep.append(n)
        dirnames[:] = keep
        files += [d / f for f in sorted(filenames)]
    return files, skipped


def report_skipped(skipped: list[dict], tool: str) -> None:
    """One stderr line per skipped directory that may hold product code (build output), so nothing is dropped silently."""
    for s in skipped:
        if s['dir'].rsplit('/', 1)[-1] in BUILD_NAMES:
            print(f'{tool}: skipped {s["dir"]}/ ({s["files"]} file(s)): {s["reason"]}; pass --include {s["dir"]} to scan it',
                  file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('repo', type=Path)
    ap.add_argument('--include', action='append', default=[])
    a = ap.parse_args()
    files, skipped = walk(a.repo, a.include)
    print(json.dumps({'files': len(files), 'skipped_dirs': skipped}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
