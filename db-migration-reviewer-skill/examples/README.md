# Example: ledger

`ledger/` is a small Flask and SQLAlchemy app with Alembic migrations against PostgreSQL 16.4
(`docker-compose.yml`). Revisions 0001 and 0002 are deployed; 0003 to 0009 are the release under review.
`table_stats.json` holds staging row counts (transactions 4.2 million rows, accounts 12 000). The deploy
is rolling. All data is invented.

`run/` is the run, listed in `run/commands.sh`: `migrations.json` (detection) and `candidates.json`
(13 candidates). The review machine had no Docker, no Podman and no loopback PostgreSQL, so the dynamic
check did not run: every finding is `matched`, none `measured`, and the review says so.
`migration_review.example.json` is the review.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| 0004: CREATE INDEX on transactions without CONCURRENTLY | blocking lock | F01, P0 |
| 0005: rename amount to amount_cents while models.py, queries.py (twice) and app.py use amount | compatibility | F02, P0, four code hits; the test and the two migration mentions are listed apart |
| 0005: ALTER COLUMN TYPE NUMERIC(12,2) to INTEGER | rewrite and narrowing | F03, P0 (rewrite); F04, P0 (data loss: the cents are rounded away, and no value is multiplied by 100) |
| 0005: downgrade is `pass` | irreversible | F05, P1 |
| 0006: one UPDATE over transactions in the migration transaction | backfill cost | F06, P1 |
| 0006: downgrade is `pass` | irreversible | F07, P1 (the backfilled NULLs can't be told apart afterwards) |
| 0007: SET NOT NULL on category with no validated CHECK | blocking lock | F08, P0 |
| 0007: app.py inserts a Transaction without category | compatibility | F09, P0 |
| No lock_timeout in env.py or any migration | lock queueing | F10, P1, one finding for the release, naming the five migrations that lock existing tables |
| Decoy: 0003 indexes budgets, created in the same migration | dismissed | C001, `table_created_in_release` |
| Decoy: 0008 ADD COLUMN currency DEFAULT 'AUD' | dismissed | C011, `constant_default_pg11` (PostgreSQL 16.4, constant default) |
| Decoy: 0009 drops accounts.legacy_code, out of the model since release 2.3 | dismissed | C012, `intended_contract_step`: no code readers; the reason still says the data goes and to take a backup |

The safer sequence in the review splits the release in three: lock timeout, concurrent index and the
new column first; dual writes, batched backfill and a NOT VALID check next; reads switched, SET NOT NULL
and the old column dropped last.

The engine version comes from `ledger/infra/database.tf` (the production instance); the
docker-compose image agrees but, being a development source, would only give an assumed version.

```bash
python scripts/validate_migration_review.py examples/migration_review.example.json --candidates examples/run/candidates.json --migrations examples/run/migrations.json --repo examples/ledger
python scripts/render_report.py examples/migration_review.example.json
```

# Example: greenhouse (Prisma)

`greenhouse/` is a small Prisma app (TypeScript) on PostgreSQL. Its chain was squashed into
`20260301000000_baseline`; the original migrations are kept in `prisma/migration_history_archive/`, where
money and weight columns are `DOUBLE PRECISION`. The baseline is deployed (older databases marked it
applied with `prisma migrate resolve`); `20260415000000_release_hardening` is the release under review.
The only engine version in the repository is the docker-compose image, so it is assumed. All data is
invented.

`run-prisma/` is the run, listed in `run-prisma/commands.sh` (13 candidates);
`migration_review.prisma.example.json` is the review.

| Planted | Expected | Got |
|---|---|---|
| Archive folder next to the chain | left out of the chain, used for type history | `excluded` lists both files |
| `CREATE TABLE IF NOT EXISTS "StockAlert"`, `CREATE UNIQUE INDEX IF NOT EXISTS` on it | no crash; index on a new table | C007 dismissed, `table_created_in_release` |
| Two-clause ALTER of Plant to DECIMAL with `USING ROUND` | both clauses checked; narrowing on pre-squash databases | F01 and F02 (P0, from the archived type); one rewrite F11 (P2), C003 dismissed as `duplicate` |
| Order.total to DECIMAL(10,2) | narrowing and a rewrite of a 2.6 million row table | F03 (P0), F08 (P1, assumed engine) |
| `ADD COLUMN "giftNote" TEXT, ADD COLUMN "channel" TEXT NOT NULL` | second clause found; `prisma.order.create` without channel | F04 (P0); the variable `data` in `importOrder()` counted as not analysed |
| Index built inside a `DO` block | no rule matches; found by review | F09 (P1, `source: agent`, basis lines 26 and 29) |
| RENAME phone, read by a Prisma `select` | compatibility | F05 (P0) |
| DROP legacy_ref, read as the Prisma field `legacyRef` | data loss and compatibility | F06 and F07 (P0) |
| No down migration (Prisma) | one irreversible finding for the release | F12 (P3) |
| No lock_timeout | lock queueing | F10 (P1) |
| Foreign key on the new table | not flagged | no candidate |

```bash
python scripts/validate_migration_review.py examples/migration_review.prisma.example.json --candidates examples/run-prisma/candidates.json --migrations examples/run-prisma/migrations.json --repo examples/greenhouse
python scripts/render_report.py examples/migration_review.prisma.example.json
```
