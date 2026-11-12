#!/usr/bin/env python3
"""Match the rule catalogue against the pending migrations and write candidates.json.

    python scan_migrations.py --migrations run/migrations.json --repo examples/ledger --out run/candidates.json \
        [--table-stats examples/ledger/table_stats.json] [--squawk squawk.sarif] [--other-readers ../reports]

Every candidate carries the facts triage needs: the statement and line, the table and its size (with the
size source), the engine and version range the rule holds for, code hits for compatibility rules, and
context facts (table created in the same release, constant default on PostgreSQL 11+, column with no code
readers, the column's type before a type change and where that came from). Nothing is dismissed here:
triage promotes or dismisses every candidate, with a reason.

Identifiers are compared unquoted ("Order" and Order are the same table; table statistics keyed by the
plain name match), and every clause of a multi-clause ALTER TABLE is checked on its own. For Prisma,
column readers are searched by column and by Prisma field name (@map), writers include
prisma.<model>.create/createMany/upsert calls, and the missing down migrations give one IRREVERSIBLE
candidate for the release rather than one per migration.
Squawk SARIF results join as candidates with source "squawk"; one at the same file and line as a built-in
candidate is attached to it instead.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import find_readers, repo_walk, sqlshape  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))['rules']
VOLATILE = re.compile(r'\b(now|random|gen_random_uuid|uuid_generate_v4|clock_timestamp|nextval|current_timestamp|statement_timestamp)\b', re.I)
DDL_LOCKING = re.compile(r'^(ALTER TABLE|CREATE INDEX|CREATE UNIQUE INDEX|DROP INDEX|DROP TABLE|TRUNCATE)\b', re.I)
LOCKED_TABLE = re.compile(r'(?:ALTER TABLE (?:ONLY )?(?:IF EXISTS )?|DROP TABLE (?:IF EXISTS )?|TRUNCATE(?: TABLE)? |'
                          r'CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?\S+ ON (?:ONLY )?)(\S+)', re.I)
NUM = re.compile(r'(?:NUMERIC|DECIMAL)\s*\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\)', re.I)
INT_DIGITS = {'SMALLINT': 4, 'INTEGER': 9, 'INT': 9, 'BIGINT': 18}
FLOATS = {'DOUBLE PRECISION': 15, 'REAL': 6}


def size_of(table: str, stats: dict, created: dict) -> dict:
    if table in created:
        return {'class': 'small', 'rows': 0, 'source': f'created in pending migration {created[table]}'}
    tables = stats.get('tables') or {}
    t = tables.get(table) or next((v for k, v in tables.items() if sqlshape.ident(k) == table), None) or \
        next((v for k, v in tables.items() if sqlshape.ident(k).lower() == table.lower()), None)
    if not t:
        return {'class': 'unknown', 'rows': None, 'source': 'no statistics given; rated as large'}
    rows, size_b = t.get('rows'), t.get('bytes') or 0
    cls = 'large' if rows > 1_000_000 or size_b > 1_000_000_000 else 'medium' if rows >= 10_000 else 'small'
    return {'class': cls, 'rows': rows, 'source': stats.get('source', 'table statistics file')}


def narrowing(frm: str, to: str) -> str | None:
    """Why a type change from frm to to can lose data, or None if it can't be shown."""
    f, t = sqlshape.canon_type(frm), sqlshape.canon_type(to)
    if f == t:
        return None
    fb, tb = f.split('(')[0].strip(), t.split('(')[0].strip()
    fn, tn = NUM.search(f), NUM.search(t)
    if fb in FLOATS and tb == 'NUMERIC':
        if not tn:
            return f'{fb} values are rounded to {FLOATS[fb]} significant digits'
        prec, scale = int(tn.group(1)), int(tn.group(2) or 0)
        return (f'{fb} values are rounded to {scale} decimal place(s), and any value of 1e{prec - scale} or more '
                'fails the conversion and aborts the migration')
    if fb in FLOATS and tb in INT_DIGITS:
        return 'fractions are rounded away and values above the integer range are rejected'
    if fb == 'DOUBLE PRECISION' and tb == 'REAL':
        return 'precision drops from about 15 to about 6 significant digits'
    if fb == 'NUMERIC' and tb in INT_DIGITS:
        if not fn:
            return 'fractions are rounded away and values above the integer range are rejected'
        prec, scale = int(fn.group(1)), int(fn.group(2) or 0)
        why = []
        if scale:
            why.append(f'the {scale} decimal place(s) are rounded away')
        if prec - scale > INT_DIGITS[tb]:
            why.append('values above the integer range are rejected')
        return ' and '.join(why) or None
    if fb == 'NUMERIC' and tb == 'NUMERIC' and tn:
        p2, s2 = int(tn.group(1)), int(tn.group(2) or 0)
        if not fn:
            return f'values are rounded to {s2} decimal place(s), and values of 1e{p2 - s2} or more are rejected'
        p1, s1 = int(fn.group(1)), int(fn.group(2) or 0)
        why = []
        if s2 < s1:
            why.append(f'values are rounded from {s1} to {s2} decimal place(s)')
        if p2 - s2 < p1 - s1:
            why.append(f'values of 1e{p2 - s2} or more are rejected')
        return ' and '.join(why) or None
    if fb in INT_DIGITS and tb == 'NUMERIC' and tn:
        p2, s2 = int(tn.group(1)), int(tn.group(2) or 0)
        return f'values of 1e{p2 - s2} or more are rejected' if p2 - s2 < INT_DIGITS[fb] + 1 else None
    order = ['SMALLINT', 'INTEGER', 'BIGINT']
    if fb in order and tb in order and order.index(tb) < order.index(fb):
        return 'values above the smaller range are rejected'
    fl, tl = re.search(r'VARCHAR\((\d+)\)', f), re.search(r'VARCHAR\((\d+)\)', t)
    if tl and (fb == 'TEXT' or (fl and int(tl.group(1)) < int(fl.group(1)))):
        return 'longer strings are rejected'
    return None


