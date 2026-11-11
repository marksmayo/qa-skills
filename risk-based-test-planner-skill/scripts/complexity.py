#!/usr/bin/env python3
"""Measure size and branching per source file, as a cyclomatic proxy (stdlib only).

    python complexity.py <repo> --out complexity.json

Python files are parsed with `ast`: each if/elif, loop, except handler, boolean operator operand,
conditional expression, comprehension condition and match case counts as one branch. Other
languages (JS/TS, Java, Kotlin, Go, C#, Swift, Dart, Ruby, PHP) use a keyword count on code with
strings and comments removed: if, else if, for, while, case, catch, &&, ||, ?: and ??. Lines of
code exclude blank lines and comments. Each file records which method was used.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions  # noqa: E402

CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.kt', '.go', '.cs', '.swift', '.dart', '.rb', '.php',
            '.scala', '.rs'}
KEYWORDS = re.compile(r'\b(if|for|while|case|catch|elif|elsif|when|guard)\b|&&|\|\||\?\?|\?(?=[^.:?]*:)')
STRINGS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`', re.S)
COMMENTS = re.compile(r'/\*.*?\*/|//[^\n]*|#[^\n]*', re.S)


def py_branches(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.With)):
            n += 0 if isinstance(node, ast.With) else 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            n += len(node.ifs)
        elif hasattr(ast, 'match_case') and isinstance(node, ast.match_case):
            n += 1
    return n


def measure(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return None
    if path.suffix == '.py':
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        loc = len([l for l in text.splitlines() if l.strip() and not l.strip().startswith('#')])
        if tree is not None:
            return {'loc': loc, 'branches': py_branches(tree), 'method': 'ast'}
    code = COMMENTS.sub('', STRINGS.sub('""', text))
    loc = len([l for l in code.splitlines() if l.strip()])
    return {'loc': loc, 'branches': len(KEYWORDS.findall(code)), 'method': 'keywords'}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    files = {}
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or p.suffix not in CODE_EXT or set(Path(rel).parts) & (detect_conventions.SKIP_DIRS | {'.git'}):
            continue
        m = measure(p)
        if m:
            files[rel] = m
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'repo': root.name, 'files': files}, indent=2) + '\n', encoding='utf-8')
    print(f'{len(files)} files, {sum(f["loc"] for f in files.values())} lines of code, '
          f'{sum(f["branches"] for f in files.values())} branches -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
