#!/usr/bin/env python3
"""Check that scripts which import their sibling scripts never leave a __pycache__ folder in the skill they ship in.

A skill is installed by copying one directory, often read-only, and is run in place: `python <skill>/scripts/x.py`.
When x.py imports a sibling (parse_results, term_safe ...), Python writes `scripts/__pycache__/*.pyc` beside it
unless told not to. A script that imports a sibling must therefore set

    sys.dont_write_bytecode = True

at module level before its first sibling import (the flag is read when a module is imported, so it must come first;
it costs nothing else, and PYTHONDONTWRITEBYTECODE=1 does the same from outside).

    python tools/check_bytecode.py                  # shared/scripts must comply (exit 1 otherwise); skill scripts
                                                    # that still need the line are listed, not failed
    python tools/check_bytecode.py --all            # skill scripts must comply too
    python tools/check_bytecode.py --only <folder>  # one folder (a skill, or shared), which must comply
    python tools/check_bytecode.py --fix [--all | --only <folder>]   # insert the line where it is missing
    python tools/check_bytecode.py --self-test

--fix never edits a skill's copy of a shared file (tools/check_shared.py --fix copies the canonical one).
"""
from __future__ import annotations
import argparse, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG_LINE = 'sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings'
SCRIPT_DIRS = ('scripts', 'plugins')


def _imports(node) -> list[tuple[str, int]]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out += [(a.name.split('.')[0], n.lineno) for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            out.append((n.module.split('.')[0], n.lineno))
    return out


def _sets_flag(stmt) -> bool:
    return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute)
            and stmt.targets[0].attr == 'dont_write_bytecode' and isinstance(stmt.targets[0].value, ast.Name)
            and stmt.targets[0].value.id == 'sys' and isinstance(stmt.value, ast.Constant) and stmt.value.value is True)


def inspect(path: Path, siblings: set[str]) -> dict | None:
    """What a script needs: None when it imports no sibling, else {siblings, first_line (of the first module-level
    statement that imports one, None when all are inside functions), flag_line (where the flag is set at module
    level, or None), ok, insert_at (1-based line to insert the flag before), needs_sys}."""
    try:
        text = path.read_text(encoding='utf-8')
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None
    mine = sorted({name for name, _ in _imports(tree) if name in siblings and name != path.stem})
    if not mine:
        return None
    first_stmt = flag_line = None
    last_import_end = 0
    sys_line = None
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _sets_flag(stmt) and flag_line is None:
            flag_line = stmt.lineno
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if first_stmt is None:
                last_import_end = stmt.end_lineno
            if isinstance(stmt, ast.Import) and any(a.name == 'sys' and not a.asname for a in stmt.names) \
                    and sys_line is None:
                sys_line = stmt.lineno
        if first_stmt is None and any(name in mine for name, _ in _imports(stmt)):
            first_stmt = stmt.lineno
    ok = flag_line is not None and (first_stmt is None or flag_line < first_stmt)
    insert_at = first_stmt if first_stmt is not None else last_import_end + 1
    if not insert_at or insert_at < 1:
        insert_at = (tree.body[0].end_lineno + 1) if tree.body else 1
    return {'siblings': mine, 'first_line': first_stmt, 'flag_line': flag_line, 'ok': ok, 'insert_at': insert_at,
            'needs_sys': sys_line is None or sys_line >= insert_at}


def fix(path: Path, info: dict) -> None:
    raw = path.read_bytes()
    nl = '\r\n' if b'\r\n' in raw else '\n'
    lines = raw.decode('utf-8').split(nl)
    at = info['insert_at'] - 1
    indent = lines[at][:len(lines[at]) - len(lines[at].lstrip())] if at < len(lines) and info['first_line'] else ''
    new = ([indent + 'import sys'] if info['needs_sys'] else []) + [indent + FLAG_LINE]
    lines[at:at] = new
    path.write_bytes(nl.join(lines).encode('utf-8'))


def folders(root: Path) -> list[Path]:
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d.name == 'shared' or d.name.endswith('-skill')):
            out += [d / s for s in SCRIPT_DIRS if (d / s).is_dir()]
    return out


def shared_copies(root: Path) -> set[Path]:
    """Skill files that are copies of a shared file (never edited in place)."""
    sys.path.insert(0, str(root / 'tools'))
    try:
        import check_shared
        mapping = check_shared.SHARED
    except Exception:   # noqa: BLE001
        mapping = {}
    finally:
        sys.path.pop(0)
    out = set()
    for rel, skills in mapping.items():
        for sk in skills:
            name, _, other = sk.partition(':')
            out.add((root / name / (other or rel)).resolve())
    return out


def scan(root: Path, only: str | None = None) -> list[dict]:
    rows = []
    for d in folders(root):
        top = d.relative_to(root).parts[0]
        if only and top != only.strip('/\\'):
            continue
        siblings = {p.stem for p in d.glob('*.py')}
        for p in sorted(d.glob('*.py')):
            info = inspect(p, siblings)
            if info:
                rows.append({'path': p, 'rel': p.relative_to(root).as_posix(), 'folder': top, **info})
    return rows


