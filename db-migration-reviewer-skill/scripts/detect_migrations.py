#!/usr/bin/env python3
"""Find a repository's migrations, the tool and engine, and turn every migration into SQL-shaped statements with lines.

    python detect_migrations.py --repo examples/ledger --out run/migrations.json [--since 0002] [--engine postgresql --engine-version 16]

Tools: Alembic (Python AST of upgrade() and downgrade(): op.* calls become the SQL they run, op.execute SQL
is split), Django (the operations list: AddField, RemoveField, RenameField, AlterField, AddIndex, RunSQL,
RunPython), and SQL files (Flyway V__/U__, Prisma migration.sql, any other .sql). Rails, Knex and Liquibase
are listed as not parsed in this version.

Engine and version: --engine/--engine-version, else alembic.ini or settings URLs (postgresql://, mysql://), and
docker-compose image tags (postgres:16.4). Without a version, the oldest the repository claims is assumed and
the output says so.

The pending set: --since <revision> (everything after it in the chain), --migrations a,b, or all.
Each migration: id, file, tool, revision chain, statements (up and down), down state (ok, empty, pass,
raises, missing), whether it runs in a transaction, tables it creates, and code it couldn't analyse.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import split_sql  # noqa: E402

SKIP = {'node_modules', '.git', 'venv', '.venv', '__pycache__'}
TYPES = {'Integer': 'INTEGER', 'BigInteger': 'BIGINT', 'SmallInteger': 'SMALLINT', 'Numeric': 'NUMERIC', 'Float': 'FLOAT', 'String': 'VARCHAR',
         'Text': 'TEXT', 'Boolean': 'BOOLEAN', 'DateTime': 'TIMESTAMP', 'Date': 'DATE', 'JSON': 'JSON', 'UUID': 'UUID'}


def src(node) -> str:
    return ast.unparse(node)


def sa_type(node) -> str:
    if isinstance(node, ast.Call):
        name = src(node.func).split('.')[-1]
        args = ','.join(src(a) for a in node.args)
        return TYPES.get(name, name.upper()) + (f'({args})' if args else '')
    name = src(node).split('.')[-1]
    return TYPES.get(name, name.upper())


def kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def lit(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def column_sql(col: ast.Call) -> tuple[str, str]:
    name = lit(col.args[0]) if col.args else '?'
    typ = sa_type(col.args[1]) if len(col.args) > 1 else '?'
    parts = [name, typ]
    nullable = kw(col, 'nullable')
    if kw(col, 'primary_key') is not None and lit(kw(col, 'primary_key')):
        parts.append('PRIMARY KEY')
    elif nullable is not None and lit(nullable) is False:
        parts.append('NOT NULL')
    d = kw(col, 'server_default')
    if d is not None:
        v = lit(d)
        parts.append(f"DEFAULT '{v}'" if isinstance(v, str) else f'DEFAULT {src(d)}')
    if any(isinstance(a, ast.Call) and src(a.func).endswith('ForeignKey') for a in col.args):
        ref = lit(next(a for a in col.args if isinstance(a, ast.Call) and src(a.func).endswith('ForeignKey')).args[0])
        parts.append('REFERENCES ' + (f'{ref.rsplit(".", 1)[0]} ({ref.rsplit(".", 1)[1]})' if '.' in ref else ref))
    return name, ' '.join(parts)


def alembic_op(call: ast.Call) -> list[str]:
    f = src(call.func)
    name = f.split('.')[-1]
    a = call.args
    if name == 'create_table':
        cols = [column_sql(c)[1] for c in a[1:] if isinstance(c, ast.Call) and src(c.func).endswith('Column')]
        return [f'CREATE TABLE {lit(a[0])} ({", ".join(cols)})']
    if name == 'drop_table':
        return [f'DROP TABLE {lit(a[0])}']
    if name == 'add_column':
        return [f'ALTER TABLE {lit(a[0])} ADD COLUMN {column_sql(a[1])[1]}']
    if name == 'drop_column':
        return [f'ALTER TABLE {lit(a[0])} DROP COLUMN {lit(a[1])}']
    if name == 'rename_table':
        return [f'ALTER TABLE {lit(a[0])} RENAME TO {lit(a[1])}']
    if name == 'alter_column':
        t, c = lit(a[0]), lit(a[1])
        out = []
        if kw(call, 'new_column_name') is not None:
            out.append(f'ALTER TABLE {t} RENAME COLUMN {c} TO {lit(kw(call, "new_column_name"))}')
        if kw(call, 'type_') is not None:
            frm = kw(call, 'existing_type')
            out.append(f'ALTER TABLE {t} ALTER COLUMN {c} TYPE {sa_type(kw(call, "type_"))}' + (f' /* from {sa_type(frm)} */' if frm is not None else ''))
        if kw(call, 'nullable') is not None:
            out.append(f'ALTER TABLE {t} ALTER COLUMN {c} {"DROP NOT NULL" if lit(kw(call, "nullable")) else "SET NOT NULL"}')
        if kw(call, 'server_default') is not None:
            out.append(f'ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT {src(kw(call, "server_default"))}')
        return out
    if name == 'create_index':
        conc = kw(call, 'postgresql_concurrently')
        uniq = kw(call, 'unique')
        cols = ', '.join(lit(a[2]) or []) if len(a) > 2 else ''
        return [f'CREATE {"UNIQUE " if uniq is not None and lit(uniq) else ""}INDEX {"CONCURRENTLY " if conc is not None and lit(conc) else ""}'
                f'{lit(a[0])} ON {lit(a[1])} ({cols})']
    if name == 'drop_index':
        return [f'DROP INDEX {lit(a[0])}']
    if name == 'create_foreign_key':
        return [f'ALTER TABLE {lit(a[1])} ADD CONSTRAINT {lit(a[0])} FOREIGN KEY ({", ".join(lit(a[3]) or [])}) REFERENCES {lit(a[2])} ({", ".join(lit(a[4]) or []) if len(a) > 4 else ""})']
    return []


def parse_alembic(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    mod = {n.targets[0].id: lit(n.value) for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    rec = {'revision': mod.get('revision'), 'down_revision': mod.get('down_revision'), 'up': [], 'down': [], 'transactional': True,
           'not_analysed': [], 'down_state': 'missing'}
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in ('upgrade', 'downgrade'):
            continue
        key = 'up' if fn.name == 'upgrade' else 'down'
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if fn.name == 'downgrade':
            rec['down_line'] = fn.lineno
            rec['down_state'] = 'pass' if all(isinstance(s, ast.Pass) for s in body) else \
                'raises' if any(isinstance(s, ast.Raise) for s in body) else 'empty' if not body else 'ok'
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and src(node.func).startswith('op.'):
                name = src(node.func).split('.')[-1]
                if name == 'execute':
                    sql = lit(node.args[0]) if node.args else None
                    if isinstance(sql, str):
                        rec[key] += [{'line': node.lineno + ln - 1, 'sql': s, 'via': 'op.execute'} for ln, s in split_sql.split(sql)]
                    else:
                        rec['not_analysed'].append({'line': node.lineno, 'what': 'op.execute with SQL built at runtime'})
                elif 'autocommit_block' in src(node.func) or name == 'get_context':
                    rec['transactional'] = False
                else:
                    rec[key] += [{'line': node.lineno, 'sql': s, 'via': f'op.{name}'} for s in alembic_op(node)]
    for k in ('up', 'down'):
        rec[k].sort(key=lambda s: s['line'])
    return rec


def parse_django(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    rec = {'revision': path.stem, 'down_revision': None, 'up': [], 'down': [], 'transactional': True, 'not_analysed': [], 'down_state': 'ok'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'atomic' for t in node.targets):
            rec['transactional'] = bool(lit(node.value))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'dependencies' for t in node.targets):
            deps = lit(node.value) or []
            rec['down_revision'] = deps[-1][1] if deps else None
        if not isinstance(node, ast.Call):
            continue
        op = src(node.func).split('.')[-1]
        k = {x.arg: x.value for x in node.keywords}
        model = lit(k.get('model_name')) or '?'
        if op == 'RemoveField':
            rec['up'].append({'line': node.lineno, 'sql': f'ALTER TABLE {model} DROP COLUMN {lit(k.get("name"))}', 'via': op})
        elif op == 'RenameField':
            rec['up'].append({'line': node.lineno, 'sql': f'ALTER TABLE {model} RENAME COLUMN {lit(k.get("old_name"))} TO {lit(k.get("new_name"))}', 'via': op})
        elif op == 'AddField':
            fld = k.get('field')
            null = isinstance(fld, ast.Call) and any(x.arg == 'null' and lit(x.value) for x in fld.keywords)
            default = isinstance(fld, ast.Call) and any(x.arg == 'default' for x in fld.keywords)
            rec['up'].append({'line': node.lineno, 'sql': f'ALTER TABLE {model} ADD COLUMN {lit(k.get("name"))} ?{"" if null else " NOT NULL"}'
                                                          f'{" DEFAULT ?" if default else ""}', 'via': op})
        elif op == 'AlterField':
            rec['up'].append({'line': node.lineno, 'sql': f'ALTER TABLE {model} ALTER COLUMN {lit(k.get("name"))} TYPE ?', 'via': op})
        elif op == 'DeleteModel':
            rec['up'].append({'line': node.lineno, 'sql': f'DROP TABLE {lit(k.get("name"))}', 'via': op})
        elif op == 'AddIndex':
            rec['up'].append({'line': node.lineno, 'sql': f'CREATE INDEX ? ON {model} (?)', 'via': op})
        elif op == 'RunSQL':
            sql = lit(node.args[0]) if node.args else lit(k.get('sql'))
            if isinstance(sql, str):
                rec['up'] += [{'line': node.lineno, 'sql': s, 'via': 'RunSQL'} for _, s in split_sql.split(sql)]
            if 'reverse_sql' not in k:
                rec['down_state'] = 'missing'
        elif op == 'RunPython':
            rec['not_analysed'].append({'line': node.lineno, 'what': 'RunPython body (read by hand; whole-table loops and missing reverse code)'})
            if len(node.args) < 2 and 'reverse_code' not in k:
                rec['down_state'] = 'missing'
    return rec


def parse_sql_file(path: Path) -> dict:
    stmts = [{'line': ln, 'sql': s, 'via': 'sql'} for ln, s in split_sql.split(path.read_text(encoding='utf-8'))]
    return {'revision': path.stem, 'down_revision': None, 'up': stmts, 'down': [], 'transactional': True, 'not_analysed': [], 'down_state': 'missing'}


def detect(repo: Path) -> tuple[str, list[Path]]:
    files = [p for p in repo.rglob('*') if p.is_file() and not (set(p.relative_to(repo).parts) & SKIP)]
    alembic = sorted(p for p in files if p.suffix == '.py' and p.parent.name == 'versions' and 'revision' in p.read_text(encoding='utf-8', errors='replace'))
    if alembic:
        return 'alembic', alembic
    django = sorted(p for p in files if p.suffix == '.py' and p.parent.name == 'migrations' and 'migrations.Migration' in p.read_text(encoding='utf-8', errors='replace'))
    if django:
        return 'django', django
    flyway = sorted(p for p in files if re.match(r'V[\d_.]+__.*\.sql$', p.name))
    if flyway:
        return 'flyway', flyway
    prisma = sorted(p for p in files if p.name == 'migration.sql')
    if prisma:
        return 'prisma', prisma
    other = [p for p in files if p.suffix == '.rb' and 'db/migrate' in p.as_posix() or p.name.startswith('changelog') or
             (p.suffix in ('.js', '.ts') and 'migrations' in p.as_posix())]
    if other:
        return 'not_parsed', other
    return 'sql', sorted(p for p in files if p.suffix == '.sql')


def engine(repo: Path, a) -> dict:
    if a.engine:
        return {'name': a.engine, 'version': a.engine_version, 'source': 'given', 'assumed': a.engine_version is None}
    name, version, source = None, None, None
    for p in repo.rglob('*'):
        if not p.is_file() or set(p.relative_to(repo).parts) & SKIP or p.suffix not in ('.ini', '.py', '.yml', '.yaml', '.env', '.toml', '.json'):
            continue
        t = p.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'\b(postgresql|postgres|mysql)(?:\+\w+)?://', t)
        if m and not name:
            name, source = ('mysql' if m.group(1) == 'mysql' else 'postgresql'), p.relative_to(repo).as_posix()
        m = re.search(r'image:\s*["\']?(postgres|mysql|mariadb):(\d+(?:\.\d+)?)', t)
        if m:
            name = name or {'postgres': 'postgresql'}.get(m.group(1), m.group(1))
            version, source = m.group(2), (source + ' and ' if source else '') + p.relative_to(repo).as_posix()
    assumed = version is None
    if assumed:
        version = {'postgresql': '10', 'mysql': '5.7'}.get(name)
    return {'name': name or 'unknown', 'version': version, 'source': source, 'assumed': assumed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--since')
    ap.add_argument('--migrations')
    ap.add_argument('--engine', choices=['postgresql', 'mysql', 'mariadb', 'sqlite'])
    ap.add_argument('--engine-version')
    a = ap.parse_args()
    tool, files = detect(a.repo)
    parser = {'alembic': parse_alembic, 'django': parse_django}.get(tool, parse_sql_file)
    migs = []
    for p in files:
        if tool == 'not_parsed':
            migs.append({'id': p.stem, 'file': p.relative_to(a.repo).as_posix(), 'parsed': False})
            continue
        r = parser(p)
        r.update(id=r['revision'] or p.stem, file=p.relative_to(a.repo).as_posix(), parsed=True)
        if tool == 'flyway':
            undo = p.with_name('U' + p.name[1:])
            r['down_state'] = 'ok' if undo.exists() else 'missing'
        r['created_tables'] = [re.match(r'CREATE TABLE (\w+)', s['sql']).group(1) for s in r['up'] if s['sql'].startswith('CREATE TABLE')]
        migs.append(r)
    # order (Alembic chain) and heads
    heads = []
    if tool == 'alembic':
        by_down = {m['down_revision']: m for m in migs}
        order, cur = [], None
        while cur in by_down:
            order.append(by_down[cur])
            cur = by_down[cur]['revision']
        rest = [m for m in migs if m not in order]
        migs = order + rest
        referenced = {m['down_revision'] for m in migs}
        heads = [m['revision'] for m in migs if m['revision'] not in referenced]
    ids = [m['id'] for m in migs]
    if a.migrations:
        pending = a.migrations.split(',')
    elif a.since and a.since in ids:
        pending = ids[ids.index(a.since) + 1:]
    else:
        pending = ids
    for m in migs:
        m['pending'] = m['id'] in pending
    out = {'repo': a.repo.as_posix(), 'tool': tool, 'engine': engine(a.repo, a), 'heads': heads, 'since': a.since, 'migrations': migs,
           'not_parsed': [m['file'] for m in migs if not m.get('parsed', True)] + ([] if tool != 'not_parsed' else [])}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{tool}: {len(migs)} migration(s), {len(pending)} pending; engine {out["engine"]["name"]} {out["engine"]["version"]}'
          f'{" (assumed)" if out["engine"]["assumed"] else ""}; heads {heads} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
