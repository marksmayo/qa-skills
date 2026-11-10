# Design: `db-migration-reviewer-skill`

Status: Designed; not built. Build order: 22.

## Mission

The skill reviews database migrations before they ship and finds the ones that are dangerous in
production. Its four questions:

- Does this lock a busy table long enough to cause an outage?
- Does it destroy or silently change data?
- Can it be rolled back?
- Will code that's still running during the deploy break against the new schema?

It reads the migration files and the application code. Optionally, it runs the migrations against
a disposable local copy of the database to time them and test the rollback.

How it differs from the existing skills:

- **code-security-auditor** looks at SQL for injection. This skill looks at schema changes for
  availability and data-safety risk.
- **ticket-to-tdd** and the test skills check behaviour. This skill checks the deploy itself: the
  window where old code, new code and the new schema all coexist.

## Decisions

- **Static review always runs.** The dynamic check runs too when it can: in a throwaway container
  when Docker or Podman is present, or on a local server the user names. Otherwise the report says
  the review is static only. The skill
  creates the database, uses it and drops it.
- **Never against production.** The skill refuses any connection string that isn't loopback or a
  container it started, unless the user names a non-production host in `authorised_by` and
  confirms it's a disposable copy. Even then it only runs migrations inside a database it created.
- **Engines in v1: PostgreSQL and MySQL.** SQLite and MariaDB rules come in a later version; a
  migration for either is reviewed with the engine-neutral rules only, and the report says so.
- **Engine-specific rules.** Locking behaviour differs a lot between PostgreSQL versions and MySQL
  with InnoDB online DDL. Every rule records the engine and version range it applies
  to. If the engine version is unknown, the skill assumes the oldest version the repo claims to
  support, and says so.
- **Squawk is optional.** Squawk lints PostgreSQL migrations well. When installed, its SARIF
  output merges into the candidates through `sarif.py`. The built-in rules work without it.

## Non-negotiable rules

- **Every finding names the migration file, the statement and line, the engine and version
  range, and the lock or data effect,** for example "`ACCESS EXCLUSIVE` on `orders` while the
  table is rewritten".
- **A guess about table size isn't evidence.** Size comes from the user, from a statistics
  export (`pg_class.reltuples`, `information_schema.tables`) or from the dynamic run. Without it,
  size is `unknown`, which is rated as large and labelled as an assumption.
- **"No findings" doesn't mean "safe to deploy".** The report lists which migrations, engines and
  code paths were checked, and what wasn't (raw SQL built at runtime, stored procedures, other
  services sharing the database).
- **Data-loss findings quote the statement, never the data.** The dynamic check counts and hashes
  rows; it never prints row values.
- **The skill never edits migrations.** Safer rewrites are given in the report as suggested code.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | required | Path to the repository |
| `migrations` | the pending set | Migration files or ids; default is the diff against the base branch through `diff_scope.py`, or all unapplied migrations if the tool can tell |
| `engine` | detected | `postgresql` or `mysql`, with version (`mariadb` and `sqlite` get engine-neutral rules only in v1) |
| `table_stats` | optional | A CSV or JSON of table names, row counts and sizes |
| `deploy_model` | `rolling` | `rolling` (old and new code run together), `blue_green`, or `stop_the_world` (maintenance window) |
| `dynamic` | `auto` | `auto` uses `container` when Docker or Podman is present and otherwise runs static only, saying so; `container` (starts a throwaway database), a loopback connection string, or `off` |
| `seed` | optional | A schema dump or seed script for the dynamic check; synthetic rows are generated up to `seed_rows` per table |
| `other_readers` | optional | Paths to other services or reporting queries that use the same database |

## Supported migration tools

| Tool | How migrations are read |
|---|---|
| Alembic | Python AST of `upgrade()` and `downgrade()`: `op.*` calls and `op.execute` SQL |
| Django | `migrations/*.py` AST: operations list, `RunSQL`, `RunPython`, `atomic`; `sqlmigrate` output when Django is runnable |
| Rails (ActiveRecord) | Ruby source pattern parse of `change`, `up` and `down`; `disable_ddl_transaction!` |
| Flyway | `V__*.sql` and `U__*.sql` files (undo migrations), Java migrations listed as not parsed |
| Liquibase | XML, YAML and JSON changelogs, `rollback` blocks, `sql` changes |
| Prisma | `migrations/*/migration.sql` |
| Knex | JavaScript source pattern parse of `up` and `down`, `knex.raw` |
| Raw SQL | Any `.sql` file the user points at |

SQL is split into statements by a small stdlib tokenizer that handles strings, comments, dollar
quoting and `DELIMITER`. It doesn't need a full SQL parser: the rules match statement shapes.

