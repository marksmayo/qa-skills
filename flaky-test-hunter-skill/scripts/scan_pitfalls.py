#!/usr/bin/env python3
"""List static flake smells in a repository's tests (stdlib only).

    python scan_pitfalls.py <repo> [--out smells.json]

Applies data/pitfalls.json to test files (and, for smells with scope "source", to non-test
source files that tests import). Every hit is a lead with a file, line and the cause class it
usually leads to. A smell is never a flake on its own: it becomes one only when runs show a
pass and a fail. Smells whose tests never failed stay in the report as smells.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / 'data' / 'pitfalls.json'
SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', 'target', '.pytest_cache', '.tox'}
TEST_NAME = re.compile(r'^\s*(?:async\s+)?def\s+(test_\w+)|^\s*(?:it|test)\s*\(\s*[\'"`]([^\'"`]+)|'
                       r'^\s*(?:public\s+)?(?:void|fun)\s+(\w+)\s*\(|^func\s+(Test\w+)\s*\(')


def files(root: Path):
    for p in sorted(root.rglob('*')):
        if p.is_file() and not any(part in SKIP for part in p.relative_to(root).parts):
            yield p


def enclosing_test(lines: list[str], i: int) -> str | None:
    for j in range(i, -1, -1):
        m = TEST_NAME.match(lines[j])
        if m:
            return next(g for g in m.groups() if g)
    return None


def scan(root: Path, data: dict) -> list[dict]:
    test_rx = [re.compile(p) for p in data['test_file_patterns']]
    smells = [{**s, 'rx': re.compile(s['pattern'])} for s in data['smells']]
    out = []
    for p in files(root):
        lang = data['language_by_suffix'].get(p.suffix)
        if not lang:
            continue
        rel = p.relative_to(root).as_posix()
        is_test = any(rx.search(rel) for rx in test_rx)
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(('#', '//', '*', '/*')):
                continue
            for sm in smells:
                if lang not in sm['languages']:
                    continue
                if (sm.get('scope') == 'source') == is_test:
                    continue
                if sm['rx'].search(line):
                    out.append({'id': f'SM-{len(out) + 1}', 'smell': sm['id'], 'file': rel, 'line': i + 1,
                                'excerpt': s[:160], 'cause': sm['cause'], 'message': sm['message'], 'fix': sm['fix'],
                                'test': enclosing_test(lines, i) if is_test else None, 'in_test_file': is_test})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    data = json.loads(DATA.read_text(encoding='utf-8'))
    hits = scan(args.repo.resolve(), data)
    result = {'pitfalls_version': data['version'], 'smells': hits}
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    by: dict[str, int] = {}
    for h in hits:
        by[h['smell']] = by.get(h['smell'], 0) + 1
    print(f"{len(hits)} smell(s): " + (', '.join(f'{v} {k}' for k, v in sorted(by.items())) or 'none'), file=sys.stderr)
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
