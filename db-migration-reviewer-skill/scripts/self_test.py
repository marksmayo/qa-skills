#!/usr/bin/env python3
"""Self-test of the parsers and rules on the bundled examples and small inline cases (stdlib; needs git for one case).

    python self_test.py

Checks: quoted identifiers and column lists, multi-clause ALTER TABLE, CREATE ... IF NOT EXISTS (both used
to crash), type narrowing including DOUBLE PRECISION to NUMERIC(p,s), Prisma folder ids, archived history left
out of the chain but kept for type history, --since that matches nothing, Prisma writers and @map readers,
the previous schema.prisma from git, and that pruned folders (node_modules) are never walked. Synthetic
Alembic cases (a bakery and a library service): two chains in one repository and --chain, the lock-timeout
check per chain, helper functions and resolved f-strings, op.get_context(), get_bind/conn.execute and
imported helpers listed as not analysed, downgrade data loss, value renames with readers in the previous
release (--base-ref), files read at run time, a Bicep engine version from --infra, and size hints; and the
validator's reproducibility rating, rollback path and component checks.
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


MIG = '''"""{doc}"""
from alembic import op
import sqlalchemy as sa
{imports}
revision = '{rev}'
down_revision = {down}
{body}
'''
ENV_PLAIN = 'from alembic import context\nfrom sqlalchemy import pool\n\n\ndef run_migrations_online():\n    context.configure(url="postgresql://localhost/x")\n'
ENV_TIMEOUT = ENV_PLAIN + '    context.execute("SET lock_timeout = \'5s\'")\n'


def write_chain(root: Path, service: str, env: str, migs: list[tuple[str, str, str, str]]):
    base = root / 'services' / service / 'db'
    (base / 'alembic' / 'versions').mkdir(parents=True)
    (base / 'alembic.ini').write_text('[alembic]\nscript_location = alembic\n', encoding='utf-8')
    (base / 'alembic' / 'env.py').write_text(env, encoding='utf-8')
    down = 'None'
    for rev, doc, imports, body in migs:
        (base / 'alembic' / 'versions' / f'{rev}_{doc.split()[0]}.py').write_text(
            MIG.format(doc=doc, imports=imports, rev=rev, down=down, body=body), encoding='utf-8')
        down = repr(rev)


def alembic_cases(tmp: Path):
    """Synthetic Alembic cases: two chains in one repo, helpers, data steps, downgrades, value renames, file reads,
    Bicep engine version in a separate infrastructure folder, size hints and readers in the previous release."""
    repo = tmp / 'two-chains'
    plain = '\n\ndef upgrade():\n    op.create_table("loaves", sa.Column("id", sa.Integer, primary_key=True))\n\n\ndef downgrade():\n    op.drop_table("loaves")\n'
    write_chain(repo, 'bakery', ENV_PLAIN, [
        ('001', 'loaves table', '', plain),
        ('002', 'oven index', '', '\n\ndef upgrade():\n    op.create_index("ix_loaves_oven", "loaves", ["oven"])\n\n\n'
                                  'def downgrade():\n    op.drop_index("ix_loaves_oven")\n')])
    helper = '''

def _rename_genre(conn):
    op.drop_constraint("ck_shelves_genre", "shelves")
    op.create_check_constraint("ck_shelves_genre", "shelves", "genre IN ('mystery', 'poetry')")
    op.execute("UPDATE shelves SET genre = 'mystery' WHERE genre = 'crime'")


def upgrade():
    cfg = op.get_context().config
    conn = op.get_bind()
    _rename_genre(conn)
    conn.execute(sa.text(cfg.get_main_option("extra_sql")))


def downgrade():
    op.execute("UPDATE shelves SET genre = 'crime'")
'''
    seeds = '''
SEED_FILE = 'seeds/branches.yaml'


def upgrade():
    with open(Path(__file__).parent / SEED_FILE) as fh:
        rows = fh.read()
    load_rows(rows)


def downgrade():
    pass
'''
    write_chain(repo, 'library', ENV_TIMEOUT, [
        ('001', 'shelves table', '', plain.replace('loaves', 'shelves')),
        ('002', 'rename genre', '', helper),
        ('003', 'branch seeds', 'from pathlib import Path\nfrom library_app.seed import load_rows\n', seeds)])
    (repo / 'library_app').mkdir()
    (repo / 'library_app' / 'genres.py').write_text("KNOWN = ['mystery', 'poetry']\n", encoding='utf-8')
    (repo / 'library_app' / 'seed.py').write_text('def load_rows(rows):\n    return rows\n', encoding='utf-8')
    infra = tmp / 'infra-repo'
    infra.mkdir()
    (infra / 'database.bicep').write_text("param pgVersion string = '15'\n\nresource db 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01' = {\n"
                                          "  name: 'library-db'\n  properties: {\n    version: pgVersion\n  }\n}\n", encoding='utf-8')
    stats = tmp / 'stats.json'
    stats.write_text(json.dumps({'source': 'synthetic', 'tables': {'shelves': {'hint': 'lookup table seeded by migrations only', 'class': 'small'}}}),
                     encoding='utf-8')
    g = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false', '-c', 'core.autocrlf=false']
    has_git = bool(shutil.which('git'))
    if has_git:  # the previous release still keys on 'crime'
        (repo / 'library_app' / 'genres.py').write_text("KNOWN = ['crime', 'poetry']\nCRIME = 'crime'\n", encoding='utf-8')
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        subprocess.run(g + ['add', '.'], check=True, capture_output=True)
        subprocess.run(g + ['commit', '-qm', 'previous release'], check=True, capture_output=True)
        (repo / 'library_app' / 'genres.py').write_text("KNOWN = ['mystery', 'poetry']\n", encoding='utf-8')
        subprocess.run(g + ['commit', '-qam', 'this release'], check=True, capture_output=True)

    out = tmp / 'chains.json'
    det = str(HERE / 'detect_migrations.py')
    r = run(det, '--repo', str(repo), '--out', str(out))
    check(r.returncode == 2 and 'services/bakery/db/alembic' in r.stderr and '--chain' in r.stderr and not out.exists(),
          'two Alembic chains without --chain: refused, and the message lists the chains')
    r = run(det, '--repo', str(repo), '--chain', 'services/bakery/db/alembic', '--since', '001', '--out', str(out))
    d = json.loads(out.read_text(encoding='utf-8')) if r.returncode == 0 else {}
    check([m['id'] for m in d.get('migrations', [])] == ['001', '002'] and (d.get('chain') or {}).get('env') == 'services/bakery/db/alembic/env.py',
          '--chain picks one chain (same ids in the other chain are no conflict) and records its env.py')
    cands = tmp / 'bakery-c.json'
    run(str(HERE / 'scan_migrations.py'), '--migrations', str(out), '--repo', str(repo), '--out', str(cands))
    c = json.loads(cands.read_text(encoding='utf-8'))['candidates'] if cands.exists() else []
    lt = next((x for x in c if x['rule'] == 'PG-NO-LOCK-TIMEOUT'), {})
    check(lt.get('file') == 'services/bakery/db/alembic/env.py' and lt.get('line') == 6 and
          lt.get('facts', {}).get('checked', [None])[0] == 'services/bakery/db/alembic/env.py',
          'lock-timeout candidate cites its own chain env.py at the configure() line, not an import')

    r = run(det, '--repo', str(repo), '--chain', 'library/db/alembic', '--since', '001', '--infra', str(infra), '--out', str(out))
    d = json.loads(out.read_text(encoding='utf-8')) if r.returncode == 0 else {}
    eng = d.get('engine', {})
    check(eng.get('version') == '15' and eng.get('assumed') is False and 'database.bicep:1' in (eng.get('source') or ''),
          'engine version from a Bicep param default in a separate infrastructure folder (--infra)')
    migs = {m['id']: m for m in d.get('migrations', [])}
    mig2, mig3 = migs.get('002', {}), migs.get('003', {})
    check(mig2.get('transactional') is True, 'op.get_context().config does not make a migration non-transactional')
    check(any('_rename_genre' in s.get('via', '') and 'CHECK' in s['sql'] for s in mig2.get('up', [])),
          'statements in a helper function called by upgrade() are analysed')
    check(any('get_bind' in x['what'] for x in mig2.get('not_analysed', [])) and any('execute' in x['what'] for x in mig2.get('not_analysed', [])),
          'op.get_bind() and conn.execute with runtime SQL are listed as not analysed')
    check(any('load_rows' in x['what'] for x in mig3.get('not_analysed', [])), 'a helper imported from another module is listed as not analysed')
    check((mig3.get('reads_files') or [{}])[0].get('exists') == {'seeds/branches.yaml': False}, 'a file read at run time is recorded with whether it exists')

    cands = tmp / 'library-c.json'
    args = [str(HERE / 'scan_migrations.py'), '--migrations', str(out), '--repo', str(repo), '--table-stats', str(stats), '--out', str(cands)]
    run(*(args + (['--base-ref', 'HEAD~1'] if has_git else [])))
    c = json.loads(cands.read_text(encoding='utf-8'))['candidates'] if cands.exists() else []
    rules = {(x['rule'], x.get('direction')) for x in c}
    check(('PG-CHECK-VALIDATE', 'up') in rules, 'CHECK constraint added in a helper is a candidate')
    check(('DML-WITHOUT-WHERE', 'down') in rules, 'UPDATE without WHERE in a downgrade is a data-loss candidate (direction down)')
    check(not any(x['rule'] == 'PG-NO-LOCK-TIMEOUT' for x in c), 'a chain whose env.py sets lock_timeout has no lock-timeout candidate')
    vr = next((x for x in c if x['rule'] == 'VALUE-RENAME-WHILE-READ'), {})
    check(vr.get('column') == 'genre' and (vr.get('facts') or {}).get('old_value') == 'crime', 'a value renamed in place is a compatibility candidate')
    if has_git:
        check(any(h.get('rev') == 'HEAD~1' and h['file'] == 'library_app/genres.py' for h in vr.get('code_hits', [])),
              'code keyed on the old value in the previous release (--base-ref) is a code hit with its revision')
    rf = next((x for x in c if x['rule'] == 'MIGRATION-READS-FILES'), {})
    check(rf.get('category') == 'reproducibility' and (rf.get('facts') or {}).get('missing_paths') == ['seeds/branches.yaml'],
          'a migration reading a file that no longer exists is a reproducibility candidate')
    sz = next((x['size'] for x in c if x['table'] == 'shelves' and x.get('size')), {})
    check(sz.get('class') == 'small' and sz.get('hinted') is True, 'a size hint sizes the table and is marked hinted')


def validator_cases(tmp: Path):
    """The validator: reproducibility rating, hinted sizes stay assumed, rollback path and components."""
    import validate_migration_review as v
    check(v.rate('reproducibility', 3, 'unknown', 'rolling') == (3, 1, 9), 'reproducibility rates size 3 x exposure 1')
    rep = json.loads((EX / 'migration_review.example.json').read_text(encoding='utf-8'))
    rep['scope']['rollback_path'] = {'method': 'down_migrations', 'source': 'synthetic'}
    rep['scope']['deploy_model'] = 'blue_green'
    rep['scope']['components'] = [{'name': 'worker', 'deploy_model': 'rolling'}]
    p = tmp / 'review.json'
    p.write_text(json.dumps(rep), encoding='utf-8')
    r = run(str(HERE / 'validate_migration_review.py'), str(p), '--candidates', str(EX / 'run' / 'candidates.json'))
    check('rollback_path says down migrations' in r.stdout, 'rollback path of down migrations with a pass down is rejected')
    check('less exposed than component worker' in r.stdout, 'a component more exposed than the deploy model is rejected')


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
        alembic_cases(tmp)
        validator_cases(tmp)
    print(f'{len(FAILS)} failure(s)' if FAILS else 'all checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
