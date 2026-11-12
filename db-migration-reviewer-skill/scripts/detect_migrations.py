#!/usr/bin/env python3
"""Find a repository's migrations, the tool and engine, and turn every migration into SQL-shaped statements with lines.

    python detect_migrations.py --repo examples/ledger --out run/migrations.json [--since 0002] [--engine postgresql --engine-version 16]

Tools: Alembic (Python AST of upgrade() and downgrade(): op.* calls become the SQL they run, op.execute SQL
is split; calls to the migration's own helper functions are followed and f-strings built from literal
arguments resolved; op.get_bind()/conn.execute data steps, SQLAlchemy Core statements, functions imported
from the application and files read at run time are listed under not_analysed; each script location with
its own env.py and versions/ is a chain, and a repository with several needs --chain <dir>), Django (the operations list: AddField, RemoveField, RenameField, AlterField, AddIndex, RunSQL,
RunPython), Knex (the schema builder, knex.raw SQL and knex('table') deletes and updates in up and down, by
source pattern; anything else is listed as not analysed), and SQL files (Flyway V__/U__, Prisma migration.sql,
any other .sql). Rails and Liquibase are listed as not parsed in this version.

Prisma: each migration's id is its folder name (20260101120000_add_orders), the chain is the folder named by
prisma.config.* migrations.path (else the one holding migration_lock.toml) in folder-name order, and the
provider comes from schema.prisma or migration_lock.toml. Prisma has no down migrations.

Archived history: migration files under a folder that looks archived or squashed (archive, squashed, history,
old) are left out of the chain and listed under "excluded"; --include-archived keeps them, --exclude-dir adds
more. Archived SQL still feeds the column type history.

Engine and version: --engine/--engine-version (--engine-source records where it came from), else settings
URLs (postgresql://, mysql://), infrastructure (CloudFormation or Terraform Engine and EngineVersion, Azure
Bicep or ARM flexibleServers version, also in --infra folders outside the repo), and docker-compose or Dockerfile image tags (postgres:16.4,
also behind a registry prefix). Without a version, the oldest the repository claims is assumed; a version
taken only from docker-compose, a Dockerfile or CI is a development source and is also marked assumed.

The pending set: --since <revision> (everything after it in the chain; an id that matches nothing is an
error, never "all"), --migrations a,b, or all.
Each migration: id, file, tool, revision chain, statements (up and down), down state (ok, empty, pass,
raises, missing), whether it runs in a transaction, tables it creates, and code it couldn't analyse.
type_history: for every column a pending migration retypes, the types it had before (earlier migrations,
archived migrations, and for Prisma schema.prisma at HEAD and, from git, before the first pending migration).
Directory walks prune node_modules, virtual environments, build output and VCS folders before descending.
"""
from __future__ import annotations
import argparse, ast, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_walk, split_sql, sqlshape  # noqa: E402

ARCHIVE = re.compile(r'(?i)(^|[_\-.])(archive|archived|squash|squashed|history|old)([_\-.]|$)')
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
    if name == 'drop_constraint' and len(a) > 1:
        return [f'ALTER TABLE {lit(a[1])} DROP CONSTRAINT {name_of(a[0])}']
    if name == 'create_check_constraint' and len(a) > 2:
        cond = lit(a[2])
        return [f'ALTER TABLE {lit(a[1])} ADD CONSTRAINT {name_of(a[0])} CHECK ({cond if isinstance(cond, str) else "?"})']
    if name == 'create_unique_constraint' and len(a) > 2:
        return [f'ALTER TABLE {lit(a[1])} ADD CONSTRAINT {name_of(a[0])} UNIQUE ({", ".join(lit(a[2]) or [])})']
    if name == 'create_primary_key' and len(a) > 2:
        return [f'ALTER TABLE {lit(a[1])} ADD CONSTRAINT {name_of(a[0])} PRIMARY KEY ({", ".join(lit(a[2]) or [])})']
    return []


def name_of(node) -> str:
    """A constraint or index name: a literal, or op.f('name')."""
    if isinstance(node, ast.Call) and src(node.func).endswith('.f') and node.args:
        node = node.args[0]
    v = lit(node)
    return v if isinstance(v, str) else '?'


# op.* calls that run no SQL themselves (or are handled elsewhere)
OP_NO_SQL = {'f', 'get_context', 'get_bind', 'inline_literal', 'implementation_for', 'invoke'}
# modules whose functions a migration calls without running SQL of its own
SAFE_MODULES = re.compile(r'^(alembic|sqlalchemy|typing|datetime|decimal|uuid|enum|re|json|os|pathlib|logging|collections|functools|itertools|__future__)(\.|$)')
FILE_READ = re.compile(r'(^|\.)(open|read_text|read_bytes|glob|rglob|iglob|iterdir|listdir|scandir|walk|safe_load|load|loadtxt|read_csv)$')
PATHISH = re.compile(r'^[\w.\-*/\\{}]+\.(ya?ml|json|csv|sql|txt|toml|ini|xml|tsv|xlsx?)$|^[\w.\-]+(/[\w.\-*{}]+)+/?$', re.I)