## Rule catalogue (`data/rules.json`, summarised in RUBRIC.md)

| Category | Examples (engine) |
|---|---|
| Blocking lock or rewrite | `ALTER COLUMN TYPE` with a rewrite (PG); `ADD COLUMN ... DEFAULT` with a volatile default (PG), or any default before PG 11; `SET NOT NULL` without a validated `CHECK (col IS NOT NULL)` first (from PG 12 the check lets it skip the full scan); `CREATE INDEX` without `CONCURRENTLY` (PG); `ADD FOREIGN KEY` without `NOT VALID` then `VALIDATE` (PG); `ALGORITHM=COPY` or an operation InnoDB can't do in place (MySQL) |
| Lock queueing | No `lock_timeout` or `statement_timeout` around DDL (PG), so one slow query makes every later query wait behind the migration |
| Data loss | `DROP TABLE`, `DROP COLUMN`, `TRUNCATE`; type narrowing (`text` to `varchar(50)`, `bigint` to `int`); `DELETE` or `UPDATE` without `WHERE` in a data migration |
| Irreversible | `downgrade` or `down` missing, empty, raising, or not restoring what `up` removed; Flyway with no undo file |
| Compatibility (expand and contract) | A rename or drop while code still reads the old name; `NOT NULL` added while code can still insert without the column; a new required column old code doesn't send |
| Transaction mistakes | `CREATE INDEX CONCURRENTLY` inside a transaction (fails); mixing DDL and a large data backfill in one transaction; MySQL DDL that auto-commits half a migration |
| Backfill cost | `UPDATE` across a whole large table in one statement with no batching |
| Ordering | A migration depends on one with a later timestamp; two heads (Alembic) or conflicting leaf nodes (Django) |

## Code compatibility check

For every column or table a migration drops or renames, the skill searches the code for readers:

- ORM models and field declarations (SQLAlchemy, Django models, ActiveRecord schema and
  `ignored_columns`, Prisma schema, TypeORM entities);
- raw SQL strings and query builders (grep for the identifier, quoted and unquoted, with
  word boundaries);
- `other_readers` paths.

A hit means old code would break during a rolling deploy, or new code still uses the column. The
finding lists each hit by file and line. Hits inside the migration itself and in tests are
listed separately and don't count.

## Dynamic check (when Docker, Podman or a loopback server is available)

1. Start a throwaway database (`docker run --rm` with a pinned image tag and a random port), or
   connect to the named loopback server and create `qa_migrate_<random>`.
2. Load the schema dump or run all earlier migrations, then generate `seed_rows` synthetic rows
   per table (default 100 000 for tables flagged by rules, 1 000 for the rest).
3. Run each target migration. Record wall time, the locks held (polling `pg_locks` or
   `performance_schema.metadata_locks` from a second connection every 100 ms), and whether a
   concurrent `SELECT` and `INSERT` on the affected table were blocked and for how long.
4. Run the down migration, then compare schema (catalogue queries) and row counts and per-table
   row hashes with the state before. A difference means the rollback isn't faithful.
5. Drop the database or stop the container.

Measured times scale with the seed. The report shows the measured time and the row count, and
never extrapolates to production size without saying it's an estimate.

## Rating

**Hazard** of the operation: data loss 4; blocking lock or rewrite 3; compatibility break 3;
irreversible 2; lock queueing, transaction mistake or backfill cost 2; ordering 1.

**Table size:** large (over 1 million rows or 1 GB, or `unknown`) 3; medium (10 000 to 1 million)
2; small 1. Data-loss and compatibility findings ignore size and use 3, because losing a small
table still loses data.

**Deploy exposure:** `rolling` 3, `blue_green` 2, `stop_the_world` 1. A maintenance window makes
locks less harmful but doesn't help with data loss, so data loss always uses 3.

`score = hazard x size x exposure`. P0 >= 27, P1 >= 18, P2 >= 8, otherwise P3.

Floors: unrecoverable data loss (a drop with no backup step and no faithful down) is P0. A
compatibility break with code hits is at least P1.

**Confidence:** `measured` (dynamic run showed the lock, block or loss), `matched` (a rule
matched a statement and its engine and version conditions hold), `assumed` (a condition depends
on an unknown, such as the engine version or table size). An `assumed` finding is capped at P1.

## Output (`migration_review.schema.json`)