def clause_line(lines: list[str], start: int, clause: str, first: bool) -> int:
    """The file line of one clause of a multi-clause statement that starts at line `start`."""
    if not lines:
        return start
    m = re.search(r'\b(ALTER|ADD|DROP|RENAME|VALIDATE|SET)\s+(?:COLUMN\s+|CONSTRAINT\s+)?(?:IF (?:NOT )?EXISTS\s+)?(\S+)', clause.split(' ', 3)[-1], re.I)
    if not m:
        return start
    word = sqlshape.ident(m.group(2))
    rx = re.compile(rf'\b{m.group(1)}\b\s+(?:COLUMN\s+|CONSTRAINT\s+)?(?:IF (?:NOT )?EXISTS\s+)?["`\[]?{re.escape(word)}\b', re.I)
    for n in range(start, min(len(lines), start + 400) + 1):
        if rx.search(lines[n - 1]):
            return n
    return start


class Scan:
    def __init__(self, data: dict, repo: Path, stats: dict, other: list[Path]):
        self.data, self.repo, self.stats, self.roots = data, repo, stats, [repo] + other
        self.engine = data['engine']
        self.tool = data.get('tool')
        self.pending = [m for m in data['migrations'] if m.get('pending') and m.get('parsed', True)]
        self.not_parsed = [m['file'] for m in data['migrations'] if m.get('pending') and not m.get('parsed', True)]
        self.created = {sqlshape.ident(t): m['id'] for m in self.pending for t in m.get('created_tables', [])}
        self.order = {m['id']: i for i, m in enumerate(data['migrations'])}
        self.history = data.get('type_history') or {}
        self.out: list[dict] = []
        self._readers: dict = {}
        schema = (data.get('prisma') or {}).get('schema')
        self.prisma_cols = sqlshape.prisma_columns(sqlshape.prisma_schema((repo / schema).read_text(encoding='utf-8', errors='replace'))) \
            if schema and (repo / schema).is_file() else {}
        self.index_before = {}
        for m in data['migrations']:
            if not m.get('pending'):
                for s in m.get('up', []):
                    hit = re.match(r'CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?(\S+) ON', sqlshape.norm_sql(s['sql']), re.I)
                    if hit:
                        self.index_before[sqlshape.ident(hit.group(1))] = m['id']

    def field_names(self, table: str, column: str) -> list[str]:
        """Names code uses for a column: the column, and its Prisma field name when @map differs."""
        names = [column]
        f = (self.prisma_cols.get(table) or {}).get(column)
        if f and f['field'] != column:
            names.append(f['field'])
        elif not f and self.prisma_cols and '_' in column.strip('_'):
            # a dropped or renamed column is gone from schema.prisma; Prisma's usual @map is snake_case to camelCase
            camel = re.sub(r'_([a-z0-9])', lambda m: m.group(1).upper(), column.strip('_'))
            if camel != column:
                names.append(camel)
        return names

    def readers(self, table: str, column: str) -> dict:
        key = (table, column)
        if key not in self._readers:
            res = {'code': [], 'test': [], 'migration': []}
            for name in self.field_names(table, column):
                hits = find_readers.readers(self.roots, name)
                for k in res:
                    res[k] += [h for h in hits[k] if (h['file'], h['line']) not in {(x['file'], x['line']) for x in res[k]}]
            self._readers[key] = res
        return self._readers[key]

    def add(self, rule: str, mig: dict | None, stmt: dict | None, table: str | None, engine_range: str, facts: dict | None = None,
            column: str | None = None, hits: list | None = None, file: str | None = None, line: int | None = None, **fmt):
        r = RULES[rule]
        effect = r['effect'].format(table=table, column=column, **fmt)
        self.out.append({
            'id': f'C{len(self.out) + 1:03d}', 'source': 'builtin', 'rule': rule, 'category': r['category'], 'hazard': r['hazard'],
            'migration': mig['id'] if mig else None, 'file': file or mig['file'], 'line': line or (stmt or {}).get('line'),
            'statement': (stmt or {}).get('sql'), 'direction': 'up', 'table': table, 'column': column,
            'engine': self.engine['name'], 'engine_version': self.engine.get('version'), 'engine_range': engine_range,
            'engine_assumed': bool(self.engine.get('assumed')),
            'size': size_of(table, self.stats, self.created) if table else None,
            'effect': effect, 'rewrite': r['rewrite'], 'code_hits': hits or [], 'facts': facts or {},
        })

    def statements(self, mig: dict) -> list[dict]:
        """The migration's up statements, one per clause of a multi-clause ALTER TABLE, each with its normalised form."""
        out, lines = [], None
        for st in mig['up']:
            norm = sqlshape.norm_sql(st['sql'])
            parts = sqlshape.clauses(norm)
            if len(parts) == 1:
                out.append(dict(st, norm=norm, stmt_line=st['line']))
                continue
            if lines is None:
                p = self.repo / mig['file']
                lines = p.read_text(encoding='utf-8', errors='replace').splitlines() if p.is_file() else []
            for k, cl in enumerate(parts):
                ln = clause_line(lines, st['line'], cl, k == 0) if st.get('via') == 'sql' else st['line']
                out.append({'line': ln, 'sql': cl, 'norm': cl, 'via': st.get('via'), 'stmt_line': st['line']})
        return out

    def types_before(self, mig: dict, stmt_line: int, table: str, col: str) -> list[dict]:
        """Every type the column had before this statement, on any path: the chain, archived history, the old schema."""
        hist = self.history.get(f'{table}.{col}') or []
        mine = self.order.get(mig['id'], 0)
        chain = [h for h in hist if h['origin'] == 'chain' and (self.order.get(h['migration'], -1) < mine or
                                                               (h['migration'] == mig['id'] and h['line'] < stmt_line))]
        out = []
        if chain:
            last = max(chain, key=lambda h: (self.order.get(h['migration'], -1), h['line']))
            out.append(dict(last, path='chain'))
        arch = [h for h in hist if h['origin'] == 'archive']
        if arch:
            out.append(dict(max(arch, key=lambda h: (h['migration'], h['line'])), path='archived history'))
        out += [dict(h, path='schema before this release') for h in hist if h['origin'] == 'schema_git']
        return out

    def run(self) -> list[dict]:
        pg, my = self.engine['name'] == 'postgresql', self.engine['name'] in ('mysql', 'mariadb')
        ver = float(re.match(r'\d+(\.\d+)?', self.engine.get('version') or '0').group(0))
        prisma_irreversible = []
        for mig in self.pending:
            ddl = 0
            for st in self.statements(mig):
                sql, norm = st['sql'], st['norm']
                up = norm.upper()
                stmt_line = st['stmt_line']
                st = {'line': st['line'], 'sql': sql}
                if DDL_LOCKING.match(norm):
                    ddl += 1
                m = re.match(r'CREATE (UNIQUE )?INDEX (CONCURRENTLY )?(IF NOT EXISTS )?(\S+) ON (?:ONLY )?(\S+)', norm, re.I)
                if m and pg:
                    table = sqlshape.ident(m.group(5))
                    if m.group(2) and mig.get('transactional'):
                        self.add('PG-CONCURRENTLY-IN-TRANSACTION', mig, st, table, 'postgresql: all versions')
                    elif not m.group(2):
                        facts = {'table_created_in_release': self.created.get(table)}
                        if m.group(3) and self.index_before.get(sqlshape.ident(m.group(4))):
                            facts['index_created_before_by'] = self.index_before[sqlshape.ident(m.group(4))]
                        self.add('PG-INDEX-LOCKS-WRITES', mig, st, table, 'postgresql: all versions', facts)
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) RENAME COLUMN (\S+) TO (\S+)', norm, re.I)
                if m:
                    table, old, new = (sqlshape.ident(x) for x in m.groups())
                    hits = self.readers(table, old)
                    self.add('RENAME-WHILE-READ', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'new_name': new, 'test_hits': len(hits['test']), 'migration_hits': len(hits['migration']),
                              'names_searched': self.field_names(table, old)},
                             column=old, hits=hits['code'])
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ALTER COLUMN (\S+) (?:SET DATA )?TYPE (.+?)(?: USING (.*?))?(?: /\* from (.+) \*/)?$', norm, re.I)
                if m:
                    table, col = sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2))
                    to, using, frm = sqlshape.type_of(m.group(3)), m.group(4), m.group(5)
                    before = ([{'type': frm, 'path': 'the migration (existing_type)', 'origin': 'migration'}] if frm else []) + \
                        self.types_before(mig, stmt_line, table, col)
                    chain_before = next((b['type'] for b in before if b.get('path') == 'chain'), frm)
                    facts = {'to': to, 'from': frm or chain_before}
                    if using:
                        facts['using'] = using
                    if chain_before:
                        facts['type_before_on_chain'] = chain_before
                        facts['same_type_on_chain'] = sqlshape.canon_type(chain_before) == sqlshape.canon_type(to)
                    if pg:
                        self.add('PG-TYPE-CHANGE-REWRITE', mig, st, table, 'postgresql: all versions (most type changes rewrite)', facts, column=col)
                    for b in before:
                        why = narrowing(b['type'], to)
                        if why:
                            src = b.get('source') or (f'{b["file"]}:{b["line"]}' if b.get('file') else b['path'])
                            others = [{'type': x['type'], 'path': x['path'], 'source': x.get('source') or (f'{x["file"]}:{x["line"]}' if x.get('file') else None)}
                                      for x in before if x is not b]
                            self.add('TYPE-NARROWING', mig, st, table, f'{self.engine["name"]}: all versions',
                                     {'why': why, 'from': b['type'], 'from_path': b['path'], 'from_source': src, 'other_types_before': others},
                                     column=col, **{'from': b['type'], 'to': to})
                            break
                m = re.match(r'ALTER TABLE (\S+) (?:MODIFY|CHANGE) (?:COLUMN )?(\S+)', norm, re.I)
                if m and my:
                    self.add('MYSQL-COPY-ALTER', mig, st, sqlshape.ident(m.group(1)), 'mysql 5.6+ / 8.0: column type changes copy the table',
                             column=sqlshape.ident(m.group(2)))
                if my and 'ALGORITHM=COPY' in up.replace(' ', ''):
                    self.add('MYSQL-COPY-ALTER', mig, st, sqlshape.ident(re.match(r'ALTER TABLE (\S+)', norm, re.I).group(1)), 'mysql: all versions')
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ADD (?:COLUMN )?(?:IF NOT EXISTS )?(\S+) (.+)$', norm, re.I)
                if m and m.group(2).upper() not in ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'CHECK', 'EXCLUDE', 'INDEX', 'KEY'):
                    table, col, rest = sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2)), m.group(3)
                    d = re.search(r'\bDEFAULT\s+(.+?)(?:\s+NOT NULL|\s+NULL|$)', rest, re.I)
                    if d and pg:
                        volatile = bool(VOLATILE.search(d.group(1)))
                        self.add('PG-ADD-COLUMN-DEFAULT', mig, st, table, 'postgresql: < 11 for any default; all versions for a volatile default',
                                 {'default': d.group(1), 'volatile': volatile, 'engine_version_11_or_later': ver >= 11,
                                  'table_created_in_release': self.created.get(table)}, column=col)
                    if re.search(r'\bNOT NULL\b', rest, re.I) and not d and table not in self.created:
                        hits = find_readers.writers_without(self.roots, table, col, self.prisma_cols)
                        self.add('NOT-NULL-WHILE-WRITTEN-WITHOUT', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'new_required_column': True, 'test_hits': len(hits['test']), 'writers_not_analysed': len(hits.get('unknown', []))},
                                 column=col, hits=hits['code'])
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ALTER COLUMN (\S+) SET NOT NULL', norm, re.I)
                if m:
                    table, col = sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2))
                    if pg:
                        checked = any(re.search(rf'CHECK \(\s*{re.escape(col)} IS NOT NULL\s*\)', sqlshape.norm_sql(s['sql']), re.I) and 'NOT VALID' not in s['sql'].upper()
                                      or re.search(r'VALIDATE CONSTRAINT', s['sql'], re.I)
                                      for mm in self.data['migrations'] for s in mm.get('up', []))
                        self.add('PG-SET-NOT-NULL-SCAN', mig, st, table, 'postgresql: all versions (12+ skips the scan after a validated CHECK)',
                                 {'validated_check_found': checked, 'table_created_in_release': self.created.get(table)}, column=col)
                    hits = find_readers.writers_without(self.roots, table, col, self.prisma_cols)
                    self.add('NOT-NULL-WHILE-WRITTEN-WITHOUT', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'test_hits': len(hits['test']), 'writers_not_analysed': len(hits.get('unknown', []))}, column=col, hits=hits['code'])
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ADD (?:CONSTRAINT \S+ )?FOREIGN KEY', norm, re.I)
                if m and pg and 'NOT VALID' not in up and sqlshape.ident(m.group(1)) not in self.created:
                    self.add('PG-FK-VALIDATE', mig, st, sqlshape.ident(m.group(1)), 'postgresql: all versions')
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ADD (?:CONSTRAINT \S+ )?(UNIQUE|PRIMARY KEY)\b', norm, re.I)
                if m and pg and 'USING INDEX' not in up and sqlshape.ident(m.group(1)) not in self.created:
                    self.add('PG-UNIQUE-CONSTRAINT-LOCK', mig, st, sqlshape.ident(m.group(1)), 'postgresql: all versions')
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) ADD (?:CONSTRAINT \S+ )?CHECK\b', norm, re.I)
                if m and pg and 'NOT VALID' not in up and sqlshape.ident(m.group(1)) not in self.created:
                    self.add('PG-CHECK-VALIDATE', mig, st, sqlshape.ident(m.group(1)), 'postgresql: all versions')
                m = re.match(r'ALTER TABLE (?:ONLY )?(?:IF EXISTS )?(\S+) DROP (?:COLUMN )?(?:IF EXISTS )?(\S+)', norm, re.I)
                if m and m.group(2).upper() not in ('CONSTRAINT', 'DEFAULT', 'NOT', 'INDEX', 'PRIMARY', 'FOREIGN'):
                    table, col = sqlshape.ident(m.group(1)), sqlshape.ident(m.group(2))
                    hits = self.readers(table, col)
                    self.add('DROP-COLUMN', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'code_readers': len(hits['code']), 'down_state': mig['down_state'],
                              'down_restores_data': False, 'names_searched': self.field_names(table, col)}, column=col, hits=hits['code'])
                    if hits['code']:
                        self.add('DROP-WHILE-READ', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'test_hits': len(hits['test']), 'names_searched': self.field_names(table, col)}, column=col, hits=hits['code'])
                m = re.match(r'DROP TABLE (?:IF EXISTS )?(\S+)', norm, re.I)
                if m:
                    self.add('DROP-TABLE', mig, st, sqlshape.ident(m.group(1)), f'{self.engine["name"]}: all versions')
                m = re.match(r'TRUNCATE (?:TABLE )?(\S+)', norm, re.I)
                if m:
                    self.add('TRUNCATE', mig, st, sqlshape.ident(m.group(1)), f'{self.engine["name"]}: all versions')
                m = re.match(r'(UPDATE|DELETE FROM) (?:ONLY )?(\S+)', norm, re.I)
                if m:
                    verb, table = m.group(1).split()[0].upper(), sqlshape.ident(m.group(2))
                    if not re.search(r'\bWHERE\b', norm, re.I):
                        self.add('DML-WITHOUT-WHERE', mig, st, table, f'{self.engine["name"]}: all versions', verb=verb)
                    elif verb == 'DELETE' and table not in self.created:
                        backup = any(re.match(rf'(INSERT INTO|CREATE TABLE) \S+ .*\bFROM {re.escape(table)}\b', sqlshape.norm_sql(s['sql']), re.I)
                                     for s in mig['up'] if s['line'] < st['line'])
                        if not backup:
                            self.add('DELETE-ROWS', mig, st, table, f'{self.engine["name"]}: all versions', {'backup_found': False})
                    if verb == 'UPDATE' and not re.search(r'\b(LIMIT|BETWEEN)\b|\bid\s*[<>]', norm, re.I):
                        self.add('BACKFILL-UNBATCHED', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'in_transaction': mig.get('transactional'), 'has_where': bool(re.search(r'\bWHERE\b', norm, re.I))})
            if mig['down_state'] in ('pass', 'empty', 'raises', 'missing'):
                if self.tool == 'prisma':
                    prisma_irreversible.append(mig)
                else:
                    self.irreversible(mig, [mig])
            if my and ddl > 1:
                self.add('MYSQL-DDL-AUTOCOMMIT', mig, mig['up'][0], None, 'mysql: all versions', {'ddl_statements': ddl})
        if prisma_irreversible:
            self.irreversible(prisma_irreversible[0], prisma_irreversible)
        if self.engine['name'] == 'postgresql':
            self.lock_timeout()
        if len(self.data.get('heads') or []) > 1:
            self.add('ALEMBIC-MULTIPLE-HEADS', None, None, None, 'any', file='migrations', line=1, heads=', '.join(self.data['heads']))
        return self.out

    def irreversible(self, mig: dict, migs: list[dict]):
        """One IRREVERSIBLE candidate per migration; for Prisma (no down migrations by design) one for the release."""
        first = mig['up'][0] if mig['up'] else None
        state = {'pass': 'only pass', 'empty': 'empty', 'raises': 'a raise', 'missing': 'missing'}[mig['down_state']]
        facts = {'down_state': mig['down_state'], 'up_statements': len(mig['up'])}
        if self.tool == 'prisma':
            state = 'missing (Prisma has no down migrations)'
            facts.update(down_model='prisma: roll forward with a new migration, or restore a backup; prisma migrate diff can draft a reverse script',
                         migrations=[m['id'] for m in migs], up_statements=sum(len(m['up']) for m in migs))
        down = {'line': mig.get('down_line') or (first or {}).get('line') or 1, 'sql': f'down migration: {state}'}
        tm = LOCKED_TABLE.match(sqlshape.norm_sql(first['sql'])) or re.match(r'UPDATE (\S+)', sqlshape.norm_sql(first['sql']), re.I) if first else None
        self.add('IRREVERSIBLE', mig, down, sqlshape.ident(tm.group(1)) if tm else None,
                 f'{self.engine["name"]}: all versions', facts, state=state)

    def lock_timeout(self):
        """One candidate for the release when DDL on existing tables runs without a lock_timeout anywhere."""
        def sets(text: str) -> bool:
            code = ' '.join(ln.split('#')[0].split('//')[0] for ln in text.splitlines())
            return bool(re.search(r'lock_timeout', code, re.I))

        def locked(s: dict) -> str | None:
            m = LOCKED_TABLE.match(sqlshape.norm_sql(s['sql']))
            return sqlshape.ident(m.group(1)) if m else None
        affected = [(m, s) for m in self.pending for s in m['up'] if DDL_LOCKING.match(sqlshape.norm_sql(s['sql'])) and locked(s) not in self.created]
        if not affected:
            return
        env = [p for p in repo_walk.files(self.repo, names=('env.py', 'knexfile.'))
               if p.name.startswith('knexfile.') or 'migrations' in p.parts or 'alembic' in p.parts]
        env = [p for p in env if p.name == 'env.py'] + sorted(p for p in env if p.name != 'env.py')
        configured = any(sets(p.read_text(encoding='utf-8', errors='replace')) for p in env) or \
            any(sets(s['sql']) for m in self.pending for s in m['up'])
        if configured:
            return
        tables = {}
        for m, s in affected:
            tables.setdefault(locked(s), []).append(m['id'])
        rank = {'unknown': 3, 'large': 3, 'medium': 2, 'small': 1}
        table = max(tables, key=lambda t: rank[size_of(t, self.stats, self.created)['class']])
        m0, s0 = next((m, s) for m, s in affected if locked(s) == table)
        f, line = (env[0].relative_to(self.repo).as_posix(), 1) if env else (m0['file'], s0['line'])
        if env:
            for n, text in enumerate(env[0].read_text(encoding='utf-8').splitlines(), 1):
                if 'configure(' in text or 'pool' in text or 'connection' in text:
                    line = n
                    break
        self.add('PG-NO-LOCK-TIMEOUT', m0, s0, table, 'postgresql: all versions',
                 {'migrations': sorted({m['id'] for m, _ in affected}), 'tables': {t: sorted(set(v)) for t, v in tables.items()},
                  'checked': [p.relative_to(self.repo).as_posix() for p in env] + ['pending migration statements']},
                 file=f, line=line)


