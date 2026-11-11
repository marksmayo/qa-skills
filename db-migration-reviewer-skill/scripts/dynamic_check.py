#!/usr/bin/env python3
"""Run the pending migrations against a throwaway PostgreSQL database: timings, locks, blocked queries, rollback.

    python dynamic_check.py --migrations run/migrations.json --out run/dynamic.json --mode container [--seed-rows 100000]
    python dynamic_check.py --migrations run/migrations.json --out run/dynamic.json --dsn postgresql://postgres@127.0.0.1:5432/postgres

Needs psycopg 3 (requirements.txt) and either Docker or Podman (--mode container, the image pinned in
data/images.lock.json) or a PostgreSQL server on a loopback address (--dsn). Any other host is refused.

Steps: create a database named qa_migrate_<random>; run the earlier migrations' statements; seed synthetic rows
(--seed-rows for tables the candidates name, 1000 for the rest); then for each pending migration, run its up
statements in one transaction while a second connection polls pg_locks every 100 ms and a third tries a SELECT
and an INSERT on each affected table with a 2-second lock_timeout; then run the down statements and compare the
schema, row counts and per-table row hashes with the state before. The database (or container) is dropped at
the end, also on failure.

The output holds counts, hashes, lock modes and times. It never holds row values.

PostgreSQL only in this version; a MySQL migration set is reported as not run.
"""
from __future__ import annotations
import argparse, hashlib, ipaddress, json, secrets, shutil, socket, subprocess, sys, threading, time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / 'data' / 'images.lock.json').read_text(encoding='utf-8'))


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (OSError, ValueError):
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_container(engine_version: str | None) -> tuple[str, str, str, str]:
    runtime = shutil.which('docker') or shutil.which('podman')
    if not runtime:
        raise SystemExit('neither docker nor podman is on PATH; use --dsn with a loopback server, or run static only')
    major = (engine_version or '16').split('.')[0]
    image = LOCK['postgresql'].get(major) or LOCK['postgresql'][LOCK['postgresql_default']]
    port, pw = free_port(), secrets.token_hex(8)
    name = f'qa_migrate_{secrets.token_hex(4)}'
    subprocess.run([runtime, 'run', '--rm', '-d', '--name', name, '-e', f'POSTGRES_PASSWORD={pw}',
                    '-p', f'127.0.0.1:{port}:5432', image], check=True, capture_output=True)
    return runtime, name, f'postgresql://postgres:{pw}@127.0.0.1:{port}/postgres', image


def connect(dsn: str, dbname: str | None = None, autocommit: bool = True):
    import psycopg
    deadline = time.time() + 60
    while True:
        try:
            kw = {'dbname': dbname} if dbname else {}
            return psycopg.connect(dsn, autocommit=autocommit, **kw)
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1)


def snapshot(conn) -> dict:
    cur = conn.cursor()
    cols = cur.execute("""SELECT table_name, column_name, data_type, is_nullable, coalesce(character_maximum_length, numeric_precision)
                          FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2""").fetchall()
    idx = cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1").fetchall()
    tables = sorted({c[0] for c in cols})
    data = {}
    for t in tables:
        n = cur.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        h = cur.execute(f'SELECT md5(coalesce(string_agg(md5(x::text), \'\' ORDER BY md5(x::text)), \'\')) FROM "{t}" x').fetchone()[0]
        data[t] = {'rows': n, 'hash': h}
    schema = hashlib.sha256(json.dumps([list(map(str, c)) for c in cols] + [i[0] for i in idx]).encode()).hexdigest()
    return {'schema_sha256': schema, 'columns': [f'{c[0]}.{c[1]} {c[2]}{" null" if c[3] == "YES" else ""}' for c in cols],
            'indexes': [i[0] for i in idx], 'tables': data}