def value_of(node, env: dict):
    """A string (or tuple of strings) an expression evaluates to with the names in env: literals, names, f-strings
    whose parts are known, + of strings and tuples; None when any part is unknown."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion in (-1, 115):
                v = value_of(part.value, env)
                if not isinstance(v, str):
                    return None
                out.append(v)
            else:
                return None
        return ''.join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        x, y = value_of(node.left, env), value_of(node.right, env)
        return x + y if isinstance(x, str) and isinstance(y, str) else None
    if isinstance(node, ast.Tuple):
        vals = [value_of(e, env) for e in node.elts]
        return tuple(vals) if all(isinstance(v, str) for v in vals) else None
    return None


def bind_call(fn: ast.FunctionDef, call: ast.Call, env: dict, module_env: dict) -> dict:
    """The names a helper sees when called with literal arguments: its parameters, then simple assignments at the top
    of its body (old, new = pair). Anything not literal stays unknown, so its SQL is listed as not analysed."""
    out = dict(module_env)
    params = [x.arg for x in fn.args.args]
    for i, arg in enumerate(call.args[:len(params)]):
        out[params[i]] = value_of(arg, env)
    for k in call.keywords:
        if k.arg:
            out[k.arg] = value_of(k.value, env)
    for st in fn.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            v, t = value_of(st.value, out), st.targets[0]
            if isinstance(t, ast.Name):
                out[t.id] = v
            elif isinstance(t, ast.Tuple) and isinstance(v, tuple) and len(v) == len(t.elts):
                for e, x in zip(t.elts, v):
                    if isinstance(e, ast.Name):
                        out[e.id] = x
    return out


def sql_literal(node, env: dict | None = None):
    """The SQL string of op.execute / connection.execute arguments: a literal, text('...') / sa.text('...'), or an
    f-string whose values are known (a helper called with literal arguments)."""
    if isinstance(node, ast.Call) and src(node.func).split('.')[-1] == 'text' and node.args:
        node = node.args[0]
    v = value_of(node, env or {})
    return v if isinstance(v, str) else None


def parse_alembic(path: Path) -> dict:
    """op.* calls in upgrade() and downgrade(), following calls to the module's own helper functions; data steps
    through the connection, SQLAlchemy Core statements, functions imported from other modules and files read at
    run time are listed as not analysed (never guessed at)."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    mod = {n.targets[0].id: lit(n.value) for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    imported = {}
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and not SAFE_MODULES.match(n.module):
            for al in n.names:
                imported[al.asname or al.name] = n.module
    rec = {'revision': mod.get('revision'), 'down_revision': mod.get('down_revision'), 'up': [], 'down': [], 'transactional': True,
           'not_analysed': [], 'down_state': 'missing', 'reads_files': []}
    noted: set = set()
    followed: set = set()

    def note(line: int, what: str, key: str):
        if (line, key) not in noted:  # one entry per line: conn.execute(sa.insert(t)) is one data step
            noted.add((line, key))
            rec['not_analysed'].append({'line': line, 'what': what, 'direction': key})

    module_env = {k: v for k, v in mod.items() if isinstance(v, (str, tuple))}

    def visit(fn: ast.FunctionDef, key: str, chain: tuple[str, ...], env: dict):
        via_sfx = f' (in {chain[-1]}(), called from {"upgrade" if key == "up" else "downgrade"}())' if chain else ''
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = src(node.func)
            name = f.split('.')[-1]
            if f.startswith('op.'):
                if name == 'execute':
                    sql = sql_literal(node.args[0], env) if node.args else None
                    if sql is not None:
                        rec[key] += [{'line': node.lineno + ln - 1, 'sql': s, 'via': 'op.execute' + via_sfx} for ln, s in split_sql.split(sql)]
                    else:
                        note(node.lineno, 'op.execute with SQL built at runtime or a SQLAlchemy Core statement (read by hand)', key)
                elif 'autocommit_block' in f:
                    rec['transactional'] = False
                elif name == 'get_bind':
                    note(node.lineno, 'op.get_bind(): a data step on the connection (read by hand; whole-table updates, missing reverse)', key)
                elif name == 'bulk_insert':
                    note(node.lineno, 'op.bulk_insert: seed rows (read by hand)', key)
                elif name == 'batch_alter_table':
                    note(node.lineno, 'op.batch_alter_table block: batch_op calls are not translated (read by hand)', key)
                elif name in OP_NO_SQL or f.startswith('op.get_context'):
                    continue
                else:
                    sqls = alembic_op(node)
                    if sqls:
                        rec[key] += [{'line': node.lineno, 'sql': s, 'via': f'op.{name}' + via_sfx} for s in sqls]
                    else:
                        note(node.lineno, f'op.{name} not translated (read by hand)', key)
            elif isinstance(node.func, ast.Name) and node.func.id in funcs and node.func.id not in ('upgrade', 'downgrade'):
                if node.func.id not in chain and (node.func.id, key) not in followed:  # a helper defined in the migration: follow it once
                    followed.add((node.func.id, key))
                    visit(funcs[node.func.id], key, chain + (node.func.id,), bind_call(funcs[node.func.id], node, env, module_env))
            elif isinstance(node.func, ast.Name) and node.func.id in imported:
                note(node.lineno, f'{node.func.id}() from {imported[node.func.id]} is not analysed (read it by hand: it may run SQL)', key)
            elif name == 'execute':  # conn.execute / bind.execute / session.execute
                sql = sql_literal(node.args[0], env) if node.args else None
                if sql is not None:
                    rec[key] += [{'line': node.lineno + ln - 1, 'sql': s, 'via': f'{f}(text)' + via_sfx} for ln, s in split_sql.split(sql)]
                else:
                    note(node.lineno, f'{f}() with SQL built at runtime or a SQLAlchemy Core statement: a data step (read by hand)', key)
            elif name in ('insert', 'update', 'delete') and (f.startswith(('sa.', 'sqlalchemy.')) or f in ('insert', 'update', 'delete')
                                                            or isinstance(node.func, ast.Attribute) and not node.args):
                note(node.lineno, f'SQLAlchemy Core {name}(): a data step (read by hand)', key)
            elif FILE_READ.search(f) and not f.startswith(('re.', 'json.loads')):
                paths = sorted({v for v in (lit(x) for x in ast.walk(node) if isinstance(x, ast.Constant)) if isinstance(v, str) and PATHISH.match(v)})
                rec['reads_files'].append({'line': node.lineno, 'call': f, 'direction': key, 'paths': paths})
                note(node.lineno, f'{f}(): reads files at run time; the step depends on files that must still exist (read by hand)', key)

    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in ('upgrade', 'downgrade'):
            continue
        key = 'up' if fn.name == 'upgrade' else 'down'
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if fn.name == 'downgrade':
            rec['down_line'] = fn.lineno
            rec['down_state'] = 'pass' if all(isinstance(s, ast.Pass) for s in body) else \
                'raises' if any(isinstance(s, ast.Raise) for s in body) else 'empty' if not body else 'ok'
        visit(fn, key, (), module_env)
    # paths named in module-level constants (SEED_DIR = 'seeds/sites') count for the file reads too
    consts = sorted({v for v in mod.values() if isinstance(v, str) and PATHISH.match(v)})
    for r in rec['reads_files']:
        r['paths'] = sorted(set(r['paths']) | set(consts))
    for k in ('up', 'down'):
        rec[k].sort(key=lambda s: s['line'])
    rec['not_analysed'].sort(key=lambda s: s['line'])
    if not rec['reads_files']:
        del rec['reads_files']
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


KNEX_TYPES = {'increments': 'SERIAL', 'bigIncrements': 'BIGSERIAL', 'integer': 'INTEGER', 'bigInteger': 'BIGINT', 'bigint': 'BIGINT',
              'smallint': 'SMALLINT', 'tinyint': 'SMALLINT', 'mediumint': 'INTEGER', 'text': 'TEXT', 'string': 'VARCHAR', 'float': 'REAL',
              'double': 'DOUBLE PRECISION', 'decimal': 'NUMERIC', 'boolean': 'BOOLEAN', 'date': 'DATE', 'datetime': 'TIMESTAMP',
              'timestamp': 'TIMESTAMP', 'time': 'TIME', 'binary': 'BYTEA', 'json': 'JSON', 'jsonb': 'JSONB', 'uuid': 'UUID',
              'enum': 'TEXT', 'enu': 'TEXT', 'specificType': None}
KNEX_SCHEMA = re.compile(r'\.(createTable|createTableIfNotExists|alterTable|table|dropTable|dropTableIfExists|renameTable|raw)\s*\(')
OPENERS = {'(': ')', '[': ']', '{': '}'}