def merge_squawk(cands: list[dict], sarif_path: Path, repo: Path) -> None:
    import sarif
    for r in sarif.read(sarif_path, repo):
        same = next((c for c in cands if c['file'] == r['file'] and c['line'] == r['line']), None)
        if same:
            same.setdefault('also_reported_by', []).append({'tool': 'squawk', 'rule': r['rule_id']})
            continue
        cands.append({'id': f'C{len(cands) + 1:03d}', 'source': 'squawk', 'rule': r['rule_id'], 'category': None, 'hazard': None,
                      'migration': None, 'file': r['file'], 'line': r['line'], 'statement': r['snippet'] or None, 'direction': 'up',
                      'table': None, 'column': None, 'engine': 'postgresql', 'engine_version': None, 'engine_range': 'squawk rule',
                      'engine_assumed': False, 'size': None, 'effect': r['message'], 'rewrite': None, 'code_hits': [], 'facts': {}})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--migrations', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--table-stats', type=Path)
    ap.add_argument('--squawk', type=Path)
    ap.add_argument('--other-readers', type=Path, action='append', default=[])
    a = ap.parse_args()
    data = json.loads(a.migrations.read_text(encoding='utf-8'))
    stats = json.loads(a.table_stats.read_text(encoding='utf-8')) if a.table_stats else {}
    scan = Scan(data, a.repo, stats, a.other_readers)
    cands = scan.run()
    if a.squawk:
        merge_squawk(cands, a.squawk, a.repo)
    out = {'migrations_file': a.migrations.as_posix(), 'table_stats': a.table_stats.as_posix() if a.table_stats else None,
           'engine': data['engine'], 'pending': [m['id'] for m in data['migrations'] if m.get('pending')],
           'squawk': a.squawk.as_posix() if a.squawk else None, 'not_parsed': scan.not_parsed, 'candidates': cands}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(cands)} candidate(s) from {len(out["pending"])} pending migration(s) -> {a.out}')
    if scan.not_parsed:
        print(f'  {len(scan.not_parsed)} pending migration(s) could not be parsed and have no candidates; review them by hand:')
        for f in scan.not_parsed:
            print(f'    {f}')
    for c in cands:
        print(f'  {c["id"]} {c["migration"] or "-":5} {c["rule"]:32} {c["table"] or "-":13} {(c["size"] or {}).get("class", "-"):7} hits={len(c["code_hits"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
