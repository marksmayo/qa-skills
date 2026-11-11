#!/usr/bin/env python3
"""Find code that still uses a column a migration drops or renames, or inserts rows without a column made NOT NULL.

    python find_readers.py --repo examples/ledger --table transactions --column amount [--writers] [--other-readers ../reports]

Readers: the identifier with word boundaries in Python, JavaScript/TypeScript, Ruby, Java, Kotlin, Go and SQL
files - ORM field declarations (`amount = Column(`), raw SQL strings, query builders and attribute access.
Writers (--writers): model constructors (`Transaction(...)` for the model whose __tablename__ is the table) and
`INSERT INTO table (...)` column lists that leave the column out.

Hits in migration directories and in tests are listed separately and never count. Each hit is file, line and
the kind of use; line text is truncated and never includes data.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path

CODE = ('.py', '.js', '.ts', '.tsx', '.jsx', '.rb', '.java', '.kt', '.go', '.sql', '.cs', '.php')
SKIP = {'node_modules', '.git', 'venv', '.venv', '__pycache__', 'dist', 'build'}


def classify(rel: str) -> str:
    parts = rel.split('/')
    if any(p in ('migrations', 'migrate', 'versions', 'alembic') for p in parts[:-1]):
        return 'migration'
    if any(p in ('tests', 'test', 'spec', '__tests__') for p in parts) or re.match(r'(test_.*|.*_test|.*\.spec|.*\.test)\.\w+$', parts[-1]):
        return 'test'
    return 'code'


def kind_of(line: str, ident: str) -> str:
    if re.search(rf'\b{re.escape(ident)}\s*=\s*(Column|mapped_column|models\.\w+Field|db\.Column)\(', line):
        return 'orm_field'
    if re.search(r'\b(SELECT|INSERT|UPDATE|DELETE|WHERE|ORDER BY|GROUP BY)\b', line, re.I):
        return 'raw_sql'
    if re.search(rf'\b{re.escape(ident)}\s*=', line):
        return 'keyword_or_assignment'
    return 'reference'


def files(roots: list[Path]):
    for root in roots:
        for p in sorted(root.rglob('*')):
            if p.is_file() and p.suffix in CODE and not (set(p.relative_to(root).parts) & SKIP):
                yield root, p


def readers(roots: list[Path], ident: str) -> dict:
    rx = re.compile(rf'(?<![\w.]){re.escape(ident)}(?!\w)')
    out = {'code': [], 'test': [], 'migration': []}
    for root, p in files(roots):
        rel = p.relative_to(root).as_posix()
        for n, line in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            if rx.search(line) and not line.lstrip().startswith(('#', '//', '--')):
                out[classify(rel)].append({'file': rel, 'line': n, 'kind': kind_of(line, ident), 'excerpt': line.strip()[:100]})
    return out


def model_names(roots: list[Path], table: str) -> list[str]:
    names = []
    for root, p in files(roots):
        if p.suffix != '.py':
            continue
        try:
            tree = ast.parse(p.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for c in ast.walk(tree):
            if isinstance(c, ast.ClassDef):
                for s in c.body:
                    if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__tablename__' for t in s.targets) and \
                            isinstance(s.value, ast.Constant) and s.value.value == table:
                        names.append(c.name)
    return names


def writers_without(roots: list[Path], table: str, column: str) -> dict:
    out = {'code': [], 'test': [], 'migration': []}
    models = model_names(roots, table)
    for root, p in files(roots):
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding='utf-8', errors='replace')
        if p.suffix == '.py' and models:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            for node in ast.walk(tree) if tree else []:
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in models:
                    given = {k.arg for k in node.keywords}
                    if column not in given and None not in given:
                        out[classify(rel)].append({'file': rel, 'line': node.lineno, 'kind': 'constructor_without_column',
                                                   'excerpt': f'{node.func.id}({", ".join(sorted(g for g in given if g))})'})
        for m in re.finditer(rf'INSERT\s+INTO\s+{re.escape(table)}\s*\(([^)]*)\)', text, re.I):
            cols = [c.strip().strip('"`') for c in m.group(1).split(',')]
            if column not in cols:
                out[classify(rel)].append({'file': rel, 'line': text[:m.start()].count('\n') + 1, 'kind': 'insert_without_column',
                                           'excerpt': m.group(0)[:100]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--table', required=True)
    ap.add_argument('--column', required=True)
    ap.add_argument('--writers', action='store_true')
    ap.add_argument('--other-readers', type=Path, action='append', default=[])
    a = ap.parse_args()
    roots = [a.repo] + a.other_readers
    res = writers_without(roots, a.table, a.column) if a.writers else readers(roots, a.column)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