def js_mask(text: str) -> str:
    """The text with comments and string contents blanked (quotes and newlines kept), so offsets still match."""
    out, i, n = list(text), 0, len(text)

    def blank(a: int, b: int):
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif text[i] in '\'"`':
            q, j = text[i], i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == '\\' else 1
            blank(i + 1, min(j, n))
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def js_close(masked: str, i: int) -> int:
    """Index of the bracket closing the one at i (or the end of the text)."""
    stack = []
    for j in range(i, len(masked)):
        c = masked[j]
        if c in OPENERS:
            stack.append(OPENERS[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j
    return len(masked) - 1


def js_args(text: str, masked: str, open_i: int) -> list[str]:
    """The top-level arguments of the call whose '(' is at open_i, as source text."""
    close, args, depth, start = js_close(masked, open_i), [], 0, open_i + 1
    for j in range(open_i + 1, close):
        c = masked[j]
        if c in OPENERS:
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            args.append(text[start:j].strip())
            start = j + 1
    if text[start:close].strip():
        args.append(text[start:close].strip())
    return args


def js_str(arg: str | None):
    """A JavaScript string literal's value; a list for an array of literals; None otherwise."""
    if not arg:
        return None
    arg = arg.strip()
    if arg[:1] in '\'"`' and arg[-1:] == arg[:1] and (arg[0] != '`' or '${' not in arg):
        return arg[1:-1]
    if arg.startswith('[') and arg.endswith(']'):
        items = [js_str(x) for x in re.split(r'\s*,\s*', arg[1:-1].strip()) if x]
        return items if all(isinstance(x, str) for x in items) else None
    return None


def js_chain(text: str, masked: str, open_i: int) -> tuple[list[tuple[str, list[str]]], int]:
    """Calls chained after the call at open_i: [(method, args)] and the index after the chain."""
    chain, j = [], js_close(masked, open_i) + 1
    while True:
        m = re.compile(r'\s*\.\s*(\w+)\s*\(').match(masked, j)
        if not m:
            return chain, j
        o = m.end() - 1
        chain.append((m.group(1), js_args(text, masked, o)))
        j = js_close(masked, o) + 1


def js_function(masked: str, name: str) -> tuple[int, int, str] | None:
    """(start, end, first parameter) of the body of the migration's up or down function."""
    sig = r'\(\s*(\w*)[^)]*\)(?:\s*:\s*[\w<>\[\]., ]+)?\s*'
    for pat in (rf'\b{name}\s*[=:]\s*(?:async\s+)?(?:function\b[^(]*)?{sig}(=>\s*)?', rf'\bfunction\s+{name}\s*{sig}()'):
        m = re.search(pat, masked)
        if not m:
            continue
        j = m.end()
        if masked[j:j + 1] == '{':
            return j + 1, js_close(masked, j), m.group(1) or 'knex'
        if m.group(2):  # expression-bodied arrow function: up to the end of the statement
            depth, k = 0, j
            while k < len(masked):
                c = masked[k]
                if c in OPENERS:
                    depth += 1
                elif c in ')]}':
                    if depth == 0:
                        break
                    depth -= 1
                elif c == ';' and depth == 0:
                    break
                k += 1
            return j, k, m.group(1) or 'knex'
    return None


def knex_column(method: str, args: list[str], mods: list[tuple[str, list[str]]]) -> tuple[str, str]:
    name = js_str(args[0]) if args else 'id' if method in ('increments', 'bigIncrements') else '?'
    if method == 'specificType':
        typ = (js_str(args[1]) if len(args) > 1 else None) or '?'
    else:
        typ = KNEX_TYPES[method]
        if method == 'string':
            typ += f'({args[1] if len(args) > 1 and args[1].isdigit() else 255})'
        if method == 'decimal' and len(args) > 2 and args[1].isdigit() and args[2].isdigit():
            typ += f'({args[1]},{args[2]})'
        elif method == 'decimal':
            typ += '(8,2)'
    parts = [name, typ]
    names = [m for m, _ in mods]
    if 'primary' in names:
        parts.append('PRIMARY KEY')
    elif 'notNullable' in names:
        parts.append('NOT NULL')
    for m, a in mods:
        if m == 'defaultTo' and a:
            v = a[0].strip()
            if 'fn.now' in v:
                v = 'now()'
            elif v.startswith(('knex.raw', 'db.raw')):
                v = js_str(re.sub(r'^\w+\.raw\s*\(|\)$', '', v)) or v
            elif js_str(v) is not None:
                v = f"'{js_str(v)}'"
            parts.append(f'DEFAULT {v}')
    return name, ' '.join(parts)


def knex_ref(mods: list[tuple[str, list[str]]]) -> str:
    """The referenced table of .references('col').inTable('t') or .references('t.col')."""
    d = dict(mods)
    ref = js_str((d.get('references') or [None])[0]) or '?'
    table = js_str((d.get('inTable') or [None])[0])
    return f'{table} ({ref})' if table else (f'{ref.rsplit(".", 1)[0]} ({ref.rsplit(".", 1)[1]})' if '.' in ref else ref)


def knex_builder(text: str, masked: str, start: int, end: int, param: str, table: str, creating: bool, line_of) -> tuple[list[dict], list[dict], list[str]]:
    """Translate a createTable/alterTable callback body into statements; returns (statements, not analysed, CREATE TABLE columns)."""
    stmts, skipped, cols = [], [], []

    def add(i: int, sql: str, via: str):
        stmts.append({'line': line_of(i), 'sql': sql, 'via': f'knex {via}'})
    for m in re.finditer(rf'\b{re.escape(param)}\s*\.\s*(\w+)\s*\(', masked[start:end]):
        i = start + m.start()
        method, o = m.group(1), start + m.end() - 1
        args = js_args(text, masked, o)
        mods, after = js_chain(text, masked, o)
        names = [x for x, _ in mods]
        a0 = js_str(args[0]) if args else None
        cols_list = a0 if isinstance(a0, list) else [a0] if a0 else []
        if method in KNEX_TYPES:
            col, sql = knex_column(method, args, mods)
            if creating:
                cols.append(sql)
                continue
            if 'alter' in names:
                add(i, f'ALTER TABLE {table} ALTER COLUMN {col} TYPE {sql.split(" ")[1]}', f'{method}().alter()')
                if 'notNullable' in names:
                    add(i, f'ALTER TABLE {table} ALTER COLUMN {col} SET NOT NULL', 'notNullable().alter()')
                if 'nullable' in names:
                    add(i, f'ALTER TABLE {table} ALTER COLUMN {col} DROP NOT NULL', 'nullable().alter()')
                continue
            add(i, f'ALTER TABLE {table} ADD COLUMN {sql}', f'{method}()')
            if 'unique' in names:
                add(i, f'ALTER TABLE {table} ADD CONSTRAINT ? UNIQUE ({col})', f'{method}().unique()')
            if 'index' in names:
                add(i, f'CREATE INDEX ? ON {table} ({col})', f'{method}().index()')
            if 'references' in names:
                add(i, f'ALTER TABLE {table} ADD FOREIGN KEY ({col}) REFERENCES {knex_ref(mods)}', f'{method}().references()')
        elif method == 'timestamps':
            if creating:
                cols += ['created_at TIMESTAMP', 'updated_at TIMESTAMP']
            else:
                add(i, f'ALTER TABLE {table} ADD COLUMN created_at TIMESTAMP', 'timestamps()')
                add(i, f'ALTER TABLE {table} ADD COLUMN updated_at TIMESTAMP', 'timestamps()')
        elif creating:
            continue  # indexes, keys and constraints on a table created here lock nothing anyone uses yet
        elif method in ('dropColumn', 'dropColumns'):
            for a in args:
                for c in (js_str(a) if isinstance(js_str(a), list) else [js_str(a) or '?']):
                    add(i, f'ALTER TABLE {table} DROP COLUMN {c}', f'{method}()')
        elif method == 'renameColumn':
            add(i, f'ALTER TABLE {table} RENAME COLUMN {a0} TO {js_str(args[1]) if len(args) > 1 else "?"}', 'renameColumn()')
        elif method in ('unique', 'primary'):
            kind = 'UNIQUE' if method == 'unique' else 'PRIMARY KEY'
            add(i, f'ALTER TABLE {table} ADD CONSTRAINT ? {kind} ({", ".join(cols_list)})', f'{method}()')
        elif method == 'index':
            add(i, f'CREATE INDEX ? ON {table} ({", ".join(cols_list)})', 'index()')
        elif method == 'foreign':
            add(i, f'ALTER TABLE {table} ADD FOREIGN KEY ({", ".join(cols_list)}) REFERENCES {knex_ref(mods)}', 'foreign()')
        elif method == 'check':
            add(i, f'ALTER TABLE {table} ADD CONSTRAINT ? CHECK ({a0 or "?"})', 'check()')
        elif method in ('dropIndex', 'dropUnique', 'dropForeign', 'dropPrimary', 'dropChecks'):
            add(i, f'ALTER TABLE {table} DROP CONSTRAINT ?' if method != 'dropIndex' else 'DROP INDEX ?', f'{method}()')
        elif method in ('setNullable', 'dropNullable'):
            add(i, f'ALTER TABLE {table} ALTER COLUMN {a0} {"DROP NOT NULL" if method == "setNullable" else "SET NOT NULL"}', f'{method}()')
        elif method == 'renameTable':
            add(i, f'ALTER TABLE {table} RENAME TO {js_str(args[1]) if len(args) > 1 else "?"}', 'renameTable()')
        else:
            skipped.append({'line': line_of(i), 'what': f'knex builder call {method}() not translated (read by hand)'})
    return stmts, skipped, cols


def parse_knex(path: Path) -> dict:
    """Knex migrations: the schema builder, knex.raw SQL and knex('table') DML in up and down, by source pattern."""
    text = path.read_text(encoding='utf-8')
    masked = js_mask(text)
    starts = [0] + [k + 1 for k, c in enumerate(text) if c == '\n']

    def line_of(i: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= i:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1
    rec = {'revision': path.stem, 'down_revision': None, 'up': [], 'down': [], 'not_analysed': [], 'down_state': 'missing',
           'transactional': not re.search(r'\bconfig\s*=\s*\{[^}]*transaction\s*:\s*false', masked)}
    for key in ('up', 'down'):
        fn = js_function(masked, key)
        if not fn:
            continue
        start, end, param = fn
        if key == 'down':
            rec['down_line'] = line_of(start)
            body = masked[start:end].strip()
            rec['down_state'] = 'empty' if not body else 'raises' if re.match(r'throw\b', body) else 'ok'
        used: list[tuple[int, int]] = []
        for m in KNEX_SCHEMA.finditer(masked, start, end):
            i, method, o = m.start(), m.group(1), m.end() - 1
            if any(s <= i < e for s, e in used):
                continue
            args = js_args(text, masked, o)
            close = js_close(masked, o)
            used.append((i, close))
            if method == 'raw':
                raw = args[0] if args else ''
                sql_text = raw[1:-1] if raw[:1] in '\'"`' else None
                if sql_text is None:
                    rec['not_analysed'].append({'line': line_of(i), 'what': 'knex.raw with SQL built at runtime'})
                    continue
                if '${' in sql_text:
                    rec['not_analysed'].append({'line': line_of(i), 'what': 'knex.raw template with interpolated values (shown as ?)'})
                    sql_text = re.sub(r'\$\{[^}]*\}', '?', sql_text)
                first = line_of(o + 1 + (len(text[o + 1:]) - len(text[o + 1:].lstrip())))
                rec[key] += [{'line': ln, 'sql': s, 'via': 'knex.raw'} for ln, s in split_sql.split(sql_text, first)]
                continue
            table = js_str(args[0]) if args else None
            table = table if isinstance(table, str) else '?'
            if method in ('dropTable', 'dropTableIfExists'):
                rec[key].append({'line': line_of(i), 'sql': f'DROP TABLE {table}', 'via': f'knex {method}()'})
                continue
            if method == 'renameTable':
                rec[key].append({'line': line_of(i), 'sql': f'ALTER TABLE {table} RENAME TO {js_str(args[1]) if len(args) > 1 else "?"}',
                                 'via': 'knex renameTable()'})
                continue
            cb = re.compile(r'\(?\s*(\w+)[^)=]*\)?\s*=>\s*\{|function\s*\w*\s*\(\s*(\w+)[^)]*\)\s*\{').search(masked, o, close)
            if not cb:
                rec['not_analysed'].append({'line': line_of(i), 'what': f'knex {method}() without a builder callback'})
                continue
            b_open = cb.end() - 1
            stmts, skipped, cols = knex_builder(text, masked, b_open + 1, js_close(masked, b_open), cb.group(1) or cb.group(2), table,
                                                method.startswith('createTable'), line_of)
            if method.startswith('createTable'):
                rec[key].append({'line': line_of(i), 'sql': f'CREATE TABLE {table} ({", ".join(cols)})', 'via': f'knex {method}()'})
            rec[key] += stmts
            rec['not_analysed'] += skipped
        # DML through the query builder: knex('table')...del() / .update()
        for m in re.finditer(r'\b(\w+)\s*\(\s*[\'"`]', masked[start:end]):
            i = start + m.start()
            if m.group(1) not in (param, 'trx', 'db') or any(s <= i < e for s, e in used):
                continue
            o = masked.index('(', i)
            table = js_str((js_args(text, masked, o) or [None])[0])
            chain, _ = js_chain(text, masked, o)
            names = [c for c, _ in chain]
            where = ' WHERE ?' if any(n.startswith(('where', 'whereIn', 'whereNot')) for n in names) else ''
            if {'del', 'delete'} & set(names):
                rec[key].append({'line': line_of(i), 'sql': f'DELETE FROM {table}{where}', 'via': 'knex query builder del()'})
            elif 'update' in names:
                rec[key].append({'line': line_of(i), 'sql': f'UPDATE {table} SET ?{where}', 'via': 'knex query builder update()'})
            elif 'insert' in names:
                rec[key].append({'line': line_of(i), 'sql': f'INSERT INTO {table} VALUES ?', 'via': 'knex query builder insert()'})
            else:
                rec['not_analysed'].append({'line': line_of(i), 'what': f'knex query on {table} not translated (read by hand)'})
        rec[key].sort(key=lambda s: s['line'])
    return rec


def parse_sql_file(path: Path, prisma: bool = False) -> dict:
    stmts = [{'line': ln, 'sql': s, 'via': 'sql'} for ln, s in split_sql.split(path.read_text(encoding='utf-8'))]
    # Prisma names a migration by its folder (20260101120000_add_orders/migration.sql); every file is migration.sql
    rev = path.parent.name if prisma or path.name == 'migration.sql' else path.stem
    rec = {'revision': rev, 'down_revision': None, 'up': stmts, 'down': [], 'transactional': True, 'not_analysed': [], 'down_state': 'missing'}
    if prisma:
        rec['down_model'] = 'prisma: no down migrations (roll forward, or restore a backup)'
    return rec


def prisma_layout(repo: Path, files: list[Path]) -> dict:
    """Where Prisma keeps its schema and migrations: prisma.config.* (schema, migrations.path), package.json "prisma",
    else the default prisma/schema.prisma; the provider from schema.prisma and migration_lock.toml."""
    out: dict = {'config': None, 'schema': None, 'migrations_path': None, 'provider': None, 'lock_provider': None}
    cfg = next((p for p in files if re.match(r'prisma\.config\.(ts|mts|cts|js|mjs|cjs)$', p.name) and p.parent == repo), None) or \
        next((p for p in files if re.match(r'prisma\.config\.(ts|mts|cts|js|mjs|cjs)$', p.name)), None)
    if cfg:
        t = cfg.read_text(encoding='utf-8', errors='replace')
        out['config'] = cfg.relative_to(repo).as_posix()
        m = re.search(r'\bschema\s*:\s*[\'"`]([^\'"`]+)', t)
        if m:
            out['schema'] = (cfg.parent / m.group(1)).resolve().relative_to(repo.resolve()).as_posix()
        m = re.search(r'\bmigrations\s*:\s*\{[^}]*?\bpath\s*:\s*[\'"`]([^\'"`]+)', t, re.S)
        if m:
            out['migrations_path'] = (cfg.parent / m.group(1)).resolve().relative_to(repo.resolve()).as_posix()
    pkg = repo / 'package.json'
    if not out['schema'] and pkg.is_file():
        try:
            out['schema'] = ((json.loads(pkg.read_text(encoding='utf-8')).get('prisma') or {}).get('schema'))
        except (ValueError, AttributeError):
            pass
    if not out['schema']:
        sch = [p for p in files if p.name == 'schema.prisma']
        out['schema'] = (next((p for p in sch if p.relative_to(repo).as_posix() == 'prisma/schema.prisma'), None) or (sch[0] if sch else None))
        out['schema'] = out['schema'].relative_to(repo).as_posix() if out['schema'] else None
    if out['schema'] and (repo / out['schema']).is_file():
        out['provider'] = sqlshape.prisma_schema((repo / out['schema']).read_text(encoding='utf-8', errors='replace'))['provider']
    lock = next((p for p in files if p.name == 'migration_lock.toml'), None)
    if lock:
        m = re.search(r'provider\s*=\s*"(\w+)"', lock.read_text(encoding='utf-8', errors='replace'))
        out['lock_provider'] = m.group(1) if m else None
        if not out['migrations_path']:
            out['migrations_path'] = lock.parent.relative_to(repo).as_posix()
    return out


def archived(rel: str, extra: list[str], use_default: bool) -> str | None:
    """Why a migration path is outside the active chain (an archive or squashed-history folder), or None."""
    parts = rel.split('/')[:-1]
    for d in extra:
        d = d.strip('/')
        if rel.startswith(d + '/') or d in parts:
            return f'--exclude-dir {d}'
    if use_default:
        hit = next((p for p in parts if ARCHIVE.search(p)), None)
        if hit:
            return f'folder {hit!r} looks like archived or squashed history (pass --include-archived to keep it)'
    return None


class ChainError(Exception):
    pass


def alembic_chains(repo: Path, files: list[Path], versions: list[Path]) -> list[dict]:
    """Each Alembic script location (the folder holding versions/ and env.py) is its own chain, with the
    alembic.ini whose script_location names it."""
    inis = []
    for p in files:
        if p.suffix == '.ini':
            t = p.read_text(encoding='utf-8', errors='replace')
            m = re.search(r'^\s*script_location\s*=\s*(.+?)\s*$', t, re.M)
            if m and '[alembic' in t:
                loc = m.group(1).replace('%(here)s', str(p.parent))
                target = (p.parent / loc) if not Path(loc).is_absolute() else Path(loc)
                inis.append((p, target.resolve()))
    out = []
    for d in sorted({p.parent.parent for p in versions}):
        env = d / 'env.py'
        ini = next((i for i, t in inis if t == d.resolve()), None)
        out.append({'dir': d.relative_to(repo).as_posix() or '.', 'env': env.relative_to(repo).as_posix() if env.is_file() else None,
                    'ini': ini.relative_to(repo).as_posix() if ini else None,
                    'migrations': sum(1 for p in versions if p.parent.parent == d)})
    return out


def detect(repo: Path, exclude_dirs: list[str] | None = None, include_archived: bool = False,
           chain: str | None = None) -> tuple[str, list[Path], dict]:
    files = list(repo_walk.files(repo))
    extra: dict = {'excluded': []}

    def keep(ps: list[Path]) -> list[Path]:
        out = []
        for p in ps:
            why = archived(p.relative_to(repo).as_posix(), exclude_dirs or [], not include_archived)
            if why:
                extra['excluded'].append({'file': p.relative_to(repo).as_posix(), 'reason': why})
            else:
                out.append(p)
        return out
    alembic = keep(sorted(p for p in files if p.suffix == '.py' and p.parent.name == 'versions' and 'revision' in p.read_text(encoding='utf-8', errors='replace')))
    if alembic:
        chains = alembic_chains(repo, files, alembic)
        if chain:
            want = chain.strip('/').replace('\\', '/')
            pick = [c for c in chains if c['dir'] == want or c['dir'].endswith('/' + want) or (c['ini'] or '') == want]
            if len(pick) != 1:
                raise ChainError(f'--chain {chain} matches {len(pick)} Alembic chain(s); the chains are: '
                                 + ', '.join(c['dir'] for c in chains))
        elif len(chains) > 1:
            raise ChainError(f'{len(chains)} Alembic chains in this repository (one per script location): '
                             + '; '.join(f'{c["dir"]} ({c["migrations"]} migrations, env {c["env"] or "none"})' for c in chains)
                             + '. Review one chain per run with --chain <dir>, e.g. --chain ' + chains[0]['dir'])
        else:
            pick = chains
        sel = pick[0]
        extra['chain'] = dict(sel, chains=[c['dir'] for c in chains])
        alembic = [p for p in alembic if p.parent.parent.relative_to(repo).as_posix() == sel['dir'] or
                   (sel['dir'] == '.' and p.parent.parent == repo)]
        return 'alembic', alembic, extra
    django = keep(sorted(p for p in files if p.suffix == '.py' and p.parent.name == 'migrations' and 'migrations.Migration' in p.read_text(encoding='utf-8', errors='replace')))
    if django:
        return 'django', django, extra
    flyway = keep(sorted(p for p in files if re.match(r'V[\d_.]+__.*\.sql$', p.name)))
    if flyway:
        return 'flyway', flyway, extra
    prisma_all = [p for p in files if p.name == 'migration.sql']
    if prisma_all:
        layout = prisma_layout(repo, files)
        extra['prisma'] = layout
        mp = layout.get('migrations_path')
        chain = []
        for p in prisma_all:
            rel = p.relative_to(repo).as_posix()
            if mp and p.parent.parent.relative_to(repo).as_posix() != mp:
                extra['excluded'].append({'file': rel, 'reason': f'outside the Prisma migrations folder {mp}'})
            else:
                chain.append(p)
        # Prisma applies migrations in folder-name order (the timestamp prefix)
        prisma = keep(sorted(chain, key=lambda p: (p.parent.name, p.as_posix())))
        if prisma:
            extra['archived_files'] = [repo / x['file'] for x in extra['excluded']]
            return 'prisma', prisma, extra
    knex = keep(sorted((p for p in files if p.suffix in ('.js', '.ts', '.mjs', '.cjs') and 'migrations' in p.parts
                        and re.search(r'\b(exports\.up|export\s+(const|async\s+function|function)\s+up)\b|\bup\s*:', p.read_text(encoding='utf-8', errors='replace'))
                        and 'knex' in p.read_text(encoding='utf-8', errors='replace').lower()), key=lambda p: p.name))
    if knex:
        return 'knex', knex, extra
    other = [p for p in files if p.suffix == '.rb' and 'db/migrate' in p.as_posix() or p.name.startswith('changelog') or
             (p.suffix in ('.js', '.ts') and 'migrations' in p.as_posix())]
    if other:
        return 'not_parsed', other, extra
    return 'sql', keep(sorted(p for p in files if p.suffix == '.sql')), extra


AZURE_DB = re.compile(r'Microsoft\.DBfor(PostgreSQL|MySQL|MariaDB)/(flexibleServers|servers)\b', re.I)


def azure_claim(t: str) -> dict | None:
    """An Azure database server's engine and version from Bicep or ARM JSON: the resource type
    Microsoft.DBforPostgreSQL/flexibleServers (or MySQL/MariaDB) and its version property, following a Bicep
    param default (param version string = '16') or an ARM [parameters('x')] defaultValue."""
    m = AZURE_DB.search(t)
    if not m:
        return None
    eng = {'postgresql': 'postgresql', 'mysql': 'mysql', 'mariadb': 'mariadb'}[m.group(1).lower()]
    after = t[m.start():]

    def line_of(i: int) -> int:
        return t[:i].count('\n') + 1
    v = re.search(r'''["']?version["']?\s*:\s*["']((\d+)(?:\.\d+)?)["']''', after)
    if v:
        return {'kind': 'infrastructure', 'name': eng, 'version': v.group(1), 'line': line_of(m.start() + v.start())}
    ref = re.search(r'\bversion\s*:\s*(\w+)\b', after) or re.search(r'''"version"\s*:\s*"\[parameters\('(\w+)'\)\]"''', after)
    if ref:
        p = ref.group(1)
        d = re.search(rf'^\s*param\s+{re.escape(p)}\s+string\s*=\s*["\'](\d+(?:\.\d+)?)["\']', t, re.M) or \
            re.search(rf'"{re.escape(p)}"\s*:\s*\{{[^}}]*?"defaultValue"\s*:\s*"(\d+(?:\.\d+)?)"', t, re.S)
        if d:
            return {'kind': 'infrastructure', 'name': eng, 'version': d.group(1), 'line': line_of(d.start(1))}
    return None


DEV_SOURCE = re.compile(r'(^|/)(docker-compose[^/]*|compose[^/]*\.ya?ml|Dockerfile[^/]*|\.github/|\.gitlab-ci|\.circleci/|.*\.dev\.|.*\.test\.)', re.I)


def engine(repo: Path, a, prisma: dict | None = None) -> dict:
    """The engine and version: given, else what the repository's infrastructure declares (CloudFormation or Terraform
    Engine/EngineVersion), else docker-compose and Dockerfile image tags (with or without a registry prefix). Of
    several versions, the oldest from the most production-like source is used, and every claim is listed. A version
    taken only from a docker-compose file, a Dockerfile or CI (development sources) is marked assumed."""
    if a.engine:
        return {'name': a.engine, 'version': a.engine_version, 'source': f'given: {a.engine_source}' if getattr(a, 'engine_source', None) else 'given',
                'assumed': a.engine_version is None}
    name, url_source, claims = None, None, []
    names = {'postgres': 'postgresql', 'postgresql': 'postgresql', 'aurora-postgresql': 'postgresql', 'mysql': 'mysql',
             'aurora-mysql': 'mysql', 'aurora': 'mysql', 'mariadb': 'mariadb'}
    roots = [(repo, '')] + [(Path(r), Path(r).as_posix().rstrip('/') + '/') for r in (getattr(a, 'infra', None) or [])]
    for root, label, p in ((r, lb, p) for r, lb in roots for p in repo_walk.files(r)):
        if p.suffix not in ('.ini', '.py', '.yml', '.yaml', '.env', '.toml', '.json', '.tf', '.bicep') and not p.name.startswith('Dockerfile'):
            continue
        if p.name == 'package-lock.json' or p.name.endswith('.lock.json'):
            continue
        t = p.read_text(encoding='utf-8', errors='replace')
        rel = label + p.relative_to(root).as_posix()
        az = azure_claim(t)
        if az:
            line = az.pop('line', None)
            claims.append(dict(az, source=f'{rel}:{line}' if line else rel))
            continue
        m = re.search(r'\b(postgresql|postgres|mysql)(?:\+\w+)?://', t)
        if m and not name and not label:
            name, url_source = ('mysql' if m.group(1) == 'mysql' else 'postgresql'), rel
        for m in re.finditer(r'(?:image:\s*|FROM\s+)["\']?(?:[\w.\-]+(?::\d+)?/)*(postgres|mysql|mariadb):(\d+(?:\.\d+)?)', t):
            claims.append({'kind': 'image', 'name': names[m.group(1)], 'version': m.group(2), 'source': rel})
        eng = re.search(r'\bEngine["\']?\s*[:=]\s*["\']?(aurora-postgresql|aurora-mysql|postgres|mysql|mariadb)\b', t)
        ver = re.search(r'\bEngineVersion["\']?\s*[:=]\s*["\']?(\d+(?:\.\d+)?)', t) or re.search(r'\bengine_version\s*=\s*"(\d+(?:\.\d+)?)', t)
        ref = re.search(r'\bEngineVersion["\']?\s*:\s*!Ref\s+(\w+)', t)
        if ref and not ver:  # a CloudFormation parameter: use its Default
            ver = re.search(rf'^\s*{ref.group(1)}:\s*\n(?:[ \t]+.*\n)*?[ \t]+Default:\s*["\']?(\d+(?:\.\d+)?)', t, re.M)
        if not eng:
            eng = re.search(r'\bengine\s*=\s*"(aurora-postgresql|aurora-mysql|postgres|mysql|mariadb)"', t)
        if eng and ver:
            claims.append({'kind': 'infrastructure', 'name': names[eng.group(1)], 'version': ver.group(1), 'source': rel})
    if not name and prisma:
        prov = prisma.get('provider') or prisma.get('lock_provider')
        if prov in ('postgresql', 'postgres', 'cockroachdb', 'mysql'):
            name = 'mysql' if prov == 'mysql' else 'postgresql'
            url_source = prisma.get('schema') if prisma.get('provider') else prisma.get('config') or 'migration_lock.toml'

    claims = [c for i, c in enumerate(claims) if c not in claims[:i]]

    def key(c):
        return tuple(int(x) for x in c['version'].split('.'))
    best = [c for c in claims if c['kind'] == 'infrastructure'] or claims
    if name:
        best = [c for c in best if c['name'] == name] or best
    chosen = min(best, key=key) if best else None
    name = name or (chosen or {}).get('name')
    version = chosen['version'] if chosen else None
    source = ' and '.join(x for x in (url_source, chosen and chosen['source']) if x) or None
    assumed = version is None
    out = {'name': name or 'unknown', 'version': version, 'source': source, 'assumed': assumed}
    if assumed:
        out['version'] = {'postgresql': '10', 'mysql': '5.7'}.get(name)
        out['assumed_reason'] = 'no version found in the repository; the oldest supported version is assumed'
    elif chosen['kind'] == 'image' and DEV_SOURCE.search(chosen['source']):
        out['assumed'] = True
        out['assumed_reason'] = (f'the version comes from {chosen["source"]}, a development or CI image, not from the production '
                                 'database; confirm it (or pass --engine-version)')
    if len(claims) > 1:
        out['claims'] = claims
    return out


TYPE_STMT = re.compile(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ALTER COLUMN (\S+) (?:SET DATA )?TYPE (.+?)(?: USING .*?)?(?: /\* from (.+) \*/)?$', re.I)
ADD_STMT = re.compile(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ADD (?:COLUMN )?(?:IF NOT EXISTS )?(\S+) (.+)$', re.I)


def statement_types(sql: str) -> list[tuple[str, str, str]]:
    """(table, column, type) set by one statement: CREATE TABLE columns, ADD COLUMN and ALTER COLUMN TYPE clauses."""
    n = sqlshape.norm_sql(sql)
    if re.match(r'CREATE (?:UNLOGGED |TEMP(?:ORARY)? )?TABLE', n, re.I):
        m = re.match(r'CREATE (?:UNLOGGED |TEMP(?:ORARY)? )?TABLE (?:IF NOT EXISTS )?(\S+)', n, re.I)
        return [(sqlshape.ident(m.group(1)), c, t) for c, t in sqlshape.column_types(n).items()] if m else []
    out = []
    for c in sqlshape.clauses(n):
        m = TYPE_STMT.match(c)
        if m:
            out.append((sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2)), sqlshape.type_of(m.group(3))))
            continue
        m = ADD_STMT.match(c)
        if m and m.group(2).upper() not in ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'EXCLUDE', 'INDEX', 'KEY'):
            out.append((sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2)), sqlshape.type_of(m.group(3))))
    return out


