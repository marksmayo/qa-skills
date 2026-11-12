#!/usr/bin/env python3
"""Self-test of the parsers and rules on the bundled examples and small inline cases (stdlib; needs git for one case).

    python self_test.py

Checks: quoted identifiers and column lists, multi-clause ALTER TABLE, CREATE ... IF NOT EXISTS (both used
to crash), type narrowing including DOUBLE PRECISION to NUMERIC(p,s), Prisma folder ids, archived history left
out of the chain but kept for type history, --since that matches nothing, Prisma writers and @map readers,
the previous schema.prisma from git, and that pruned folders (node_modules) are never walked.
Everything runs in a temporary folder; the examples are only read.
"""
from __future__ import annotations
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_walk, scan_migrations, sqlshape  # noqa: E402

EX = HERE.parent / 'examples'
FAILS: list[str] = []


def check(cond: bool, what: str):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        FAILS.append(what)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding='utf-8')


def main() -> int:
    n = sqlshape.norm_sql('CREATE UNIQUE INDEX IF NOT EXISTS "A_key" ON "WebhookEvent"("source", "eventId")')
    check(n == 'CREATE UNIQUE INDEX IF NOT EXISTS A_key ON WebhookEvent (source, eventId)', 'quoted identifiers and column lists normalised')
    check(sqlshape.ident('public."Order"') == 'Order', 'default schema and quotes stripped from a table name')
    cl = sqlshape.clauses(sqlshape.norm_sql('ALTER TABLE "T" ADD COLUMN "a" INT, ADD COLUMN "b" NUMERIC(10,2) DEFAULT 0'))
    check(cl == ['ALTER TABLE T ADD COLUMN a INT', 'ALTER TABLE T ADD COLUMN b NUMERIC(10,2) DEFAULT 0'], 'multi-clause ALTER TABLE split per clause')
    nar = scan_migrations.narrowing
    check(bool(nar('DOUBLE PRECISION', 'DECIMAL(12,2)')), 'DOUBLE PRECISION to DECIMAL(12,2) narrows')
    check(bool(nar('NUMERIC(12,4)', 'NUMERIC(12,2)')) and bool(nar('NUMERIC(14,2)', 'NUMERIC(10,2)')), 'NUMERIC scale and precision cuts narrow')
    check(nar('DECIMAL(12, 2)', 'NUMERIC(12,2)') is None and nar('INTEGER', 'BIGINT') is None, 'equal or wider types do not narrow')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        out = tmp / 'm.json'
        r = run(str(HERE / 'detect_migrations.py'), '--repo', str(EX / 'greenhouse'), '--since', 'no_such_migration', '--out', str(out))
        check(r.returncode == 2 and not out.exists(), '--since that matches nothing is an error, not "everything pending"')
        r = run(str(HERE / 'detect_migrations.py'), '--repo', str(EX / 'greenhouse'), '--since', '20260301000000_baseline', '--out', str(out))
        d = json.loads(out.read_text(encoding='utf-8')) if r.returncode == 0 else {}
        check([m['id'] for m in d.get('migrations', [])] == ['20260301000000_baseline', '20260415000000_release_hardening'],
              'Prisma ids are folder names, in folder order, archive left out')
        check(len(d.get('excluded', [])) == 2, 'archived migrations listed as excluded')
        check(d.get('engine', {}).get('assumed') is True, 'a docker-compose engine version is marked assumed')
        hard = (d.get('migrations') or [{}, {}])[1]
        check('StockAlert' in hard.get('created_tables', []), 'CREATE TABLE IF NOT EXISTS "StockAlert" recorded as created')
        check(any(h['origin'] == 'archive' and h['type'] == 'DOUBLE PRECISION' for h in d.get('type_history', {}).get('Order.total', [])),
              'archived history feeds the type history')
        cands = tmp / 'c.json'
        run(str(HERE / 'scan_migrations.py'), '--migrations', str(out), '--repo', str(EX / 'greenhouse'),
            '--table-stats', str(EX / 'greenhouse' / 'table_stats.json'), '--out', str(cands))
        c = json.loads(cands.read_text(encoding='utf-8'))['candidates'] if cands.exists() else []
        rules = [(x['rule'], x['table'], x['column']) for x in c]
        check(('TYPE-NARROWING', 'Order', 'total') in rules, 'narrowing found from the archived DOUBLE PRECISION type')
        check(('TYPE-NARROWING', 'Plant', 'weight_kg') in rules, 'second clause of a multi-clause ALTER analysed')
        check(not any(x['rule'] == 'PG-FK-VALIDATE' for x in c), 'a foreign key on a table created in the release is not flagged')
        idx = next((x for x in c if x['rule'] == 'PG-INDEX-LOCKS-WRITES'), {})
        check((idx.get('facts') or {}).get('table_created_in_release') == '20260415000000_release_hardening', 'index on a new quoted table knows the table is new')
        check(sum(1 for x in c if x['rule'] == 'IRREVERSIBLE') == 1, 'one IRREVERSIBLE candidate for the Prisma release')
        nn = next((x for x in c if x['rule'] == 'NOT-NULL-WHILE-WRITTEN-WITHOUT'), {})
        check([h['file'] for h in nn.get('code_hits', [])] == ['src/orders.ts'], 'prisma.order.create without the NOT NULL column is a writer hit')
        dr = next((x for x in c if x['rule'] == 'DROP-WHILE-READ'), {})
        check(bool(dr.get('code_hits')), 'dropped snake_case column found through its camelCase Prisma field')
        sz = next((x for x in c if x['table'] == 'Order'), {}).get('size', {})
        check(sz.get('class') == 'large', 'statistics keyed by the plain table name size a quoted table')

        # the previous schema.prisma from git, and pruned folders
        repo = tmp / 'repo'
        shutil.copytree(EX / 'greenhouse', repo)
        (repo / 'node_modules' / 'pkg').mkdir(parents=True)
        (repo / 'node_modules' / 'pkg' / 'migration.sql').write_text('DROP TABLE "Order";\n', encoding='utf-8')
        check(not any('node_modules' in p.parts for p in repo_walk.files(repo)), 'node_modules is pruned from the walk')
        if shutil.which('git'):
            hard_dir = repo / 'prisma' / 'migrations' / '20260415000000_release_hardening'
            keep = tmp / 'hardening'
            shutil.move(str(hard_dir), str(keep))
            schema = repo / 'prisma' / 'schema.prisma'
            new_schema = schema.read_text(encoding='utf-8')
            schema.write_text(new_schema.replace('@db.Decimal(10, 2)\n  status', '\n  status').replace('total      Decimal', 'total      Float'), encoding='utf-8')
            g = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', '-c', 'core.autocrlf=false']
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
            subprocess.run(g + ['add', '.'], check=True, capture_output=True)
            subprocess.run(g + ['commit', '-qm', 'before'], check=True, capture_output=True)
            shutil.move(str(keep), str(hard_dir))
            schema.write_text(new_schema, encoding='utf-8')
            shutil.rmtree(repo / 'prisma' / 'migration_history_archive')
            subprocess.run(g + ['add', '-A'], check=True, capture_output=True)
            subprocess.run(g + ['commit', '-qm', 'after'], check=True, capture_output=True)
            r = run(str(HERE / 'detect_migrations.py'), '--repo', str(repo), '--since', '20260301000000_baseline', '--out', str(out))
            d = json.loads(out.read_text(encoding='utf-8')) if r.returncode == 0 else {}
            check(any(h['origin'] == 'schema_git' and h['type'] == 'DOUBLE PRECISION' for h in d.get('type_history', {}).get('Order.total', [])),
                  'the previous schema.prisma (git) gives the type before the release')
        else:
            print('skip git case (git not installed)')
    print(f'{len(FAILS)} failure(s)' if FAILS else 'all checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
