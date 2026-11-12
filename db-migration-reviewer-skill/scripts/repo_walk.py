#!/usr/bin/env python3
"""Walk a repository's files, pruning dependency, build and cache directories before descending into them.

    python repo_walk.py <repo> [--suffix .sql ...]

Every script that searches the repository walks it through `files()`, so node_modules, virtual
environments, build output and VCS metadata are never entered (walking them and filtering afterwards
took minutes on a JavaScript repository with node_modules installed).
"""
from __future__ import annotations
import os, sys
from pathlib import Path

PRUNE = frozenset({'node_modules', '.git', '.hg', '.svn', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv',
                   '__pycache__', '.turbo', '.cache', '.tox', '.mypy_cache', '.pytest_cache', '.gradle', 'target', 'vendor',
                   '.terraform', '.vercel', '.svelte-kit', 'bower_components'})


def files(root: Path, suffixes: tuple[str, ...] | None = None, names: tuple[str, ...] | None = None,
          extra_prune: frozenset | set = frozenset()):
    """Yield every file under root (sorted per directory), skipping pruned directories without entering them."""
    prune = PRUNE | set(extra_prune)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in prune and not d.endswith('.egg-info'))
        for f in sorted(filenames):
            if suffixes is not None and not f.endswith(suffixes):
                continue
            if names is not None and f not in names and not any(f.startswith(n) for n in names if n.endswith('.')):
                continue
            yield Path(dirpath) / f


if __name__ == '__main__':
    root = Path(sys.argv[1])
    suf = tuple(sys.argv[sys.argv.index('--suffix') + 1:]) if '--suffix' in sys.argv else None
    for p in files(root, suf):
        print(p.relative_to(root).as_posix())
