#!/usr/bin/env python3
"""Find code that still uses a column a migration drops or renames, or inserts rows without a column made NOT NULL.

    python find_readers.py --repo examples/ledger --table transactions --column amount [--writers] [--other-readers ../reports]

Readers: the identifier with word boundaries in Python, JavaScript/TypeScript, Ruby, Java, Kotlin, Go, SQL
and Prisma schema files - ORM field declarations (`amount = Column(`, `amount Decimal` in schema.prisma), ORM
queries (`prisma.order.findMany({ select: { amount: true } })`), raw SQL strings, query builders and
attribute access. Pass the column name and, for Prisma, the field name when @map differs.
Writers (--writers): model constructors (`Transaction(...)` for the model whose __tablename__ is the table),
`INSERT INTO table (...)` column lists that leave the column out, and Prisma
`prisma.<model>.create/createMany/upsert` calls whose data object leaves the field out (a data object that is
a variable or uses a spread can't be read, and is counted as not analysed).

Hits in migration directories and in tests are listed separately and never count. Each hit is file, line and
the kind of use; line text is truncated and never includes data. Dependency, build and VCS folders are
pruned before the walk descends into them.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_walk, sqlshape  # noqa: E402

CODE = ('.py', '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs', '.rb', '.java', '.kt', '.go', '.sql', '.cs', '.php', '.prisma')
JS = ('.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs')
ORM_CALL = re.compile(r'\.\s*(findMany|findFirst|findFirstOrThrow|findUnique|findUniqueOrThrow|create|createMany|createManyAndReturn|update|updateMany|'
                      r'upsert|delete|deleteMany|aggregate|groupBy|count)\s*\(')
_FILES: dict = {}


def classify(rel: str) -> str:
    parts = rel.split('/')
    if any(p in ('migrations', 'migrate', 'versions', 'alembic') for p in parts[:-1]) or parts[-1] == 'migration.sql':
        return 'migration'
    if any(p in ('tests', 'test', 'spec', '__tests__', 'e2e') for p in parts) or re.match(r'(test_.*|.*_test|.*\.spec|.*\.test)\.\w+$', parts[-1]):
        return 'test'
    return 'code'


def kind_of(line: str, ident: str, suffix: str = '') -> str:
    e = re.escape(ident)
    if re.search(rf'\b{e}\s*=\s*(Column|mapped_column|models\.\w+Field|db\.Column)\(', line):
        return 'orm_field'
    if suffix == '.prisma' and re.match(rf'\s*{e}\s+\w', line):
        return 'orm_field'
    if suffix in JS and (ORM_CALL.search(line) or re.search(rf'(?<![\w.]){e}\s*:\s*(true|false|\{{|\[|[\w\'"`])', line)):
        return 'orm_query'
    if re.search(r'\b(SELECT|INSERT|UPDATE|DELETE|WHERE|ORDER BY|GROUP BY)\b', line) or \
            re.search(r'[\'"`]\s*(select|insert|update|delete)\b.*\b(from|into|set|where)\b', line, re.I):
        return 'raw_sql'
    if re.search(rf'\b{e}\s*=', line):
        return 'keyword_or_assignment'
    return 'reference'


def files(roots: list[Path]):
    for root in roots:
        key = Path(root).resolve()
        if key not in _FILES:
            _FILES[key] = list(repo_walk.files(root, CODE))
        for p in _FILES[key]:
            yield root, p


def readers(roots: list[Path], ident: str) -> dict:
    ident = sqlshape.ident(ident)
    rx = re.compile(rf'(?<![\w.]){re.escape(ident)}(?!\w)')
    out = {'code': [], 'test': [], 'migration': []}
    for root, p in files(roots):
        rel = p.relative_to(root).as_posix()
        for n, line in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            if rx.search(line) and not line.lstrip().startswith(('#', '//', '--')):
                out[classify(rel)].append({'file': rel, 'line': n, 'kind': kind_of(line, ident, p.suffix), 'excerpt': line.strip()[:100]})
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


def _close(text: str, i: int) -> int:
    """Index of the bracket closing the one at i, skipping strings and comments."""
    pairs, stack, j, n = {'(': ')', '{': '}', '[': ']'}, [], i, len(text)
    while j < n:
        c = text[j]
        if c in '\'"`':
            k = j + 1
            while k < n and text[k] != c:
                k += 2 if text[k] == '\\' else 1
            j = k + 1
            continue
        if text.startswith('//', j):
            k = text.find('\n', j)
            j = n if k < 0 else k
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j
        j += 1
    return n - 1


def _prisma_writes(text: str, model: str, field: str, relation: str | None):
    """(offset, method, 'missing' | 'unknown') for each prisma.<model>.create/createMany/upsert whose data leaves field out."""
    acc = model[0].lower() + model[1:]
    for m in re.finditer(rf'\.\s*{re.escape(acc)}\s*\.\s*(create|createMany|createManyAndReturn|upsert)\s*\(', text):
        open_i = m.end() - 1
        args = text[open_i:_close(text, open_i) + 1]
        key = 'create' if m.group(1) == 'upsert' else 'data'
        d = re.search(rf'\b{key}\s*:\s*', args)
        if not d:
            yield m.start(), m.group(1), 'unknown'
            continue
        k = d.end()
        if args[k:k + 1] not in '{[':
            yield m.start(), m.group(1), 'unknown'  # data is a variable: can't tell what it holds
            continue
        body = args[k:_close(args, k) + 1]
        names = [field] + ([relation] if relation else [])
        if any(re.search(rf'(?<![\w.]){re.escape(nm)}\s*[:,}}\n]', body) for nm in names):
            continue
        if '...' in body:
            yield m.start(), m.group(1), 'unknown'
            continue
        yield m.start(), m.group(1), 'missing'


def writers_without(roots: list[Path], table: str, column: str, prisma_cols: dict | None = None) -> dict:
    table, column = sqlshape.ident(table), sqlshape.ident(column)
    out = {'code': [], 'test': [], 'migration': [], 'unknown': []}
    models = model_names(roots, table)
    pc = (prisma_cols or {}).get(table) or {}
    pf = pc.get(column)
    pmodel = (pf or next(iter(pc.values()), None) or {}).get('model')
    if pf and pf.get('default'):
        pmodel = None  # @default/@updatedAt: Prisma or the database fills the column, so a create without it still works
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
        if pmodel and p.suffix in JS:
            for off, method, state in _prisma_writes(text, pmodel, (pf or {}).get('field', column), (pf or {}).get('relation')):
                hit = {'file': rel, 'line': text[:off].count('\n') + 1, 'kind': f'prisma_{method}_without_column',
                       'excerpt': text[off:off + 100].split('\n')[0].strip()}
                if state == 'unknown':
                    out['unknown'].append(dict(hit, kind=f'prisma_{method}_data_not_literal'))
                else:
                    out[classify(rel)].append(hit)
        for m in re.finditer(rf'INSERT\s+INTO\s+["`]?{re.escape(table)}["`]?\s*\(([^)]*)\)', text, re.I):
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
    ap.add_argument('--prisma-schema', type=Path, help='schema.prisma, for field names (@map) and Prisma writes')
    ap.add_argument('--other-readers', type=Path, action='append', default=[])
    a = ap.parse_args()
    roots = [a.repo] + a.other_readers
    pcols = sqlshape.prisma_columns(sqlshape.prisma_schema(a.prisma_schema.read_text(encoding='utf-8'))) if a.prisma_schema else {}
    res = writers_without(roots, a.table, a.column, pcols) if a.writers else readers(roots, a.column)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
