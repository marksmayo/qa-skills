#!/usr/bin/env python3
"""Find a repository's migrations, the tool and engine, and turn every migration into SQL-shaped statements with lines.

    python detect_migrations.py --repo examples/ledger --out run/migrations.json [--since 0002] [--engine postgresql --engine-version 16]

Tools: Alembic (Python AST of upgrade() and downgrade(): op.* calls become the SQL they run, op.execute SQL
is split), Django (the operations list: AddField, RemoveField, RenameField, AlterField, AddIndex, RunSQL,
RunPython), Knex (the schema builder, knex.raw SQL and knex('table') deletes and updates in up and down, by
source pattern; anything else is listed as not analysed), and SQL files (Flyway V__/U__, Prisma migration.sql,
any other .sql). Rails and Liquibase are listed as not parsed in this version.

Engine and version: --engine/--engine-version, else settings URLs (postgresql://, mysql://), infrastructure
(CloudFormation or Terraform Engine and EngineVersion), and docker-compose or Dockerfile image tags (postgres:16.4,
also behind a registry prefix). Without a version, the oldest the repository claims is assumed and
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
    knex = sorted((p for p in files if p.suffix in ('.js', '.ts', '.mjs', '.cjs') and 'migrations' in p.parts
                   and re.search(r'\b(exports\.up|export\s+(const|async\s+function|function)\s+up)\b|\bup\s*:', p.read_text(encoding='utf-8', errors='replace'))
                   and 'knex' in p.read_text(encoding='utf-8', errors='replace').lower()), key=lambda p: p.name)
    if knex:
        return 'knex', knex
    other = [p for p in files if p.suffix == '.rb' and 'db/migrate' in p.as_posix() or p.name.startswith('changelog') or
             (p.suffix in ('.js', '.ts') and 'migrations' in p.as_posix())]
    if other:
        return 'not_parsed', other
    return 'sql', sorted(p for p in files if p.suffix == '.sql')


def engine(repo: Path, a) -> dict:
    """The engine and version: given, else what the repository's infrastructure declares (CloudFormation or Terraform
    Engine/EngineVersion), else docker-compose and Dockerfile image tags (with or without a registry prefix). Of
    several versions, the oldest from the most production-like source is used, and every claim is listed."""
    if a.engine:
        return {'name': a.engine, 'version': a.engine_version, 'source': 'given', 'assumed': a.engine_version is None}
    name, url_source, claims = None, None, []
    names = {'postgres': 'postgresql', 'postgresql': 'postgresql', 'aurora-postgresql': 'postgresql', 'mysql': 'mysql',
             'aurora-mysql': 'mysql', 'aurora': 'mysql', 'mariadb': 'mariadb'}
    for p in sorted(repo.rglob('*')):
        if not p.is_file() or set(p.relative_to(repo).parts) & SKIP or \
                (p.suffix not in ('.ini', '.py', '.yml', '.yaml', '.env', '.toml', '.json', '.tf') and not p.name.startswith('Dockerfile')):
            continue
        t = p.read_text(encoding='utf-8', errors='replace')
        rel = p.relative_to(repo).as_posix()
        m = re.search(r'\b(postgresql|postgres|mysql)(?:\+\w+)?://', t)
        if m and not name:
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
    if assumed:
        version = {'postgresql': '10', 'mysql': '5.7'}.get(name)
    out = {'name': name or 'unknown', 'version': version, 'source': source, 'assumed': assumed}
    if len(claims) > 1:
        out['claims'] = claims
    return out


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
    parser = {'alembic': parse_alembic, 'django': parse_django, 'knex': parse_knex}.get(tool, parse_sql_file)
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
