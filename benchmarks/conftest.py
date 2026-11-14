"""Shared helpers for the benchmarks: make the stdlib scripts importable without installing anything.

The canonical shared scripts live in shared/scripts/. Skill-specific scripts are loaded by path under
a unique module name, because several skills ship scripts with the same file name.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / 'shared' / 'scripts'

sys.dont_write_bytecode = True  # never leave __pycache__ inside skill folders
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))


def load_script(skill: str, name: str):
    """Import <skill>/scripts/<name>.py as module '<skill>.<name>' (siblings resolve from its own folder)."""
    path = ROOT / skill / 'scripts' / f'{name}.py'
    mod_name = f'bench_{skill.replace("-", "_")}_{name}'
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    here = str(path.parent)
    if here not in sys.path:
        sys.path.insert(1, here)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def read_tree(root: Path, suffixes: tuple[str, ...]) -> dict[str, str]:
    """Relative POSIX path -> text for every file under root with one of the suffixes."""
    return {p.relative_to(root).as_posix(): p.read_text(encoding='utf-8')
            for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in suffixes}