| Section | Content |
|---|---|
| `scope` | Repo, migration tool, engine and version (and how it was found), deploy model, migrations reviewed |
| `metadata` | Tools and versions (Squawk, database image), dynamic mode, seed sizes |
| `migrations` | Each migration: id, file, statements with lines, transaction mode, has down, dynamic results |
| `findings` | Rule id, category, statement and line, engine and version range, lock or data effect, table size and its source, hazard, exposure, score, priority, confidence, code hits, safer rewrite, verification step |
| `triage` | Every candidate (rule match or Squawk result): promoted or dismissed with reason |
| `dynamic` | Per migration: timings, locks seen, blocked queries, rollback comparison |
| `headline` | Counts by priority, migrations with a P0, "safe sequence" suggestion if one exists |
| `limitations` | Unparsed migrations (Java, RunPython bodies), unknown engine version, dynamic not run |

Safer rewrites follow expand and contract: add the new column nullable, backfill in batches,
dual-write, switch reads, then drop in a later release. The report lays out the steps as
separate migrations and says which release each belongs to.

## Validator fails when

- A finding has no file, line or statement, or no engine and version range.
- A `measured` finding has no dynamic result behind it, or a dynamic result shows a database or
  host other than one the skill created or a loopback address.
- A table size is used without a source, or `unknown` is rated below large.
- An `assumed` finding is ranked P0.
- A compatibility finding has no code hits, or counts hits from tests or the migration itself.
- Any candidate isn't triaged.
- Row values appear anywhere in the report or evidence.
- The headline says the migrations are "safe" without the scope statement.

## Edge cases

- **Django `RunPython` and Alembic data steps:** the skill reads the code, flags whole-table
  loops and missing reverse functions, and marks the rest as not analysed.
- **Online schema-change tools** (`gh-ost`, `pt-online-schema-change`, `pgroll`): if the repo uses
  one, lock rules for the operations it handles are dismissed with that reason.
- **Partitioned tables, views and materialized views:** rules know that an index on a partitioned
  parent can't be built `CONCURRENTLY` (PG) and that dropping a column breaks dependent views.
- **Squashed or generated migrations:** only the migrations in scope are reviewed; the squash is
  noted.
- **Multiple databases** in one repo: each migration is matched to its engine by config.
- **No migration tool:** schema changes in plain SQL scripts are reviewed as raw SQL.

## Tool stack

| Tool | Licence | Role |
|---|---|---|
| Built-in rules (stdlib) | repo licence | Always run |
| Squawk | GPL-3.0 (to confirm when pinning) | Optional PostgreSQL linter; run as a separate CLI, so its licence places no conditions on the code reviewed |
| `django-migration-linter` | Apache-2.0 | Optional, Django projects |
| `strong_migrations` | MIT | Optional, Rails projects that already use it; its checks are read as candidates |
| PostgreSQL and MySQL images | PostgreSQL, GPL-2.0 | Dynamic check only, pinned image tags in `data/images.lock.json` |
| `psycopg`, `PyMySQL` | LGPL-3.0, MIT | Dynamic check only, declared in `requirements.txt`; static review needs nothing |

## Files

```text
db-migration-reviewer-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt   # requirements only for the dynamic check
  data/rules.json                  # rule id, engine + version range, statement shape, effect, rewrite
  data/images.lock.json
  schemas/migration_review.schema.json
  scripts/diff_scope.py  sarif.py  # shared copies
          detect_migrations.py     # tool, engine, version, migration order, pending set
          split_sql.py             # stdlib statement splitter
          scan_migrations.py       # rules -> candidates.json (merges Squawk SARIF)
          find_readers.py          # code that still reads dropped or renamed columns
          dynamic_check.py         # throwaway database, timing, locks, rollback comparison
          validate_migration_review.py  render_report.py
  examples/
```

## Example

`examples/ledger/` is a small Flask and SQLAlchemy app with Alembic migrations against
PostgreSQL 16. The answer key in its README lists:

- `CREATE INDEX` on `transactions` without `CONCURRENTLY`;
- a column rename from `amount` to `amount_cents` while two queries still read `amount`;
- `ALTER COLUMN TYPE` from `numeric` to `integer` (rewrite and narrowing);
- `SET NOT NULL` on a column old code doesn't send;
- a `downgrade()` that's only `pass`;
- a backfill `UPDATE` over the whole table in one transaction;
- no `lock_timeout` anywhere.

Decoys, for triage to dismiss: an `ADD COLUMN` with a constant default (fast since PG 11), an index
on a table created in the same migration, and a drop of a column that's been in
`ignored_columns`-style exclusion for a release. `examples/run/` holds a real dynamic run on a
container with 100 000 seeded rows, including the lock polling and rollback comparison.

## Resolved questions

Agreed with the user before the build.

1. **Docker or Podman for the dynamic check?** Yes, when present; otherwise the review is static only and says so.
2. **Which engines in v1?** PostgreSQL and MySQL first; SQLite and MariaDB later.
3. **Unknown table sizes?** Treated as large, and the report says so.
