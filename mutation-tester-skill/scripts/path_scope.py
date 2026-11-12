#!/usr/bin/env python3
"""Make a scope.json for a path scope: every line of the files you name (stdlib only).

    python path_scope.py --repo <repo> --path src/billing [--path src/auth/rules.py:40-120] --out scope.json

The output has the same shape as diff_scope.py's, so builtin_mutator.py, run_engine.py and
normalize_results.py read it unchanged. Each --path is a file or a directory under --repo,
optionally with a line range for a single file (`file.py:40-120`). Directories are walked with
caches, virtual environments, node_modules, build output and VCS folders pruned before descending.

Every file is classified like diff_scope.py does (source, test, generated, migration, vendored,
types, config, docs, binary, other), so a directory scope still mutates only source files; the
others are listed with the reason. Each file's `ranges` are all its lines (or the range given),
its `status` is "unchanged" (nothing is diffed), and its functions come from the shared scanner.
The scope records `source.kind: "path"` and `source.paths` for the report's `metadata.scope`
(`mode: "path"`, `paths`).
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import diff_scope  # noqa: E402

PRUNE = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv', 'env', '.tox', '.nox', '.mypy_cache',
         '.pytest_cache', '.ruff_cache', 'dist', 'build', '.next', 'coverage', 'htmlcov', '.idea', '.vscode', 'site-packages'}
SPEC = re.compile(r'^(?P<path>.+?):(?P<a>\d+)-(?P<b>\d+)$')


def walk(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not d.endswith('.egg-info'))
        out += [Path(dirpath) / f for f in sorted(filenames)]
    return out


def entry_for(repo: Path, p: Path, span: tuple[int, int] | None) -> dict:
    rel = p.resolve().relative_to(repo).as_posix()
    try:
        body = p.read_text(encoding='utf-8')
        binary = False
    except (UnicodeDecodeError, OSError):
        body, binary = None, True
    n = len(body.splitlines()) if body is not None else 0
    kind, why = diff_scope.classify(rel, '\n'.join((body or '').splitlines()[:5]), binary)
    if span:
        a, b = max(1, span[0]), min(n, span[1])
        rng = [[a, b]] if n and a <= b else []
    else:
        rng = [[1, n]] if n else []
    entry = {'path': rel, 'old_path': None, 'status': 'unchanged', 'kind': kind, 'kind_reason': why,
             'ranges': rng, 'deleted_at': [], 'lines_added': 0, 'lines_deleted': 0, 'present': body is not None}
    diff_scope.attach_functions(entry, body)
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--path', action='append', required=True,
                    help='file or directory relative to --repo; a file may take a line range, file.py:40-120 (repeatable)')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    files, seen, paths = [], set(), []
    for spec in args.path:
        m = SPEC.match(spec)
        raw, span = (m.group('path'), (int(m.group('a')), int(m.group('b')))) if m else (spec, None)
        p = (repo / raw).resolve()
        try:
            p.relative_to(repo)
        except ValueError:
            raise SystemExit(f'{spec}: not inside {repo}')
        if not p.exists():
            raise SystemExit(f'{spec}: no such file or directory under the repo')
        if span and not p.is_file():
            raise SystemExit(f'{spec}: a line range needs a file, not a directory')
        rel = p.relative_to(repo).as_posix() or '.'
        paths.append(rel + (f':{span[0]}-{span[1]}' if span else ''))
        for f in ([p] if p.is_file() else walk(p)):
            key = f.resolve()
            if key in seen:
                continue
            seen.add(key)
            files.append(entry_for(repo, f, span))
    by_kind: dict[str, int] = {}
    for f in files:
        by_kind[f['kind']] = by_kind.get(f['kind'], 0) + 1
    out = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'repo': repo.name,
           'source': {'kind': 'path', 'paths': paths, 'diff_file': None, 'base_ref': None, 'merge_base': None, 'head': None},
           'files': files,
           'summary': {'files': len(files), 'by_status': {'unchanged': len(files)} if files else {}, 'by_kind': by_kind,
                       'lines_in_scope': sum(b - a + 1 for f in files if f['kind'] == 'source' for a, b in f['ranges']),
                       'lines_added': 0, 'lines_deleted': 0}}
    text = json.dumps(out, indent=2) + '\n'
    if not any(f['kind'] == 'source' and f['ranges'] for f in files):
        print('WARNING: no source lines in scope (every file is a test, config, docs or other kind)', file=sys.stderr)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
        print(f'{len(files)} files ({", ".join(f"{v} {k}" for k, v in sorted(by_kind.items()))}); '
              f'{out["summary"]["lines_in_scope"]} source lines in scope -> {args.out}')
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