def git_out(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def type_history(repo: Path, tool: str, migs: list[dict], extra: dict) -> dict:
    """For each column a pending migration retypes, the types it had before: from the chain (earlier migrations), from
    archived or squashed migrations, and for Prisma from schema.prisma before the first pending migration was added
    (git) and at HEAD. The scan compares each with the new type to find narrowing on any upgrade path."""
    wanted = set()
    for m in migs:
        for s in m.get('up', []) if m.get('pending') else []:
            for cl in sqlshape.clauses(sqlshape.norm_sql(s['sql'])):
                hit = TYPE_STMT.match(cl)
                if hit:
                    wanted.add((sqlshape.ident(hit.group(1)), sqlshape.ident(hit.group(2))))
    hist: dict = {}
    if not wanted:
        return hist
    full: dict = {}
    texts: dict = {}

    def col_line(file: str, start: int, col: str) -> int:
        """The line that declares col, at or after the statement's first line (else the statement's line)."""
        if file not in texts:
            p = repo / file
            texts[file] = p.read_text(encoding='utf-8', errors='replace').splitlines() if p.is_file() else []
        rx = re.compile(rf'(^|[\s,(])["`\[]?{re.escape(col)}["`\]]?\s+\w', re.I)
        for n in range(start, min(len(texts[file]), start + 400) + 1):
            if rx.search(texts[file][n - 1]):
                return n
        return start

    def replay(sql: str, **src):
        """Record the types one statement sets; carry a column's history across RENAME COLUMN and RENAME TO."""
        n = sqlshape.norm_sql(sql)
        r = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) RENAME COLUMN (\S+) TO (\S+)', n, re.I)
        if r:
            t, old, new = (sqlshape.ident(x) for x in r.groups())
            full[(t, new)] = list(full.get((t, old), []))
            return
        r = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) RENAME TO (\S+)', n, re.I)
        if r:
            old, new = sqlshape.ident(r.group(1)), sqlshape.ident(r.group(2))
            for (t, c), v in list(full.items()):
                if t == old:
                    full[(new, c)] = list(v)
            return
        for t, c, typ in statement_types(sql):
            entry = dict(type=typ, column=c, **src)
            if src.get('file', '').endswith('.sql'):
                entry['line'] = col_line(src['file'], src['line'], c)
            full.setdefault((t, c), []).append(entry)
    for p in sorted(extra.get('archived_files') or [], key=lambda p: (p.parent.name, p.name)):
        if p.suffix != '.sql':
            continue
        for ln, sql in split_sql.split(p.read_text(encoding='utf-8', errors='replace')):
            replay(sql, origin='archive', migration=p.parent.name if p.name == 'migration.sql' else p.stem,
                   file=p.relative_to(repo).as_posix(), line=ln)
    for m in migs:
        for s in m.get('up', []):
            replay(s['sql'], origin='chain', migration=m['id'], file=m['file'], line=s['line'])
    for k in sorted(wanted):
        if full.get(k):
            hist[f'{k[0]}.{k[1]}'] = full[k]

    def add(t, c, typ, **src):
        if (t, c) in wanted:
            hist.setdefault(f'{t}.{c}', []).append(dict(type=typ, **src))
    schema = (extra.get('prisma') or {}).get('schema')
    if tool == 'prisma' and schema and (repo / schema).is_file():
        for t, cols in sqlshape.prisma_columns(sqlshape.prisma_schema((repo / schema).read_text(encoding='utf-8', errors='replace'))).items():
            for c, f in cols.items():
                add(t, c, f['type'], origin='schema', file=schema, line=f['line'], field=f['field'], model=f['model'])
        first = next((m for m in migs if m.get('pending')), None)
        top = git_out(repo, 'rev-parse', '--show-toplevel')
        if first and top and Path(top.strip()).resolve() == repo.resolve():
            sha = (git_out(repo, 'log', '--diff-filter=A', '--format=%H', '-1', '--', first['file']) or '').strip()
            before = git_out(repo, 'show', f'{sha}^:{schema}') if sha else None
            if before:
                for t, cols in sqlshape.prisma_columns(sqlshape.prisma_schema(before)).items():
                    for c, f in cols.items():
                        add(t, c, f['type'], origin='schema_git', file=schema, line=f['line'], field=f['field'], model=f['model'],
                            revision=f'{sha[:7]}^', source=f'git show {sha[:7]}^:{schema}')
    return hist


