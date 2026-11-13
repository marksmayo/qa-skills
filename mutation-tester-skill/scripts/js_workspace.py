#!/usr/bin/env python3
"""JavaScript workspace helpers for running a package's tests outside its own directory (stdlib only).

A package inside a monorepo (pnpm, npm or Yarn workspaces) often reaches outside itself: its
tsconfig `extends` "../../tsconfig.base.json", and pnpm's node_modules hold *relative* links
("pixelmatch -> ../../../node_modules/.pnpm/...", "@scope/core -> ../../core"). A copy of the
package placed anywhere else breaks both; on Windows even a junction to the package's
node_modules does not help, because Windows resolves a relative link against the path it was
reached through, not against the real directory.

`build_mirror` therefore recreates the workspace's directory levels in a scratch directory:
at each level from the workspace root down to the package's parent, the files are copied
and every other directory (sibling packages, the root node_modules) is linked, never copied.
A copy of the package (or a Stryker sandbox) placed at the package's own depth in the mirror
then resolves every `../..` exactly as the original does. `remove_tree` deletes a mirror
without ever following a link into the real workspace.

    python js_workspace.py <package dir>       # print what the helpers see (root, manager, versions)
"""
from __future__ import annotations
import json, os, shutil, stat, sys
from pathlib import Path

WS_MARKERS = ('pnpm-workspace.yaml', 'lerna.json', 'nx.json', 'turbo.json', 'rush.json')
MIRROR_SKIP = {'.git', '.hg', '.svn'}
MAX_COPY = 5 * 1024 * 1024          # a root-level file larger than this is linked, not copied


def read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def is_link(p: Path) -> bool:
    """A symlink or a Windows junction (never descend into one when deleting)."""
    if os.path.islink(p):
        return True
    isj = getattr(os.path, 'isjunction', None)
    if isj is not None:
        return isj(p)
    try:
        return bool(os.lstat(p).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)   # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False


def link_dir(src: Path, dst: Path) -> None:
    """A directory link: a symlink, or a junction where Windows refuses symlinks."""
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))


def unlink(p: Path) -> None:
    """Remove a link or junction itself, never what it points at."""
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)        # a junction


def remove_tree(root: Path) -> None:
    """Delete a scratch tree, unlinking every link or junction in it instead of following it."""
    if not os.path.lexists(root):
        return
    if is_link(root):
        unlink(root)
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        d = Path(dirpath)
        for n in list(dirnames):
            if is_link(d / n):
                unlink(d / n)
                dirnames.remove(n)
        for n in filenames:
            if is_link(d / n):
                unlink(d / n)
    shutil.rmtree(root, ignore_errors=True)


def workspace_root(pkg: Path) -> Path | None:
    """The nearest ancestor of a package that is a workspace root (pnpm-workspace.yaml, a package.json
    with "workspaces", lerna/nx/turbo/rush), or None when the package is not inside one."""
    pkg = pkg.resolve()
    for d in pkg.parents:
        if any((d / m).is_file() for m in WS_MARKERS) or 'workspaces' in read_json(d / 'package.json'):
            return d
        if (d / '.git').exists():
            return None          # the repository root, and no workspace marker on the way
    return None


def package_manager(pkg: Path) -> str:
    for d in (pkg.resolve(), *pkg.resolve().parents):
        if (d / 'pnpm-lock.yaml').is_file() or (d / 'pnpm-workspace.yaml').is_file():
            return 'pnpm'
        if (d / 'yarn.lock').is_file():
            return 'yarn'
        if (d / 'package-lock.json').is_file() or (d / '.git').exists():
            return 'npm'
    return 'npm'


def find_module(pkg: Path, name: str) -> Path | None:
    """node_modules/<name> as Node would find it from the package (its own node_modules, then upwards)."""
    for d in (pkg.resolve(), *pkg.resolve().parents):
        p = d / 'node_modules' / name
        if (p / 'package.json').is_file():
            return p
    return None


def module_version(pkg: Path, name: str) -> str | None:
    p = find_module(pkg, name)
    return read_json(p / 'package.json').get('version') if p else None


def major(version: str | None) -> int | None:
    try:
        return int(str(version).lstrip('^~v').split('.')[0])
    except (ValueError, TypeError):
        return None


def install_command(pkg: Path, packages: list[str]) -> str:
    """How to add dev dependencies to this package with its own package manager (shown, never run)."""
    pm, root = package_manager(pkg), workspace_root(pkg)
    names = ' '.join(packages)
    name = read_json(pkg / 'package.json').get('name') or pkg.name
    if pm == 'pnpm':
        return f'pnpm add -D --filter {name} {names}' if root else f'pnpm add -D {names}'
    if pm == 'yarn':
        return f'yarn workspace {name} add -D {names}' if root else f'yarn add -D {names}'
    return f'npm i -D -w {name} {names}' if root else f'npm i -D {names}'


def build_mirror(pkg: Path, root: Path, dest: Path) -> Path:
    """Recreate the workspace levels from `root` down to the package's parent in `dest`; return the path
    the package itself would have in the mirror (not created). Files are copied, directories linked."""
    pkg, root = pkg.resolve(), root.resolve()
    rel = pkg.relative_to(root).parts
    remove_tree(dest)
    real, mirror = root, dest
    for i, part in enumerate(rel):
        mirror.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(real):
            if entry.name == part or entry.name in MIRROR_SKIP:
                continue
            src = Path(entry.path)
            if entry.is_dir():
                link_dir(src, mirror / entry.name)
            elif entry.is_file():
                if entry.stat().st_size > MAX_COPY:
                    try:
                        os.symlink(src, mirror / entry.name)
                        continue
                    except OSError:
                        pass
                shutil.copy2(src, mirror / entry.name)
        real, mirror = real / part, mirror / part
    return mirror


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    pkg = Path(argv[0]).resolve()
    out = {'package': str(pkg), 'workspace_root': str(workspace_root(pkg) or ''), 'package_manager': package_manager(pkg),
           'versions': {n: module_version(pkg, n) for n in ('typescript', 'vitest', 'jest', '@stryker-mutator/core')}}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