def self_test() -> int:
    import shutil, tempfile
    fails = []
    tmp = Path(tempfile.mkdtemp(prefix='check-bytecode-'))
    try:
        sc = tmp / 'kite-skill' / 'scripts'
        sc.mkdir(parents=True)
        (sc / 'helper.py').write_text('X = 1\n', encoding='utf-8')
        (sc / 'plain.py').write_text('import json, sys\nprint(json.dumps(sys.argv))\n', encoding='utf-8')
        (sc / 'top.py').write_text('"""Doc."""\nfrom __future__ import annotations\nimport json, sys\nfrom pathlib import Path\n'
                                   'sys.path.insert(0, str(Path(__file__).parent))\nimport helper\n', encoding='utf-8')
        (sc / 'lazy.py').write_text('"""Doc."""\nimport json\n\n\ndef run():\n    import helper\n    return helper.X\n',
                                    encoding='utf-8')
        (sc / 'tried.py').write_text('import os\ntry:\n    import helper\nexcept ImportError:\n    helper = None\n',
                                     encoding='utf-8')
        (sc / 'late.py').write_text('import sys\nimport helper\nsys.dont_write_bytecode = True\n', encoding='utf-8')
        (sc / 'good.py').write_text('import sys\nsys.dont_write_bytecode = True\nimport helper\n', encoding='utf-8')
        rows = {r['rel'].rsplit('/', 1)[-1]: r for r in scan(tmp)}
        if sorted(rows) != ['good.py', 'late.py', 'lazy.py', 'top.py', 'tried.py']:
            fails.append(f'only scripts that import a sibling are checked: {sorted(rows)}')
        bad = sorted(k for k, r in rows.items() if not r['ok'])
        if bad != ['late.py', 'lazy.py', 'top.py', 'tried.py']:
            fails.append(f'a script is wrong when the flag is missing or set after the sibling import: {bad}')
        for r in rows.values():
            if not r['ok']:
                fix(r['path'], r)
        again = scan(tmp)
        if [r['rel'] for r in again if not r['ok']]:
            fails.append(f'--fix makes every script comply: {[r["rel"] for r in again if not r["ok"]]}')
        for name in ('top.py', 'lazy.py', 'tried.py', 'late.py'):
            text = (sc / name).read_text(encoding='utf-8')
            try:
                compile(text, name, 'exec')
            except SyntaxError as e:
                fails.append(f'{name} no longer compiles after --fix: {e}')
        if (sc / 'lazy.py').read_text(encoding='utf-8').splitlines()[2:4] != ['import sys', FLAG_LINE]:
            fails.append(f'a script that never imported sys gets the import too: {(sc / "lazy.py").read_text(encoding="utf-8")!r}')
        import subprocess
        res = subprocess.run([sys.executable, str(sc / 'top.py')], capture_output=True, text=True,
                             env={k: v for k, v in __import__('os').environ.items() if k != 'PYTHONDONTWRITEBYTECODE'})
        if res.returncode or (sc / '__pycache__').exists():
            fails.append(f'a fixed script runs and leaves no __pycache__: exit {res.returncode}, {res.stderr[-200:]}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('check_bytecode self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('check_bytecode self-test: ok')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--all', action='store_true', help='skill scripts must comply too')
    ap.add_argument('--only', metavar='FOLDER', help='check one top-level folder (it must comply)')
    ap.add_argument('--fix', action='store_true', help='insert the line where it is missing')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    rows = scan(ROOT, args.only)
    copies = shared_copies(ROOT)
    bad = [r for r in rows if not r['ok']]
    if args.fix:
        fixed = 0
        for r in bad:
            must = args.all or bool(args.only) or r['folder'] == 'shared'
            if must and r['path'].resolve() not in copies:
                fix(r['path'], r)
                fixed += 1
                print(f'FIXED {r["rel"]}')
        print(f'{fixed} script(s) fixed; run tools/check_shared.py --fix to copy the shared ones into the skills')
        rows = scan(ROOT, args.only)
        bad = [r for r in rows if not r['ok']]
    failing, pending = [], []
    for r in bad:
        if r['path'].resolve() in copies:
            continue      # a copy of a shared file: judged on the canonical one
        (failing if args.all or args.only or r['folder'] == 'shared' else pending).append(r)
    for r in failing:
        where = f'line {r["first_line"]}' if r['first_line'] else 'inside a function'
        print(f'MISSING {r["rel"]}: imports {", ".join(r["siblings"])} ({where}) without sys.dont_write_bytecode = True '
              f'before it')
    if pending:
        print(f'{len(pending)} skill script(s) still import siblings without the flag (not failed; --all fails them, '
              f'--fix --only <skill> inserts the line):')
        for r in pending:
            print(f'  {r["rel"]}')
    if failing:
        print(f'{len(failing)} script(s) would write __pycache__ into the skill folder.')
        return 1
    print(f'{len(rows) - len(bad)} of {len(rows)} sibling-importing script(s) set the flag; none that must is missing it.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