def files_exist(repo: Path, mig_file: Path, chain: dict | None, pattern: str) -> bool:
    """Whether a path a migration reads at run time exists now (relative to the migration, its chain or the repo;
    a glob or a {placeholder} matches if any file does)."""
    pat = re.sub(r'\{[^}]*\}', '*', pattern.replace('\\', '/')).lstrip('/')
    # the migration's folder and every folder above it up to the repo root (a path is usually relative to one of them)
    bases, d = [], (repo / mig_file).parent.resolve()
    root = repo.resolve()
    while True:
        bases.append(d)
        if d == root or root not in d.parents:
            break
        d = d.parent
    if chain:
        bases.append((repo / chain['dir']).resolve())
    for b in bases:
        try:
            if any(ch in pat for ch in '*?['):
                if next(iter(b.glob(pat)), None):
                    return True
            elif (b / pat).exists():
                return True
        except (OSError, ValueError):
            continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--since', help='the last deployed migration (Prisma: its folder name); everything after it is pending')
    ap.add_argument('--migrations', help='comma-separated migration ids to review')
    ap.add_argument('--exclude-dir', action='append', default=[], help='a folder (relative to the repo, or a folder name) to leave out of the chain')
    ap.add_argument('--include-archived', action='store_true',
                    help='keep folders that look like archived or squashed history (archive, squashed, history, old) in the chain')
    ap.add_argument('--chain', help='Alembic: the script location (folder holding env.py and versions/) to review, when the repo has several chains')
    ap.add_argument('--engine', choices=['postgresql', 'mysql', 'mariadb', 'sqlite'])
    ap.add_argument('--engine-version')
    ap.add_argument('--engine-source', help='where a given --engine-version comes from (e.g. infra-repo/db.bicep:11), recorded as provenance')
    ap.add_argument('--infra', type=Path, action='append', default=[],
                    help='another repository or folder holding the infrastructure (Terraform, CloudFormation, Bicep, ARM) to read the engine version from')
    a = ap.parse_args()
    try:
        tool, files, extra = detect(a.repo, a.exclude_dir, a.include_archived, a.chain)
    except ChainError as ex:
        print(f'error: {ex}', file=sys.stderr)
        return 2
    parser = {'alembic': parse_alembic, 'django': parse_django, 'knex': parse_knex}.get(tool)
    migs = []
    for p in files:
        if tool == 'not_parsed':
            migs.append({'id': p.stem, 'file': p.relative_to(a.repo).as_posix(), 'parsed': False})
            continue
        r = parser(p) if parser else parse_sql_file(p, prisma=tool == 'prisma')
        r.update(id=r['revision'] or p.stem, file=p.relative_to(a.repo).as_posix(), parsed=True)
        if tool == 'flyway':
            undo = p.with_name('U' + p.name[1:])
            r['down_state'] = 'ok' if undo.exists() else 'missing'
        for rf in r.get('reads_files', []):
            rf['exists'] = {x: files_exist(a.repo, p.relative_to(a.repo), extra.get('chain'), x) for x in rf['paths']}
        r['created_tables'] = [sqlshape.ident(m.group(1)) for s in r['up']
                               for m in [re.match(r'CREATE (?:UNLOGGED |TEMP(?:ORARY)? )?TABLE (?:IF NOT EXISTS )?(\S+)', sqlshape.norm_sql(s['sql']), re.I)] if m]
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
    if tool == 'prisma':
        for prev, m in zip([None] + migs[:-1], migs):
            m['down_revision'] = prev['id'] if prev else None
    ids = [m['id'] for m in migs]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        files_of = {i: [m['file'] for m in migs if m['id'] == i] for i in dup[:3]}
        print(f'error: migration ids are not unique within the chain ({", ".join(dup[:5])}): '
              + '; '.join(f'{i} in {", ".join(fs)}' for i, fs in files_of.items())
              + '. Leave the copy that is not part of the chain out with --exclude-dir <folder>'
              + (' (or pick the chain with --chain)' if tool == 'alembic' else ''), file=sys.stderr)
        return 2
    if a.migrations:
        pending = [x.strip() for x in a.migrations.split(',') if x.strip()]
        unknown = [x for x in pending if x not in ids]
        if unknown:
            print(f'error: --migrations names {", ".join(unknown)}, not in the chain ({len(ids)} migrations: {", ".join(ids[-5:])} ...)', file=sys.stderr)
            return 2
    elif a.since:
        if a.since not in ids:
            print(f'error: --since {a.since} matches no migration in the chain; nothing was selected. '
                  f'The chain has {len(ids)} migration(s), the last ones: {", ".join(ids[-5:]) or "none"}'
                  + (' (Prisma ids are folder names)' if tool == 'prisma' else ''), file=sys.stderr)
            return 2
        pending = ids[ids.index(a.since) + 1:]
    else:
        pending = ids
        if tool == 'prisma' and len(ids) > 1:
            print(f'warning: no --since given, so all {len(ids)} Prisma migrations are pending. Pass --since <folder name> of the '
                  'last migration in _prisma_migrations (or the baseline marked applied with prisma migrate resolve).', file=sys.stderr)
    for m in migs:
        m['pending'] = m['id'] in pending
    out = {'repo': a.repo.as_posix(), 'tool': tool, 'engine': engine(a.repo, a, extra.get('prisma')), 'heads': heads, 'since': a.since,
           'migrations': migs, 'excluded': extra['excluded'],
           'not_parsed': [m['file'] for m in migs if not m.get('parsed', True)] + ([] if tool != 'not_parsed' else [])}
    if extra.get('prisma'):
        out['prisma'] = extra['prisma']
    if extra.get('chain'):
        out['chain'] = extra['chain']
    out['type_history'] = type_history(a.repo, tool, migs, extra)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    eng = out['engine']
    print(f'{tool}: {len(migs)} migration(s), {len(pending)} pending; engine {eng["name"]} {eng["version"]}'
          f'{" (assumed)" if eng["assumed"] else ""}; heads {heads}'
          f'{"; " + str(len(out["excluded"])) + " file(s) excluded from the chain" if out["excluded"] else ""} -> {a.out}', file=sys.stderr)
    if eng['assumed'] and eng.get('assumed_reason'):
        print(f'  engine version assumed: {eng["assumed_reason"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
