#!/usr/bin/env python3
"""Match the rule catalogue against the pending migrations and write candidates.json.

    python scan_migrations.py --migrations run/migrations.json --repo examples/ledger --out run/candidates.json \
        [--table-stats examples/ledger/table_stats.json] [--squawk squawk.sarif] [--other-readers ../reports]

Every candidate carries the facts triage needs: the statement and line, the table and its size (with the
size source), the engine and version range the rule holds for, code hits for compatibility rules, and
context facts (table created in the same release, constant default on PostgreSQL 11+, column with no code
readers). Nothing is dismissed here: triage promotes or dismisses every candidate, with a reason.
Squawk SARIF results join as candidates with source "squawk"; one at the same file and line as a built-in
candidate is attached to it instead.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import find_readers  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))['rules']
VOLATILE = re.compile(r'\b(now|random|gen_random_uuid|uuid_generate_v4|clock_timestamp|nextval|current_timestamp|statement_timestamp)\b', re.I)
DDL_LOCKING = re.compile(r'^(ALTER TABLE|CREATE INDEX|CREATE UNIQUE INDEX|DROP INDEX|DROP TABLE|TRUNCATE)\b', re.I)
NUM = re.compile(r'(NUMERIC|DECIMAL)\s*\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\)', re.I)
INT_DIGITS = {'SMALLINT': 4, 'INTEGER': 9, 'INT': 9, 'BIGINT': 18}


def size_of(table: str, stats: dict, created: dict) -> dict:
    if table in created:
        return {'class': 'small', 'rows': 0, 'source': f'created in pending migration {created[table]}'}
    t = (stats.get('tables') or {}).get(table)
    if not t:
        return {'class': 'unknown', 'rows': None, 'source': 'no statistics given; rated as large'}
    rows, size_b = t.get('rows'), t.get('bytes') or 0
    cls = 'large' if rows > 1_000_000 or size_b > 1_000_000_000 else 'medium' if rows >= 10_000 else 'small'
    return {'class': cls, 'rows': rows, 'source': stats.get('source', 'table statistics file')}


def narrowing(frm: str, to: str) -> str | None:
    """Why a type change from frm to to can lose data, or None if it can't be shown."""
    f, t = frm.upper(), to.upper()
    m = NUM.search(f)
    if m and (t.split('(')[0].strip() in INT_DIGITS):
        prec, scale = int(m.group(2)), int(m.group(3) or 0)
        why = []
        if scale:
            why.append(f'the {scale} decimal place(s) are rounded away')
        if prec - scale > INT_DIGITS[t.split('(')[0].strip()]:
            why.append('values above the integer range are rejected')
        return ' and '.join(why) or None
    order = ['SMALLINT', 'INTEGER', 'BIGINT']
    fb, tb = f.split('(')[0].strip(), t.split('(')[0].strip()
    if fb in order and tb in order and order.index(tb) < order.index(fb):
        return 'values above the smaller range are rejected'
    fl, tl = re.search(r'VARCHAR\s*\((\d+)\)', f), re.search(r'VARCHAR\s*\((\d+)\)', t)
    if tl and (fb == 'TEXT' or (fl and int(tl.group(1)) < int(fl.group(1)))):
        return 'longer strings are rejected'
    return None