def seed(conn, table: str, rows: int) -> None:
    cur = conn.cursor()
    cols = cur.execute("""SELECT column_name, data_type, is_nullable FROM information_schema.columns
                          WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,)).fetchall()
    exprs = []
    for name, typ, nullable in cols:
        if name == 'id':
            exprs.append('g')
        elif name.endswith('_id'):
            exprs.append('1 + (g % 1000)')
        elif typ in ('integer', 'bigint', 'smallint'):
            exprs.append('(g % 30000)')
        elif typ == 'numeric':
            exprs.append('round((g % 100000) / 7.0, 2)')
        elif 'char' in typ or typ == 'text':
            exprs.append("NULL" if nullable == 'YES' and name != 'name' else "'s' || g")
        elif 'timestamp' in typ or typ == 'date':
            exprs.append("now() - (g || ' minutes')::interval")
        elif typ == 'boolean':
            exprs.append('g % 2 = 0')
        else:
            exprs.append('NULL')
    cur.execute(f'INSERT INTO "{table}" ({", ".join(chr(34) + c[0] + chr(34) for c in cols)}) '
                f'SELECT {", ".join(exprs)} FROM generate_series(1, %s) g', (rows,))


class LockPoller(threading.Thread):
    def __init__(self, dsn, db, pid):
        super().__init__(daemon=True)
        self.conn, self.pid, self.stop, self.seen = connect(dsn, db), pid, threading.Event(), {}

    def run(self):
        cur = self.conn.cursor()
        while not self.stop.is_set():
            for rel, mode in cur.execute("""SELECT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
                                            WHERE l.pid = %s AND l.granted AND c.relnamespace = 'public'::regnamespace""", (self.pid,)):
                self.seen.setdefault(rel, set()).add(mode)
            time.sleep(0.1)


def probe(dsn, db, table, results):
    """Try a read and a write on table while the migration runs; record whether each waited."""
    import psycopg
    conn = connect(dsn, db)
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '2s'")
    for kind, sql in (('select', f'SELECT 1 FROM "{table}" LIMIT 1'), ('insert', f'INSERT INTO "{table}" DEFAULT VALUES')):
        t0 = time.time()
        try:
            cur.execute('BEGIN')
            cur.execute(sql)
            cur.execute('ROLLBACK')
            results.append({'table': table, 'query': kind, 'blocked': False, 'waited_ms': round((time.time() - t0) * 1000)})
        except psycopg.errors.LockNotAvailable:
            cur.execute('ROLLBACK')
            results.append({'table': table, 'query': kind, 'blocked': True, 'waited_ms': round((time.time() - t0) * 1000)})
        except psycopg.Error as e:
            cur.execute('ROLLBACK')
            results.append({'table': table, 'query': kind, 'blocked': False, 'error': type(e).__name__})
    conn.close()


def run_statements(conn, stmts: list[dict], transactional: bool) -> None:
    cur = conn.cursor()
    if transactional:
        cur.execute('BEGIN')
    for s in stmts:
        cur.execute(s['sql'].split(' /* from ')[0])
    if transactional:
        cur.execute('COMMIT')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--migrations', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, help='tables named here get --seed-rows; the rest get 1000')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--mode', choices=['container', 'dsn'], default='container')
    ap.add_argument('--dsn')
    ap.add_argument('--seed-rows', type=int, default=100_000)
    a = ap.parse_args()
    data = json.loads(a.migrations.read_text(encoding='utf-8'))
    if data['engine']['name'] != 'postgresql':
        a.out.write_text(json.dumps({'status': 'not_run', 'reason': f'dynamic check supports PostgreSQL only; engine is {data["engine"]["name"]}'}, indent=1))
        return 0
    hot = set()
    if a.candidates:
        hot = {c['table'] for c in json.loads(a.candidates.read_text(encoding='utf-8'))['candidates'] if c.get('table')}
    runtime = container = image = None
    if a.dsn:
        host = urlparse(a.dsn).hostname or 'localhost'
        if not loopback(host):
            raise SystemExit(f'refused: {host} is not a loopback address; the dynamic check only runs on a database it creates locally')
        dsn = a.dsn
    else:
        runtime, container, dsn, image = start_container(data['engine'].get('version'))
    db = f'qa_migrate_{secrets.token_hex(4)}'
    admin = connect(dsn)
    out = {'status': 'ran', 'host': urlparse(dsn).hostname, 'database': db, 'created_by_skill': True, 'container': container,
           'image': image, 'seed_rows': a.seed_rows, 'migrations': []}
    try:
        admin.execute(f'CREATE DATABASE {db}')
        conn = connect(dsn, db)
        for m in data['migrations']:
            if m.get('pending'):
                break
            run_statements(conn, m['up'], True)
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
        for t in tables:
            seed(conn, t, a.seed_rows if t in hot else 1000)
        for m in [m for m in data['migrations'] if m.get('pending')]:
            rec = {'id': m['id'], 'up': {}, 'down': {}}
            before = snapshot(conn)
            pid = conn.info.backend_pid
            poller = LockPoller(dsn, db, pid)
            poller.start()
            probes: list = []
            affected = sorted({t for t in tables if any(f' {t} ' in f' {s["sql"]} ' for s in m['up'])})
            threads = [threading.Thread(target=lambda t=t: (time.sleep(0.2), probe(dsn, db, t, probes))) for t in affected]
            t0 = time.time()
            for th in threads:
                th.start()
            try:
                run_statements(conn, m['up'], m.get('transactional', True))
                rec['up'] = {'status': 'ok', 'ms': round((time.time() - t0) * 1000)}
            except Exception as e:  # report and continue with the next migration
                conn.rollback() if not conn.autocommit else None
                rec['up'] = {'status': 'failed', 'error': type(e).__name__, 'ms': round((time.time() - t0) * 1000)}
            for th in threads:
                th.join()
            poller.stop.set()
            poller.join()
            rec['locks'] = {k: sorted(v) for k, v in poller.seen.items()}
            rec['probes'] = probes
            after_up = snapshot(conn)
            rec['rows_changed'] = {t: {'before': before['tables'][t]['rows'], 'after': after_up['tables'].get(t, {}).get('rows'),
                                       'hash_changed': before['tables'][t]['hash'] != after_up['tables'].get(t, {}).get('hash')}
                                   for t in before['tables']}
            if m['down_state'] != 'ok' or rec['up']['status'] != 'ok':
                rec['down'] = {'status': 'not_run', 'reason': f'down is {m["down_state"]}' if m['down_state'] != 'ok' else 'up failed'}
            else:
                t1 = time.time()
                try:
                    run_statements(conn, m['down'], m.get('transactional', True))
                    after_down = snapshot(conn)
                    rec['down'] = {'status': 'ok', 'ms': round((time.time() - t1) * 1000),
                                   'schema_restored': after_down['schema_sha256'] == before['schema_sha256'],
                                   'rows_restored': {t: after_down['tables'].get(t, {}).get('hash') == v['hash'] for t, v in before['tables'].items()}}
                    run_statements(conn, m['up'], m.get('transactional', True))
                except Exception as e:
                    rec['down'] = {'status': 'failed', 'error': type(e).__name__}
            out['migrations'].append(rec)
        conn.close()
    finally:
        try:
            admin.execute(f'DROP DATABASE IF EXISTS {db} WITH (FORCE)')
            admin.close()
        finally:
            if container:
                subprocess.run([runtime, 'stop', container], capture_output=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'dynamic check: {len(out["migrations"])} migration(s) -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