class Scan:
    def __init__(self, data: dict, repo: Path, stats: dict, other: list[Path]):
        self.data, self.repo, self.stats, self.roots = data, repo, stats, [repo] + other
        self.engine = data['engine']
        self.pending = [m for m in data['migrations'] if m.get('pending')]
        self.created = {t: m['id'] for m in self.pending for t in m.get('created_tables', [])}
        self.out: list[dict] = []
        self._readers: dict = {}

    def readers(self, column: str) -> dict:
        if column not in self._readers:
            self._readers[column] = find_readers.readers(self.roots, column)
        return self._readers[column]

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

    def run(self) -> list[dict]:
        pg, my = self.engine['name'] == 'postgresql', self.engine['name'] in ('mysql', 'mariadb')
        ver = float(re.match(r'\d+(\.\d+)?', self.engine.get('version') or '0').group(0))
        for mig in self.pending:
            ddl = 0
            for st in mig['up']:
                sql = st['sql']
                up = sql.upper()
                if DDL_LOCKING.match(sql):
                    ddl += 1
                m = re.match(r'CREATE (UNIQUE )?INDEX (CONCURRENTLY )?(?:IF NOT EXISTS )?\S+ ON (\S+)', sql, re.I)
                if m and pg:
                    table = m.group(3)
                    if m.group(2) and mig.get('transactional'):
                        self.add('PG-CONCURRENTLY-IN-TRANSACTION', mig, st, table, 'postgresql: all versions')
                    elif not m.group(2):
                        self.add('PG-INDEX-LOCKS-WRITES', mig, st, table, 'postgresql: all versions',
                                 {'table_created_in_release': self.created.get(table)})
                m = re.match(r'ALTER TABLE (\S+) RENAME COLUMN (\S+) TO (\S+)', sql, re.I)
                if m:
                    table, old, new = m.groups()
                    hits = self.readers(old)
                    self.add('RENAME-WHILE-READ', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'new_name': new, 'test_hits': len(hits['test']), 'migration_hits': len(hits['migration'])},
                             column=old, hits=hits['code'])
                m = re.match(r'ALTER TABLE (\S+) ALTER COLUMN (\S+) TYPE (.+?)(?: USING .*?)?(?: /\* from (.+) \*/)?$', sql, re.I)
                if m:
                    table, col, to, frm = m.groups()
                    if pg:
                        self.add('PG-TYPE-CHANGE-REWRITE', mig, st, table, 'postgresql: all versions (most type changes rewrite)',
                                 {'to': to, 'from': frm}, column=col)
                    why = narrowing(frm, to) if frm else None
                    if why:
                        self.add('TYPE-NARROWING', mig, st, table, f'{self.engine["name"]}: all versions', {'why': why}, column=col,
                                 **{'from': frm, 'to': to})
                m = re.match(r'ALTER TABLE (\S+) (?:MODIFY|CHANGE) (?:COLUMN )?(\S+)', sql, re.I)
                if m and my:
                    self.add('MYSQL-COPY-ALTER', mig, st, m.group(1), 'mysql 5.6+ / 8.0: column type changes copy the table', column=m.group(2))
                if my and 'ALGORITHM=COPY' in up.replace(' ', ''):
                    self.add('MYSQL-COPY-ALTER', mig, st, re.match(r'ALTER TABLE (\S+)', sql, re.I).group(1), 'mysql: all versions')
                m = re.match(r'ALTER TABLE (\S+) ADD COLUMN (\S+) (.+)$', sql, re.I)
                if m:
                    table, col, rest = m.groups()
                    d = re.search(r'\bDEFAULT\s+(.+?)(?:\s+NOT NULL|\s+NULL|$)', rest, re.I)
                    if d and pg:
                        volatile = bool(VOLATILE.search(d.group(1)))
                        self.add('PG-ADD-COLUMN-DEFAULT', mig, st, table, 'postgresql: < 11 for any default; all versions for a volatile default',
                                 {'default': d.group(1), 'volatile': volatile, 'engine_version_11_or_later': ver >= 11,
                                  'table_created_in_release': self.created.get(table)}, column=col)
                    if re.search(r'\bNOT NULL\b', rest, re.I) and not d and table not in self.created:
                        hits = find_readers.writers_without(self.roots, table, col)
                        self.add('NOT-NULL-WHILE-WRITTEN-WITHOUT', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'new_required_column': True, 'test_hits': len(hits['test'])}, column=col, hits=hits['code'])
                m = re.match(r'ALTER TABLE (\S+) ALTER COLUMN (\S+) SET NOT NULL', sql, re.I)
                if m:
                    table, col = m.groups()
                    if pg:
                        checked = any(re.search(rf'CHECK \(\s*{col} IS NOT NULL\s*\)', s['sql'], re.I) and 'NOT VALID' not in s['sql'].upper()
                                      or re.search(r'VALIDATE CONSTRAINT', s['sql'], re.I)
                                      for mm in self.data['migrations'] for s in mm['up'])
                        self.add('PG-SET-NOT-NULL-SCAN', mig, st, table, 'postgresql: all versions (12+ skips the scan after a validated CHECK)',
                                 {'validated_check_found': checked, 'table_created_in_release': self.created.get(table)}, column=col)
                    hits = find_readers.writers_without(self.roots, table, col)
                    self.add('NOT-NULL-WHILE-WRITTEN-WITHOUT', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'test_hits': len(hits['test'])}, column=col, hits=hits['code'])
                m = re.match(r'ALTER TABLE (\S+) ADD (?:CONSTRAINT \S+ )?FOREIGN KEY', sql, re.I)
                if m and pg and 'NOT VALID' not in up and m.group(1) not in self.created:
                    self.add('PG-FK-VALIDATE', mig, st, m.group(1), 'postgresql: all versions')
                m = re.match(r'ALTER TABLE (\S+) DROP COLUMN (?:IF EXISTS )?(\S+)', sql, re.I)
                if m:
                    table, col = m.groups()
                    hits = self.readers(col)
                    self.add('DROP-COLUMN', mig, st, table, f'{self.engine["name"]}: all versions',
                             {'code_readers': len(hits['code']), 'down_state': mig['down_state'],
                              'down_restores_data': False}, column=col, hits=hits['code'])
                    if hits['code']:
                        self.add('DROP-WHILE-READ', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'test_hits': len(hits['test'])}, column=col, hits=hits['code'])
                m = re.match(r'DROP TABLE (?:IF EXISTS )?(\S+)', sql, re.I)
                if m:
                    self.add('DROP-TABLE', mig, st, m.group(1), f'{self.engine["name"]}: all versions')
                m = re.match(r'TRUNCATE (?:TABLE )?(\S+)', sql, re.I)
                if m:
                    self.add('TRUNCATE', mig, st, m.group(1), f'{self.engine["name"]}: all versions')
                m = re.match(r'(UPDATE|DELETE FROM) (\S+)', sql, re.I)
                if m:
                    verb, table = m.group(1).split()[0].upper(), m.group(2)
                    if not re.search(r'\bWHERE\b', sql, re.I):
                        self.add('DML-WITHOUT-WHERE', mig, st, table, f'{self.engine["name"]}: all versions', verb=verb)
                    if verb == 'UPDATE' and not re.search(r'\b(LIMIT|BETWEEN)\b|\bid\s*[<>]', sql, re.I):
                        self.add('BACKFILL-UNBATCHED', mig, st, table, f'{self.engine["name"]}: all versions',
                                 {'in_transaction': mig.get('transactional'), 'has_where': bool(re.search(r'\bWHERE\b', sql, re.I))})
            if mig['down_state'] in ('pass', 'empty', 'raises', 'missing'):
                first = mig['up'][0] if mig['up'] else None
                state = {'pass': 'only pass', 'empty': 'empty', 'raises': 'a raise', 'missing': 'missing'}[mig['down_state']]
                down = {'line': mig.get('down_line') or (first or {}).get('line'), 'sql': f'down migration: {state}'}
                self.add('IRREVERSIBLE', mig, down, (re.match(r'(?:ALTER TABLE|UPDATE|CREATE INDEX \S+ ON|DROP TABLE) (\S+)', first['sql'], re.I) or [None, None])[1] if first else None,
                         f'{self.engine["name"]}: all versions', {'down_state': mig['down_state'], 'up_statements': len(mig['up'])}, state=state)
            if my and ddl > 1:
                self.add('MYSQL-DDL-AUTOCOMMIT', mig, mig['up'][0], None, 'mysql: all versions', {'ddl_statements': ddl})
        if self.engine['name'] == 'postgresql':
            self.lock_timeout()
        if len(self.data.get('heads') or []) > 1:
            self.add('ALEMBIC-MULTIPLE-HEADS', None, None, None, 'any', file='migrations', line=1, heads=', '.join(self.data['heads']))
        return self.out

    def lock_timeout(self):
        """One candidate for the release when DDL on existing tables runs without a lock_timeout anywhere."""
        def sets(text: str) -> bool:
            code = ' '.join(ln.split('#')[0] for ln in text.splitlines())
            return bool(re.search(r'lock_timeout', code, re.I))
        affected = [(m, s) for m in self.pending for s in m['up'] if DDL_LOCKING.match(s['sql'])
                    and (re.match(r'(?:ALTER TABLE|DROP TABLE|TRUNCATE(?: TABLE)?|CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?\S+ ON) (\S+)', s['sql'], re.I) or [None, None])[1] not in self.created]
        if not affected:
            return
        env = [p for p in self.repo.rglob('env.py') if 'migrations' in p.parts or 'alembic' in p.parts]
        configured = any(sets(p.read_text(encoding='utf-8', errors='replace')) for p in env) or \
            any(sets(s['sql']) for m in self.pending for s in m['up'])
        if configured:
            return
        tables = {}
        for m, s in affected:
            t = re.match(r'(?:ALTER TABLE|DROP TABLE|TRUNCATE(?: TABLE)?|CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?\S+ ON) (\S+)', s['sql'], re.I).group(1)
            tables.setdefault(t, []).append(m['id'])
        rank = {'unknown': 3, 'large': 3, 'medium': 2, 'small': 1}
        table = max(tables, key=lambda t: rank[size_of(t, self.stats, self.created)['class']])
        m0, s0 = next((m, s) for m, s in affected if table in s['sql'])
        f, line = (env[0].relative_to(self.repo).as_posix(), 1) if env else (m0['file'], s0['line'])
        if env:
            for n, text in enumerate(env[0].read_text(encoding='utf-8').splitlines(), 1):
                if 'configure(' in text:
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
    cands = Scan(data, a.repo, stats, a.other_readers).run()
    if a.squawk:
        merge_squawk(cands, a.squawk, a.repo)
    out = {'migrations_file': a.migrations.as_posix(), 'table_stats': a.table_stats.as_posix() if a.table_stats else None,
           'engine': data['engine'], 'pending': [m['id'] for m in data['migrations'] if m.get('pending')],
           'squawk': a.squawk.as_posix() if a.squawk else None, 'candidates': cands}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(cands)} candidate(s) from {len(out["pending"])} pending migration(s) -> {a.out}')
    for c in cands:
        print(f'  {c["id"]} {c["migration"] or "-":5} {c["rule"]:32} {c["table"] or "-":13} {(c["size"] or {}).get("class", "-"):7} hits={len(c["code_hits"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
